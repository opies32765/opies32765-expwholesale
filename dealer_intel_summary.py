"""dealer_intel_summary.py — Dealer DB Graph System: Layer 4

Daily Gemini-Flash-written narrative for a dealer portal. Pulls:
    - dealer_intel_segments + dealer_intel_snapshot (this dealer's state)
    - network_segment_performance              (peer-network heat)
    - acquisition blind-spots                  (computed in-process)
    - EW reference pricing                     (bids.ai_price recent +
                                                lsl_training.sale_price)
    - per-make XGBoost predictions             (optional, when ai_price
                                                already populated on bids)

Calls Gemini 2.5 Flash with a structured-output schema:
    {
      headline,
      what_you_move_best:        [{title, detail, sample}, ...],
      watch_list:                [{vin, ymm, dol, asking, anchor, action}, ...],
      acquisition_blind_spots:   [{segment, peers_selling, sold_30d, avg_dol,
                                   your_active, narrative}, ...]
    }

Stores response in dealer_intel_summary. Portal renders it.

CLI:
    python3 dealer_intel_summary.py [--dealer-slug encore]
                                    [--dry-run] [--limit-watch 5]

Runs AFTER dealer_intel.py + dealer_intel_network.py in the cron chain.
"""
from __future__ import annotations
import argparse
import json
import logging
import os
import sys
import time
from datetime import date, datetime
from decimal import Decimal

sys.path.insert(0, '/opt/expwholesale')

import psycopg2
import psycopg2.extras

DB = dict(host='localhost', port=5433, dbname='expwholesale',
          user='expuser', password='ExpWholesale2026!')

GEMINI_MODEL = os.environ.get('DEALER_INTEL_SUMMARY_MODEL', 'gemini-3.5-flash')

log = logging.getLogger('dealer_intel_summary')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    stream=sys.stdout,
)


# ── Decimal/JSON helper ─────────────────────────────────────────────────

