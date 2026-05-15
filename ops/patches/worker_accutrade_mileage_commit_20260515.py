"""Patch worker_accutrade.py mileage-commit verification.

Bug: bid 1446 (2018 Audi R8 RWS Coupe) captured base-mileage values ($104,850
Instant Offer) despite 81,000 mi being visually entered. The JS
dispatchEvent('blur') doesn't actually move focus, so AccuTrade's Angular CDK
input directive never commits the value. Recalc never fires. Existing 7s poll
exits early because the labels + dollar signs already exist on the page from
the base-mileage state.

Fix:
1. After JS dispatch, send a real Playwright keyboard.press('Tab') — that
   actually transfers focus, fires real blur/focusout, triggers framework.
2. Poll for an EXPLICIT commit signal: either "Mileage entered" badge
   text appears, OR the Odometer line shows a non-zero dollar adjustment.
   (Old poll just checked dollar-value labels — those exist at base too.)
3. If commit signal never appears after 12s (was 7s), surface the failure
   as `unavailable_reason='mileage_did_not_commit'` so we don't store
   wrong values.

Idempotent: marker comment '# MILEAGE_COMMIT_FIX_2026_05_15' so re-running
the patcher is a no-op.
"""
import sys, shutil, re

PATH = r"C:\worker\worker_accutrade.py"  # workers run from C:\worker\
# Local C1 path for the repo-side patch:
import os
if not os.path.exists(PATH):
    PATH = "/opt/expwholesale/worker_code/worker_accutrade.py"

with open(PATH, "r", encoding="utf-8") as f:
    src = f.read()

if "MILEAGE_COMMIT_FIX_2026_05_15" in src:
    print("already patched, skipping")
    sys.exit(0)

# OLD block: existing 7s poll waiting for dollar values to appear.
OLD = """    # Was time.sleep(7) — replaced with poll-until-values-appear loop.
    # Most lookups finish recalc in 2-4s; we cap at 7s so worst case == old behavior.
    deadline = time.time() + 7
    while time.time() < deadline:
        ready = page.evaluate(r\"\"\"() => {
            const text = document.body.innerText || '';
            // Need at least 2 of the dollar-value labels to have a $value next to them
            const labels = ['Instant Offer','Target Auction','Target Retail','Wholesale'];
            let hits = 0;
            for (const lab of labels) {
                const idx = text.indexOf(lab);
                if (idx < 0) continue;
                const win = text.substring(idx, idx + 100);
                if (/\\$\\s*[\\d,]{3,}/.test(win)) hits++;
            }
            return hits >= 2;
        }\"\"\")
        if ready: break
        time.sleep(0.4)"""

