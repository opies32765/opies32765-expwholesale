"""
Unified vAuto Worker — Beelink.

ONE Chrome instance (undetected_chromedriver), ONE vAuto session.
Serves both EW (priority, 5s urgent check) and CarHub (5 min poll).
EW urgent bids always jump the queue.

Usage:
  python vauto_worker_beelink.py              # continuous loop
  python vauto_worker_beelink.py --once       # single pass then exit
  python vauto_worker_beelink.py --ew-only    # only EW
  python vauto_worker_beelink.py --carhub-only # only CarHub
"""

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

try:
    from enrich.env_loader import load_env
    load_env()
except Exception:
    pass

import undetected_chromedriver as uc
import requests as http_requests

# ── Config ────────────────────────────────────────────────────────────────────
CARHUB_SERVER = os.environ.get("CARHUB_SERVER", "http://62.146.226.100:8096")
EW_SERVER = os.environ.get("EW_SERVER", "https://experience-wholesale.net")

# Worker identity for the EW server's claim/dispatch queue. Without these,
# ew_get_pending() doesn't include them as query params and every worker's
# claim collapses onto the server-side default 'trainer/primary' row.
WORKER_ID = os.environ.get("WORKER_ID", "trainer")
WORKER_PRIORITY = os.environ.get("WORKER_PRIORITY", "primary")

EW_URGENT_INTERVAL = 5
EW_POLL_INTERVAL = 5
CARHUB_POLL_INTERVAL = 300
MAX_PER_POLL = 5
CHROME_VERSION = 147

# Keep-alive: touch each upstream every N seconds of idle to refresh session
# cookies before Cox/AccuTrade/iPacket age them out. Empirically a 5-day idle
# causes full re-auth; 30 min hitting each keeps cookies fresh indefinitely.
KEEPALIVE_INTERVAL = 1800  # 30 min

KEEPALIVE_SITES = [
    ("vAuto",     "https://provision.vauto.com/",
        ["/login", "okta.com", "coxautoinc.com/signin", "bridgeid"]),
    ("AccuTrade", "https://appraiser3.accu-trade.com/",
        ["/auth/login", "auth0.accu-trade.com", "/u/login"]),
    ("iPacket",   "https://dpapp.autoipacket.com/",
        ["/login", "/signin", "/auth/"]),
]

VAUTO_PROFILE = ROOT / "vauto_profile"
VAUTO_REPORTS = ROOT / "vauto_reports"

# ── Telegram alerts ─────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = "8528106109:AAFczHqjWoiUBs7adZwBEJ6217bQzYGhI_o"
TELEGRAM_CHAT_ID = "7985611488"

# Track state for alerts
_consecutive_failures = 0
_lookups_done = 0
_last_lookup_at = None
_last_activity_ts = 0.0  # epoch; bumped on any lookup or keepalive


def _send_telegram(msg):
    """Fire-and-forget Telegram alert. Never raises."""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        http_requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML",
        }, timeout=5)
    except Exception:
        pass


def _worker_label(serve_ew, serve_carhub):
    """Return a label like 'CarHub' or 'EW' based on which mode."""
    if serve_carhub and not serve_ew:
        return "CarHub"
    if serve_ew and not serve_carhub:
        return "EW"
    return "CarHub+EW"


# ── Heartbeat ───────────────────────────────────────────────────────────────
_last_heartbeat_time = 0
HEARTBEAT_INTERVAL = 60  # seconds

# Synthetic-OK signal — required by server's auto-promote logic.
# After a degraded worker recovers, the server needs 3 heartbeats with
# synthetic_ok=true (spanning >=3 min) before it auto-promotes back to
# primary. Without this signal a once-demoted worker stays degraded forever.
# Set in three places: (a) after every successful real lookup (5-min credit),
# (b) after a successful explicit synthetic check (3-min credit), (c) by the
# main-loop dispatcher when the worker is healthy but idle.
_synthetic_ok_until = 0          # timestamp until which heartbeats carry synthetic_ok=true
_last_synthetic_check_at = 0     # last time we ran an explicit reachability probe
SYNTHETIC_CHECK_INTERVAL = 60    # seconds between explicit checks when no recent success
SYNTHETIC_PROBE_URL = "https://provision.vauto.app.coxautoinc.com/Va/Default.aspx"


