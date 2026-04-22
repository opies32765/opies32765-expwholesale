#!/usr/bin/env python3
"""One-time backfill: probe URL of every currently-missing car across all
dealers. Flip to sold if combined confidence >= threshold. Logs flips.

Safe to re-run — only acts on status='missing' rows."""
import json
import requests

from dealer_scanner import (
    probe_sold_signals, sold_confidence, SOLD_CONFIDENCE_THRESHOLD, get_conn,
)

sess = requests.Session()
sess.headers['User-Agent'] = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
)

flipped = 0
probed = 0

with get_conn() as conn, conn.cursor() as cur:
    cur.execute(
        "SELECT id, dealer_id, vin, url, missing_scans, "
        "       year || ' ' || make || ' ' || model AS ymm "
        "FROM dealer_inventory WHERE status='missing'"
    )
    rows = cur.fetchall()
    print(f'Probing {len(rows)} missing cars across all dealers...')
    for r in rows:
        url = r['url']
        if not url:
            continue
        probed += 1
        sigs = probe_sold_signals(url, sess)
        base_conf = min(0.8, 0.25 * max(1, r['missing_scans'] or 1))
        base_sig = {'type': 'missing_from_scan',
                    'detail': f"consecutive={r['missing_scans']}",
                    'confidence': base_conf}
        all_sigs = [base_sig] + sigs
        score = sold_confidence(all_sigs)
        detail = f'  inv={r["id"]} d={r["dealer_id"]} [{r["ymm"]}] score={score:.2f}'
        if sigs:
            detail += f' +probe={[s["type"] for s in sigs]}'
        print(detail)
        if score >= SOLD_CONFIDENCE_THRESHOLD:
            cur.execute(
                "UPDATE dealer_inventory SET status='sold', "
                "sold_at=COALESCE(sold_at, NOW()), sold_confidence=%s, "
                "sold_signals=%s::jsonb, updated_at=NOW() WHERE id=%s",
                (score, json.dumps(all_sigs), r['id'])
            )
            for s in sigs:
                cur.execute(
                    "INSERT INTO dealer_sold_signals "
                    "(dealer_id, inventory_id, vin, signal_type, signal_detail, "
                    "confidence, scan_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s,NULL)",
                    (r['dealer_id'], r['id'], r['vin'] or None,
                     s['type'], s.get('detail'), s['confidence'])
                )
            flipped += 1
    conn.commit()

print(f'\nDone. Probed {probed}, flipped {flipped} to sold.')
