"""ew_screener_lib.py — deal screener for the EW bid page. (2026-07-26, v2 objective)

OBJECTIVE CORRECTION (operator, 2026-07-26): capital is NOT a constraint for EW. The first
build ranked by gross PERCENTAGE, which is a capital-efficiency measure — it flagged an
$80,000 BMW earning $2,368 as PASS in favour of a $22,000 Escalade earning $1,936. That is
correct only if money is scarce. It is not. Ranking by percentage actively flagged EW's best
deals as bad ones.

Measured on 28,816 LSL deals, by price band:
    <$15k    loss 4.5%   mean gross $1,286      <- worst on BOTH axes
    $15-30k  loss 4.4%   mean gross $1,451
    $30-50k  loss 4.1%   mean gross $1,850
    $50-100k loss 3.7%   mean gross $2,579
    $100k+   loss 1.9%   mean gross $5,222      <- best on BOTH axes
Expensive cars earn ~4x the dollars AND lose money less than half as often.

So this version ranks by EXPECTED GROSS DOLLARS and flags genuine LOSS RISK. PASS now means
"this is likely to lose money or return very little", not "the percentage is thin".

What actually predicts a loser (28,816 deals, 3.9% lose money overall):
    mileage 100k+   -> 8.5% loss, only $1,501 mean gross   <- the real red flag
    mileage 60-100k -> 5.4% loss
    buy > 1.08x wholesale -> 5.2% loss, $1,913 mean gross
    everything else -> ~3-4% loss

SAFETY (LISTING_NEVER_WAITS_ON_ASSESSMENT_2026_06_18): pure arithmetic on values already
loaded for the render — no DB, no network, no I/O. Returns None on ANY failure and never
raises at import, so the worst case is the badge being absent.
"""
import json, os

_MODEL_PATH = os.environ.get("EW_SCREENER_MODEL", "/opt/expwholesale/screener_model_v2.json")
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
    return ("1 <0.90" if r < .90 else "2 0.90-0.98" if r < .98 else "3 0.98-1.02" if r < 1.02
            else "4 1.02-1.08" if r < 1.08 else "5 >1.08")

def score_bid(*, mmr, bid_price, year=None, mileage=None, model_year_now=2026):
    """Expected gross DOLLARS + probability this deal loses money."""
    if not _M:
        return None
    try:
        mmr = float(mmr or 0); bid_price = float(bid_price or 0)
        if mmr <= 0 or bid_price <= 0:
            return None
        r = bid_price / mmr
        odo = int(mileage) if mileage else None
        pb, mt, rt = _price_band(bid_price), _miles_tier(odo), _ratio_tier(r)

        # expected gross dollars: band base, adjusted for how well we are buying
        band = _M["band_gross"].get(pb) or _M["grand_gross"]
        adj = _M["ratio_gross_mult"].get(rt, 1.0)
        exp_dollars = int(band * adj)

        # loss probability: worst (most pessimistic) of the mileage and ratio signals,
        # because either one alone is enough to sink a deal
        loss = max(_M["miles_loss"].get(mt, _M["grand_loss"]),
                   _M["ratio_loss"].get(rt, _M["grand_loss"]))

        # thresholds in DOLLARS, not percent. grand_gross is the all-deals median.
        good = _M["grand_gross"]
        if loss >= 0.07 or exp_dollars < good * 0.6:
            rec, why = "PASS", ("high loss risk" if loss >= 0.07 else "thin dollars")
        elif exp_dollars >= good and loss <= 0.045:
            rec, why = "BUY", "strong dollars, low risk"
        else:
            rec, why = "OK", "acceptable"
        if r > 1.08 and rec == "BUY":
            rec, why = "OK", "paying over the anchor"

        # Only surface loss risk when it actually MEANS something. Across 2,208 live bids 80%
        # land at 3.4-4.2% against a 3.9% base rate, so printing it on every card is noise that
        # reads like a warning while saying "normal". Elevated cases are high mileage (8.5%) and
        # bidding over the anchor (5.2%) -- those are worth a flag.
        risk_flag = None
        if loss >= 0.07:
            risk_flag = "HIGH RISK - %.0f%% of these lose money" % (100 * loss)
        elif loss >= 0.05:
            risk_flag = "elevated risk %.1f%%" % (100 * loss)

        return {"recommendation": rec, "why": why, "risk_flag": risk_flag,
                "expected_gross_dollars": exp_dollars,
                "expected_gross_pct": round(100.0 * exp_dollars / bid_price, 1),
                "loss_risk_pct": round(100 * loss, 1),
                "walk_away": int(mmr * 1.08),
                "bid_over_mmr": round(r, 3),
                "color": {"BUY": "#1a7f37", "OK": "#9a6700", "PASS": "#b42318"}[rec],
                "note": "bidding %.0f%% of MMR" % (r * 100)}
    except Exception as e:
        print(f"[screener] score failed: {e}", flush=True)
        return None