def _send_heartbeat(serve_ew, serve_carhub, chrome_alive=True):
    """POST heartbeat to the appropriate server(s). Never raises."""
    global _last_heartbeat_time
    now = time.time()
    if now - _last_heartbeat_time < HEARTBEAT_INTERVAL:
        return
    _last_heartbeat_time = now

    # WORKER_ID + WORKER_PRIORITY come from .env (loaded at top of module).
    # Without these in the payload, server-side defaulting collapses every
    # box's heartbeat onto the same "trainer" row.
    _worker_id = os.environ.get("WORKER_ID", "trainer")
    _worker_priority = os.environ.get("WORKER_PRIORITY", "primary")

    payload = {
        "worker_id": _worker_id,
        "priority": _worker_priority,
        "role": "ew_worker",
        "chrome_alive": chrome_alive,
        "lookups_done": _lookups_done,
        "last_lookup_at": _last_lookup_at,
        "synthetic_ok": now < _synthetic_ok_until,
    }
    if serve_carhub:
        try:
            payload["worker"] = "carhub"
            http_requests.post(f"{CARHUB_SERVER}/api/vauto/heartbeat",
                               json=payload, timeout=5)
        except Exception:
            pass
    if serve_ew:
        try:
            payload["worker"] = "ew"
            http_requests.post(f"{EW_SERVER}/api/vauto/heartbeat",
                               json=payload, timeout=5)
        except Exception:
            pass


class ChromeDead(Exception):
    pass


def _post_file_with_retry(url, filepath, max_attempts=3, timeout=60, label="upload"):
    """Upload a file with retries on connection errors. Returns the response
    object on success or None. Handles ConnectionResetError / aborted / timeout
    with exponential backoff."""
    if not filepath or not os.path.exists(filepath):
        return None
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            with open(filepath, "rb") as f:
                r = http_requests.post(
                    url,
                    files={"file": (os.path.basename(filepath), f, "image/png")},
                    timeout=timeout,
                )
            if r.status_code == 200:
                return r
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        if attempt < max_attempts:
            wait = 2 ** (attempt - 1)  # 1s, 2s, 4s
            print(f"  [{label}] attempt {attempt}/{max_attempts} failed ({last_err}), retry in {wait}s")
            time.sleep(wait)
    print(f"  [{label}] all {max_attempts} attempts failed: {last_err}")
    return None


# ── Chrome ───────────────────────────────────────────────────────────────────

def _kill_zombie_chromes():
    """Kill ALL chrome.exe and chromedriver processes to clear zombies.

    This is called ONLY on initial startup and after ChromeDead — never
    between normal lookups (we keep one Chrome alive to avoid Cox 2FA).
    """
    import subprocess
    for proc_name in ("chrome.exe", "chromedriver.exe", "undetected_chromedriver.exe"):
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", proc_name],
                capture_output=True, timeout=10,
            )
        except Exception:
            pass
    # Give OS time to release ports / file locks
    time.sleep(2)


def _clean_profile_locks():
    """Remove stale singleton lock files from the Chrome profile directory.

    Chrome writes these on launch and removes them on clean shutdown. If
    Chrome crashes or is killed (e.g., Session 0 zombies), the lock files
    survive and block the next launch with 'profile in use' errors.
    """
    for lock_file in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        p = VAUTO_PROFILE / lock_file
        try:
            if p.exists():
                p.unlink()
                print(f"  Removed stale lock: {lock_file}")
        except Exception as e:
            print(f"  Could not remove {lock_file}: {e}")


def create_driver(kill_zombies=True):
    VAUTO_PROFILE.mkdir(parents=True, exist_ok=True)

    if kill_zombies:
        print("  Killing zombie Chrome/chromedriver processes...")
        _kill_zombie_chromes()

    _clean_profile_locks()

    opts = uc.ChromeOptions()
    opts.add_argument(f"--user-data-dir={VAUTO_PROFILE}")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1600,900")
    opts.add_argument("--disable-popup-blocking")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--password-store=basic")
    opts.page_load_strategy = "eager"
    return uc.Chrome(options=opts, use_subprocess=True, version_main=CHROME_VERSION)


# ── EW API ───────────────────────────────────────────────────────────────────

def ew_check_urgent():
    try:
        r = http_requests.get(f"{EW_SERVER}/api/vauto/urgent", timeout=5)
        if r.status_code == 200:
            return r.json().get("urgent", False)
    except Exception:
        pass
    return False


