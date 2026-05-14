"""canonicalize_bid.py — VIN canonicalization for bid intake.

Phase 3 (2026-05-11): Wire NHTSA decode + VIN-prefix trim lookup into every
bid intake site. Before this module, `canon_year/make/model/trim/source/
confidence` were never written for bid VINs (0 of 1064 bids in last 30
days had `canon_make` set). Downstream systems (AccuTrade overseer, vAuto
BFF Trim filter, dealer_match) all read canon_* via COALESCE — when those
columns are NULL, the system falls back to seller-typed `bids.trim` which
is often wrong (bid 1188: seller said "Carrera 4 (Coupe)", VIN said GT3).

Resolution order:
  1. NHTSA (`nhtsa_decode.decode_vin`) — cached, free, ~150ms first hit
  2. VIN-prefix lookup (`vin_prefix_trim`) — fills trim when NHTSA returns
     blank (Porsche, exotics)
  3. iPacket / AccuTrade overseer — already running downstream, will write
     canon_trim if intake-time NHTSA + VIN-prefix both came up blank

Confidence scale:
  0.95+ = NHTSA returned trim AND VIN-prefix agreed
  0.85  = single high-confidence source (NHTSA trim OR clear VIN-prefix)
  0.70  = NHTSA YMM clean, trim ambiguous, VIN-body known
  0.50  = NHTSA YMM clean, trim blank, no VIN-prefix hit
  <0.5  = NHTSA error or partial decode
"""
from __future__ import annotations
import logging
from typing import Optional

log = logging.getLogger(__name__)