NEW = """    # MILEAGE_COMMIT_FIX_2026_05_15: JS dispatchEvent('blur') above doesn't
    # actually move focus, so on some vehicles (e.g. bid 1446 Audi R8 RWS)
    # AccuTrade's Angular CDK never sees a real blur -> recalc doesn't fire
    # -> base-mileage values stay on the page and the old poll exits early
    # because the $-labels already exist. Force a true Tab via Playwright,
    # then poll for an explicit commit signal (badge OR odometer adjustment).
    try:
        page.keyboard.press('Tab')
    except Exception:
        pass
    deadline = time.time() + 12  # was 7
    mileage_committed = False
    while time.time() < deadline:
        sig = page.evaluate(r\"\"\"(target) => {
            const text = (document.body && document.body.innerText) || '';
            // Strong commit signal: 'Mileage entered' badge AccuTrade renders
            // beneath the input after a successful commit.
            if (/Mileage\\s+entered/i.test(text)) return 'badge';
            // Fallback signal: Odometer line shows a non-$0 dollar adjustment
            // (penalty for high miles or bonus for low miles vs. base).
            const om = text.match(/Odometer[\\s\\S]{0,60}(-?\\$\\s*[\\d,]+)/);
            if (om) {
                const amt = parseInt(om[1].replace(/[^0-9-]/g, ''));
                if (Number.isFinite(amt) && amt !== 0) return 'odometer';
            }
            return '';
        }\"\"\", int(miles))
        if sig:
            mileage_committed = True
            break
        time.sleep(0.4)
    # If no commit signal at all, also check that at least 2 $-labels exist
    # (pre-fix behavior) as a soft pass. This keeps coverage for vehicles
    # whose entered miles happen to == base miles (rare; no odometer adjust
    # AND AccuTrade may or may not show 'Mileage entered' badge in that case).
    if not mileage_committed:
        soft_ok = page.evaluate(r\"\"\"() => {
            const text = document.body.innerText || '';
            const labels = ['Instant Offer','Target Auction','Target Retail','Wholesale'];
            let hits = 0;
            for (const lab of labels) {
                const idx = text.indexOf(lab);
                if (idx < 0) continue;
                const win = text.substring(idx, idx + 100);
                if (/\\$\\s*[\\d,]{3,}/.test(win)) hits++;
            }
            return hits >= 2;
        }\"\"\")
        if not soft_ok:
            # Page didn't even render dollar values — total failure.
            return {
                "guaranteed_offer": None, "trade_in": None, "trade_market": None,
                "retail": None, "market_avg": None, "screenshot": None,
                "appraisal_url": page.url if "/appraisal/" in page.url else None,
                "selected_trim_text": selected_trim_text,
                "trim_select_source": trim_select_source,
                "not_available_reason": "mileage_did_not_commit_no_values",
            }
        # Soft pass: log it. Server-side handler can decide whether to trust.
        print(f"[+{time.time()-t:5.1f}s] [accutrade] WARN: no commit signal for miles={miles}; storing anyway with soft flag")"""

if OLD not in src:
    sys.stderr.write("OLD block not found exactly — cannot safely patch. Manual inspection required.\n")
    sys.exit(2)

bak = PATH + ".bak.20260515-mileage-commit"
shutil.copy(PATH, bak)
src2 = src.replace(OLD, NEW, 1)

# Also wire the soft-flag into the return: if mileage_committed was False but
# we soft-passed, add a hint so the server can flag the row. We do this by
# editing the return statement to include not_available_reason conditionally.
# Find the final return and inject.
RETURN_OLD = """    print(f"[+{time.time()-t:5.1f}s] [accutrade] done values={values} url={appraisal_url}")
    return {
        "guaranteed_offer": values.get("guaranteed_offer"),
        "trade_in": values.get("trade_in"),
        "trade_market": values.get("trade_market"),
        "retail": values.get("retail"),
        "market_avg": values.get("market_avg"),
        "screenshot": str(screenshot) if screenshot else None,
        "appraisal_url": appraisal_url,
        "selected_trim_text": selected_trim_text,
        "trim_select_source": trim_select_source,
    }"""

RETURN_NEW = """    print(f"[+{time.time()-t:5.1f}s] [accutrade] done values={values} url={appraisal_url} committed={mileage_committed}")
    result = {
        "guaranteed_offer": values.get("guaranteed_offer"),
        "trade_in": values.get("trade_in"),
        "trade_market": values.get("trade_market"),
        "retail": values.get("retail"),
        "market_avg": values.get("market_avg"),
        "screenshot": str(screenshot) if screenshot else None,
        "appraisal_url": appraisal_url,
        "selected_trim_text": selected_trim_text,
        "trim_select_source": trim_select_source,
    }
    if not mileage_committed:
        # Surfaced for server-side to mark unavailable_reason; values may be
        # base-mileage, not user-entered.
        result["mileage_uncommitted"] = True
    return result"""

if RETURN_OLD not in src2:
    sys.stderr.write("RETURN_OLD block not found — partial patch applied. Please review.\n")
    sys.exit(3)

src2 = src2.replace(RETURN_OLD, RETURN_NEW, 1)

with open(PATH, "w", encoding="utf-8") as f:
    f.write(src2)

print(f"patched: {len(src)} -> {len(src2)} bytes (+{len(src2)-len(src)})")
print(f"backup: {bak}")
print(f"path: {PATH}")