def ew_get_pending():
    try:
        # worker_id + priority drive the server's atomic claim. Without
        # these every worker shares one 'trainer/primary' identity and
        # only one of them effectively gets work at a time.
        params = {'worker_id': WORKER_ID, 'priority': WORKER_PRIORITY}
        r = http_requests.get(f"{EW_SERVER}/api/vauto/pending",
                              params=params, timeout=15)
        if r.status_code == 200:
            return r.json().get("pending", [])
    except Exception as e:
        print(f"  [EW] Error fetching pending: {e}")
    return []


def ew_submit(data):
    try:
        r = http_requests.post(f"{EW_SERVER}/api/vauto/submit", json=data, timeout=15)
        return r.status_code == 200
    except Exception as e:
        print(f"  [EW] Error submitting: {e}")
        return False


def ew_upload(filepath):
    r = _post_file_with_retry(f"{EW_SERVER}/api/vauto/upload_report",
                              filepath, label="EW vAuto upload")
    if r is None:
        return None
    try:
        fname = r.json().get("filename")
        if fname:
            return f"/vauto_reports/{fname}"
    except Exception:
        pass
    return None


# ── AccuTrade API ────────────────────────────────────────────────────────────

_consecutive_at_failures = 0


def accutrade_submit(data):
    try:
        r = http_requests.post(f"{EW_SERVER}/api/accutrade/submit", json=data, timeout=15)
        return r.status_code == 200
    except Exception as e:
        print(f"  [EW] AccuTrade submit error: {e}")
        return False


def accutrade_upload(filepath):
    r = _post_file_with_retry(f"{EW_SERVER}/api/accutrade/upload_report",
                              filepath, label="EW AccuTrade upload")
    if r is None:
        return None
    try:
        fname = r.json().get("filename")
        if fname:
            return f"/accutrade_reports/{fname}"
    except Exception:
        pass
    return None


def do_accutrade(driver, vin, miles, trim=None):
    """Run AccuTrade lookup. Returns dict or None. Never raises ChromeDead."""
    try:
        from enrich.accutrade import lookup_vin as at_lookup
        return at_lookup(driver, vin, miles, wait_timeout=45, trim=trim)
    except Exception as e:
        print(f"  [EW] AccuTrade lookup error: {e}")
        return None


# ── iPacket API ──────────────────────────────────────────────────────────────

_consecutive_ip_failures = 0


def ipacket_submit(data):
    try:
        r = http_requests.post(f"{EW_SERVER}/api/ipacket/submit", json=data, timeout=15)
        return r.status_code == 200
    except Exception as e:
        print(f"  [EW] iPacket submit error: {e}")
        return False


def ipacket_upload(filepath):
    r = _post_file_with_retry(f"{EW_SERVER}/api/ipacket/upload_report",
                              filepath, label="EW iPacket upload")
    if r is None:
        return None
    try:
        fname = r.json().get("filename")
        if fname:
            return f"/ipacket_reports/{fname}"
    except Exception:
        pass
    return None


def do_ipacket(driver, vin):
    """Run iPacket sticker lookup. Returns dict or None. Never raises ChromeDead."""
    try:
        from enrich.ipacket import lookup_vin as ip_lookup
        return ip_lookup(driver, vin, wait_timeout=30)
    except Exception as e:
        print(f"  [EW] iPacket lookup error: {e}")
        return None


# ── CarHub API ───────────────────────────────────────────────────────────────

def carhub_get_pending():
    try:
        r = http_requests.get(f"{CARHUB_SERVER}/api/vauto/pending", timeout=15)
        if r.status_code == 200:
            return r.json().get("pending", [])
    except Exception as e:
        print(f"  [CarHub] Error fetching pending: {e}")
    return []


def carhub_submit(data):
    try:
        r = http_requests.post(f"{CARHUB_SERVER}/api/vauto/submit", json=data, timeout=15)
        return r.status_code == 200
    except Exception as e:
        print(f"  [CarHub] Error submitting: {e}")
        return False


def carhub_upload(filepath):
    if not filepath or not os.path.exists(filepath):
        return None
    try:
        with open(filepath, "rb") as f:
            r = http_requests.post(
                f"{CARHUB_SERVER}/api/vauto/upload_report",
                files={"file": (os.path.basename(filepath), f, "image/png")},
                timeout=30,
            )
        if r.status_code == 200:
            return r.json().get("path")
    except Exception as e:
        print(f"  [CarHub] Error uploading: {e}")
    return None