def canonicalize_bid_vin(bid_id: int, conn,
                         force: bool = False) -> dict:
    """Decode VIN for `bid_id`, write canon_* fields, return summary dict.

    Idempotent: skips when canon_make is already set AND force=False.
    Safe to call repeatedly (e.g. after operator updates the VIN).

    Args:
        bid_id: target bid
        conn: open psycopg2 connection (caller's transaction)
        force: re-decode even when canon_make is already populated

    Returns dict with keys:
        ok, bid_id, vin, canon_year, canon_make, canon_model,
        canon_trim, canon_source, canon_confidence, reason
    """
    out = {'ok': False, 'bid_id': bid_id, 'vin': None,
           'canon_year': None, 'canon_make': None, 'canon_model': None,
           'canon_trim': None, 'canon_source': None,
           'canon_confidence': None, 'reason': None}

    cur = conn.cursor()
    cur.execute(
        "SELECT vin, year, make, model, trim, canon_make, canon_trim, "
        "       canon_source "
        "FROM bids WHERE id = %s", (bid_id,))
    row = cur.fetchone()
    if not row:
        out['reason'] = 'bid_not_found'
        return out

    # psycopg2 RealDictCursor vs default tuple handling
    if hasattr(row, 'keys'):
        vin = row['vin']
        existing_canon_make = row.get('canon_make')
        existing_canon_source = row.get('canon_source')
    else:
        vin = row[0]
        existing_canon_make = row[5]
        existing_canon_source = row[7]

    out['vin'] = vin

    # ── VIN structural validation (length + char set + ISO 3779 check digit)
    # Failing here is terminal — write bids.vin_invalid_reason so the worker
    # eligibility query excludes this bid and the dashboard shows a red
    # 'INVALID VIN' badge. Skip the decode entirely.
    try:
        from vin_validate import validate as _vin_validate
        v_check = _vin_validate(vin)
    except Exception as _e:
        log.warning('bid %d vin_validate import failed: %s', bid_id, _e)
        v_check = {'valid': True, 'reason': None}  # fail open
    if not v_check['valid']:
        try:
            cur.execute(
                'UPDATE bids SET vin_invalid_reason = %s WHERE id = %s',
                (v_check['reason'], bid_id))
            # Release any in-flight claim so the worker pool moves on.
            cur.execute(
                'UPDATE bids SET vauto_claimed_at = NULL, vauto_claimed_by = NULL '
                'WHERE id = %s', (bid_id,))
        except Exception as _e:
            log.warning('bid %d set vin_invalid_reason failed: %s', bid_id, _e)
        out['reason'] = f'invalid_vin:{v_check["reason"]}'
        out['vin_invalid_reason'] = v_check['reason']
        return out

    # Idempotency: don't overwrite a high-confidence canon (e.g. overseer
    # already wrote canon_trim='GT3' from accutrade_overseer source). Only
    # NHTSA-blank bids should be re-attempted.
    if existing_canon_make and not force:
        # If the existing source is the AccuTrade overseer (high-confidence
        # trim), keep it but DON'T overwrite — just return what's there.
        out['reason'] = f'already_canonicalized:{existing_canon_source}'
        out['ok'] = True
        return out

    # 1) Claude Sonnet 4.6 → NHTSA fallback (cached forever per VIN in
    # vin_decode_cache). decode_vin_smart's cascade:
    #   cache hit → return
    #   miss → Claude call (~3s); conf ≥ 0.7 → use, cache as claude_sonnet_4_6
    #   Claude error or low conf → NHTSA fallback, cache as nhtsa_fallback
    # Returns same year/make/model/trim shape the rest of this function
    # expects, plus body_style and confidence we propagate up.
    nhtsa = {}
    nh_err = None
    try:
        from claude_vin_decoder import decode_vin_smart
        from nhtsa_decode import decode_vin as nhtsa_decode
        smart = decode_vin_smart(vin, conn, nhtsa_fallback=nhtsa_decode) or {}
        if smart:
            nhtsa = {
                'year':       smart.get('year'),
                'make':       smart.get('make'),
                'model':      smart.get('model'),
                'trim':       smart.get('trim'),
                'body_class': smart.get('body_style'),
                '_smart_source':     smart.get('source'),
                '_smart_confidence': float(smart.get('confidence') or 0),
            }
    except Exception as e:
        log.warning('bid %d decode_vin_smart failed: %s', bid_id, e)
        nh_err = f'smart_exception: {e}'
        # Last-ditch: try raw NHTSA so we don't leave canon_* empty
        try:
            from nhtsa_decode import decode_vin as _nh
            nhtsa = _nh(vin, conn=conn) or {}
        except Exception as e2:
            log.warning('bid %d raw NHTSA fallback failed: %s', bid_id, e2)
            nhtsa = {'error': f'all_decoders_failed: {e} | {e2}'}

    nh_year = nhtsa.get('year')
    nh_make = (nhtsa.get('make') or '').strip() or None
    nh_model = (nhtsa.get('model') or '').strip() or None
    nh_trim = (nhtsa.get('trim') or '').strip() or None
    nh_body = (nhtsa.get('body_class') or '').strip() or None
    nh_err = nh_err or nhtsa.get('error')

    # 2) VIN-prefix fallback for trim-blind makes (Porsche etc.)
    try:
        from vin_prefix_trim import lookup_trim_from_vin_prefix
        prefix = lookup_trim_from_vin_prefix(vin)
    except Exception as e:
        log.warning('bid %d vin_prefix lookup failed: %s', bid_id, e)
        prefix = {'trim': None, 'confidence': 0, 'source': None,
                  'note': None, 'body': None}

    pfx_trim = prefix.get('trim')
    pfx_conf = float(prefix.get('confidence') or 0)

    # ── Resolve canonical trim + source ───────────────────────────────────
    canon_trim = None
    canon_source = None
    canon_confidence = 0.5
    smart_src = nhtsa.get('_smart_source')
    smart_conf = nhtsa.get('_smart_confidence') or 0.0
    if nh_trim and pfx_trim and smart_src in (None, 'nhtsa_fallback'):
        # Legacy v1 path — NHTSA + VIN prefix both voted, no Claude signal.
        canon_trim = nh_trim
        canon_source = 'nhtsa+vin_prefix'
        canon_confidence = 0.95
    elif nh_trim and smart_src:
        # decode_vin_smart returned a trim — trust Claude's confidence directly.
        canon_trim = nh_trim
        canon_source = smart_src  # 'claude_sonnet_4_6' / 'cache' / 'nhtsa_fallback' / 'claude_low_conf'
        canon_confidence = max(smart_conf, 0.5)
    elif nh_trim:
        # NHTSA-only path (decode_vin_smart unavailable)
        canon_trim = nh_trim
        canon_source = 'nhtsa'
        canon_confidence = 0.85
    elif pfx_trim:
        canon_trim = pfx_trim
        canon_source = 'vin_prefix'
        canon_confidence = pfx_conf
    elif nh_make and nh_model:
        canon_trim = None
        canon_source = smart_src or 'nhtsa_ymm_only'
        canon_confidence = max(smart_conf, 0.5)
    elif nh_err:
        canon_source = 'decode_error'
        canon_confidence = 0.0

    # ── Persist canonical YMM + trim ─────────────────────────────────────
    # canon_confidence is stored as TEXT in this schema (legacy decision).
    # Cast to str so psycopg2 doesn't try to mix numeric/text in the CASE.
    canon_conf_text = (f'{canon_confidence:.3f}'
                       if canon_confidence is not None else None)
    try:
        # When force=True, overwrite ALL canon_* unconditionally — caller
        # explicitly asked us to re-decode (e.g. backfilling bids whose
        # canon was set by the old NHTSA-only path and is now wrong).
        # When force=False (default), the idempotency CASE only fills
        # NULL/empty cells so we never clobber a high-confidence write
        # from a downstream pipeline (e.g. accutrade_overseer canon_trim).
        if force:
            cur.execute("""
                UPDATE bids SET
                    canon_year       = %s,
                    canon_make       = %s,
                    canon_model      = %s,
                    canon_trim       = %s,
                    canon_source     = %s,
                    canon_confidence = %s,
                    canon_decoded_at = NOW()
                WHERE id = %s
            """, (
                nh_year, nh_make, nh_model,
                canon_trim, canon_source, canon_conf_text,
                bid_id,
            ))
        else:
            cur.execute("""
                UPDATE bids SET
                    canon_year       = COALESCE(%s, canon_year),
                    canon_make       = COALESCE(%s, canon_make),
                    canon_model      = COALESCE(%s, canon_model),
                    canon_trim       = CASE
                        WHEN %s::text IS NOT NULL
                             AND (canon_trim IS NULL OR canon_trim = '')
                        THEN %s::text ELSE canon_trim END,
                    canon_source     = CASE
                        WHEN canon_source IS NULL THEN %s::text
                        ELSE canon_source END,
                    canon_confidence = CASE
                        WHEN canon_confidence IS NULL THEN %s::text
                        ELSE canon_confidence END,
                    canon_decoded_at = COALESCE(canon_decoded_at, NOW())
                WHERE id = %s
            """, (
                nh_year, nh_make, nh_model,
                canon_trim, canon_trim,
                canon_source, canon_conf_text,
                bid_id,
            ))
        # Note: NOT committing here — caller owns the transaction
        out.update({
            'ok': True,
            'canon_year': nh_year,
            'canon_make': nh_make,
            'canon_model': nh_model,
            'canon_trim': canon_trim,
            'canon_source': canon_source,
            'canon_confidence': canon_confidence,
            'reason': 'wrote' if cur.rowcount else 'no_change',
            'nhtsa_error': nh_err,
            'vin_body': prefix.get('body'),
        })
    except Exception as e:
        log.warning('bid %d canon UPDATE failed: %s', bid_id, e)
        out['reason'] = f'update_failed: {e}'

    return out


