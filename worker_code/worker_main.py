"""
Playwright-based EW Worker (VM edition).

Polls EW server for pending vAuto bids, runs vAuto + AccuTrade + iPacket
through process_bid.py, uploads screenshots / PDFs, submits results, and
publishes heartbeats + cookie refresh in background threads.

Replaces the fragile bare-metal Trainer / Worker-2 (Chrome 147 + UC 3.5.5)
worker. Uses bundled Playwright Chromium instead.

Usage:
  python worker_main.py              # continuous loop
  python worker_main.py --once       # one pass then exit
  python worker_main.py --ew-only    # default already; CarHub removed
"""

import argparse
import os
import sys
import time
import threading
import traceback
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import requests as http_requests

# Reuse the working bid pipeline as a module — DO NOT modify these.
from process_bid import process_bid

# ── Config ────────────────────────────────────────────────────────────────────
EW_SERVER = os.environ.get("EW_SERVER", "https://experience-wholesale.net")
WORKER_ID = os.environ.get("WORKER_ID", "vm-worker-1")
WORKER_PRIORITY = os.environ.get("WORKER_PRIORITY", "primary")
WORKER_SOURCE = os.environ.get("WORKER_SOURCE", "ew")

EW_POLL_INTERVAL = 5
HEARTBEAT_INTERVAL = 30  # bumped from 60 for tighter watchdog detection
COOKIE_EXPORT_INTERVAL = 60

# Cookie refresh constants (mirrors Beelink-115 cookie_export.py)
EW_REFRESH_URL = f"{EW_SERVER}/api/vauto/refresh_cookies"
ENTITY_ID = os.environ.get(
    "EW_ENTITY_ID",
    "jwaCvVdjsSFLY6C4O3LS63o-dJrUWByBui-rLqfI30Y=",  # matches Beelink-115's value
)
PLATFORM_USER_ID = os.environ.get(
    "EW_PLATFORM_USER_ID",
    "871ccb54-8ee2-4b06-884c-763673204ae9",
)
WANTED_COOKIE_DOMAINS = ("coxautoinc.com", "vauto.com",
                         "vauto.app.coxautoinc.com")

# Telegram
TELEGRAM_BOT_TOKEN = "8528106109:AAFczHqjWoiUBs7adZwBEJ6217bQzYGhI_o"
TELEGRAM_CHAT_ID = "7985611488"

# Profile dir for the cookie-export side-thread Playwright session
PROFILE_DIR = Path(r"C:\worker\vauto_profile")

# ── Shared state ─────────────────────────────────────────────────────────────
_lookups_done = 0
_last_lookup_at = None
_consecutive_failures = 0
_synthetic_ok_until = 0.0
_stop_event = threading.Event()


# ── Telegram ─────────────────────────────────────────────────────────────────

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


# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _post_file_with_retry(url, filepath, max_attempts=3, timeout=60, label="upload"):
    """Upload a file with retries on connection errors. Returns the response
    object on success or None."""
    if not filepath or not os.path.exists(filepath):
        return None
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            with open(filepath, "rb") as f:
                # Pick a sane content-type — PDFs vs PNGs both work as octet-stream
                # but the EW server has historically accepted image/png for either.
                ext = os.path.splitext(filepath)[1].lower()
                ctype = "application/pdf" if ext == ".pdf" else "image/png"
                r = http_requests.post(
                    url,
                    files={"file": (os.path.basename(filepath), f, ctype)},
                    timeout=timeout,
                )
            if r.status_code == 200:
                return r
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        if attempt < max_attempts:
            wait = 2 ** (attempt - 1)
            print(f"  [{label}] attempt {attempt}/{max_attempts} failed ({last_err}), retry in {wait}s")
            time.sleep(wait)
    print(f"  [{label}] all {max_attempts} attempts failed: {last_err}")
    return None


def _upload(endpoint_path, filepath, server_prefix, label):
    """POST a file to /api/<endpoint_path>, return server-relative path."""
    if not filepath or not os.path.exists(filepath):
        return None
    r = _post_file_with_retry(f"{EW_SERVER}{endpoint_path}", filepath, label=label)
    if r is None:
        return None
    try:
        fname = r.json().get("filename")
        if fname:
            return f"{server_prefix}/{fname}"
    except Exception:
        pass
    return None


def vauto_upload(filepath):
    return _upload("/api/vauto/upload_report", filepath,
                   "/vauto_reports", "vAuto upload")


