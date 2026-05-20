"""End-to-end bid processor: vAuto + AccuTrade + iPacket all PARALLEL.

Three Playwright instances run concurrently in separate threads, each with
its own profile dir. No SSO collisions because all three vendors use
different auth systems:
  - vAuto    -> Cox SSO    (OscarPas)
  - AccuTrade-> Auth0       (opies32765@gmail.com / Sedecremlun35$)
  - iPacket  -> iPacket SSO (opies32765@gmail.com / Sedecremlun34$)

Wall-clock per bid drops from ~92s sequential to ~40s = max(vauto, accutrade, ipacket).
"""
import sys, time, json, threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import worker_vauto
import worker_accutrade
import worker_ipacket

from playwright.sync_api import sync_playwright

VAUTO_PROFILE_DIR     = Path(r"C:\worker\vauto_profile")
ACCUTRADE_PROFILE_DIR = Path(r"C:\worker\accutrade_profile")
IPACKET_PROFILE_DIR   = Path(r"C:\worker\ipacket_profile")
for d in (VAUTO_PROFILE_DIR, ACCUTRADE_PROFILE_DIR, IPACKET_PROFILE_DIR):
    d.mkdir(parents=True, exist_ok=True)

# Test bid
TEST_VIN = "WUASUAFG3CN000625"
TEST_MILES = 30000
TEST_TRIM = None

# Per-vendor wall-clock cap. Single hung lookup can't block the bid forever.
LOOKUP_TIMEOUT_SEC = 90


def _run_lookup_in_own_browser(profile_dir, runner):
    """Spin up an isolated Playwright/Chromium just for this one lookup,
    invoke runner(page, ctx) and return its result. Always closes."""
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(  # STEALTH-2026-05-10
            user_data_dir=str(profile_dir), headless=False,
            viewport={"width": 1500, "height": 1000},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            locale="en-US",
            timezone_id="America/New_York",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        try: ctx.add_init_script("""() => {
  // Hide automation
  Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
  // Languages
  Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
  // Plugins (length > 0 — many bot detectors test this)
  Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5].map(() => ({}))
  });
  // Chrome runtime
  window.chrome = window.chrome || { runtime: {} };
  // Permissions API quirk
  const _q = (window.navigator.permissions && window.navigator.permissions.query) || null;
  if (_q) {
    window.navigator.permissions.query = (parameters) => (
      parameters && parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : _q(parameters)
    );
  }
}""")
        except Exception: pass
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            return runner(page, ctx)
        finally:
            try: ctx.close()
            except Exception: pass


def process_bid(vin, miles, trim=None, on_phase=None, bid_id=None):
    """Run all three lookups in parallel.

    on_phase: optional callback (phase: str, state: str) for watchdog markers.
    """
    t = time.time()
    print(f"=== process_bid: {vin} miles={miles:,} ===")
    result = {"vin": vin, "miles": miles, "vauto": None, "accutrade": None, "ipacket": None}

    _phase_lock = threading.Lock()
    def _phase(phase, state):
        if on_phase is None: return
        with _phase_lock:
            try: on_phase(phase, state)
            except Exception: pass

    def _wrap(name, profile, runner):
        _phase(name, "started")
        try:
            result[name] = _run_lookup_in_own_browser(profile, runner)
        except Exception as e:
            import traceback; traceback.print_exc()
            result[name] = {"error": str(e)}
        _phase(name, "done")

    threads = [
        threading.Thread(
            target=_wrap, name="vauto", daemon=True,
            args=("vauto", VAUTO_PROFILE_DIR,
                  lambda page, ctx: worker_vauto.lookup(page, ctx, vin, miles, t)),
        ),
        threading.Thread(
            target=_wrap, name="accutrade", daemon=True,
            args=("accutrade", ACCUTRADE_PROFILE_DIR,
                  lambda page, ctx: worker_accutrade.lookup(page, ctx, vin, miles, t, trim=trim, bid_id=bid_id)),
        ),
        threading.Thread(
            target=_wrap, name="ipacket", daemon=True,
            args=("ipacket", IPACKET_PROFILE_DIR,
                  lambda page, ctx: worker_ipacket.lookup(page, ctx, vin, t, bid_id=bid_id)),
        ),
    ]
    for th in threads: th.start()

    deadline = time.time() + LOOKUP_TIMEOUT_SEC
    for th in threads:
        remaining = max(1.0, deadline - time.time())
        th.join(timeout=remaining)
        if th.is_alive():
            name = th.name
            print(f"[!] {name} thread still alive past {LOOKUP_TIMEOUT_SEC}s — abandoning")
            if result.get(name) is None:
                result[name] = {"error": f"{name}_timeout"}

    print(f"\n=== TOTAL ELAPSED: {time.time()-t:.1f}s ===")
    return result


if __name__ == "__main__":
    res = process_bid(TEST_VIN, TEST_MILES, TEST_TRIM)
    print("\n=== FULL RESULT ===")
    print(json.dumps(res, indent=2, default=str))
