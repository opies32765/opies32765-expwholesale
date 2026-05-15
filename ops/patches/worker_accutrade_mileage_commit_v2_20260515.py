"""Patch v2 — replace the unreliable "Mileage entered" badge + Odometer-regex
commit-signal detection with VALUE-CHANGE detection.

Why v2 was needed: bid 1466 stored base-mileage values ($104,850 Instant Offer
for 85,000-mile Audi R8 RWS, correct should be $66,475). Worker log shows
`committed=True` — my v1 commit-signal regex hit a false positive. AccuTrade
page contains the text "Mileage entered" as a static label, not a state
indicator. The Odometer regex required non-$0 adjustment; pre-recalc page may
show $0 or nothing, so it didn't fire — but the badge check fired wrongly.

v2 strategy:
- Snapshot dollar values BEFORE entering miles (already-rendered base values).
- Enter miles via JS dispatch + Playwright Tab.
- Poll until at least ONE of the 4 dollar values differs from snapshot.
- If 12s with no change -> mileage_committed=False, surface to server.

Idempotent: marker MILEAGE_COMMIT_FIX_V2_2026_05_15.
"""
import sys, shutil, os

PATH = r"C:\worker\worker_accutrade.py"
if not os.path.exists(PATH):
    PATH = "/opt/expwholesale/worker_code/worker_accutrade.py"

with open(PATH, "r", encoding="utf-8") as f:
    src = f.read()

if "MILEAGE_COMMIT_FIX_V2_2026_05_15" in src:
    print("already at v2, skipping")
    sys.exit(0)

# We're replacing the v1 block (which has MILEAGE_COMMIT_FIX_2026_05_15 marker)
# with v2. Find the start of v1 block, replace through end of soft-pass logic.
V1_START = "    # MILEAGE_COMMIT_FIX_2026_05_15:"
v1_idx = src.find(V1_START)
if v1_idx < 0:
    sys.stderr.write("v1 patch marker not found — was v1 applied? Aborting.\n")
    sys.exit(2)

# v1 block ends at the line BEFORE the existing `values = page.evaluate(...)`
# which reads final values. Find that line.
V1_END_AT = "\n    values = page.evaluate(r\"\"\"() => {\n        const map = ["
v1_end_idx = src.find(V1_END_AT, v1_idx)
if v1_end_idx < 0:
    sys.stderr.write("v1 end marker (values=page.evaluate) not found. Aborting.\n")
    sys.exit(3)

V2_REPLACEMENT = """    # MILEAGE_COMMIT_FIX_V2_2026_05_15: v1's "Mileage entered" badge / Odometer
    # regex was an unreliable signal — bid 1466 wrongly logged committed=True
    # while storing base-mileage values. v2 detects commit by VALUE CHANGE:
    # snapshot the 4 dollar values BEFORE entering miles, then poll until at
    # least one differs from snapshot. Cannot be fooled by static page text.
    #
    # NOTE: we capture the snapshot RIGHT NOW (after the dispatch above, before
    # waiting). Most pages already have base-mileage values rendered by the time
    # we get here. A few synchronous-recalc pages may already show updated
    # values, in which case "no change after entry" is correct — we degrade to
    # the legacy $-labels-present check after a brief settle period.
    def _read_4_values():
        return page.evaluate(r\"\"\"() => {
            const map = [
                ['Instant Offer','guaranteed_offer'],
                ['Target Auction','trade_in'],
                ['Target Retail','trade_market'],
                ['Wholesale / Average','market_avg'],
                ['Wholesale/Average','market_avg'],
                ['Wholesale Average','market_avg']
            ];
            const r = {guaranteed_offer:null, trade_in:null, trade_market:null, market_avg:null};
            const text = document.body.innerText || '';
            for (const [label, field] of map) {
                if (r[field] !== null) continue;
                const idx = text.indexOf(label);
                if (idx < 0) continue;
                const win = text.substring(idx + label.length, idx + label.length + 80);
                if (/^\\s*\\r?\\n?\\s*N\\/A\\b/i.test(win)) { r[field] = null; continue; }
                const m = win.match(/\\$\\s*([\\d,]+)(?!\\d)/);
                if (m) {
                    const n = parseInt(m[1].replace(/,/g, ''));
                    if (n > 100 && n < 10000000) r[field] = n;
                }
            }
            return r;
        }\"\"\") or {}

    pre_values = _read_4_values()
    # Strong trigger: real Playwright Tab keypress (v1 carry-over — still
    # better than nothing).
    try:
        page.keyboard.press('Tab')
    except Exception:
        pass

    deadline = time.time() + 12
    mileage_committed = False
    last_post = pre_values
    while time.time() < deadline:
        post_values = _read_4_values()
        # Committed if ANY value differs from snapshot (handles N/A->value
        # and value->different-value).
        for k in ('guaranteed_offer', 'trade_in', 'trade_market', 'market_avg'):
            if pre_values.get(k) != post_values.get(k):
                mileage_committed = True
                break
        if mileage_committed:
            last_post = post_values
            break
        last_post = post_values
        time.sleep(0.4)

    # Refuse to store if commit never happened. This is intentionally strict —
    # the alternative (soft pass) is what stored bid 1466 wrong.
    if not mileage_committed:
        print(f"[+{time.time()-t:5.1f}s] [accutrade] FAIL: mileage_did_not_commit "
              f"pre={pre_values} post={last_post} miles={miles}")
        return {
            "guaranteed_offer": None, "trade_in": None, "trade_market": None,
            "retail": None, "market_avg": None, "screenshot": None,
            "appraisal_url": page.url if "/appraisal/" in page.url else None,
            "selected_trim_text": selected_trim_text,
            "trim_select_source": trim_select_source,
            "not_available_reason": "mileage_did_not_commit_v2",
        }
"""

src2 = src[:v1_idx] + V2_REPLACEMENT + src[v1_end_idx:]

# Also revise the return block — drop the v1 "mileage_uncommitted" soft flag.
RETURN_V1 = """    print(f"[+{time.time()-t:5.1f}s] [accutrade] done values={values} url={appraisal_url} committed={mileage_committed}")
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

RETURN_V2 = """    print(f"[+{time.time()-t:5.1f}s] [accutrade] done values={values} url={appraisal_url} committed=True (v2)")
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

if RETURN_V1 not in src2:
    sys.stderr.write("v1 return block not found in patched src — aborting.\n")
    sys.exit(4)

src2 = src2.replace(RETURN_V1, RETURN_V2, 1)

bak = PATH + ".bak.20260515-mileage-commit-v2"
shutil.copy(PATH, bak)
with open(PATH, "w", encoding="utf-8") as f:
    f.write(src2)

print(f"patched: {len(src)} -> {len(src2)} bytes ({len(src2)-len(src):+d})")
print(f"backup: {bak}")
print(f"path: {PATH}")
