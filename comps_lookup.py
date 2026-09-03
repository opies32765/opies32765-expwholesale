"""comps_lookup.py - "the closest car in miles that actually sold."

Runs ON C1. This is the function the bid card and build_prompt both call.

Ranking is deliberately simple and explainable, because that is what survived
the 2026-08-29 holdout: a hard trim/style filter made accuracy WORSE (21.3% vs
19.4%) by starving the comp set, while loosening the model year was worth 10x
more than any string matching (exact-year 18.0% -> +/-2yr 47.7% coverage).

So: bound the year, match the model, then sort by miles. Never filter to empty
- always return the nearest and SHOW the gap, so a human can discount it.
"""
import os, sys
import psycopg2, psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from edge_canon import canon_make, canon_model, models_match, trims_match

YEAR_BAND = 2

DSN = os.environ.get("DATABASE_URL")
if not DSN:
    for ln in open("/etc/default/expwholesale-mcp"):
        if ln.strip().startswith("DATABASE_URL="):
            DSN = ln.strip().split("=", 1)[1].strip().strip('"').strip("'")


def _conn():
    return psycopg2.connect(DSN, connect_timeout=10)


# TRIM_SCHEMA_LIVE_2026_09_02 — trim_schema replaces edge_canon.trims_match in
# the card path. Graded on the independent 245-pair corpus: 0 false matches
# (0/141), 0 of 333,274 row-weighted exposures, missed 42.3% (exact-only was
# 91.2%). Vocabulary is loaded ONCE and cached; if it cannot load the card
# abstains rather than falling back to a looser rule.
_VOCAB = None
_VOCAB_FAILED = False


def _vocab():
    global _VOCAB, _VOCAB_FAILED
    if _VOCAB is None and not _VOCAB_FAILED:
        try:
            import trim_schema as _ts
            _VOCAB = _ts.load()
        except Exception as e:
            _VOCAB_FAILED = True
            print('[auction-comps] trim_schema load FAILED: %s' % e, flush=True)
    return _VOCAB


def closest_comps(year, make, model, miles, *, limit=5, outcome='sold',
                  year_band=YEAR_BAND, conn=None):
    """Nearest sold comps by odometer. Returns rows with the gaps precomputed.

    outcome='sold'    -> what like cars actually brought (no VIN available)
    outcome='no_sale' -> like cars still AVAILABLE (these DO carry a full VIN,
                         so they can be enriched A-to-Z)
    """
    if not year or not make:
        return []
    cm, cmod = canon_make(make), canon_model(model)
    own = conn is None
    c = conn or _conn()
    try:
        cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT auction_slug, sale_date, stock_no, vin, year, make, model,
                   style, color, odometer, grade, has_cr, lights, price,
                   canon_model, picture_count
              FROM auction_comps
             WHERE canon_make = %s
               AND year BETWEEN %s AND %s
               AND outcome = %s
               AND (%s = 'sold') = (price IS NOT NULL)
               AND odometer IS NOT NULL
          ORDER BY sale_date DESC
             LIMIT 4000
        """, (cm, year - year_band, year + year_band, outcome, outcome))
        cands = [dict(r) for r in cur.fetchall()]
    finally:
        if own:
            c.close()

    out = []
    for r in cands:
        if not models_match(cmod, r["canon_model"] or ""):
            continue
        r["d_year"] = r["year"] - year
        r["d_miles"] = (r["odometer"] - miles) if miles else None
        r["d_grade"] = None
        out.append(r)

    # closest in miles is the operator's stated ranking; year gap breaks ties
    out.sort(key=lambda r: (abs(r["d_miles"]) if r["d_miles"] is not None else 10**9,
                            abs(r["d_year"])))
    return out[:limit]


def render(bid_year, bid_make, bid_model, bid_miles, rows):
    """One-line-per-comp text form - what the bid card will show."""
    if not rows:
        return "   (no like car found)"
    lines = []
    for r in rows:
        dy = f"{r['d_year']:+d}yr" if r["d_year"] else "same yr"
        dm = f"{r['d_miles']:+,} mi" if r["d_miles"] is not None else "?"
        g = f"gr {r['grade']}" if r["grade"] is not None else "no CR"
        price = f"${r['price']:,}" if r["price"] else "unsold"
        lines.append(
            f"   {r['year']} {r['make']} {r['model']} {r['style'] or ''}".rstrip()
            + f" · {r['odometer']:,} mi · {g}"
            + f" · {r['auction_slug']} {r['sale_date']}"
            + f"  ->  {price}   ({dy}, {dm})")
    return "\n".join(lines)


if __name__ == "__main__":
    # Demo against real recent EW bids that were assessed AND bought.
    conn = _conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT b.id, COALESCE(b.canon_year,b.year) yr, COALESCE(b.canon_make,b.make) mk,
               COALESCE(b.canon_model,b.model) md, b.mileage,
               a.actual_purchase_cost, a.ai_recommendation
          FROM ai_accuracy a JOIN bids b ON b.id = a.bid_id
         WHERE a.actual_purchase_cost > 0 AND b.mileage > 0
           AND b.created_at > now() - interval '30 days'
      ORDER BY b.created_at DESC LIMIT 40
    """)
    bids = cur.fetchall()
    hits = 0
    for b in bids:
        rows = closest_comps(b["yr"], b["mk"], b["md"], b["mileage"], limit=3, conn=conn)
        if not rows:
            continue
        hits += 1
        print(f"\nBID {b['id']}  {b['yr']} {b['mk']} {b['md']} · {b['mileage']:,} mi"
              f"  | EW paid ${int(b['actual_purchase_cost']):,}"
              f" | model said ${int(b['ai_recommendation']):,}")
        print(render(b["yr"], b["mk"], b["md"], b["mileage"], rows))
        if hits >= 8:
            break
    print(f"\n({hits} of {len(bids)} recent bought bids had a like car)")
    conn.close()


