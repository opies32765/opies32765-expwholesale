#!/usr/bin/env python3
"""OFFER_SCOUT_2026_08_14 — proactive dealer offer drafts for owner review.

Targets a dealer's aged (high effective-DOL) and price-dropped inventory from
the latest dealer_opportunities snapshot, computes a deterministic offer band
anchored on MMR wholesale, has the LOCAL 9B brain pick a price within the band
and draft the outreach email, then writes rows to dealer_offer_drafts with
status='draft' for the owners to review at /dealer-offers.

HARD GUARANTEES (code-enforced, per standing rules):
  * NOTHING here sends email or SMS. Output is review rows only.
  * The 9B is called DIRECTLY at EW_BRAIN_URL — no Gemini fallback ever.
    Brain down => the run fails loudly.
  * The dealer-facing email text carries NO number except the offer price,
    which CODE substitutes into the {{OFFER}} placeholder after clamping.
    Drafts whose email contains any other dollar figure / long digit run are
    regenerated once, then fall back to a deterministic template.
  * MMR / rBook / valuation figures appear ONLY in offer_rationale
    (owner-internal) — never in email_draft (enrichment-leak rule).

Usage:
    ./venv/bin/python dealer_offer_scout.py --dealer-id 1 [--limit 25]
        [--dol-min 45] [--drop-days 30] [--dry-run]
"""
import argparse
import datetime
import json
import re
import sys
import urllib.request

import psycopg2
import psycopg2.extras

DB = "dbname=expwholesale user=expuser password=ExpWholesale2026! host=localhost port=5433"
BRAIN_ENV = "/etc/ew-brain.env"

# ── Offer-band constants (owner-calibration knobs — tune here) ────────────
FLOOR_PCT_OF_MMR = 0.90    # default floor = 90% of MMR wholesale avg
FLOOR_PCT_AGED = 0.87      # floor when effective DOL >= AGED_DOL
AGED_DOL = 90
CEIL_PCT_OF_MMR = 1.00     # never offer above MMR wholesale avg
ROUND_TO = 100             # offers rounded to nearest $100

# ── Like-car AI track record (LIKE_CARS_2026_08_14) ────────────────────────
# ai_accuracy sign convention (reconcile_ai_accuracy.py): delta = actual - ai_rec,
# so POSITIVE bias_pct = we actually PAID MORE than the AI recommended on like
# cars (model runs low); NEGATIVE = like cars transacted BELOW the model's
# number (model runs high). The band shifts by a graduated fraction of the
# measured bias — same tier philosophy as bias_correction.py — and NEVER
# raises the ceiling above min(MMR, asking).
BIAS_WINDOW_DAYS = 90      # bias_segments window (refreshed daily 08:00 cron)
BIAS_MIN_N = 4             # below this, track record shown but not applied
BIAS_CAP_PCT = 6.0         # max band shift either direction
BIAS_TIERS = [             # (min_n, max_stddev, strength)
    (15, 8.0,  0.8),
    (8,  None, 0.5),
    (4,  None, 0.25),
]

PROMPT = """You are the acquisitions desk at Experience Wholesale, a licensed
Florida wholesale buyer. We buy inventory outright from franchise and
independent dealers. Below are the facts about one vehicle currently in a
dealer's used inventory that we want to make a cash offer on.

VEHICLE: {yr} {make} {model}{trim_c}, {miles:,} miles
DEALER: {dealer}
Asking price: ${asking:,}
Days on their lot: {dol}
{drop_line}
MMR wholesale average (our anchor, INTERNAL — never reveal): ${mmr:,}
Allowed offer band (INTERNAL): ${floor:,} to ${ceil:,}
{track_line}

Reply with ONLY a JSON object, no markdown fences, with exactly these keys:
  "offer_price": integer — your offer, a multiple of {round_to} inside the
      allowed band. Consider: longer time on lot and recent price cuts mean
      the dealer is more motivated, so lean lower; a fresh desirable unit
      deserves the top of the band.
  "rationale": 2-3 sentences for OUR owners explaining why this price —
      reference the aging/price-cut signals and the anchor. Internal only.
  "email": 3-5 sentence friendly, professional email body to the dealer's
      used-car manager offering to buy this exact unit. Rules for the email:
      write the literal placeholder {{{{OFFER}}}} where the dollar amount
      goes, and it must appear exactly once; do NOT include any other number,
      price, valuation, mileage or day-count; do NOT mention MMR, book values
      or data sources; no subject line, no signature block; refer to the car
      naturally (year make model). Mention we can close and pick up quickly.
"""


