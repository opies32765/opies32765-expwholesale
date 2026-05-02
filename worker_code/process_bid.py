"""End-to-end bid processor: vAuto -> AccuTrade -> iPacket on a single VIN.

ONE Playwright context, ONE persistent profile shared across all 3 sites.
This is the production worker function shape minus the EW-server polling/upload.
"""
import sys, time, json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import worker_vauto
import worker_accutrade
import worker_ipacket

from playwright.sync_api import sync_playwright

PROFILE_DIR = Path(r"C:\worker\vauto_profile")

# Test bid — Audi R8 (bid #602)
TEST_VIN = "WUASUAFG3CN000625"
TEST_MILES = 30000
TEST_TRIM = None


def process_bid(vin, miles, trim=None, on_phase=None):
    """Run vAuto -> AccuTrade -> iPacket on a single VIN.

    on_phase: optional callback (phase: str, state: str) called at the
    boundaries of each lookup so an outer worker can drive watchdog timers.
    Phases: 'vauto', 'accutrade', 'ipacket'. States: 'started', 'done'.
    """
    t = time.time()
    print(f"=== process_bid: {vin} miles={miles:,} ===")
    result = {"vin": vin, "miles": miles, "vauto": None, "accutrade": None, "ipacket": None}

    def _phase(phase, state):
        if on_phase is None:
            return
        try:
            on_phase(phase, state)
        except Exception:
            pass

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR), headless=False,
            viewport={"width": 1500, "height": 1000},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # vAuto
        _phase("vauto", "started")
        try:
            result["vauto"] = worker_vauto.lookup(page, ctx, vin, miles, t)
        except Exception as e:
            import traceback; traceback.print_exc()
            result["vauto"] = {"error": str(e)}
        _phase("vauto", "done")

        # Switch the page reference to whatever vauto left us on
        page = next((pg for pg in ctx.pages if not pg.is_closed()), page)

        # AccuTrade
        _phase("accutrade", "started")
        try:
            result["accutrade"] = worker_accutrade.lookup(page, ctx, vin, miles, t, trim=trim)
        except Exception as e:
            import traceback; traceback.print_exc()
            result["accutrade"] = {"error": str(e)}
        _phase("accutrade", "done")

        page = next((pg for pg in ctx.pages if not pg.is_closed()), page)

        # iPacket
        _phase("ipacket", "started")
        try:
            result["ipacket"] = worker_ipacket.lookup(page, ctx, vin, t)
        except Exception as e:
            import traceback; traceback.print_exc()
            result["ipacket"] = {"error": str(e)}
        _phase("ipacket", "done")

        ctx.close()

    print(f"\n=== TOTAL ELAPSED: {time.time()-t:.1f}s ===")
    return result


if __name__ == "__main__":
    res = process_bid(TEST_VIN, TEST_MILES, TEST_TRIM)
    print("\n=== FULL RESULT ===")
    print(json.dumps(res, indent=2, default=str))