# ── Bid-page entry point ───────────────────────────────────────────────
# AUCTION_COMPS_CARD_2026_08_29
def for_bid(bid, *, n_sold=4, n_avail=1, conn=None):
    """Everything the Auction Activity card needs for one bid.

    NEVER raises and NEVER blocks: any failure returns an empty dict so the
    listing renders regardless. Same rule as every other source card --
    LISTING_NEVER_WAITS_ON_ASSESSMENT_2026_06_18.
    """
    empty = {'sold': [], 'avail': [], 'ns_rate': None, 'ok': False}
    try:
        # canon_* can carry a HALLUCINATED marque: bid 6678, VIN 1GYS9HK93TR430320
        # (a 2026 Cadillac Escalade) has canon_make=PORSCHE canon_model=911 at 0.95
        # confidence from claude_sonnet_4_6, and wmi_guard never caught it because
        # WMI_MAKE has no GM entries. Preferring canon put $150k Porsche comps on an
        # Escalade. The DISPLAY columns are what the page header shows, so trust those,
        # and if the two disagree on MAKE we cannot identify the car at all -- show
        # NOTHING rather than comps for the wrong vehicle.
        # See memory project_ew_wmi_make_guard_20260616.
        disp_mk, cn_mk = (bid.get('make') or '').strip(), (bid.get('canon_make') or '').strip()
        if disp_mk and cn_mk and canon_make(disp_mk) != canon_make(cn_mk):
            print('[auction-comps] bid=%s SUPPRESSED: make conflict display=%s canon=%s'
                  % (bid.get('id'), disp_mk, cn_mk), flush=True)
            return empty
        yr = bid.get('year') or bid.get('canon_year')
        mk = disp_mk or cn_mk
        md = (bid.get('model') or bid.get('canon_model') or '').strip()
        miles = bid.get('mileage')
        if not (yr and mk and md):
            return empty
        own = conn is None
        c = conn or _conn()
        try:
            # OPERATOR DIRECTIVE 2026-09-02: a "like vehicle" means the SAME
            # MODEL YEAR. Not +/-1, not +/-2. year_band=0, no exceptions.
            # OPERATOR DIRECTIVE 2026-09-02: "a big horn is not a sport and a
            # sport is not a laramie." Trim is a price tier, so a like car must
            # match TRIM as well as year and model. Pull a wide candidate set
            # first, then filter -- filtering after a small limit would return
            # nothing whenever the nearest few by mileage are the wrong trim.
            # BUG FOUND 2026-09-02 on bid 6683: the trim filter was only applied
            # WHEN a trim was known. Before enrichment bids.trim is empty, so the
            # card rendered every same-year comp UNFILTERED -- SE and HSE Dynamic
            # shown as like cars for an HSE Silver Edition -- and then vanished the
            # moment Carfax wrote the real trim. The operator watched it happen.
            # Unknown trim means we CANNOT verify a like car, so abstain. The card
            # now appears when it can be trusted instead of appearing wrong first.
            bid_trim = bid.get('trim') or bid.get('canon_trim') or ''
            if not bid_trim:
                return empty
            v = _vocab()
            if v is None:
                return empty
            import trim_schema as _ts
            wide = closest_comps(yr, mk, md, miles, limit=200,
                                 outcome='sold', year_band=0, conn=c)
            keep = []
            for r in wide:
                matched, _b, _c2 = _ts.match_bid_to_comp(v, bid, r)
                if matched:
                    keep.append(r)
            sold = keep[:n_sold]
            # OPERATOR DIRECTIVE 2026-09-03: "ranked by lowest miles period the
            # end".  SELECTION is unchanged -- still the n_sold nearest his car's
            # odometer, per his 08-30 "surface the closest vehicle in miles".
            # Only the DISPLAY ORDER changes.  Ranking by distance-from-subject
            # read as unsorted: on bid 6690 a 22,110 mi car sat 4th because it was
            # 23,267 from a 45,377 mi subject -- and 45,377 appeared nowhere on the
            # card, so the order could not be reasoned about.  Lowest odo first is
            # self-evident without any reference number.
            sold.sort(key=lambda r: r["odometer"])
            # OPERATOR DIRECTIVE 2026-09-02: the no-sale CAR is not displayed.
            # It can never carry a price -- EDGE does not publish the high bid on
            # a no-sale (verified on both the no-sale list page and the vehicle
            # detail page: only "Proxy Bid / Bid now" and a countdown, never an
            # amount). MMR is already surfaced by the vAuto card, so repeating it
            # here adds nothing. A car with no number is noise. The no-sale COUNT
            # still feeds ns_rate, which is a real signal.
            avail = []
            # no-sale rate for this make = how sticky the segment is.
            # High rate -> the sold cars are the survivors; bid the low end.
            cur = c.cursor()
            cur.execute("""
                SELECT count(*) FILTER (WHERE outcome='no_sale')::float
                       / NULLIF(count(*), 0)
                  FROM auction_comps
                 WHERE canon_make = %s AND year = %s
            """, (canon_make(mk), yr))
            row = cur.fetchone()
            rate = float(row[0]) if row and row[0] is not None else None
        finally:
            if own:
                c.close()
        return {'sold': label_rows(sold), 'avail': label_rows(avail),
                'ns_rate': rate, 'ok': bool(sold or avail)}
    except Exception as e:
        print('[auction-comps] bid=%s err=%s' % (bid.get('id'), e), flush=True)
        return empty


# Edge's slugs are not human-readable. The card shows these instead.
AUCTION_LABEL = {
    'orlandolongwoodaafl': 'Orlando Longwood',
    'southfloridaaa':      'South Florida',
    'anaaorlando':         'AutoNation Orlando',
    'orlandoaa':           'Orlando AA',
    'aaayatb':             'Tampa Bay',
    'aaayam':              'Miami',
    'aaayafm':             'Fort Myers',
    'aaayasa':             'Sunset',
    'aaapensacola':        'Pensacola',
    'vemoaag':             'VEMO Gainesville',
    'jacksonvilleaa':      'Jacksonville',
    'speedwayaa':          'Speedway',
    'daxtampafl2':         'DAX Tampa',
    'anaaatlanta': 'AutoNation ATL',
    'vipauctions': 'Dealers AA ATL',
    'aaaatlanta': "America's AA ATL",
}


def label_rows(rows):
    for r in rows:
        r['auction_label'] = AUCTION_LABEL.get(r.get('auction_slug'),
                                               r.get('auction_slug'))
    return rows
