# vauto_api_writer.py — Phase 2 of the vAuto API cutover.
# Assembles a full vauto_lookups record from the BFF API, renders the Carfax +
# AutoCheck snapshots headless on C1, and (in write mode) writes the live
# vauto_lookups row. GATED behind the VAUTO_API_ON flag. On ANY failure it
# writes nothing, so the browser worker still handles the bid (fallback).
#
#   python3 vauto_api_writer.py <bid_id>            # DRY: assemble+render, no DB write
#   python3 vauto_api_writer.py <bid_id> write      # backup + live write vauto_lookups
#   python3 vauto_api_writer.py <bid_id> restore    # restore from backup
import os, sys, json, time
sys.path.insert(0, '/opt/expwholesale')
from urllib.parse import urlparse, parse_qs
import psycopg2, psycopg2.extras
from psycopg2.extras import Json
import shadow_worker as sw
from vauto_bff_direct import (fetch_price_guides, fetch_competitive_set,
                              parse_competitive_set, fetch_manheim_transactions)
try:
    from market_intel import compute_market_intel
except Exception:
    compute_market_intel = None

FLAG = "/opt/expwholesale/VAUTO_API_ON"
VAUTO_REPORTS_DIR = "/opt/expwholesale/vauto_reports"
CHROME = "/root/.cache/ms-playwright/chromium_headless_shell-1217/chrome-headless-shell-linux64/chrome-headless-shell"
BFF2 = 'https://slot2.bff.megazord.vauto.app.coxautoinc.com'


def _grade(block, disp, cond):
    for p in (block or {}).get('pricings') or []:
        if p.get('disposition') == disp and p.get('condition') == cond:
            return (p.get('basePrice') or 0) + (p.get('odometerAdjustment') or 0) + (p.get('historicalAdjustment') or 0)
    return None