def _jsonable(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    return obj


def _clean(d):
    """Recursively convert Decimal/date for JSON serialization."""
    if isinstance(d, dict):
        return {k: _clean(v) for k, v in d.items()}
    if isinstance(d, list):
        return [_clean(x) for x in d]
    return _jsonable(d)


# ── Data load ───────────────────────────────────────────────────────────

def load_dealer(cur, slug):
    cur.execute("SELECT id, name, portal_slug FROM dealers WHERE portal_slug=%s LIMIT 1",
                (slug,))
    return cur.fetchone()


def load_segment_perf_self(cur, dealer_id):
    """This dealer's own segment rollups (from dealer_intel_segments)."""
    cur.execute("""
        SELECT DISTINCT ON (segment_key)
               segment_key, make, year_band, mileage_band,
               sold_volume, avg_dol_days, active_count, aging_count,
               verdict, confidence, window_days, snapshot_date
          FROM dealer_intel_segments
         WHERE dealer_id = %s
         ORDER BY segment_key, snapshot_date DESC
    """, (dealer_id,))
    return [dict(r) for r in cur.fetchall()]


def load_network_perf(cur, today):
    """Network-wide segment performance for today's snapshot."""
    cur.execute("""
        SELECT segment_key, make, year_band, mileage_band,
               dealers_selling, sold_volume, avg_dol_days,
               active_count, dealers_with_active, heat_score
          FROM network_segment_performance
         WHERE snapshot_date = %s
         ORDER BY heat_score DESC
    """, (today,))
    return [dict(r) for r in cur.fetchall()]


def load_watch_list(cur, dealer_id, limit=10):
    """The aging units that need attention (price_drop + sell_now chips)."""
    cur.execute("""
        SELECT s.dealer_inventory_id, s.vin, s.chip, s.confidence,
               s.reasoning_text, s.days_on_lot, s.segment_avg_dol,
               s.asking_price, s.rbook_p50, s.rbook_p75, s.mmr_now,
               di.year, di.make, di.model, di.trim, di.mileage,
               di.ext_color
          FROM dealer_intel_snapshot s
          JOIN dealer_inventory di ON di.id = s.dealer_inventory_id
         WHERE s.dealer_id = %s
           AND s.chip IN ('price_drop','sell_now')
         ORDER BY s.days_on_lot DESC NULLS LAST
         LIMIT %s
    """, (dealer_id, limit))
    return [dict(r) for r in cur.fetchall()]


def load_ew_acquisition_refs(cur, dealer_id, days=60):
    """Per-make EW acquisition price baseline (what EW has been bidding
    on similar units). Used to anchor pricing nudges in the brief.

    Joins bids → dealer_inventory by exact YMM match. Only includes
    bids with a non-null ai_price (the AI's buy recommendation)."""
    cur.execute("""
        SELECT di.make, di.year,
               COUNT(DISTINCT b.id)            AS bid_count,
               ROUND(AVG(b.ai_price)::numeric, 0) AS avg_ai_price
          FROM dealer_inventory di
          LEFT JOIN bids b
                 ON UPPER(b.make) = UPPER(di.make)
                AND UPPER(b.model) = UPPER(di.model)
                AND ABS(COALESCE(b.year,0) - COALESCE(di.year,0)) <= 1
                AND b.ai_price IS NOT NULL
                AND b.created_at > NOW() - (INTERVAL '1 day' * %s)
         WHERE di.dealer_id = %s AND di.status='active'
         GROUP BY di.make, di.year
        HAVING COUNT(DISTINCT b.id) >= 2
         ORDER BY bid_count DESC
    """, (days, dealer_id))
    return [dict(r) for r in cur.fetchall()]


def load_lsl_sale_refs(cur, dealer_id, days=180):
    """Per-make LSL wholesale-sale baseline (what EW has actually flipped
    similar units for). Used as a market reference WITHOUT exposing EW
    gross — only the sale price is cited, not the spread."""
    cur.execute("""
        SELECT di.make,
               COUNT(*)::int                       AS sales_count,
               ROUND(AVG(l.sale_price)::numeric, 0) AS avg_sale_price
          FROM dealer_inventory di
          JOIN lsl_training l
            ON UPPER(l.make_name) = UPPER(di.make)
           AND UPPER(l.model_name) = UPPER(di.model)
           AND ABS(COALESCE(l.year,0) - COALESCE(di.year,0)) <= 1
           AND l.sale_price IS NOT NULL
           AND l.sold_at IS NOT NULL
           AND l.sold_at > NOW() - (INTERVAL '1 day' * %s)
         WHERE di.dealer_id = %s AND di.status='active'
         GROUP BY di.make
        HAVING COUNT(*) >= 3
         ORDER BY sales_count DESC
    """, (days, dealer_id))
    return [dict(r) for r in cur.fetchall()]


def compute_blind_spots(self_segments, network_perf, min_dealers=3,
                        min_sold=8, top_n=10):
    """Acquisition blind spots: segments where peer dealers are turning
    units fast but this dealer is underweight (active_count <= 1).

    Returns list of dicts ranked by heat_score, capped at top_n."""
    own_by_key = {s['segment_key']: s for s in self_segments}
    out = []
    for n in network_perf:
        if n['dealers_selling'] < min_dealers:
            continue
        if n['sold_volume'] < min_sold:
            continue
        own = own_by_key.get(n['segment_key'])
        own_active = (own or {}).get('active_count') or 0
        # Underweight: 0 or 1 active here vs many sold at peers
        if own_active > 1:
            continue
        out.append({
            'segment_key': n['segment_key'],
            'make': n['make'],
            'year_band': n['year_band'],
            'mileage_band': n['mileage_band'],
            'peers_selling': n['dealers_selling'],
            'sold_30d': n['sold_volume'],
            'avg_dol': float(n['avg_dol_days']) if n['avg_dol_days'] else None,
            'your_active': own_active,
            'network_active': n['active_count'],
            'heat_score': float(n['heat_score']) if n['heat_score'] else 0.0,
        })
    out.sort(key=lambda x: -x['heat_score'])
    return out[:top_n]


def load_what_you_move_best(self_segments, top_n=5):
    """The strongest selling segments for THIS dealer. Confidence-tiered."""
    candidates = [s for s in self_segments
                  if (s.get('sold_volume') or 0) >= 1
                  and s.get('verdict') in ('strong', 'normal')]
    candidates.sort(key=lambda s: (
        -(s.get('sold_volume') or 0),
        float(s.get('avg_dol_days') or 9999),
    ))
    return candidates[:top_n]


def load_sample_sizes(cur, dealer_id):
    cur.execute("SELECT COUNT(*) AS n FROM bids WHERE ai_price IS NOT NULL "
                "AND created_at > NOW() - INTERVAL '60 days'")
    ew_bids_60d = cur.fetchone()['n']
    cur.execute("SELECT COUNT(*) AS n FROM lsl_training "
                "WHERE sold_at > NOW() - INTERVAL '180 days'")
    ew_sales_180d = cur.fetchone()['n']
    cur.execute("SELECT COUNT(DISTINCT dealer_id) AS n FROM dealer_inventory "
                "WHERE dealer_id <> %s AND status IN ('active','sold')",
                (dealer_id,))
    peer_dealers = cur.fetchone()['n']
    cur.execute("SELECT COUNT(*) AS n FROM dealer_inventory "
                "WHERE dealer_id <> %s AND status='sold' "
                "AND sold_at > NOW() - INTERVAL '30 days'",
                (dealer_id,))
    peer_sold_30d = cur.fetchone()['n']
    return {
        'ew_bids_60d': ew_bids_60d,
        'ew_sales_180d': ew_sales_180d,
        'peer_dealers': peer_dealers,
        'peer_sold_30d': peer_sold_30d,
    }


# ── Prompt + Gemini ─────────────────────────────────────────────────────

PROMPT_TEMPLATE = """You are writing the day's brief for a wholesale used-vehicle dealer's portal.
EW (Experience Wholesale) is the platform — a middleman that buys cars from dealers and sells to other dealers. Encore (this dealer) is a luxury/exotic used-car operator.

Your output is a DEALER-FACING SUMMARY. Tone: data-driven, no fluff, no marketing-speak.
Cite specific numbers from the inputs. NEVER reveal EW's margin or what EW makes on a car.
You may cite EW's wholesale acquisition price ("what EW has paid for similar cars")
and EW's wholesale sale price ("what EW has sold similar cars for") as MARKET REFERENCES.
Frame those as evidence of where the market is, not as what EW makes.

INPUTS (JSON):
{payload}

Produce three sections + a 1-line headline. Use the schema below verbatim.
Each list item must cite specific numbers. Don't invent numbers — only use what's in the inputs.
Don't recommend things if the data isn't there; if a section is empty, return an empty list.

For WATCH LIST items, frame each as either:
  - "MMR/rBook holding, you have room to retail this even at higher DOL" (when market is firm)
  - "DOL high AND market drifting, consider wholesaling soon" (when both signals say move)
The brief should help the dealer decide HOLD-FOR-RETAIL vs MOVE-WHOLESALE on each watch unit.

For ACQUISITION BLIND SPOTS, frame as: "N peer dealers in EW's network turning X units of [segment] in [avg_dol] days; you have [your_active] active. Consider sourcing."
Do not invent peer dealer names. Just say "peer dealers."

For WHAT YOU MOVE BEST, lead with the dealer's strongest sold segments by volume + speed.
Acknowledge thin data when applicable.
"""

GEMINI_SCHEMA = {
    "type": "object",
    "required": ["headline", "what_you_move_best", "watch_list",
                 "acquisition_blind_spots"],
    "properties": {
        "headline": {"type": "string"},
        "what_you_move_best": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["title", "detail"],
                "properties": {
                    "title":  {"type": "string"},
                    "detail": {"type": "string"},
                    "sample": {"type": "string"},
                }
            }
        },
        "watch_list": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["vehicle", "dol", "asking", "action"],
                "properties": {
                    "vehicle": {"type": "string"},
                    "dol":     {"type": "integer"},
                    "asking":  {"type": "integer"},
                    "anchor":  {"type": "string"},
                    "action":  {"type": "string"},
                }
            }
        },
        "acquisition_blind_spots": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["segment", "narrative"],
                "properties": {
                    "segment":        {"type": "string"},
                    "peers_selling":  {"type": "integer"},
                    "sold_30d":       {"type": "integer"},
                    "avg_dol":        {"type": "number"},
                    "your_active":    {"type": "integer"},
                    "narrative":      {"type": "string"},
                }
            }
        }
    }
}