# ── Lookup + process ─────────────────────────────────────────────────────────

def do_lookup(driver, vin, miles):
    from enrich import vauto
    try:
        return vauto.lookup_vin(driver, vin, miles, wait_timeout=180)
    except Exception as e:
        err = str(e).lower()
        if 'invalid session' in err or 'not reachable' in err or 'cannot connect' in err:
            raise ChromeDead(f"Chrome dead: {e}")
        raise


def _classify_lookup_error(err):
    """Map a lookup exception to a release-reason string the server can act on.

    Server-side auto-demote excludes `dns_unresolved` from the bad-job count
    because DNS hiccups are infrastructure issues, not worker problems. A
    worker that hits a transient Cox DNS outage shouldn't get demoted and
    stuck (the 2026-04-29 incident that stranded Trainer for 22.5 hours).
    """
    s = str(err)
    sl = s.lower()
    if 'err_name_not_resolved' in sl or 'getaddrinfo' in sl or 'name or service not known' in sl:
        return 'dns_unresolved'
    if 'did not hydrate' in sl or 'hydration' in sl:
        return 'vauto_hydration_timeout'
    if 'err_internet_disconnected' in sl or 'err_network_changed' in sl or 'err_connection_' in sl:
        return 'network_transient'
    return f'lookup_error:{type(err).__name__}'


def _run_synthetic_check(driver):
    """Lightweight reachability check that drives the server's auto-promote
    logic. Navigates to vAuto's home in the live Chrome session and
    confirms we land on a Cox URL (not a login redirect, not a network
    error). Sets _synthetic_ok_until so the next ~3 minutes of heartbeats
    carry synthetic_ok=true. Cheap by design — single GET + 2s wait.

    Why this matters: the server's auto-promote requires 3 of these in a
    row spanning 3+ minutes before flipping a degraded worker back to
    primary. Without this function being called, an auto-demoted worker
    polls forever and never claims another bid (the bear trap that
    stranded Trainer 22.5h on 2026-04-29).
    """
    global _synthetic_ok_until, _last_synthetic_check_at
    _last_synthetic_check_at = time.time()
    try:
        driver.get(SYNTHETIC_PROBE_URL)
        time.sleep(2)
        url = (driver.current_url or "").lower()
        if ('coxautoinc.com' in url
                and 'signin' not in url
                and 'okta' not in url
                and '/u/login' not in url):
            _synthetic_ok_until = time.time() + 180  # 3-min credit
            return True
        print(f"  [synthetic-check] non-cox/login URL: {url[:120]}")
    except Exception as e:
        print(f"  [synthetic-check] failed: {e}")
    return False


def _release_ew_claim(bid_id, reason):
    """Tell EW server we can't finish bid_id. Server clears the claim
    within seconds so another worker (or this one on retry) can take it.
    Without this, abandoned bids sit in_progress until the 5-min stale-
    claim sweep — observed today as the Trainer-zombie pattern that
    stranded bid 385."""
    try:
        http_requests.post(
            f"{EW_SERVER}/api/vauto/release_claim",
            json={"bid_id": bid_id, "reason": (reason or 'unknown')[:64]},
            timeout=5,
        )
    except Exception:
        pass


def _detect_session_loss(driver):
    """After every nav, check if we got redirected to Cox/Okta sign-in.
    If so the worker should self-pause until cookies are restored."""
    try:
        url = (driver.current_url or '').lower()
    except Exception:
        return False
    return any(s in url for s in (
        'bridge.coxautoinc.com', 'signin.cox', 'sso.coxautoinc.com',
        'okta.com/login', '/u/login',
    ))


def _post_session_lost(detail, current_url=''):
    """Tell EW server this worker lost its Cox session. Server flips
    paused=true; dispatch routes around us until a human re-auths."""
    try:
        http_requests.post(
            f"{EW_SERVER}/api/worker/session_lost",
            json={
                "worker_id": WORKER_ID,
                "url": current_url,
                "detail": detail[:200],
            },
            timeout=5,
        )
    except Exception:
        pass