def brain_cfg():
    cfg = {}
    with open(BRAIN_ENV) as fh:
        for ln in fh:
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    if not cfg.get("EW_BRAIN_URL"):
        sys.exit("EW_BRAIN_URL missing from /etc/ew-brain.env — refusing to run")
    return cfg


def brain_call(cfg, prompt, max_tokens=1600, temperature=0.3):
    """Direct OpenAI-compatible call to the local 9B. Raises on any failure —
    deliberately NO fallback to any external model."""
    body = json.dumps({
        "model": "ew-brain",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        # Same as local_brain_shim: without this the 9B spends the whole
        # budget on a thinking preamble and never reaches the JSON.
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    req = urllib.request.Request(
        cfg["EW_BRAIN_URL"].rstrip("/") + "/v1/chat/completions",
        data=body, method="POST",
        headers={"Content-Type": "application/json",
                 # Cloudflare fronts brain.* and 403s the default
                 # python-urllib User-Agent; any explicit UA passes.
                 "User-Agent": "ew-offer-scout/1.0",
                 "Authorization": "Bearer " + cfg.get("EW_BRAIN_KEY", "")})
    with urllib.request.urlopen(req, timeout=int(cfg.get("EW_BRAIN_TIMEOUT", 45))) as r:
        out = json.loads(r.read())
    return out["choices"][0]["message"]["content"]


def parse_brain_json(text):
    """Tolerant JSON extraction: strip fences / leading prose, parse the first
    top-level object."""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError("no JSON object in brain reply")
    return json.loads(m.group(0))


_DIGIT_RUN = re.compile(r"\$\s*\d|\d{4,}|\d{1,3},\d{3}")


def email_is_clean(email, year=None):
    """True when the email carries exactly one {{OFFER}} placeholder and no
    dollar/price-like figure. The vehicle YEAR is the one digit-run the model
    is allowed (it is told to name the car naturally); every other digit run
    or dollar sign is grounds for rejection."""
    if email.count("{{OFFER}}") != 1:
        return False
    probe = email.replace("{{OFFER}}", "")
    if year:
        probe = probe.replace(str(year), "")
    return not _DIGIT_RUN.search(probe)


def rescue_placeholder(email):
    """The 9B often writes one literal dollar figure where {{OFFER}} belongs
    (and that figure can even disagree with its own offer_price — observed
    live). If the email has NO placeholder and EXACTLY ONE dollar figure,
    swap it for the placeholder so code, never the model, owns the number.
    Anything more ambiguous falls through to the template fallback."""
    if "{{OFFER}}" in email:
        return email
    figs = re.findall(r"\$\s?\d[\d,]*", email)
    if len(figs) == 1:
        return email.replace(figs[0], "{{OFFER}}", 1)
    return email


def template_email(yr, make, model):
    return (f"Hi, this is the buying desk at Experience Wholesale. We noticed the "
            f"{yr} {make} {model} on your lot and we'd like to make you a firm cash "
            f"offer of {{{{OFFER}}}} for it, as-is. We're licensed Florida wholesale "
            f"buyers, we can have funds and transport arranged within days, and "
            f"there's no fee or obligation on your side. If the number works, reply "
            f"here or give us a call and we'll get it done quickly.")


def clamp_offer(raw, floor, ceil):
    try:
        v = int(raw)
    except (TypeError, ValueError):
        v = floor
    v = max(floor, min(ceil, v))
    return int(round(v / ROUND_TO) * ROUND_TO)


def _model_token(model):
    """First token of a model name, hyphens folded: 'S-Class'->'S',
    'G 550'->'G', 'Dbx'->'DBX', 'AMG GT'->'AMG'."""
    import re as _re
    m = _re.sub(r"[-]", " ", (model or "").upper()).strip()
    return m.split(" ")[0] if m else ""


_LC_SRC = """
    WITH src AS (
      SELECT DISTINCT ON (vin) upper(make) AS mk,
             split_part(replace(upper(model), '-', ' '), ' ', 1) AS mtok,
             COALESCE(delta_pct, estimate_delta_pct) AS dp
        FROM ai_accuracy
       WHERE (delta_pct IS NOT NULL OR estimate_delta_pct IS NOT NULL)
         AND bid_id > 0 AND vin IS NOT NULL AND vin <> ''
         AND ai_recommendation IS NOT NULL
         AND reconciled_at > NOW() - (%(win)s || ' days')::interval
         AND COALESCE(actual_purchased_at, client_estimate_at)
             > NOW() - (%(win)s || ' days')::interval
       ORDER BY vin, ai_assessed_at DESC NULLS LAST
    )
    SELECT count(*) AS n, round(avg(dp)::numeric, 2) AS bias_pct,
           round(stddev_samp(dp)::numeric, 2) AS stddev_pct
      FROM src WHERE {where}
"""


def like_car_adjustment(conn, make, model, year, mileage):
    """LIKE_CARS_TOKEN_MATCH: AI track record on like cars, straight off
    ai_accuracy (VIN-deduped, 90d, same filters as bias_correction's
    refresh_segments). Sign: positive bias = we actually paid ABOVE the AI.

    Levels: model-token match (full tier strengths) -> make only (capped
    0.25) -> fleet-wide (display only, never shifts the band).
    Returns (stats_dict_or_None, shift_pct)."""
    mk = (make or "").upper().strip()
    mtok = _model_token(model)
    cur = conn.cursor()
    levels = []
    if mk and mtok:
        levels.append(("model", "mk = %(mk)s AND mtok = %(mtok)s",
                       f"{mk}|{mtok}*"))
    if mk:
        levels.append(("make", "mk = %(mk)s", f"{mk}|any"))
    levels.append(("fleet", "TRUE", "fleet|any"))

    params = {"win": BIAS_WINDOW_DAYS, "mk": mk, "mtok": mtok}
    chosen = None
    for level, where, seg_key in levels:
        cur.execute(_LC_SRC.format(where=where), params)
        n, bias, sd = cur.fetchone()
        if n and n >= BIAS_MIN_N:
            chosen = {"lookup_level": level, "segment_key": seg_key,
                      "n": int(n), "bias_pct": float(bias),
                      "stddev_pct": float(sd) if sd is not None else None}
            break
    cur.close()
    if chosen is None:
        return None, 0.0

    strength = 0.0
    if chosen["lookup_level"] != "fleet":
        sd = chosen["stddev_pct"]
        for min_n, max_sd, st in BIAS_TIERS:
            if chosen["n"] >= min_n and (max_sd is None or
                                         (sd is not None and sd <= max_sd)):
                strength = st
                break
        if chosen["lookup_level"] == "make":
            strength = min(strength, 0.25)

    shift = max(-BIAS_CAP_PCT, min(BIAS_CAP_PCT, strength * chosen["bias_pct"]))
    chosen.update(applied=bool(shift), shift_pct=round(shift, 2),
                  strength=strength, window_days=BIAS_WINDOW_DAYS)
    return chosen, shift


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dealer-id", type=int, required=True)
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--dol-min", type=int, default=45)
    ap.add_argument("--drop-days", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = brain_cfg()
    # Fail fast if the brain is down — before touching the DB.
    brain_call(cfg, 'Reply with the single word: ok', max_tokens=10)

    run_batch = datetime.datetime.now().strftime("%Y%m%d-%H%M")

    conn = psycopg2.connect(DB)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Candidates: latest opportunity snapshot for this dealer, re-joined to
    # live inventory (must still be active), effective DOL recomputed live
    # (the snapshot's dealer_dol froze verified_days_on_lot without adding
    # elapsed time). Carfax red flags gate hard.
    # SELECT_V2_2026_08_14: candidates from LIVE inventory + MMR, not from
    # dealer_opportunities — that table is threshold-gated to under-MMR cars
    # only, which excludes at/above-MMR inventory (i.e. most aged exotics,
    # the prime offer targets). The opportunity row is an optional LEFT JOIN
    # for carfax signals + score. MMR anchor must be <= 14 days old.
    cur.execute("""
        SELECT di.id AS inventory_id, di.vin, di.year, di.make, di.model,
               di.trim, di.mileage,
               o.id AS opportunity_id, o.score AS opp_score, o.signals,
               di.price AS asking_price,
               COALESCE(di.verified_days_on_lot
                          + EXTRACT(EPOCH FROM (NOW() - di.verified_at))/86400,
                        EXTRACT(EPOCH FROM (NOW() - di.source_added_at))/86400,
                        EXTRACT(EPOCH FROM (NOW() - di.first_seen_at))/86400
               )::int AS effective_dol,
               CASE WHEN di.price_drop_at > NOW() - (%(dropd)s || ' days')::interval
                    THEN di.price_drop_amount END AS price_drop_amount,
               CASE WHEN di.price_drop_at > NOW() - (%(dropd)s || ' days')::interval
                    THEN EXTRACT(EPOCH FROM (NOW() - di.price_drop_at))/86400
               END::int AS price_drop_days_ago,
               m.wholesale_avg AS mmr_wholesale_avg, m.fetched_at AS mmr_fetched_at,
               d.name AS dealer_name
          FROM dealer_inventory di
          JOIN dealers d ON d.id = di.dealer_id
          JOIN dealer_mmr m ON m.vin = di.vin AND m.wholesale_avg IS NOT NULL
               AND m.fetched_at > NOW() - INTERVAL '14 days'
          LEFT JOIN dealer_opportunities o ON o.inventory_id = di.id
               AND o.snapshot_date = (SELECT MAX(snapshot_date)
                                        FROM dealer_opportunities)
         WHERE di.dealer_id = %(did)s
           AND di.status = 'active'
           AND di.vin IS NOT NULL AND length(di.vin) = 17
           AND COALESCE(di.price, 0) > 0
           AND NOT COALESCE((o.signals->'carfax'->>'total_loss')::bool, false)
           AND NOT COALESCE((o.signals->'carfax'->>'frame_damage')::bool, false)
           AND NOT COALESCE((o.signals->'carfax'->>'odo_rollback')::bool, false)
         ORDER BY o.score DESC NULLS LAST, effective_dol DESC NULLS LAST
    """, {"did": args.dealer_id, "dropd": args.drop_days})
    rows = cur.fetchall()

    targets = [r for r in rows
               if (r["effective_dol"] or 0) >= args.dol_min
               or (r["price_drop_amount"] or 0) > 0][:args.limit]
    print(f"dealer {args.dealer_id}: {len(rows)} snapshot rows, "
          f"{len(targets)} targets (dol>={args.dol_min} or drop<={args.drop_days}d), "
          f"limit {args.limit}, batch {run_batch}")

    written = skipped = fallback = 0
    for t in targets:
        mmr = t["mmr_wholesale_avg"]
        asking = t["asking_price"]
        dol = t["effective_dol"] or 0
        floor_pct = FLOOR_PCT_AGED if dol >= AGED_DOL else FLOOR_PCT_OF_MMR
        ceil = int(min(mmr * CEIL_PCT_OF_MMR, asking))
        floor = min(int(mmr * floor_pct), ceil)
        floor = int(round(floor / ROUND_TO) * ROUND_TO)
        ceil = int(round(ceil / ROUND_TO) * ROUND_TO)
        if ceil <= 0 or floor <= 0:
            skipped += 1
            continue

        # LIKE_CARS_2026_08_14: shift the band by the AI's measured bias on
        # like cars. Negative bias (model ran high) drops both ends; positive
        # bias (we had to pay above the model) raises only the FLOOR — the
        # ceiling never exceeds min(MMR, asking).
        base_floor, base_ceil = floor, ceil
        lc_stats, lc_shift = like_car_adjustment(
            conn, t['make'], t['model'], t['year'], t['mileage'])
        if lc_shift < 0:
            floor = int(floor * (1 + lc_shift / 100))
            ceil = int(ceil * (1 + lc_shift / 100))
        elif lc_shift > 0:
            floor = min(ceil, int(floor * (1 + lc_shift / 100)))
        floor = int(round(floor / ROUND_TO) * ROUND_TO)
        ceil = int(round(ceil / ROUND_TO) * ROUND_TO)
        floor = min(floor, ceil)
        if lc_stats is not None:
            lc_stats['base_floor'] = base_floor
            lc_stats['base_ceiling'] = base_ceil

        if lc_stats and lc_stats.get('n') and int(lc_stats['n']) >= BIAS_MIN_N:
            _bias = float(lc_stats['bias_pct'])
            _dir = ('we typically had to pay ABOVE the model to win them'
                    if _bias > 0 else
                    "cars like this transacted BELOW the model's number")
            _app = (f" The allowed band above already reflects this "
                    f"({lc_stats['shift_pct']:+.1f}%)." if lc_stats.get('applied')
                    else ' Not strong enough to shift the band.')
            track_line = (f"AI track record on like cars (INTERNAL): across "
                          f"{lc_stats['n']} reconciled deals "
                          f"[{lc_stats['segment_key']}], the price we actually "
                          f"paid vs our AI's recommendation ran {_bias:+.1f}%"
                          f" — {_dir}.{_app} Reference this in your rationale.")
        else:
            track_line = ('AI track record on like cars (INTERNAL): no '
                          'reconciled history for this segment yet.')

        drop_line = (f"Recent price cut: ${t['price_drop_amount']:,} "
                     f"({t['price_drop_days_ago']} days ago)"
                     if t["price_drop_amount"] else "Recent price cut: none")
        trim_c = f" {t['trim']}" if t.get("trim") else ""
        prompt = PROMPT.format(yr=t["year"], make=t["make"], model=t["model"],
                               trim_c=trim_c, miles=t["mileage"] or 0,
                               dealer=t["dealer_name"], asking=asking, dol=dol,
                               drop_line=drop_line, mmr=mmr, floor=floor,
                               ceil=ceil, round_to=ROUND_TO,
                               track_line=track_line)

        offer = rationale = email = None
        model_used = "ew-brain"
        for attempt in (1, 2):
            try:
                reply = parse_brain_json(brain_call(cfg, prompt))
                offer = clamp_offer(reply.get("offer_price"), floor, ceil)
                rationale = (reply.get("rationale") or "").strip()
                email = rescue_placeholder((reply.get("email") or "").strip())
                if email_is_clean(email, t["year"]):
                    break
                email = None  # dirty email → one retry, then template
            except Exception as e:
                print(f"  {t['vin']}: brain attempt {attempt} failed: {e}")
        if offer is None:
            offer = floor  # deterministic: motivated-seller default
            rationale = "9B unavailable/unparseable — deterministic floor offer."
        if email is None:
            email = template_email(t["year"], t["make"], t["model"])
            model_used = "ew-brain+template-email"
            fallback += 1
        email = email.replace("{{OFFER}}", f"${offer:,}")

        print(f"  {t['vin']} {t['year']} {t['make']} {t['model']}: "
              f"ask ${asking:,} dol {dol} mmr ${mmr:,} → offer ${offer:,} "
              f"[{floor:,}-{ceil:,}] (lc {lc_shift:+.1f}%) ({model_used})")
        if args.dry_run:
            continue
        cur.execute("""
            INSERT INTO dealer_offer_drafts
              (dealer_id, inventory_id, opportunity_id, vin, year, make, model,
               trim, mileage, asking_price, effective_dol, price_drop_amount,
               price_drop_days_ago, mmr_wholesale_avg, mmr_fetched_at, opp_score,
               offer_floor, offer_ceiling, offer_price, offer_rationale,
               email_draft, model_used, run_batch, like_car_stats, bias_shift_pct)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (dealer_id, vin, run_batch) DO NOTHING
        """, (args.dealer_id, t["inventory_id"], t["opportunity_id"], t["vin"],
              t["year"], t["make"], t["model"], t.get("trim"), t["mileage"],
              asking, dol, t["price_drop_amount"], t["price_drop_days_ago"],
              mmr, t["mmr_fetched_at"], t["opp_score"], floor, ceil, offer,
              rationale, email, model_used, run_batch,
              json.dumps(lc_stats) if lc_stats else None, lc_shift))
        conn.commit()
        written += 1

    print(f"done: {written} drafts written, {skipped} skipped (no band), "
          f"{fallback} template-email fallbacks"
          + (" [DRY RUN — nothing written]" if args.dry_run else ""))
    conn.close()


if __name__ == "__main__":
    main()