def _gemini_client():
    """Return a configured genai client or None.

    Matches the canonical pattern used by app.py:_gemini() — Vertex AI
    mode via the service account JSON, project + location pinned. This
    inherits the same auth the bid pipeline has been using for months."""
    try:
        from google import genai
        os.environ.setdefault(
            'GOOGLE_APPLICATION_CREDENTIALS',
            '/opt/expwholesale/google_vision_key.json')
        return genai.Client(
            vertexai=True,
            project='my-project-dia-492415',
            location='global',
        )
    except Exception as e:
        log.error('gemini client init failed: %s', e)
        return None


def call_gemini(payload):
    client = _gemini_client()
    if not client:
        return None, {}, 'no client'
    from google.genai import types
    prompt = PROMPT_TEMPLATE.format(payload=json.dumps(payload, default=_jsonable))
    cfg = types.GenerateContentConfig(
        # 2048 truncates mid-section on the watch_list; bump to 4096 so
        # 10 watch items + 10 blind spots + 5 best-segments all fit.
        # Flash thinking can still eat some of this; if we see truncation
        # again, disable thinking with thinking_budget=0.
        max_output_tokens=4096,
        temperature=0.4,
        response_mime_type='application/json',
        response_schema=GEMINI_SCHEMA,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )
    t0 = time.time()
    try:
        resp = client.models.generate_content(
            model=GEMINI_MODEL, contents=prompt, config=cfg)
        elapsed = int((time.time() - t0) * 1000)
        text = resp.text or ''
        meta = {
            'generation_ms': elapsed,
            'prompt_tokens': getattr(resp.usage_metadata, 'prompt_token_count', None)
                              if hasattr(resp, 'usage_metadata') else None,
            'output_tokens': getattr(resp.usage_metadata, 'candidates_token_count', None)
                              if hasattr(resp, 'usage_metadata') else None,
        }
        try:
            parsed = json.loads(text) if text else {}
        except json.JSONDecodeError as je:
            log.error('gemini returned non-JSON despite schema: %s', je)
            log.error('text was: %s', text[:500])
            return None, meta, 'parse-fail'
        return parsed, meta, None
    except Exception as e:
        return None, {}, str(e)