def process_ew(driver, item):
    global _consecutive_failures, _lookups_done, _last_lookup_at
    vin = item["vin"]
    miles = item.get("mileage") or 0
    bid_id = item["bid_id"]
    label = f"{item.get('year', '')} {item.get('make', '')} {item.get('model', '')}".strip()

    print(f"  [EW] {vin} ({label or '?'}) bid #{bid_id}")
    try:
        result = do_lookup(driver, vin, miles)
    except ChromeDead:
        # Chrome itself died — release the claim so another worker can
        # try this bid while we restart Chrome. Without this the bid
        # sits in_progress for 5 min before the stale-claim sweep.
        _release_ew_claim(bid_id, 'chrome_dead_during_lookup')
        raise
    except Exception as e:
        try:
            cur_url = driver.current_url
        except Exception:
            cur_url = ''
        if _detect_session_loss(driver):
            print(f"  [EW] !! Cox session lost (URL: {cur_url}) — pausing")
            _post_session_lost(f'lookup error: {e}', cur_url)
            _release_ew_claim(bid_id, 'session_lost')
            _send_telegram(f"\U0001f534 EW worker {WORKER_ID}: Cox session lost — needs re-auth")
            return False
        _release_ew_claim(bid_id, _classify_lookup_error(e))
        print(f"  [EW] vAuto error: {e}")
        _consecutive_failures += 1
        if _consecutive_failures == 3:
            _send_telegram(f"\U0001f534 EW vAuto worker: {_consecutive_failures} consecutive lookup failures\nLast: {e}")
        return False

    if not result:
        _consecutive_failures += 1
        if _consecutive_failures == 3:
            _send_telegram(f"\U0001f534 EW vAuto worker: {_consecutive_failures} consecutive lookup failures (empty result)")
        return False

    global _synthetic_ok_until
    _consecutive_failures = 0
    _lookups_done += 1
    _last_lookup_at = time.strftime('%Y-%m-%d %H:%M:%S')
    # Successful end-to-end lookup is the strongest possible synthetic signal —
    # carry the synthetic_ok flag for 5 minutes (covers ~5 heartbeats, well
    # past the 3-in-3-min auto-promote threshold).
    _synthetic_ok_until = max(_synthetic_ok_until, time.time() + 300)

    # Push fresh cookies — every EW worker is a backup cookie keeper so
    # api_workers always have valid auth. Throttled internally to 60s.
    try:
        from cookie_export import export_cookies as _export_cookies
        _export_cookies(driver)
    except Exception as _e:
        print(f"  [cookie-export] {_e}")

    cf_server = ew_upload(result.get("carfax_screenshot"))
    ac_server = ew_upload(result.get("autocheck_screenshot"))

    payload = {
        "bid_id": bid_id, "vin": vin,
        "rbook": result.get("rbook"),
        "wholesale_avg": result.get("wholesale_avg"),
        "retail_avg": result.get("retail_avg"),
        "mmr_val": result.get("mmr"),
        "kbb_val": result.get("kbb"),
        "kbb_com_val": result.get("kbb_com"),
        "jd_power_val": result.get("jd_power"),
        "price_rank": result.get("price_rank"),
        "adj_pct_market": result.get("adj_pct_market"),
        "title_status": result.get("title_status"),
        "carfax_screenshot": cf_server or None,
        "autocheck_screenshot": ac_server or None,
        "raw": result.get("raw", {}),
        "appraisal_url": result.get("appraisal_url"),
    }
    ok = ew_submit(payload)
    bb = result.get("wholesale_avg") or 0
    print(f"    {'OK' if ok else 'FAIL'}: BB=${bb:,} title={result.get('title_status')}")

    # ── AccuTrade lookup (non-fatal — won't block vAuto/iPacket on error) ──
    global _consecutive_at_failures
    at_start = time.time()
    try:
        at_result = do_accutrade(driver, vin, miles, trim=item.get('trim'))
        at_elapsed = int(time.time() - at_start)
        if at_result and at_result.get('not_available'):
            # AccuTrade has no data for this VIN (ultra-rare / brand-new car).
            # Record the state so bid card shows NOT AVAILABLE instead of pending.
            reason = at_result.get('reason', 'unavailable')
            at_ok = accutrade_submit({
                'bid_id': bid_id, 'vin': vin,
                'not_available': True,
                'unavailable_reason': reason,
            })
            print(f"    AccuTrade {'OK' if at_ok else 'FAIL'} ({at_elapsed}s): NOT AVAILABLE — {reason[:80]}")
            _consecutive_at_failures = 0  # expected for some VINs, not a failure
        elif at_result:
            at_screenshot = accutrade_upload(at_result.get('screenshot'))
            at_payload = {
                'bid_id': bid_id, 'vin': vin,
                'guaranteed_offer': at_result.get('guaranteed_offer'),
                'trade_in': at_result.get('trade_in'),
                'trade_market': at_result.get('trade_market'),
                'retail': at_result.get('retail'),
                'market_avg': at_result.get('market_avg'),
                'local_comps': at_result.get('local_comps'),
                'screenshot': at_screenshot,
                'raw': at_result.get('raw', {}),
                'appraisal_url': at_result.get('appraisal_url'),
            }
            at_ok = accutrade_submit(at_payload)
            filled = sum(1 for k in ('guaranteed_offer','trade_in','trade_market','retail','market_avg')
                         if at_result.get(k) is not None)
            g = at_result.get('guaranteed_offer') or 0
            print(f"    AccuTrade {'OK' if at_ok else 'FAIL'} ({at_elapsed}s): {filled}/5 values, guaranteed=${g:,}")
            _consecutive_at_failures = 0
        else:
            # None = genuine error (login/nav/network) — worth alerting
            _consecutive_at_failures += 1
            print(f"    AccuTrade: empty result ({at_elapsed}s), consec_failures={_consecutive_at_failures}")
            if _consecutive_at_failures == 3:
                _send_telegram(f"\U0001f534 AccuTrade: {_consecutive_at_failures} consecutive failures (empty result)")
    except Exception as e:
        at_elapsed = int(time.time() - at_start)
        print(f"    AccuTrade error (non-fatal, {at_elapsed}s): {e}")
        _consecutive_at_failures += 1
        if _consecutive_at_failures == 3:
            _send_telegram(f"\U0001f534 AccuTrade: {_consecutive_at_failures} consecutive failures\n{e}")

    # ── iPacket sticker lookup (non-fatal — never blocks vAuto) ──────────
    global _consecutive_ip_failures
    try:
        ip_result = do_ipacket(driver, vin)
        if ip_result and ip_result.get('not_available'):
            # VIN is a real VIN but iPacket has no sticker — record the state so
            # EW dashboard shows "NOT AVAILABLE" instead of pulsing PENDING forever
            reason = ip_result.get('reason', 'unavailable')
            ip_ok = ipacket_submit({
                'bid_id': bid_id, 'vin': vin,
                'not_available': True,
                'unavailable_reason': reason,
            })
            print(f"    iPacket {'OK' if ip_ok else 'FAIL'}: NOT AVAILABLE — {reason[:80]}")
            _consecutive_ip_failures = 0  # not a failure — expected for some VINs
        elif ip_result:
            ip_screenshot = ipacket_upload(ip_result.get('screenshot'))
            ip_payload = {
                'bid_id': bid_id, 'vin': vin,
                'total_msrp': ip_result.get('total_msrp'),
                'base_price': ip_result.get('base_price'),
                'exterior_color': ip_result.get('exterior_color'),
                'interior_color': ip_result.get('interior_color'),
                'screenshot': ip_screenshot,
                'raw': ip_result.get('raw', {}),
            }
            ip_ok = ipacket_submit(ip_payload)
            msrp = ip_result.get('total_msrp') or 0
            print(f"    iPacket {'OK' if ip_ok else 'FAIL'}: MSRP=${msrp:,}")
            _consecutive_ip_failures = 0
        else:
            # None = genuine error (login/nav/network) — worth alerting
            _consecutive_ip_failures += 1
            if _consecutive_ip_failures == 3:
                _send_telegram(f"\U0001f534 iPacket: {_consecutive_ip_failures} consecutive failures (empty result)")
    except Exception as e:
        print(f"    iPacket error (non-fatal): {e}")
        _consecutive_ip_failures += 1
        if _consecutive_ip_failures == 3:
            _send_telegram(f"\U0001f534 iPacket: {_consecutive_ip_failures} consecutive failures\n{e}")

    return ok