def accutrade_upload(filepath):
    return _upload("/api/accutrade/upload_report", filepath,
                   "/accutrade_reports", "AccuTrade upload")


def ipacket_upload(filepath):
    return _upload("/api/ipacket/upload_report", filepath,
                   "/ipacket_reports", "iPacket upload")


# ── EW API ───────────────────────────────────────────────────────────────────

def ew_get_pending():
    try:
        params = {"worker_id": WORKER_ID, "priority": WORKER_PRIORITY, "source": WORKER_SOURCE}
        r = http_requests.get(f"{EW_SERVER}/api/vauto/pending",
                              params=params, timeout=30)
        if r.status_code == 200:
            return r.json().get("pending", [])
    except Exception as e:
        print(f"  [EW] Error fetching pending: {e}")
    return []


def _ew_submit_with_retry(url, payload, name, max_attempts=3):
    """POST payload to EW server, retry on transient failures.
    timeout=60 catches slow gunicorn responses (was 15 — too tight when
    gunicorn is mid-AI-assessment). Retries 1s/3s/8s on 5xx or exception.
    Don't retry on 4xx (permanent client error). Logs status+body on
    every non-200 so we can diagnose."""
    last_err = "no attempts"
    for attempt in range(1, max_attempts + 1):
        try:
            r = http_requests.post(url, json=payload, timeout=60)
            if r.status_code == 200:
                return True
            try: body = (r.text or "")[:300]
            except Exception: body = "<no body>"
            last_err = f"HTTP {r.status_code} body={body!r}"
            if 400 <= r.status_code < 500:
                # Permanent — don't retry
                print(f"  [EW] {name} submit attempt {attempt}/{max_attempts}: {last_err} (4xx — no retry)")
                return False
            print(f"  [EW] {name} submit attempt {attempt}/{max_attempts}: {last_err}")
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            print(f"  [EW] {name} submit attempt {attempt}/{max_attempts} exception: {last_err}")
        if attempt < max_attempts:
            time.sleep([1, 3, 8][attempt - 1] if attempt - 1 < 3 else 8)
    print(f"  [EW] {name} submit gave up after {max_attempts} attempts: {last_err}")
    return False


def ew_submit_vauto(payload):
    return _ew_submit_with_retry(f"{EW_SERVER}/api/vauto/submit", payload, "vAuto")


def ew_submit_accutrade(payload):
    return _ew_submit_with_retry(f"{EW_SERVER}/api/accutrade/submit", payload, "AccuTrade")


def ew_submit_ipacket(payload):
    return _ew_submit_with_retry(f"{EW_SERVER}/api/ipacket/submit", payload, "iPacket")


def _post_phase(bid_id, phase, state):
    """Fire-and-forget per-phase progress report. Watchdog uses this to
    detect stuck bids in 30-90s instead of 5min."""
    if not bid_id:
        return
    try:
        http_requests.post(
            f"{EW_SERVER}/api/worker/progress",
            json={
                "worker_id": WORKER_ID,
                "bid_id": bid_id,
                "phase": phase,
                "state": state,
            },
            timeout=4,
        )
    except Exception:
        pass


def _post_session_lost(detail, current_url=""):
    try:
        http_requests.post(
            f"{EW_SERVER}/api/worker/session_lost",
            json={
                "worker_id": WORKER_ID,
                "url": current_url,
                "detail": (detail or "")[:200],
            },
            timeout=5,
        )
    except Exception:
        pass


def _is_signin_url(url):
    if not url:
        return False
    u = url.lower()
    return any(s in u for s in (
        "bridge.coxautoinc.com", "signin.cox", "sso.coxautoinc.com",
        "okta.com/login", "/u/login", "/signin",
    ))


# ── Heartbeat thread ─────────────────────────────────────────────────────────

def _heartbeat_loop():
    """Push a heartbeat to /api/vauto/heartbeat every HEARTBEAT_INTERVAL."""
    while not _stop_event.is_set():
        try:
            now = time.time()
            payload = {
                "worker_id": WORKER_ID,
                "priority": WORKER_PRIORITY,
                "role": "ew_worker",
                "chrome_alive": True,
                "lookups_done": _lookups_done,
                "last_lookup_at": _last_lookup_at,
                "synthetic_ok": now < _synthetic_ok_until,
                "worker": "ew",
            }
            r = http_requests.post(f"{EW_SERVER}/api/vauto/heartbeat",
                                   json=payload, timeout=15)
            # Server-driven self-exit: watchdog flagged us as stuck. NSSM
            # auto-restarts in a few seconds.
            try:
                if r.status_code == 200 and r.json().get("exit"):
                    print("  [heartbeat] server requested exit — terminating")
                    _send_telegram(
                        f"⚠️ EW worker {WORKER_ID}: exiting on server request"
                    )
                    os._exit(5)
            except Exception:
                pass
        except Exception as e:
            print(f"  [heartbeat] error: {e}")
        # Sleep in 1-sec chunks so --once shutdown is responsive
        for _ in range(HEARTBEAT_INTERVAL):
            if _stop_event.is_set():
                return
            time.sleep(1)