# ── Persist ─────────────────────────────────────────────────────────────

def upsert_summary(cur, dealer_id, today, parsed, meta, sample_sizes,
                   raw_payload):
    cur.execute("""
        INSERT INTO dealer_intel_summary
          (dealer_id, snapshot_date, headline,
           what_you_move_best, watch_list, acquisition_blind_spots,
           sample_sizes, model_name, prompt_tokens, output_tokens,
           raw_response, generation_ms, computed_at)
        VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb,
                %s, %s, %s, %s::jsonb, %s, NOW())
        ON CONFLICT (dealer_id, snapshot_date) DO UPDATE
          SET headline                = EXCLUDED.headline,
              what_you_move_best      = EXCLUDED.what_you_move_best,
              watch_list              = EXCLUDED.watch_list,
              acquisition_blind_spots = EXCLUDED.acquisition_blind_spots,
              sample_sizes            = EXCLUDED.sample_sizes,
              model_name              = EXCLUDED.model_name,
              prompt_tokens           = EXCLUDED.prompt_tokens,
              output_tokens           = EXCLUDED.output_tokens,
              raw_response            = EXCLUDED.raw_response,
              generation_ms           = EXCLUDED.generation_ms,
              computed_at             = NOW()
    """, (
        dealer_id, today, parsed.get('headline'),
        json.dumps(parsed.get('what_you_move_best', [])),
        json.dumps(parsed.get('watch_list', [])),
        json.dumps(parsed.get('acquisition_blind_spots', [])),
        json.dumps(sample_sizes),
        meta.get('model_name', GEMINI_MODEL),
        meta.get('prompt_tokens'),
        meta.get('output_tokens'),
        json.dumps({'parsed': parsed, 'payload': raw_payload}, default=_jsonable),
        meta.get('generation_ms'),
    ))