def process_carhub(driver, item):
    global _consecutive_failures, _lookups_done, _last_lookup_at
    vin = item["vin"]
    miles = item.get("mileage") or 0
    lid = item["listing_id"]
    label = f"{item.get('year', '')} {item.get('make', '')} {item.get('model', '')}".strip()

    print(f"  [CarHub] {vin} ({label or '?'})")
    try:
        result = do_lookup(driver, vin, miles)
    except ChromeDead:
        raise
    except Exception as e:
        print(f"  [CarHub] vAuto error: {e}")
        _consecutive_failures += 1
        if _consecutive_failures == 3:
            _send_telegram(f"\U0001f534 CarHub vAuto worker: {_consecutive_failures} consecutive lookup failures\nLast: {e}")
        return False

    if not result:
        _consecutive_failures += 1
        if _consecutive_failures == 3:
            _send_telegram(f"\U0001f534 CarHub vAuto worker: {_consecutive_failures} consecutive lookup failures (empty result)")
        return False

    _consecutive_failures = 0
    _lookups_done += 1
    _last_lookup_at = time.strftime('%Y-%m-%d %H:%M:%S')

    cf_server = carhub_upload(result.get("carfax_screenshot"))
    ac_server = carhub_upload(result.get("autocheck_screenshot"))

    payload = {
        "vin": vin, "listing_id": lid,
        "rbook": result.get("rbook"),
        "wholesale_avg": result.get("wholesale_avg"),
        "retail_avg": result.get("retail_avg"),
        "mmr_val": result.get("mmr"),
        "kbb_val": result.get("kbb"),
        "kbb_com_val": result.get("kbb_com"),
        "jd_power_val": result.get("jd_power"),
        "price_rank": result.get("price_rank"),
        "adj_pct_market": result.get("adj_pct_market"),
        "title_status": result.get("title_status"),
        "carfax_screenshot": cf_server or result.get("carfax_screenshot"),
        "autocheck_screenshot": ac_server or result.get("autocheck_screenshot"),
        "raw": result.get("raw", {}),
    }
    ok = carhub_submit(payload)
    bb = result.get("wholesale_avg") or 0
    print(f"    {'OK' if ok else 'FAIL'}: BB=${bb:,} title={result.get('title_status')}")
    return ok


