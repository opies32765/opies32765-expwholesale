# Worker fix handoff — AccuTrade appraisal not saved on exotics (bid 3494, Lamborghini Temerario)

**File:** `worker_accutrade.py` (on the 6 worker VMs; C1 reference copy: `/opt/expwholesale/worker_code/worker_accutrade.py`)

**Symptom:** intermittent — same VIN, two runs:
- Run 1: odometer committed → page showed Target Retail → C1 vision OCR'd `trade_market=$466,948`, but `appraisal_url=None` (never saved).
- Run 2 (reprocess): odometer did NOT commit → worker bailed `not_available='accutrade_manual_quote_only'` → no value at all.

Operator requirement (verbatim intent): **the AccuTrade appraisal must save for EVERY car, regardless of values or vehicle class. "Exotic / manual-quote" is NOT an acceptable reason to skip the save — a car is a car.** Three compounding worker bugs below; #1 and #2 are the primary causes, #3 is the secondary (it bit run 1).

This is NOT an exotic-data limitation. 3494 run 1 committed and showed a real Target Retail ($466,948) — the data is reachable. The worker just intermittently fails to *confirm* the odometer commit and then bails with an excuse reason. Fix the worker so it commits + saves on every VIN.

---

## BUG 1 — odometer-commit CONFIRMATION is unreliable (any VIN)
`mileage_committed` flakes False when the value-change detector can't see a delta (e.g. the page already had an appraisal at the same miles → pre==post → false-FAIL). There's a `commit-via-degrade` path ("typed miles in input + page has values") but it doesn't fire reliably. **Fix:** confirm the commit deterministically — if the miles value is present in the odometer input AND the page renders any value tile, treat it as committed (don't depend on a pre/post delta). Don't gate the whole appraisal on a flaky delta check. (Vision-driven odometer entry is the durable long-term fix already in the followups.)

## BUG 2 — the worker GIVES UP instead of saving (the "manual_quote" excuse) — line ~940-967
```python
if not mileage_committed:
    # ... detects "contact inventory consultant" banner -> returns not_available='accutrade_manual_quote_only'
```
When commit can't be confirmed, the worker returns NA **without checking whether the page already shows values** — throwing away a savable appraisal (run 1 proves the values are on screen). **Fix:** never return `manual_quote_only` while a price tile (Target Retail / Manheim / Instant Offer / etc.) has a value. Always: scrape the tiles + screenshot + **save the appraisal + capture the URL**, and return those values. Only return `manual_quote_only` when the no-data banner is present AND every price tile is genuinely empty (true Cox content gap) — and even then, save whatever appraisal record AccuTrade allows.

## BUG 3 — appraisal URL capture is a fragile one-shot — line ~1056

## Root cause (worker side, line ~1056)
```python
appraisal_url = page.url if "/appraisal/" in page.url else None
```
This is a **one-shot read of `page.url` at a single instant**. If the appraisal save/navigation to `/appraisal/<id>` hasn't completed (or the leg hit its time budget) the moment this line runs, `page.url` isn't on `/appraisal/` yet → `appraisal_url=None` is POSTed. The C1 submit handler upserts `appraisal_url=COALESCE(EXCLUDED.appraisal_url, …)`, so a None from the worker = permanently blank. There is **no AccuTrade URL re-capture queue** (unlike vAuto's Beelink), so the worker's POST is the only chance — if it sends None, the save is lost until a full reprocess.

## Fix — robust capture (handles the timing race + state)
Add this helper near the top of `worker_accutrade.py`:

```python
def _capture_appraisal_url(page, settle_ms=2500):
    """ACCU_URL_ROBUST_2026_06_17: the appraisal permalink used to be captured as a
    single `page.url if "/appraisal/" in page.url else None`, which returned None
    whenever the save/nav to /appraisal/<id> hadn't completed at that instant
    (bid 3494: values+screenshot fine, appraisal_url=None -> never saved). Capture
    it regardless of timing/state."""
    import re as _re
    if "/appraisal/" in (page.url or ""):
        return page.url
    # in-flight save/navigation -> settle and re-check
    try:
        page.wait_for_timeout(settle_ms)
    except Exception:
        pass
    if "/appraisal/" in (page.url or ""):
        return page.url
    # a saved-appraisal anchor rendered on the page
    try:
        href = page.evaluate(
            r"""() => { const a = document.querySelector('a[href*="/appraisal/"]');
                       return a ? a.href : null; }""")
        if href and "/appraisal/" in href:
            return href
    except Exception:
        pass
    # reconstruct from an appraisal id in the URL query or DOM
    try:
        base = (page.url or "").split("/appraisal", 1)[0].rstrip("/")
        m = _re.search(r"[?&](?:appraisalId|id)=([A-Za-z0-9_\-=%]+)", page.url or "", _re.I)
        aid = m.group(1) if m else page.evaluate(
            r"""() => { const el = document.querySelector('[data-appraisal-id],[data-appraisalid],#appraisalId');
                       return el ? (el.getAttribute('data-appraisal-id') || el.getAttribute('data-appraisalid') || el.value) : null; }""")
        if base and aid:
            return base + "/appraisal/" + str(aid)
    except Exception:
        pass
    return None
```

Then replace **line ~1056** (and the same pattern in the fail-path blocks at ~963 and ~1009 if you want them covered too):

```python
# was: appraisal_url = page.url if "/appraisal/" in page.url else None
appraisal_url = _capture_appraisal_url(page)
```

## Notes
- **Steps 1–2 (settle + re-check `page.url`) are the high-value fix** — they cover the timing race that almost certainly caused 3494 (3492 saved fine, so the flow normally *does* reach `/appraisal/`). Ship those even if you skip the DOM selectors.
- **Steps 3–4 DOM/id selectors are best-effort guesses** — adjust `a[href*="/appraisal/"]` and the `data-appraisal-id` selectors to AccuTrade's actual markup if needed.
- If AccuTrade requires an explicit **Save** click to mint the permalink and the worker doesn't do it, add the Save click + `await` before capture; the robust capture is then the safety net.
- Deploy to all 6 worker VMs (QGA `_qga_deploy_both.py` / anchor-checked + py_compile), pilot one worker first.
- Recovery for already-affected bids: clear the partial `accutrade_lookups` row + set `bids.accutrade_retry_at=NOW()` (the `/api/accutrade/pending` re-run path) — done for 3494.