# ── Cookie export thread ─────────────────────────────────────────────────────

def _cookie_export_loop():
    """Open a lightweight Playwright context against the same persistent
    profile, scrape Cox/vAuto cookies, POST to EW. Repeats every minute.

    Uses a separate browser instance to avoid colliding with the main
    process_bid context (Playwright can't share a persistent profile across
    two simultaneous launches). To prevent that collision we only RUN this
    thread between bids — it acquires the context, dumps cookies, closes.
    Worst case the thread fails to open (profile in use) and we just skip.
    """
    from playwright.sync_api import sync_playwright

    while not _stop_event.is_set():
        # Sleep first so we don't fight the very first bid for the profile lock
        for _ in range(COOKIE_EXPORT_INTERVAL):
            if _stop_event.is_set():
                return
            time.sleep(1)

        scoped = {}
        try:
            with sync_playwright() as p:
                ctx = p.chromium.launch_persistent_context(  # STEALTH-2026-05-10
                    user_data_dir=str(PROFILE_DIR),
                    headless=True,
                    viewport={"width": 1200, "height": 800},
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
                    locale="en-US",
                    timezone_id="America/New_York",
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-first-run",
                        "--no-default-browser-check",
                    ],
                )
                try:
                    raw = ctx.cookies()
                finally:
                    ctx.close()
            for c in raw:
                domain = (c.get("domain") or "").lstrip(".")
                if any(d in domain for d in WANTED_COOKIE_DOMAINS):
                    scoped[c["name"]] = c["value"]
        except Exception as e:
            # Most likely "profile in use" — main loop is mid-bid. Skip silently.
            msg = str(e)
            if "in use" not in msg.lower() and "lock" not in msg.lower():
                print(f"  [cookie-export] context failed: {e}")
            continue

        if "vAutoAuth" not in scoped:
            # No Cox session yet — don't push junk
            continue

        try:
            r = http_requests.post(EW_REFRESH_URL, json={
                "label": WORKER_ID,
                "cookies": scoped,
                "entity_id": ENTITY_ID,
                "platform_user_id": PLATFORM_USER_ID,
            }, timeout=10)
            if r.status_code == 200:
                print(f"  [cookie-export] OK ({len(scoped)} cookies)")
            else:
                print(f"  [cookie-export] HTTP {r.status_code}: {r.text[:120]}")
        except Exception as e:
            print(f"  [cookie-export] post failed: {e}")


# ── Per-bid pipeline ─────────────────────────────────────────────────────────