def canonicalize_bid_vin_async(bid_id: int, db_factory):
    """Spawn a daemon thread that opens its own DB connection, canonicalizes
    the bid, and commits. Used at intake hot path so the API response
    doesn't block on NHTSA's ~200ms call.

    db_factory: callable returning a fresh psycopg2 connection
                (e.g. `app.get_db`).
    """
    import threading

    def _go():
        try:
            with db_factory() as conn:
                result = canonicalize_bid_vin(bid_id, conn)
                conn.commit()
                log.info('canonicalize bid %d: %s', bid_id,
                         {k: v for k, v in result.items()
                          if k in ('canon_make', 'canon_trim', 'canon_source',
                                   'canon_confidence', 'reason')})
        except Exception as e:
            log.warning('canonicalize bid %d thread err: %s', bid_id, e)

    t = threading.Thread(target=_go, daemon=True,
                         name=f'canon-{bid_id}')
    t.start()


# ── Backfill CLI ───────────────────────────────────────────────────────────

if __name__ == '__main__':
    import argparse
    import os
    import sys
    import psycopg2
    import psycopg2.extras

    p = argparse.ArgumentParser(description='Bid VIN canonicalizer')
    p.add_argument('--bid', type=int, help='single bid to canonicalize')
    p.add_argument('--backfill', action='store_true',
                   help='canonicalize every bid where canon_make IS NULL')
    p.add_argument('--limit', type=int, default=None)
    p.add_argument('--force', action='store_true')
    args = p.parse_args()

    DB_URL = os.environ.get(
        'DATABASE_URL',
        'postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale')
    conn = psycopg2.connect(DB_URL,
                            cursor_factory=psycopg2.extras.RealDictCursor)

    if args.bid:
        r = canonicalize_bid_vin(args.bid, conn, force=args.force)
        conn.commit()
        import json
        print(json.dumps(r, indent=2, default=str))
        sys.exit(0)

    if args.backfill:
        cur = conn.cursor()
        sql = ("SELECT id FROM bids WHERE vin IS NOT NULL "
               "AND length(vin) = 17 "
               "AND (canon_make IS NULL OR canon_make = '')")
        if args.limit:
            sql += f" ORDER BY id DESC LIMIT {int(args.limit)}"
        else:
            sql += " ORDER BY id DESC"
        cur.execute(sql)
        bids = [r['id'] for r in cur.fetchall()]
        print(f'[backfill] {len(bids)} bids to canonicalize', flush=True)
        ok = 0
        for i, bid_id in enumerate(bids, 1):
            r = canonicalize_bid_vin(bid_id, conn, force=args.force)
            if r.get('ok'):
                ok += 1
            if i % 25 == 0:
                conn.commit()
                print(f'  [progress] {i}/{len(bids)} ok={ok}', flush=True)
        conn.commit()
        print(f'[backfill] DONE: {ok}/{len(bids)} ok', flush=True)
        sys.exit(0)

    p.print_help()