# ── Keep-alive ───────────────────────────────────────────────────────────────

def _keepalive_touch(driver):
    """Navigate to each upstream to refresh session cookies. If any redirects
    to a login page, Telegram-alert the client — they need to RDP in and
    re-auth before the next bid blows up. Non-fatal: never raises."""
    alerts = []
    for name, url, login_patterns in KEEPALIVE_SITES:
        try:
            print(f"  [keep-alive] Touching {name}...")
            driver.get(url)
            time.sleep(4)
            current = (driver.current_url or "").lower()
            is_login = any(p.lower() in current for p in login_patterns)
            if is_login:
                print(f"  [keep-alive] {name} session EXPIRED: {current[:120]}")
                alerts.append((name, current[:180]))
            else:
                print(f"  [keep-alive] {name} OK")
        except Exception as e:
            print(f"  [keep-alive] {name} error: {e}")

    try:
        driver.get("about:blank")
    except Exception:
        pass

    if alerts:
        host = os.environ.get('COMPUTERNAME', 'worker')
        msg = "⚠️ Session(s) expired on " + host + " — re-login needed:\n"
        for name, url in alerts:
            msg += f"• {name}: {url}\n"
        _send_telegram(msg)


# ── Main loop ────────────────────────────────────────────────────────────────

def run_once(driver, serve_ew=True, serve_carhub=True):
    total_ok = 0
    slots = MAX_PER_POLL

    # EW first — always priority
    if serve_ew:
        ew_pending = ew_get_pending()
        if ew_pending:
            batch = ew_pending[:slots]
            print(f"  [EW] {len(ew_pending)} pending, processing {len(batch)}")
            for item in batch:
                if process_ew(driver, item):
                    total_ok += 1
                slots -= 1
                time.sleep(3)

    # CarHub with remaining slots
    if serve_carhub and slots > 0:
        ch_pending = carhub_get_pending()
        if ch_pending:
            batch = ch_pending[:slots]
            print(f"  [CarHub] {len(ch_pending)} pending, processing {len(batch)}")
            for item in batch:
                # Check for EW urgent mid-batch — interrupt CarHub if needed
                if serve_ew and ew_check_urgent():
                    print(f"  [EW] URGENT bid detected — pausing CarHub")
                    ew_pending = ew_get_pending()
                    for ew_item in ew_pending[:3]:
                        process_ew(driver, ew_item)
                        time.sleep(3)
                if process_carhub(driver, item):
                    total_ok += 1
                time.sleep(3)

    return total_ok