def process_one_bid(item):
    """Run process_bid against one EW pending item and submit results.
    Returns True if the vAuto submit succeeded, False otherwise."""
    global _lookups_done, _last_lookup_at, _consecutive_failures, _synthetic_ok_until

    bid_id = item.get("bid_id")
    vin = item.get("vin")
    miles = item.get("mileage") or 0
    trim = item.get("trim")
    label = " ".join(str(item.get(k, "")) for k in ("year", "make", "model")).strip()

    print(f"\n[bid #{bid_id}] {vin} ({label or '?'}) miles={miles:,} trim={trim}")

    # Phase callbacks fired from inside process_bid around each lookup —
    # this matches per-phase watchdog timeouts (vauto 90s, accutrade 80s,
    # ipacket 40s) instead of bracketing all 3 phases as one giant "vauto."
    def _phase_cb(phase, state):
        _post_phase(bid_id, phase, state)

    try:
        result = process_bid(vin, miles, trim, on_phase=_phase_cb)
    except Exception as e:
        traceback.print_exc()
        # If process_bid raised because Cox sent us to signin, surface that.
        emsg = str(e)
        if _is_signin_url(emsg):
            _post_session_lost(f"process_bid error: {emsg}", emsg)
            _send_telegram(f"\U0001f534 EW worker {WORKER_ID}: Cox session lost — needs re-auth")
        _consecutive_failures += 1
        if _consecutive_failures == 3:
            _send_telegram(f"\U0001f534 EW worker {WORKER_ID}: 3 consecutive bid failures\nLast: {emsg[:300]}")
        return False

    vauto = result.get("vauto") or {}
    accu = result.get("accutrade") or {}
    ipkt = result.get("ipacket") or {}

    # Detect appraisal_url that points to a sign-in page (session lost)
    for sub in (vauto, accu):
        url = (sub or {}).get("appraisal_url")
        if _is_signin_url(url):
            _post_session_lost("appraisal_url is sign-in page", url)
            _send_telegram(f"\U0001f534 EW worker {WORKER_ID}: Cox session lost mid-bid")
            break

    # ── vAuto submit ─────────────────────────────────────────────────────
    vauto_ok = False
    if vauto and not vauto.get("error"):
        carfax_path = vauto_upload(vauto.get("carfax_screenshot"))
        autocheck_path = vauto_upload(vauto.get("autocheck_screenshot"))

        # Field renames per spec — see w2_main.py lines 580-602
        kbb = vauto.get("kbb")
        kbb_com = vauto.get("kbb_com")
        retail_avg = kbb if kbb is not None else kbb_com

        v_payload = {
            "bid_id": bid_id,
            "vin": vin,
            "rbook": vauto.get("rbook"),
            "wholesale_avg": vauto.get("black_book"),
            "retail_avg": retail_avg,
            "mmr_val": vauto.get("mmr"),
            "kbb_val": kbb,
            "kbb_com_val": kbb_com,
            "jd_power_val": vauto.get("jd_power"),
            "price_rank": vauto.get("price_rank"),
            "adj_pct_market": vauto.get("adj_pct_market"),
            "title_status": vauto.get("title_status"),
            "carfax_screenshot": carfax_path,
            "autocheck_screenshot": autocheck_path,
            "raw": vauto.get("raw", {}),
            "appraisal_url": vauto.get("appraisal_url"),
        }
        vauto_ok = ew_submit_vauto(v_payload)
        bb = vauto.get("black_book") or 0
        print(f"  vAuto {'OK' if vauto_ok else 'FAIL'}: BB=${bb:,} title={vauto.get('title_status')}")
        if vauto_ok:
            _post_phase(bid_id, "vauto", "done")
    else:
        print(f"  vAuto skipped: {vauto.get('error') if vauto else 'no result'}")
        # Mark vauto done even on skip so watchdog doesn't treat it as stuck —
        # the bid will still be released when this function returns False.
        _post_phase(bid_id, "vauto", "done")

    # ── AccuTrade submit ──────────────────────────────────────────────────
    _post_phase(bid_id, "accutrade", "started")
    if accu and not accu.get("error"):
        if accu.get("not_available"):
            ok = ew_submit_accutrade({
                "bid_id": bid_id, "vin": vin,
                "not_available": True,
                "unavailable_reason": accu.get("reason", "unavailable"),
            })
            print(f"  AccuTrade {'OK' if ok else 'FAIL'}: NOT AVAILABLE")
        else:
            screenshot_path = accutrade_upload(accu.get("screenshot"))
            a_payload = {
                "bid_id": bid_id, "vin": vin,
                "guaranteed_offer": accu.get("guaranteed_offer"),
                "trade_in": accu.get("trade_in"),
                "trade_market": accu.get("trade_market"),
                "retail": accu.get("retail"),
                "market_avg": accu.get("market_avg"),
                "local_comps": accu.get("local_comps"),
                "screenshot": screenshot_path,
                "raw": accu.get("raw", {}),
                "appraisal_url": accu.get("appraisal_url"),
            }
            ok = ew_submit_accutrade(a_payload)
            g = accu.get("guaranteed_offer") or 0
            print(f"  AccuTrade {'OK' if ok else 'FAIL'}: guaranteed=${g:,}")
    else:
        print(f"  AccuTrade skipped: {accu.get('error') if accu else 'no result'}")
    _post_phase(bid_id, "accutrade", "done")

    # ── iPacket submit ────────────────────────────────────────────────────
    _post_phase(bid_id, "ipacket", "started")
    if ipkt and not ipkt.get("error"):
        if ipkt.get("not_available"):
            # Refinement C: forward screenshot if iPacket captured one before
            # giving up (viewer_did_not_render path). Server-side INSERT
            # already accepts NULL or a path.
            na_payload = {
                "bid_id": bid_id, "vin": vin,
                "not_available": True,
                "unavailable_reason": ipkt.get("reason", "unavailable"),
            }
            if ipkt.get("screenshot"):
                na_payload["screenshot"] = ipacket_upload(ipkt.get("screenshot"))
            ok = ew_submit_ipacket(na_payload)
            print(f"  iPacket {'OK' if ok else 'FAIL'}: NOT AVAILABLE")
        else:
            screenshot_path = ipacket_upload(ipkt.get("screenshot"))
            i_payload = {
                "bid_id": bid_id, "vin": vin,
                "total_msrp": ipkt.get("total_msrp"),
                "base_price": ipkt.get("base_price"),
                "exterior_color": ipkt.get("exterior_color"),
                "interior_color": ipkt.get("interior_color"),
                "screenshot": screenshot_path,
                "raw": ipkt.get("raw", {}),
            }
            ok = ew_submit_ipacket(i_payload)
            msrp = ipkt.get("total_msrp") or 0
            print(f"  iPacket {'OK' if ok else 'FAIL'}: MSRP=${msrp:,}")
    else:
        # 2026-05-10: ALWAYS submit something so the assess-gate has
        # visibility. Previously this branch printed 'skipped' and let
        # phase=done land on bid_phase_progress without a corresponding
        # ipacket_lookups row -- assess-gate hung forever waiting on
        # ipkt=True. Now we submit not_available=True with the error so
        # the bid progresses instead of stalling.
        _err = ipkt.get('error') if ipkt else 'no result'
        print(f"  iPacket skipped: {_err} — submitting not_available")
        try:
            ok = ew_submit_ipacket({
                "bid_id": bid_id, "vin": vin,
                "not_available": True,
                "unavailable_reason": f"worker error: {_err}",
            })
            print(f"  iPacket {'OK' if ok else 'FAIL'}: skipped->NA")
        except Exception as _se:
            print(f"  iPacket NA-submit failed: {_se}")
    _post_phase(bid_id, "ipacket", "done")

    # Successful bid pipeline -- 5 min of synthetic_ok credit for auto-promote
    if vauto_ok:
        _consecutive_failures = 0
        _lookups_done += 1
        _last_lookup_at = time.strftime("%Y-%m-%d %H:%M:%S")
        _synthetic_ok_until = max(_synthetic_ok_until, time.time() + 300)
    else:
        _consecutive_failures += 1
        if _consecutive_failures == 3:
            _send_telegram(f"\U0001f534 EW worker {WORKER_ID}: 3 consecutive bid failures (vauto submit failing)")

    return vauto_ok


