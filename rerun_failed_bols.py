"""BOL_REPROCESS_2026_08_07 — re-run the queued no_vin/needs_match BOLs through the
FIXED _bol_extract (9B + salvage parse). Uses the production functions, not a copy.
Dry-run by default; pass --apply to write."""
import sys, os, json, re
sys.path.insert(0, '/opt/expwholesale')
os.chdir('/opt/expwholesale')
import recon_routes as R          # pulls in app -> local_brain_shim -> the 9B
import psycopg2, psycopg2.extras

DSN = os.environ.get('DATABASE_URL')
if not DSN:
    # no embedded credential here — this file is committed to GitHub
    import subprocess as _sp
    _env = _sp.run(['systemctl', 'show', 'expwholesale', '-p', 'Environment', '--value'],
                   capture_output=True).stdout.decode()
    DSN = _env.split('DATABASE_URL=')[1].split()[0]
APPLY = '--apply' in sys.argv

db = psycopg2.connect(DSN)
cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute("""SELECT id, subject, status, body_text, attachments
                 FROM recon_inbound_bol
                WHERE status IN ('no_vin','needs_match') ORDER BY id""")
rows = cur.fetchall()
print('queued failures: %d   mode=%s\n' % (len(rows), 'APPLY' if APPLY else 'DRY-RUN'))

fixed = still = 0
for r in rows:
    # BOL_GROUNDED_VIN_2026_08_28 — mirror the route: scan the SUBJECT too, and track
    # which VINs/stocks are GROUNDED (found verbatim in the sender's own text or in the
    # document's text layer) so an unmatched row can still display what we actually read.
    vins, stocks, label = set(), set(), ''
    grounded, gstocks = set(), set()
    _hdr = ((r['subject'] or '') + ' ' + (r['body_text'] or '')).upper()
    for m in re.findall(r'[A-HJ-NPR-Z0-9]{17}', _hdr):
        if R._vin_ok(m): vins.add(m); grounded.add(m)
    for m in re.findall(r'\bLL\d{3,6}\b', _hdr): stocks.add(m); gstocks.add(m)
    saved = sorted(r['attachments'] or [],
                   key=lambda a: 0 if 'pdf' in (a.get('content_type') or '').lower() else 1)
    for s in saved:
        p = s.get('path')
        if not p or not os.path.exists(p): continue
        try: data = open(p, 'rb').read()
        except Exception: continue
        try: ex = R._bol_extract(data, s.get('content_type') or '')
        except Exception as e:
            print('  id=%-3s EXTRACT ERR %s' % (r['id'], e)); continue
        vins.update(ex['vins']); stocks.update(ex['stocks'])
        grounded.update(ex.get('grounded') or []); gstocks.update(ex.get('gstocks') or [])
        if not label and ex['label']: label = ex['label']
    unit = R._bol_match_unit(cur, sorted(vins), sorted(stocks))
    ymm = False
    if not unit:
        unit, _nc = R._bol_match_ymm(cur, label)
        if unit:
            ymm = True
        elif _nc > 1:
            print('  (YMM ambiguous: %d candidates for %r)' % (_nc, label))
    status = 'matched' if unit else ('needs_match' if (vins or stocks) else 'no_vin')
    tag = 'MATCHED%s -> unit %s %s' % (' by YMM' if ymm else '', unit['id'], unit.get('stock_no') or '') if unit \
          else ('vins=%s grounded=%s stocks=%s' % (sorted(vins), sorted(grounded), sorted(stocks))
                if (vins or stocks) else 'still nothing')
    print('  id=%-3s %-12s -> %-12s %s' % (r['id'], r['status'], status, tag))
    if unit: fixed += 1
    else: still += 1
    if APPLY:
        cur.execute("""UPDATE recon_inbound_bol SET status=%s, matched_unit_id=%s,
                        extracted_vin=%s, extracted_stock=%s,
                        extracted_label=COALESCE(NULLIF(%s,''), extracted_label)
                       WHERE id=%s""",
                    (status, (unit['id'] if unit else None),
                     # BOL_GROUNDED_VIN_2026_08_28 — persist the unit's VIN when matched,
                     # otherwise a DOCUMENT-GROUNDED one. The 9B fabricates check-digit-valid
                     # VINs (2FRDKGVX9ZDAR62PU etc.), but a fabrication never appears in the
                     # text layer, so grounding is what separates the two — not the match.
                     ((unit['vin'] if unit else None) or (sorted(grounded)[0] if grounded else None)),
                     ((unit.get('stock_no') if unit else None)
                      or (sorted(gstocks)[0] if gstocks else None)), label[:200], r['id']))
        if unit:
            cur.execute("SELECT 1 FROM recon_photos WHERE url LIKE %s LIMIT 1",
                        ('/api/recon/bol/%d/doc/%%' % r['id'],))
            if not cur.fetchone():
                R._bol_attach(cur, unit, r['id'], r['subject'] or '', saved)
            cur.execute("INSERT INTO recon_notes (unit_id, step_id, author, body, category) "
                        "VALUES (%s,%s,'email_bol',%s,'general')",
                        (unit['id'], unit['current_step_id'],
                         '\U0001f4c4 BOL re-processed after extraction fix — auto-matched & attached'))
            try: cur.execute("UPDATE recon_units SET has_bol=TRUE WHERE id=%s", (unit['id'],))
            except Exception: pass
if APPLY:
    db.commit(); print('\nCOMMITTED.')
else:
    db.rollback(); print('\n(dry run — nothing written)')
print('newly matched=%d  still unmatched=%d' % (fixed, still))
db.close()