# ── Main ────────────────────────────────────────────────────────────────

def run(dealer_slug, dry_run=False, limit_watch=10):
    today = date.today()
    with psycopg2.connect(**DB) as db:
        with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            dealer = load_dealer(cur, dealer_slug)
            if not dealer:
                log.error('dealer %r not found', dealer_slug)
                return 2
            dealer_id = dealer['id']
            log.info('dealer=%s id=%d slug=%s dry=%s',
                     dealer['name'], dealer_id, dealer_slug, dry_run)

            self_segs = load_segment_perf_self(cur, dealer_id)
            network = load_network_perf(cur, today)
            watch = load_watch_list(cur, dealer_id, limit=limit_watch)
            blind_spots = compute_blind_spots(self_segs, network)
            best = load_what_you_move_best(self_segs)
            ew_acq = load_ew_acquisition_refs(cur, dealer_id, days=60)
            ew_sale = load_lsl_sale_refs(cur, dealer_id, days=180)
            sample_sizes = load_sample_sizes(cur, dealer_id)

            log.info('loaded self_segs=%d network=%d watch=%d blind_spots=%d '
                     'best=%d ew_acq_groups=%d ew_sale_groups=%d',
                     len(self_segs), len(network), len(watch),
                     len(blind_spots), len(best), len(ew_acq), len(ew_sale))

            payload = {
                'dealer_name': dealer['name'],
                'snapshot_date': str(today),
                'what_you_move_best_candidates': _clean(best),
                'watch_units': _clean(watch),
                'acquisition_blind_spots': _clean(blind_spots),
                'ew_acquisition_references_60d': _clean(ew_acq),
                'ew_sale_references_180d': _clean(ew_sale),
                'sample_sizes': sample_sizes,
            }

            parsed, meta, err = call_gemini(payload)
            if err or not parsed:
                log.error('gemini call failed: %s', err)
                return 1
            log.info('gemini ok · prompt_tokens=%s output_tokens=%s ms=%s',
                     meta.get('prompt_tokens'), meta.get('output_tokens'),
                     meta.get('generation_ms'))
            log.info('headline: %s', parsed.get('headline'))
            log.info('sections: best=%d watch=%d blind=%d',
                     len(parsed.get('what_you_move_best') or []),
                     len(parsed.get('watch_list') or []),
                     len(parsed.get('acquisition_blind_spots') or []))

            if dry_run:
                log.info('dry-run — skipping write')
                print(json.dumps(parsed, indent=2))
                return 0

            upsert_summary(cur, dealer_id, today, parsed, meta,
                            sample_sizes, payload)
            db.commit()
            log.info('summary persisted for dealer_id=%d date=%s',
                     dealer_id, today)
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--dealer-slug', default='encore')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--limit-watch', type=int, default=10)
    args = p.parse_args()
    try:
        return run(args.dealer_slug, args.dry_run, args.limit_watch)
    except Exception as e:
        log.exception('failed: %s', e)
        return 1


if __name__ == '__main__':
    sys.exit(main())