# ── Main loop ────────────────────────────────────────────────────────────────

def run_pass():
    pending = ew_get_pending()
    if not pending:
        print("  0 pending")
        return 0
    print(f"  {len(pending)} pending")
    ok_count = 0
    for item in pending:
        try:
            if process_one_bid(item):
                ok_count += 1
        except Exception:
            traceback.print_exc()
        # Small breather between bids
        time.sleep(2)
    return ok_count


def main():
    ap = argparse.ArgumentParser(description="EW VM Worker (Playwright)")
    ap.add_argument("--once", action="store_true",
                    help="One pass through pending then exit")
    ap.add_argument("--ew-only", action="store_true",
                    help="(default) Only serve EW; CarHub is dead")
    args = ap.parse_args()

    print(f"EW VM Worker — Playwright")
    print(f"  WORKER_ID={WORKER_ID} priority={WORKER_PRIORITY} source={WORKER_SOURCE}")
    print(f"  EW_SERVER={EW_SERVER}")
    print(f"  poll={EW_POLL_INTERVAL}s  heartbeat={HEARTBEAT_INTERVAL}s  cookie_export={COOKIE_EXPORT_INTERVAL}s")

    # Background threads (daemon=True so --once exits cleanly)
    hb_thread = threading.Thread(target=_heartbeat_loop, daemon=True, name="heartbeat")
    hb_thread.start()
    if not args.once:
        ck_thread = threading.Thread(target=_cookie_export_loop, daemon=True, name="cookie_export")
        ck_thread.start()

    try:
        if args.once:
            run_pass()
            return

        while True:
            try:
                run_pass()
            except KeyboardInterrupt:
                raise
            except Exception:
                traceback.print_exc()
                _send_telegram(f"\U0001f534 EW worker {WORKER_ID}: unexpected loop error\n{traceback.format_exc()[-500:]}")
            time.sleep(EW_POLL_INTERVAL)
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        _stop_event.set()


if __name__ == "__main__":
    main()
