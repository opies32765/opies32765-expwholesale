"""patch_bid_detail_speed.py — make bid_detail render faster.

Replaces the SELECT * FROM vauto_lookups with an explicit column list that
excludes the heavy rbook_competitive_set + manheim_transactions JSONB
columns (~152 KB combined for popular vehicles, eats 120ms+ on TOAST
decompression). Reads the new market_intel_cached column instead.

Lazy-fills market_intel_cached for old rows that don't have it yet
(one-shot extra query on first view, then fast forever).
"""
import sys

APP_PY = '/opt/expwholesale/app.py'
with open(APP_PY, 'r', encoding='utf-8') as fp:
    src = fp.read()

if 'market_intel_cached' in src:
    print('Already patched — no-op.')
    sys.exit(0)

# Replace the heavy SELECT * + the live market_intel compute block.
ANCHOR = """    # vAuto lookup data
    cur.execute("SELECT * FROM vauto_lookups WHERE bid_id = %s", (bid_id,))
    vauto_data = cur.fetchone()"""

NEW_VAUTO_SELECT = """    # vAuto lookup data — explicit columns, drops heavy JSONB blobs
    # (rbook_competitive_set + manheim_transactions) which are only used
    # for market_intel and are now read from market_intel_cached.
    cur.execute(\"\"\"
        SELECT id, bid_id, vin, rbook, mmr, kbb, kbb_com, jd_power, black_book,
               title_status, price_rank, adj_pct_market,
               carfax_screenshot, autocheck_screenshot, carfax_share_url,
               looked_up_at, appraisal_url,
               rbook_completed_at, manheim_completed_at,
               enrichment_state, market_intel_cached,
               api_carfax, api_price_guides, api_refreshed_at
        FROM vauto_lookups WHERE bid_id = %s
    \"\"\", (bid_id,))
    vauto_data = cur.fetchone()"""

ANCHOR_MI = """    # Compute market_intel for the rBook Retail Comps card + MMR closest sale.
    # Uses the same fn the AI assessment uses, on the same data — so the
    # numbers shown match what the model sees.
    market_intel = None
    try:
        from market_intel import compute_market_intel as _mi
        def _maybe_parse(x):
            if isinstance(x, str):
                try:
                    import json as _j
                    return _j.loads(x)
                except Exception:
                    return None
            return x
        _manheim = _maybe_parse((vauto_data or {}).get('manheim_transactions')) if vauto_data else None
        _rbook   = _maybe_parse((vauto_data or {}).get('rbook_competitive_set')) if vauto_data else None
        market_intel = _mi(
            {'year': bid.get('year'), 'make': bid.get('make'),
             'model': bid.get('model'), 'mileage': bid.get('mileage'),
             'vin': bid.get('vin')},
            _manheim, _rbook, None,  # buyer_intel optional here
        )"""

NEW_MI = """    # Read cached market_intel from vauto_lookups (populated when rbook
    # completes via vauto_enrichment.kick_direct_enrichment, or lazily
    # on first view of older bids).
    market_intel = None
    try:
        from market_intel import compute_market_intel as _mi
        def _maybe_parse(x):
            if isinstance(x, str):
                try:
                    import json as _j
                    return _j.loads(x)
                except Exception:
                    return None
            return x

        if vauto_data and vauto_data.get('market_intel_cached'):
            cached = vauto_data['market_intel_cached']
            market_intel = _maybe_parse(cached) if isinstance(cached, str) else cached
        elif vauto_data:
            # Lazy fill: compute live and persist for next render.
            # One-shot extra fetch of the heavy JSONB columns.
            _db2 = get_db()
            _cur2 = _db2.cursor()
            _cur2.execute(\"SELECT rbook_competitive_set, manheim_transactions \"
                          \"FROM vauto_lookups WHERE bid_id=%s\", (bid_id,))
            _extra = _cur2.fetchone()
            if _extra:
                _manheim = _maybe_parse(_extra.get('manheim_transactions'))
                _rbook   = _maybe_parse(_extra.get('rbook_competitive_set'))
                market_intel = _mi(
                    {'year': bid.get('year'), 'make': bid.get('make'),
                     'model': bid.get('model'), 'mileage': bid.get('mileage'),
                     'vin': bid.get('vin')},
                    _manheim, _rbook, None,
                )
                if market_intel:
                    import json as _j
                    _cur2.execute(\"UPDATE vauto_lookups SET market_intel_cached=%s::jsonb \"
                                  \"WHERE bid_id=%s\",
                                  (_j.dumps(market_intel), bid_id))
                    _db2.commit()
            _db2.close()"""

failed = []
for old, new, label in [(ANCHOR, NEW_VAUTO_SELECT, 'vauto_lookups SELECT'),
                         (ANCHOR_MI, NEW_MI, 'market_intel block')]:
    if old in src:
        src = src.replace(old, new, 1)
        print(f'  patched {label}')
    else:
        failed.append(label)

if failed:
    print(f'ERROR: anchors not found for: {failed}')
    sys.exit(1)

with open(APP_PY, 'w', encoding='utf-8') as fp:
    fp.write(src)
print('Done.')