def _rbook(parsed):
    rows = (parsed or {}).get('rows') or []
    prices = sorted([int(r['price']) for r in rows if isinstance(r, dict) and r.get('price')])
    return prices[len(prices) // 2] if prices else None


def _title_from_carfax(rep):
    if not rep:
        return None
    if rep.get('hasTotalLoss') or rep.get('hasFrameDamage') or rep.get('hasAccidentIndicators'):
        return 'accident'
    if rep.get('hasCleanTitle'):
        return 'clean'
    return None


def assemble(http, bid, apid_sess):
    vin, miles = bid['vin'], bid.get('mileage')
    veh, opts = sw.decode_vin(http, vin, miles)
    if not veh:
        return None
    veh['odometer'] = int(miles or 0)
    ck, hd = http.clean_cookies, dict(http.headers)
    pg = fetch_price_guides(veh, ck, hd, appraisal_id=apid_sess) or {}
    mhb = pg.get('manheim') or {}
    parsed_comp = parse_competitive_set(fetch_competitive_set(veh, ck, hd, appraisal_id=apid_sess))
    mh = fetch_manheim_transactions(veh, ck, hd, appraisal_id=apid_sess)
    rep, share = sw.fetch_carfax(http, vin)
    ac_html = None
    try:
        r = http.get(BFF2 + '/api/autocheck/getReport?vin=' + vin, timeout=25)
        if r.status_code == 200:
            ac_html = r.text
    except Exception:
        pass
    mi = None
    if compute_market_intel:
        try:
            mi = compute_market_intel({'year': bid.get('year'), 'make': bid.get('make'),
                                       'model': bid.get('model'), 'mileage': miles, 'vin': vin},
                                      mh, parsed_comp, None)
        except Exception:
            mi = None
    return {
        'bid_id': bid['id'], 'vin': vin, 'market_intel': mi,
        'mmr': mhb.get('averageAuctionPrice'), 'rbook': _rbook(parsed_comp),
        'black_book': _grade(pg.get('blackBook'), 'Wholesale', 'Average'),
        'kbb': sw.book_val(pg.get('kbb') or {}, 'kbb'),
        'kbb_com': sw.book_val(pg.get('kbbOnline') or {}, 'kbbOnline'),
        'jd_power': sw.book_val(pg.get('nada') or {}, 'nada'),
        'title_status': _title_from_carfax(rep),
        'carfax_json': rep, 'carfax_share_url': share,
        'manheim_transactions': mh, 'rbook_competitive_set': parsed_comp,
        'autocheck_html': ac_html,
    }


def _render(out, url=None, html=None):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        br = pw.chromium.launch(headless=True, executable_path=CHROME, args=['--no-sandbox'])
        try:
            pg = br.new_page(viewport={'width': 1100, 'height': 900})
            if url:
                try:
                    pg.goto(url, wait_until='networkidle', timeout=45000)
                except Exception:
                    pg.goto(url, wait_until='load', timeout=45000)
            else:
                pg.set_content(html, wait_until='load', timeout=30000)
            pg.wait_for_timeout(2500)
            pg.screenshot(path=out, full_page=True)
        finally:
            br.close()
    return out


def render_snapshots(rec):
    vin, ts = rec['vin'], int(time.time())
    if rec.get('carfax_share_url'):
        fn = "carfax_api_%s_%d.png" % (vin, ts)
        try:
            _render(os.path.join(VAUTO_REPORTS_DIR, fn), url=rec['carfax_share_url'])
            rec['carfax_screenshot'] = '/vauto_reports/' + fn
        except Exception as e:
            print("  carfax render err:", e)
    if rec.get('autocheck_html'):
        fn = "autocheck_api_%s_%d.png" % (vin, ts)
        try:
            _render(os.path.join(VAUTO_REPORTS_DIR, fn), html=rec['autocheck_html'])
            rec['autocheck_screenshot'] = '/vauto_reports/' + fn
        except Exception as e:
            print("  autocheck render err:", e)
    return rec


def write_row(cur, rec):
    raw = {'source': 'api_cutover_v1', 'mmr': rec.get('mmr'), 'rbook': rec.get('rbook'),
           'black_book': rec.get('black_book'), 'kbb': rec.get('kbb')}
    cur.execute("""INSERT INTO vauto_lookups
        (bid_id, vin, rbook, black_book, mmr, kbb, kbb_com, jd_power, title_status,
         carfax_screenshot, autocheck_screenshot, carfax_json, manheim_transactions,
         rbook_competitive_set, market_intel_cached, raw_json,
         rbook_completed_at, manheim_completed_at, looked_up_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, NOW(), NOW(), NOW())
        ON CONFLICT (bid_id) DO UPDATE SET
          vin=EXCLUDED.vin, rbook=EXCLUDED.rbook, black_book=EXCLUDED.black_book, mmr=EXCLUDED.mmr,
          kbb=EXCLUDED.kbb, kbb_com=EXCLUDED.kbb_com, jd_power=EXCLUDED.jd_power, title_status=EXCLUDED.title_status,
          carfax_screenshot=EXCLUDED.carfax_screenshot, autocheck_screenshot=EXCLUDED.autocheck_screenshot,
          carfax_json=EXCLUDED.carfax_json, manheim_transactions=EXCLUDED.manheim_transactions,
          rbook_competitive_set=EXCLUDED.rbook_competitive_set, market_intel_cached=EXCLUDED.market_intel_cached,
          raw_json=EXCLUDED.raw_json, rbook_completed_at=NOW(), manheim_completed_at=NOW(), looked_up_at=NOW()""",
      (rec['bid_id'], rec['vin'], rec.get('rbook'), rec.get('black_book'), rec.get('mmr'),
       rec.get('kbb'), rec.get('kbb_com'), rec.get('jd_power'), rec.get('title_status'),
       rec.get('carfax_screenshot'), rec.get('autocheck_screenshot'),
       Json(rec['carfax_json']) if rec.get('carfax_json') else None,
       Json(rec['manheim_transactions']) if rec.get('manheim_transactions') else None,
       Json(rec['rbook_competitive_set']) if rec.get('rbook_competitive_set') else None,
       Json(rec['market_intel']) if rec.get('market_intel') else None,
       Json(raw)))


def enqueue_solo(cur, bid_id):
    """Trigger the worker's AccuTrade-only + iPacket-only solo runs (vAuto is now
    provided by the API). Mirrors the accutrade_retry_at marker on both legs."""
    cur.execute("UPDATE bids SET accutrade_retry_at=NOW(), accutrade_retry_claimed_at=NULL, "
                "ipacket_retry_at=NOW(), ipacket_retry_claimed_at=NULL WHERE id=%s", (bid_id,))


def process_canary_bid(conn, bid, sess, apid):
    """Full API-vAuto path for one canary bid: assemble -> render -> write
    vauto_lookups -> enqueue AccuTrade/iPacket-only. On ANY failure it rolls back
    (writes nothing) so the browser worker still handles the bid. Returns True on
    success. The assess gate fires on its own when accu+ipkt complete."""
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        http = sw.make_http(sess)
        rec = assemble(http, bid, apid)
        if not rec or not rec.get('mmr'):
            conn.rollback()
            print("[vauto-api] bid %s no MMR -> browser fallback" % bid['id'], flush=True)
            return False
        render_snapshots(rec)
        write_row(cur, rec)
        enqueue_solo(cur, bid['id'])
        conn.commit()
        print("[vauto-api] bid %s WROTE vauto (mmr=%s rbook=%s) + enqueued accu/ipkt-only"
              % (bid['id'], rec.get('mmr'), rec.get('rbook')), flush=True)
        return True
    except Exception as e:
        conn.rollback()
        print("[vauto-api] bid %s FAILED -> browser fallback: %s: %s" % (bid['id'], type(e).__name__, e), flush=True)
        return False


def main():
    bid_id = int(sys.argv[1]); mode = sys.argv[2] if len(sys.argv) > 2 else 'dry'
    conn = sw.db(); conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    if mode == 'restore':
        cur.execute("DELETE FROM vauto_lookups WHERE bid_id=%s", (bid_id,))
        cur.execute("INSERT INTO vauto_lookups SELECT * FROM vauto_lookups_writer_bak WHERE bid_id=%s", (bid_id,))
        cur.execute("DELETE FROM vauto_lookups_writer_bak WHERE bid_id=%s", (bid_id,))
        conn.commit()
        print("RESTORED bid", bid_id); return

    cur.execute("SELECT id, vin, mileage, year, make, model FROM bids WHERE id=%s", (bid_id,))
    bid = cur.fetchone()
    cur.execute("SELECT label FROM vauto_session ORDER BY refreshed_at DESC LIMIT 1")
    sess = sw.load_session(conn, cur.fetchone()['label'])
    cur.execute("SELECT appraisal_url FROM vauto_lookups WHERE appraisal_url LIKE '%Id=%' ORDER BY looked_up_at DESC LIMIT 1")
    apid = parse_qs(urlparse(cur.fetchone()['appraisal_url']).query).get('Id', [None])[0]

    t0 = time.time()
    http = sw.make_http(sess)
    rec = assemble(http, bid, apid)
    if not rec:
        print("ASSEMBLE FAILED — fallback to browser"); return
    render_snapshots(rec)
    print("bid %s %s %s %s %dmi  (%.1fs)" % (bid_id, bid['year'], bid['make'], bid['model'], bid['mileage'], time.time() - t0))
    print("  mmr=%s rbook=%s black_book=%s kbb=%s kbb_com=%s jd=%s title=%s" % (
        rec['mmr'], rec['rbook'], rec['black_book'], rec['kbb'], rec['kbb_com'], rec['jd_power'], rec['title_status']))
    print("  manheim_txns=%s carfax_shot=%s autocheck_shot=%s" % (
        len((rec['manheim_transactions'] or {}).get('transactions') or []),
        rec.get('carfax_screenshot'), rec.get('autocheck_screenshot')))

    if mode == 'write':
        if not os.path.exists(FLAG):
            print("VAUTO_API_ON flag absent — refusing live write"); return
        cur.execute("CREATE TABLE IF NOT EXISTS vauto_lookups_writer_bak (LIKE vauto_lookups INCLUDING ALL)")
        cur.execute("DELETE FROM vauto_lookups_writer_bak WHERE bid_id=%s", (bid_id,))
        cur.execute("INSERT INTO vauto_lookups_writer_bak SELECT * FROM vauto_lookups WHERE bid_id=%s", (bid_id,))
        write_row(cur, rec)
        conn.commit()
        print("LIVE WRITE done (backup saved to vauto_lookups_writer_bak). Restore: ... %s restore" % bid_id)
    else:
        print("DRY RUN — no DB write. Snapshots rendered to vauto_reports/.")


if __name__ == "__main__":
    main()