def main():
    global _consecutive_failures, _last_activity_ts
    _last_activity_ts = time.time()
    ap = argparse.ArgumentParser(description="Unified vAuto Worker (Beelink)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--ew-only", action="store_true")
    ap.add_argument("--carhub-only", action="store_true")
    args = ap.parse_args()

    serve_ew = not args.carhub_only
    serve_carhub = not args.ew_only
    label = _worker_label(serve_ew, serve_carhub)
    hostname = os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", "unknown"))

    sources = []
    if serve_ew:
        sources.append(f"EW ({EW_SERVER})")
    if serve_carhub:
        sources.append(f"CarHub ({CARHUB_SERVER})")

    print(f"Unified vAuto Worker — Chrome {CHROME_VERSION}")
    print(f"  Serving: {' + '.join(sources)}")
    print(f"  EW urgent: {EW_URGENT_INTERVAL}s | EW poll: {EW_POLL_INTERVAL}s | CarHub: {CARHUB_POLL_INTERVAL}s")

    driver = create_driver()

    # Startup alert
    _send_telegram(f"\u2705 {label} vAuto worker started on {hostname}")

    if args.once:
        try:
            run_once(driver, serve_ew, serve_carhub)
        finally:
            try:
                driver.quit()
            except Exception:
                pass
        return

    last_ew_poll = 0
    last_carhub_poll = 0

    try:
        while True:
            now = time.time()
            ts = time.strftime('%H:%M:%S')

            # Send heartbeat every 60s
            _send_heartbeat(serve_ew, serve_carhub, chrome_alive=True)

            # Periodic synthetic check — feeds the server's auto-promote logic
            # so a transient DNS/Cox outage that auto-demoted us doesn't strand
            # the worker forever. Throttled: only fires when no recent success
            # is keeping the synthetic flag valid, AND only every 60s. Touches
            # vAuto's home, doesn't claim or process anything.
            if (now >= _synthetic_ok_until
                    and now - _last_synthetic_check_at >= SYNTHETIC_CHECK_INTERVAL):
                _run_synthetic_check(driver)

            urgent = serve_ew and ew_check_urgent()
            do_ew = urgent or (serve_ew and (now - last_ew_poll >= EW_POLL_INTERVAL))
            do_carhub = serve_carhub and (now - last_carhub_poll >= CARHUB_POLL_INTERVAL)

            if do_ew or do_carhub:
                queues = []
                if do_ew:
                    queues.append("EW" + (" URGENT" if urgent else ""))
                if do_carhub:
                    queues.append("CarHub")

                print(f"\n[{ts}] Polling: {' + '.join(queues)}")
                try:
                    processed = run_once(driver, serve_ew=do_ew, serve_carhub=do_carhub)
                    if do_ew:
                        last_ew_poll = now
                    if do_carhub:
                        last_carhub_poll = now
                    # Any real processing counts as session-refreshing activity
                    if processed:
                        _last_activity_ts = time.time()
                except ChromeDead as e:
                    print(f"  {e}")
                    _send_telegram(f"\u26a0\ufe0f {label} vAuto worker: Chrome crashed, restarting...\n{e}")
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    print("  Restarting Chrome (killing zombies + cleaning locks)...")
                    driver = create_driver(kill_zombies=True)
                    _consecutive_failures = 0
                except Exception as e:
                    print(f"  Error: {e}")
                    traceback.print_exc()
                    _send_telegram(f"\u26a0\ufe0f {label} vAuto worker: unexpected error, restarting Chrome...\n{e}")
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    print("  Restarting Chrome (killing zombies + cleaning locks)...")
                    driver = create_driver(kill_zombies=True)
                    _consecutive_failures = 0

            # Keep-alive: if idle > KEEPALIVE_INTERVAL with no activity, touch
            # vAuto/AccuTrade/iPacket to refresh cookies + alert if re-login needed
            if time.time() - _last_activity_ts > KEEPALIVE_INTERVAL:
                try:
                    print(f"\n[{time.strftime('%H:%M:%S')}] Keep-alive (idle > {KEEPALIVE_INTERVAL//60}m)")
                    _keepalive_touch(driver)
                except Exception as e:
                    print(f"  [keep-alive] failed: {e}")
                _last_activity_ts = time.time()

            time.sleep(EW_URGENT_INTERVAL)

    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
