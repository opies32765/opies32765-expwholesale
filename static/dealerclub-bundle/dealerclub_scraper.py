"""dealerclub_scraper.py — pull live auctions off app.dealerclub.com.

Architecture:
  1. Playwright login (once or on session expiry). Saves cookies +
     localStorage to state/session.json. Datacenter-IP friendly: try
     headless from Contabo 1 first; fall back to operator-supplied
     cookies if reCAPTCHA trips.
  2. Daemon loop (systemd service): hit the REST API every N seconds
     with the saved cookies, replaying via Playwright's APIRequestContext
     to keep the browser-fingerprint Cloudflare expects.
  3. For each active auction: POST a normalized payload to EW's
     /api/dealerclub/lot endpoint. The endpoint dedupes by external_id,
     creates a bid on first-seen (creation_source='dealerclub'), and
     updates current_price/end_time/bid_count on subsequent polls.

Modes:
  --recon           Headed browser — log in interactively, save session
                    state, run one scrape with verbose output. Use once
                    on first deploy to verify everything works.
  --once            One scrape pass + exit. Useful for cron-style testing.
  --daemon          Continuous loop with smart backoff. Use under systemd.

Smart backoff (--daemon):
  - 30s polls while >=1 auction is active
  - 5min polls when nothing is live (idle)

Credentials: env vars DC_EMAIL + DC_PASSWORD (only needed on first run
or when session expires).
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
from pathlib import Path

# Force UTF-8 stdout/stderr on Windows so non-ASCII chars don't kill print
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except (AttributeError, Exception):
    pass

# Auto-load a sibling secrets.env (KEY=VALUE per line, # for comments).
# Lets Windows Task Scheduler / systemd both run without needing to bake
# env vars into the service unit — just drop the file next to the script.
_secrets_path = Path(__file__).resolve().parent / 'secrets.env'
if _secrets_path.exists():
    for _line in _secrets_path.read_text(encoding='utf-8').splitlines():
        _line = _line.strip()
        if not _line or _line.startswith('#') or '=' not in _line:
            continue
        _k, _, _v = _line.partition('=')
        os.environ.setdefault(_k.strip(), _v.strip())

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ── Config ────────────────────────────────────────────────────────────────

DC_EMAIL = os.environ.get('DC_EMAIL', 'oscar@experience-wholesale.com')
DC_PASSWORD = os.environ.get('DC_PASSWORD', '')  # MUST be set in env

APP_URL = 'https://app.dealerclub.com/'
LOGIN_URL = 'https://app.dealerclub.com/login'
API_BASE = 'https://api.dealerclub.com'
LIVE_AUCTIONS_API = f'{API_BASE}/auctions/marketplace/active/'
ME_API = f'{API_BASE}/users/me/'
TRANSPORT_QUOTE_API = f'{API_BASE}/transportation/quote/'
JWT_REFRESH_API     = f'{API_BASE}/users/jwt/refresh/'

# Transport-quote config (locked 2026-05-12 per operator)
TRANSPORT_ADDRESS_ID = 553            # 1210 S Andrews Ave, Pompano Beach FL
ENCLOSED_PRICE_THRESHOLD = 60_000     # use enclosed trailer when current bid above this

EW_ENDPOINT = os.environ.get(
    'EW_DEALERCLUB_ENDPOINT',
    'http://127.0.0.1:9001/api/dealerclub/lot')
EW_SHARED_SECRET = os.environ.get('EW_DEALERCLUB_SECRET', '')

STATE_DIR = Path(__file__).parent / 'state'
STATE_DIR.mkdir(exist_ok=True)
SESSION_FILE = STATE_DIR / 'session.json'

# Smart backoff (seconds)
POLL_INTERVAL_ACTIVE = 30   # auctions live → poll every 30s
POLL_INTERVAL_IDLE = 300    # nothing live → poll every 5min

NAV_TIMEOUT_MS = 30000

# Intake filter — only pull lots that match the operator's buying box.
# Locked 2026-05-12: year >= 2015 AND odometer < 70k.
# No price cap — late-model exotics with low miles are valid even if
# the bid is high.
MIN_YEAR = 2015
MAX_ODOMETER = 70_000


def lot_passes_filter(lot: dict) -> tuple[bool, str]:
    """Return (keep, reason). Reason is for logging only."""
    year = lot.get('year') or 0
    miles = lot.get('odometer') or 0
    if year and year < MIN_YEAR:
        return False, f'year {year} < {MIN_YEAR}'
    if miles and miles >= MAX_ODOMETER:
        return False, f'odometer {miles:,} mi >= {MAX_ODOMETER:,} mi'
    return True, 'pass'


# ── Login + session ───────────────────────────────────────────────────────

def login(page, *, debug=False):
    """Drive the DealerClub login. Raises on failure.

    The Vue.js form re-renders during initial hydration which can detach
    inputs mid-fill (verified 2026-05-12). We work around it by waiting
    for the SPA to be fully idle first, then using locator+evaluate to
    set values directly (bypasses the keystroke-by-keystroke fill that
    races with Vue's reactive updates), then dispatching input events
    so Vue picks the change up.
    """
    page.goto(LOGIN_URL, wait_until='domcontentloaded',
              timeout=NAV_TIMEOUT_MS)
    # Critical: wait for the Vue SPA to finish its initial hydration
    # before touching inputs. networkidle alone isn't enough — the form
    # re-mounts after the SPA boots. Add an explicit 2s settle.
    try:
        page.wait_for_load_state('networkidle', timeout=20000)
    except PWTimeout:
        pass
    page.wait_for_timeout(2000)

    if not DC_PASSWORD:
        raise RuntimeError('DC_PASSWORD env var is not set')

    # Find inputs with retries — re-query if Vue re-renders during fill
    def _fill_inputs():
        # Set values via the DOM directly + fire input events so Vue's
        # v-model picks them up (regular .fill() races the rerender).
        page.evaluate("""
            ([email, pw]) => {
                const setVal = (sel, val) => {
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    const setter = Object.getOwnPropertyDescriptor(
                        Object.getPrototypeOf(el), 'value').set;
                    setter.call(el, val);
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    return true;
                };
                setVal('input[type="email"], input[name*="email" i]', email);
                setVal('input[type="password"]', pw);
            }
        """, [DC_EMAIL, DC_PASSWORD])

    try:
        page.wait_for_selector('input[type="email"], input[name*="email" i]',
                               timeout=15000, state='attached')
    except PWTimeout:
        if debug:
            (STATE_DIR / 'last_login_initial.html').write_text(
                page.content(), encoding='utf-8')
        raise RuntimeError(f'login: email input never rendered at {page.url}')

    _fill_inputs()
    page.wait_for_timeout(500)
    # Sanity re-fill in case Vue cleared on mount
    val = page.evaluate("document.querySelector('input[type=\"email\"]').value")
    if not val or '@' not in val:
        page.wait_for_timeout(1000)
        _fill_inputs()

    if debug:
        (STATE_DIR / 'last_login_filled.html').write_text(
            page.content(), encoding='utf-8')

    # Submit
    clicked = False
    for sel in (
        'button[type="submit"]',
        'button:has-text("Log In")',
        'button:has-text("Login")',
        'button:has-text("Sign In")',
    ):
        try:
            page.click(sel, timeout=3000)
            clicked = True
            break
        except PWTimeout:
            continue
    if not clicked:
        page.press('input[type="password"]', 'Enter')

    # Wait for the SPA to navigate away from /login
    deadline = time.time() + 25
    while time.time() < deadline:
        page.wait_for_timeout(500)
        if '/login' not in page.url:
            break
    if '/login' in page.url and 'password' in page.content().lower():
        if debug:
            (STATE_DIR / 'last_login_after.html').write_text(
                page.content(), encoding='utf-8')
        raise RuntimeError(f'login appears to have failed — '
                           f'still at {page.url}')

    page.wait_for_timeout(2000)
    if debug:
        (STATE_DIR / 'last_post_login.html').write_text(
            page.content(), encoding='utf-8')

    print(f'  logged in. landing url: {page.url}', flush=True)


def save_session(ctx) -> None:
    state = ctx.storage_state()
    SESSION_FILE.write_text(json.dumps(state, indent=2), encoding='utf-8')


def load_session_if_fresh(max_age_hours=20) -> dict | None:
    if not SESSION_FILE.exists():
        return None
    age_h = (time.time() - SESSION_FILE.stat().st_mtime) / 3600
    if age_h > max_age_hours:
        print(f'  session {age_h:.1f}h old (>{max_age_hours}h) — refresh',
              flush=True)
        return None
    return json.loads(SESSION_FILE.read_text(encoding='utf-8'))


# ── API calls via Playwright's APIRequestContext ──────────────────────────
#
# DealerClub stores its access token in the DC_ACCESS cookie (scoped to
# app.dealerclub.com, httpOnly=False so JS can read it). The SPA copies
# the value into an `Authorization: Bearer …` header for every call to
# api.dealerclub.com. We mirror that here — extract DC_ACCESS from the
# browser context's cookies and inject it as a Bearer header.

def _extract_cookie(ctx, name: str) -> str | None:
    for c in ctx.cookies():
        if c.get('name') == name:
            return c.get('value')
    return None


def _extract_access_token(ctx) -> str | None:
    return _extract_cookie(ctx, 'DC_ACCESS')


def try_refresh_access_token(ctx, api_ctx) -> str | None:
    """Hit POST /users/jwt/refresh/ with DC_REFRESH. On 200 the response
    carries new access + refresh tokens; we update the context cookies
    (so subsequent calls + the saved storage_state use them) and return
    the new access token. On any failure return None — caller falls
    back to full re-login.
    """
    refresh = _extract_cookie(ctx, 'DC_REFRESH')
    if not refresh:
        return None
    r = api_ctx.post(JWT_REFRESH_API,
                     data={'refresh': refresh},
                     headers={
                         'Content-Type': 'application/json',
                         'Origin': 'https://app.dealerclub.com',
                         'Referer': 'https://app.dealerclub.com/',
                     })
    if r.status != 200:
        print(f'  jwt refresh failed: {r.status} {r.text()[:160]}',
              flush=True)
        return None
    try:
        j = r.json()
    except Exception:
        return None
    new_access  = j.get('access')
    new_refresh = j.get('refresh') or refresh
    if not new_access:
        return None
    # Update cookies in-place so storage_state.save_session() persists them
    ctx.add_cookies([
        {'name': 'DC_ACCESS',  'value': new_access,
         'domain': 'app.dealerclub.com', 'path': '/',
         'secure': True, 'httpOnly': False, 'sameSite': 'Lax'},
        {'name': 'DC_REFRESH', 'value': new_refresh,
         'domain': 'app.dealerclub.com', 'path': '/',
         'secure': True, 'httpOnly': False, 'sameSite': 'Lax'},
    ])
    print(f'  jwt refresh OK (new access expires in ~20min)', flush=True)
    return new_access


def _auth_headers(token: str | None) -> dict:
    headers = {
        'Accept': '*/*',
        'Content-Type': 'application/json',
        'Origin': 'https://app.dealerclub.com',
        'Referer': 'https://app.dealerclub.com/',
    }
    if token:
        headers['Authorization'] = f'Bearer {token}'
    return headers


def _api_get(api_ctx, url: str, token: str | None = None) -> dict | None:
    r = api_ctx.get(url, headers=_auth_headers(token))
    if r.status in (401, 403):
        print(f'  API auth failed: {r.status} on {url}', flush=True)
        return None
    if r.status >= 400:
        print(f'  API {r.status} on {url}: {r.text()[:200]}', flush=True)
        return None
    try:
        return r.json()
    except Exception as e:
        print(f'  API non-JSON on {url}: {e}', flush=True)
        return None


def _api_post(api_ctx, url: str, body: dict,
              token: str | None = None) -> dict | None:
    r = api_ctx.post(url, data=body, headers=_auth_headers(token))
    if r.status in (401, 403):
        print(f'  API auth failed: {r.status} on {url}', flush=True)
        return None
    if r.status >= 400:
        print(f'  API {r.status} on {url}: {r.text()[:200]}', flush=True)
        return None
    try:
        return r.json()
    except Exception as e:
        print(f'  API non-JSON on {url}: {e}', flush=True)
        return None


# ── Transport quotes ──────────────────────────────────────────────────────
#
# DealerClub exposes POST /transportation/quote/ which returns the actual
# price, mileage, and ETA from the seller's pickup_address to one of our
# saved delivery addresses. Quotes expire in ~7 days, so we cache by
# external_id in-memory and never re-quote during the auction lifetime.
# A lot's trailer choice (open vs enclosed) is locked at the time we
# first see it — even if the price climbs mid-auction, we don't re-quote
# because the cost delta wouldn't shift opportunity by a tier.

_quote_cache: dict = {}   # external_id -> {price, mileage, eta_min, eta_max, enclosed}
_quote_cache_logged: set = set()   # track which lots we've already announced


def fetch_transport_quote(api_ctx, token, external_id: str,
                          current_price: int | None) -> dict | None:
    """Pull a transport-cost quote for one auction. Cached by external_id
    for the lifetime of the daemon. Returns dict with price/mileage/eta
    or None on failure."""
    if external_id in _quote_cache:
        return _quote_cache[external_id]
    enclosed = bool(current_price and current_price > ENCLOSED_PRICE_THRESHOLD)
    body = {
        'auction_id': external_id,
        'address_id': TRANSPORT_ADDRESS_ID,
        'enclosed_trailer': enclosed,
    }
    r = api_ctx.post(TRANSPORT_QUOTE_API, data=body,
                     headers=_auth_headers(token))
    if r.status >= 400:
        print(f'    transport quote {external_id}: HTTP {r.status} '
              f'{r.text()[:120]}', flush=True)
        return None
    try:
        j = r.json()
    except Exception:
        return None
    quote = {
        'price':    j.get('price'),
        'mileage':  j.get('mileage'),
        'eta_min':  j.get('eta_min'),
        'eta_max':  j.get('eta_max'),
        'enclosed': enclosed,
    }
    _quote_cache[external_id] = quote
    return quote


# ── Normalize one auction row → EW-friendly dict ──────────────────────────

def normalize_lot(row: dict) -> dict:
    """Map the marketplace/active row into the shape EW's endpoint wants."""
    img = (row.get('featured_image') or {}).get('thumbnail_url')
    # high_bid is null when there are no bids and a dict like
    # {"id":..,"user_id":..,"dealership_id":..,"amount":2700} once
    # bidding starts. Pull the amount out so EW gets a plain int.
    hb_raw = row.get('high_bid')
    if isinstance(hb_raw, dict):
        hb = hb_raw.get('amount')
    else:
        hb = hb_raw
    return {
        'external_id':         row.get('external_id'),
        'vin':                 (row.get('vin') or '').strip().upper() or None,
        'year':                row.get('year'),
        'make':                row.get('make'),
        'model':               row.get('model'),
        'trim':                row.get('trim'),
        'odometer':            row.get('odometer'),
        'drivetrain':          row.get('drivetrain'),
        'current_price':       row.get('current_price'),
        'high_bid':            hb,
        'bid_count':           row.get('bid_count'),
        'unique_bidder_count': row.get('unique_bidder_count'),
        'end_time':            row.get('end_time'),
        'duration_in_minutes': row.get('duration_in_minutes'),
        'reserve_met':         row.get('reserve_met'),
        'is_no_reserve':       row.get('is_no_reserve'),
        'reserve_progress_color': row.get('reserve_progress_color'),
        'reserve_price':       row.get('reserve_price'),
        'status':              row.get('status'),
        'featured_image_url':  img,
        'detail_url': (f'https://app.dealerclub.com/auction/'
                       f'{row.get("external_id")}'),
    }


# ── EW pipe ────────────────────────────────────────────────────────────────

def post_to_ew(lots: list[dict]) -> dict:
    """POST each normalized lot to EW. Returns a summary tally."""
    import requests
    summary = {'new': 0, 'updated': 0, 'errors': 0,
               'created_bids': []}
    if not EW_SHARED_SECRET:
        print(f'  EW_DEALERCLUB_SECRET not set; skipping EW POSTs',
              flush=True)
        return summary
    for lot in lots:
        if not lot.get('external_id'):
            continue
        try:
            resp = requests.post(EW_ENDPOINT, json=lot,
                                 headers={'X-Auth': EW_SHARED_SECRET},
                                 timeout=15)
            j = resp.json() if resp.headers.get('content-type','').startswith(
                'application/json') else {}
            status = j.get('status', 'unknown')
            if status == 'new':
                summary['new'] += 1
                if j.get('bid_id'):
                    summary['created_bids'].append(j['bid_id'])
            elif status in ('updated', 'already_seen'):
                summary['updated'] += 1
            else:
                summary['errors'] += 1
                print(f'  EW {resp.status_code} for {lot["external_id"]}: '
                      f'{resp.text[:160]}', flush=True)
        except Exception as e:
            summary['errors'] += 1
            print(f'  EW POST err for {lot.get("external_id")}: {e}',
                  flush=True)
    return summary


# ── Scrape orchestration ──────────────────────────────────────────────────

def one_pass(playwright, *, headed=False, debug=False, dry_run=False):
    """Single poll cycle. Returns (lot_count, summary) tuple."""
    state = load_session_if_fresh()

    browser = playwright.chromium.launch(headless=not headed)
    ctx_kwargs: dict = {'storage_state': state} if state else {}
    ctx = browser.new_context(**ctx_kwargs)
    api_ctx = ctx.request

    try:
        # Pull DC_ACCESS from cookies — that's the Bearer token the SPA
        # uses for every api.dealerclub.com call.
        token = _extract_access_token(ctx)

        # Verify session via /users/me. On 401, try refresh-token first
        # (5-day rolling window); only if refresh also fails do we run the
        # full Playwright login flow (which is brittle behind reCAPTCHA).
        me = _api_get(api_ctx, ME_API, token=token)
        if me is None or 'user' not in (me or {}):
            print('  /users/me failed — trying refresh-token flow',
                  flush=True)
            new_token = try_refresh_access_token(ctx, api_ctx)
            if new_token:
                token = new_token
                save_session(ctx)
                me = _api_get(api_ctx, ME_API, token=token)
        if me is None or 'user' not in (me or {}):
            print('  refresh-token failed — running full login flow',
                  flush=True)
            page = ctx.new_page()
            login(page, debug=debug)
            save_session(ctx)
            api_ctx = ctx.request
            token = _extract_access_token(ctx)
            me = _api_get(api_ctx, ME_API, token=token)
        if me is None or 'user' not in (me or {}):
            raise RuntimeError('login flow finished but /users/me still '
                               'returns no user payload')

        user_email = (me.get('user') or {}).get('email')
        print(f'  authenticated as {user_email}', flush=True)

        # The killer endpoint — full active list in one POST.
        data = _api_post(api_ctx, LIVE_AUCTIONS_API,
                         {'offset': 0, 'limit': 100,
                          'saved_search_ids': [], 'filters': []},
                         token=token)
        if data is None:
            print('  active-auctions API returned no data', flush=True)
            return 0, {}

        results = data.get('results') or []
        count = data.get('count', 0)
        print(f'  {len(results)}/{count} active auctions returned',
              flush=True)

        # Save raw response for debugging
        if debug:
            (STATE_DIR / 'last_active.json').write_text(
                json.dumps(data, indent=2), encoding='utf-8')

        # Normalize then filter
        all_lots = [normalize_lot(r) for r in results]
        lots = []
        skipped = 0
        for L in all_lots:
            ok, reason = lot_passes_filter(L)
            if ok:
                lots.append(L)
            else:
                skipped += 1
                print(f'    skip {L.get("external_id")}  {reason}',
                      flush=True)
        if skipped:
            print(f'  filtered: {len(lots)} kept, {skipped} skipped',
                  flush=True)

        # Attach a real transport-cost quote per lot (cached in-memory by
        # external_id; only the first time we see a lot does this fire).
        q_fetched = 0
        for L in lots:
            eid = L.get('external_id')
            if not eid:
                continue
            q = fetch_transport_quote(api_ctx, token, eid,
                                      L.get('current_price'))
            if q:
                L['transport_price']   = q.get('price')
                L['transport_mileage'] = q.get('mileage')
                L['transport_eta_min'] = q.get('eta_min')
                L['transport_eta_max'] = q.get('eta_max')
                L['transport_enclosed'] = q.get('enclosed')
                if eid not in _quote_cache_logged:
                    _quote_cache_logged.add(eid)
                    q_fetched += 1
        if q_fetched:
            print(f'  transport quotes fetched: {q_fetched} new '
                  f'(cache size {len(_quote_cache)})', flush=True)

        # Refresh session state so cookies stay fresh on disk
        save_session(ctx)

        if dry_run:
            print('  DRY-RUN: not POSTing to EW', flush=True)
            for L in lots:
                print(f'    {L["external_id"]:<16} '
                      f'{(L.get("year") or 0):4} {(L.get("make") or ""):14}'
                      f' {(L.get("model") or ""):16}  '
                      f'${(L.get("current_price") or 0):>9,}  '
                      f'{(L.get("end_time") or "")[:19]}  '
                      f'vin={L.get("vin") or "(none)"}')
            return len(lots), {}

        summary = post_to_ew(lots)
        print(f'  EW: {summary["new"]} new, '
              f'{summary["updated"]} updated, '
              f'{summary["errors"]} errors',
              flush=True)
        return len(lots), summary
    finally:
        browser.close()


def daemon_loop(*, headed=False, debug=False):
    """Run forever, polling with smart backoff."""
    with sync_playwright() as p:
        while True:
            t0 = time.time()
            try:
                count, _ = one_pass(p, headed=headed, debug=debug)
                interval = POLL_INTERVAL_ACTIVE if count > 0 \
                           else POLL_INTERVAL_IDLE
            except Exception as e:
                print(f'[daemon] one_pass error: {e}', flush=True)
                interval = 60  # short retry on error
            dt = time.time() - t0
            sleep_s = max(5, interval - dt)
            print(f'[daemon] cycle took {dt:.1f}s, sleeping {sleep_s:.0f}s',
                  flush=True)
            time.sleep(sleep_s)


# ── CLI ───────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='DealerClub scraper')
    ap.add_argument('--recon', action='store_true',
                    help='Headed browser + verbose debug dumps')
    ap.add_argument('--once', action='store_true',
                    help='One scrape pass and exit')
    ap.add_argument('--daemon', action='store_true',
                    help='Continuous loop (use under systemd)')
    ap.add_argument('--dry-run', action='store_true',
                    help='Scrape but do not POST to EW')
    ap.add_argument('--debug', action='store_true',
                    help='Save HTML/JSON snapshots even in headless mode')
    ap.add_argument('--manual-login', action='store_true',
                    help='Open a headed browser, let the user log in by '
                         'hand, then press Enter in the terminal to save '
                         'the session. Use when auto-login flow trips a '
                         'Vue hydration race or captcha.')
    args = ap.parse_args()

    if args.manual_login:
        with sync_playwright() as p:
            print('Opening headed browser. Log in to dealerclub.com,')
            print('then come back to THIS terminal and press Enter.')
            browser = p.chromium.launch(headless=False)
            ctx = browser.new_context()
            page = ctx.new_page()
            page.goto(LOGIN_URL)
            input('  [press Enter once you are signed in and on /auctions] ')
            save_session(ctx)
            print(f'session saved to {SESSION_FILE}')
            browser.close()
        sys.exit(0)

    if args.daemon:
        daemon_loop(headed=args.recon, debug=args.debug)
        sys.exit(0)

    with sync_playwright() as p:
        one_pass(p, headed=args.recon,
                 debug=args.recon or args.debug,
                 dry_run=args.dry_run or args.recon)
