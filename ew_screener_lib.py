"""ew_screener_lib.py — deal screener for the EW bid page. (v3, 2026-07-27)

v3 CHANGE — THE BADGE WAS QUOTING THE WRONG NUMBER
--------------------------------------------------
v2 was fit on `gross_dollars`, which is only sale_price - purchase_cost. EW's actual
per-deal profit is `front_value`, which nets out recon, pack and fees. front_value runs
~55% of gross_dollars in EVERY year (2026: $1,551 vs $2,859), so the badge was overstating
what a deal is worth by roughly 80%.

v2 also blended 2019-2026. The loss rate has fallen steadily (7.29% in 2019 -> 2.36% in
2026 on front_value), so the old blend was simultaneously too pessimistic about risk and
too optimistic about dollars. v3 fits on 2025-01-01 onward: 6,291 deals.

    median front_value  $1,000   (v2 quoted $1,750 of gross_dollars)
    base loss rate       3.24%   (v2 said 3.92%)

THE "PAYING OVER THE ANCHOR" RULE WAS BACKWARDS — REMOVED
---------------------------------------------------------
v2 downgraded any BUY where the bid exceeded 1.08x MMR, because on gross_dollars that tier
looked like the worst (5.19% loss). On front_value the ordering INVERTS, and does so in both
years independently:

    bid/wholesale   loss (front)      n
    <0.90              4.65%         710   <- worst
    0.90-0.98          3.38%        1125
    0.98-1.02          3.20%         938
    1.02-1.08          2.98%        1613
    1.08-1.25          2.83%        1663   <- best
    >1.25              3.31%         242   <- back to base rate

It is mechanical, not noise: cars bought well under wholesale are cheap BECAUSE they need
work, and recon eats the front while leaving gross intact. The <0.90 ordering holds in 2025
and 2026 independently (5.03% / 4.03%). So the penalty is gone.

The 1.25 split matters: a single ">1.08" bucket averaged to 2.89% and let a bid at 125% of
MMR inherit the SAFEST label, when 1.25-1.50 actually runs 4.29% — the worst of the
over-anchor range. Split, extreme overbids now read as ordinary risk rather than best.

⚠ NOTE THE SELECTION EFFECT, and why nothing here rewards overpaying. EW paid over the anchor
on cars it had good reason to want; that is not the same as any random car being safe to
overpay for. v3 removes the false penalty but deliberately grants no bonus either — the
fitted dollar multiplier for >1.08 is a flat 1.00x. The ratio moves RISK, not dollars. The
bidder still sees "bidding N% of MMR" and applies judgement.

THRESHOLDS ARE RELATIVE NOW
---------------------------
v2 hardcoded "PASS if loss >= 0.07" / "BUY if loss <= 0.045" against a 3.92% base — i.e.
1.79x and 1.15x base. Hardcoding is what rots a model: as the base rate fell toward 3.2%,
"loss <= 0.045" became nearly always true and everything would drift to BUY. v3 stores the
MULTIPLES in the model and derives cutoffs from the fitted base, so the meaning survives.

Refit with:  sudo -u postgres venv/bin/python fit_screener_v3.py --write   (see that script)

SAFETY (LISTING_NEVER_WAITS_ON_ASSESSMENT_2026_06_18): pure arithmetic on values already
loaded for the render — no DB, no network, no I/O. Returns None on ANY failure and never
raises at import, so the worst case is the badge being absent.
"""
import json, os

_MODEL_PATH = os.environ.get("EW_SCREENER_MODEL", "/opt/expwholesale/screener_model_v3.json")
_M = None
try:
    _M = json.load(open(_MODEL_PATH))
except Exception as _e:
    print(f"[screener] model unavailable ({_e}) - badge disabled", flush=True)

def _price_band(v):
    return ("a <15k" if v < 15000 else "b 15-30k" if v < 30000 else "c 30-50k" if v < 50000
            else "d 50-100k" if v < 100000 else "e 100k+")

def _miles_tier(o):
    if not o: return "unk"
    return "1 <30k" if o < 30000 else "2 30-60k" if o < 60000 else "3 60-100k" if o < 100000 else "4 100k+"

def _ratio_tier(r):
    # MUST stay identical to ratio_tier() in fit_screener_v3.py — if these drift, the model
    # is looked up with keys it was never fitted on and every bid silently falls back to the
    # grand mean.
    # The 1.25 split exists because a single ">1.08" bucket hid real structure: 1.08-1.25 is
    # genuinely low risk, but 1.25-1.50 runs 4.29% loss, the worst of the over-anchor range.
    return ("1 <0.90" if r < .90 else "2 0.90-0.98" if r < .98 else "3 0.98-1.02" if r < 1.02
            else "4 1.02-1.08" if r < 1.08 else "5 1.08-1.25" if r < 1.25 else "6 >1.25")

def score_bid(*, mmr, bid_price, year=None, mileage=None, model_year_now=2026):
    """Typical FRONT-END gross dollars + probability this deal loses money."""
    if not _M:
        return None
    try:
        mmr = float(mmr or 0); bid_price = float(bid_price or 0)
        if mmr <= 0 or bid_price <= 0:
            return None
        r = bid_price / mmr
        odo = int(mileage) if mileage else None
        pb, mt, rt = _price_band(bid_price), _miles_tier(odo), _ratio_tier(r)

        # typical front dollars: band base, adjusted for how well we are buying
        band = _M["band_gross"].get(pb) or _M["grand_gross"]
        adj = _M["ratio_gross_mult"].get(rt, 1.0)
        exp_dollars = int(band * adj)

        # loss probability: worst (most pessimistic) of the mileage and ratio signals,
        # because either one alone is enough to sink a deal
        loss = max(_M["miles_loss"].get(mt, _M["grand_loss"]),
                   _M["ratio_loss"].get(rt, _M["grand_loss"]))

        # Cutoffs derived from the FITTED base rate, not hardcoded — see docstring.
        good    = _M["grand_gross"]
        base    = _M["grand_loss"]
        pass_at = base * _M.get("pass_loss_mult", 1.79)
        buy_at  = base * _M.get("buy_loss_mult", 1.15)
        thin_at = good * _M.get("thin_frac", 0.60)

        if loss >= pass_at or exp_dollars < thin_at:
            rec, why = "PASS", ("high loss risk" if loss >= pass_at else "thin dollars")
        elif exp_dollars >= good and loss <= buy_at:
            rec, why = "BUY", "strong front, low risk"
        else:
            rec, why = "OK", "acceptable"

        # Only surface loss risk when it actually MEANS something. Most bids land within a
        # whisker of the base rate, so printing it on every card is noise that reads like a
        # warning while saying "normal". Flag only genuinely elevated cases — which on this
        # model means high mileage (6.4% at 100k+) rather than the bid/anchor ratio.
        risk_flag = None
        if loss >= pass_at:
            risk_flag = "HIGH RISK - %.0f%% of these lose money" % (100 * loss)
        elif loss >= base * 1.28:
            risk_flag = "elevated risk %.1f%%" % (100 * loss)

        return {"recommendation": rec, "why": why, "risk_flag": risk_flag,
                "typical_front_dollars": exp_dollars,
                "typical_front_pct": round(100.0 * exp_dollars / bid_price, 1),
                "loss_risk_pct": round(100 * loss, 1),
                "bid_over_mmr": round(r, 3),
                "color": {"BUY": "#1a7f37", "OK": "#9a6700", "PASS": "#b42318"}[rec],
                "note": "bidding %.0f%% of MMR" % (r * 100)}
    except Exception as e:
        print(f"[screener] score failed: {e}", flush=True)
        return None
