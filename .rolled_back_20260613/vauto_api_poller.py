# vauto_api_poller.py — Phase 2 autonomous poller for the vAuto API cutover.
# For bids on the vauto_api_canary allowlist that still need vAuto, assembles +
# writes the record via the API and enqueues AccuTrade/iPacket-only solo runs.
# GATED behind VAUTO_API_ON; does nothing when the flag is absent OR the
# allowlist is empty. Each bid is fully guarded (browser fallback on any error).
#   python3 vauto_api_poller.py once    # single pass (testing)
#   python3 vauto_api_poller.py         # loop (systemd service)
import os, sys, time
sys.path.insert(0, '/opt/expwholesale')
from urllib.parse import urlparse, parse_qs
import psycopg2.extras
import shadow_worker as sw
import vauto_api_writer as W

FLAG = "/opt/expwholesale/VAUTO_API_ON"
POLL = float(os.environ.get('VAUTO_API_POLL', '5'))


def _session_and_apid(conn):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT label FROM vauto_session ORDER BY refreshed_at DESC LIMIT 1")
    row = cur.fetchone()
    sess = sw.load_session(conn, row['label']) if row else None
    cur.execute("SELECT appraisal_url FROM vauto_lookups WHERE appraisal_url LIKE '%Id=%' ORDER BY looked_up_at DESC LIMIT 1")
    arow = cur.fetchone()
    apid = parse_qs(urlparse(arow['appraisal_url']).query).get('Id', [None])[0] if arow else None
    return sess, apid


def find_canary_bids(conn):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT b.id, b.vin, b.mileage, b.year, b.make, b.model
        FROM bids b JOIN vauto_api_canary c ON c.bid_id = b.id
        WHERE b.vin IS NOT NULL AND length(b.vin)=17 AND COALESCE(b.mileage,0)>0
          AND b.vin_invalid_reason IS NULL
          AND (b.needs_verification_at IS NULL OR b.needs_verification_cleared_at IS NOT NULL)
          AND NOT EXISTS (SELECT 1 FROM vauto_lookups v WHERE v.bid_id=b.id
                           AND (v.raw_json IS NOT NULL OR v.appraisal_url='__not_found__'))
        ORDER BY b.created_at DESC LIMIT 5
    """)
    return cur.fetchall()


def one_pass(conn):
    if not os.path.exists(FLAG):
        return 0
    bids = find_canary_bids(conn); conn.rollback()
    if not bids:
        return 0
    sess, apid = _session_and_apid(conn); conn.rollback()
    if not sess:
        print("[vauto-api] no vauto_session — skip", flush=True)
        return 0
    n = 0
    for bid in bids:
        if W.process_canary_bid(conn, bid, sess, apid):
            n += 1
    return n


def main():
    conn = sw.db(); conn.autocommit = False
    mode = sys.argv[1] if len(sys.argv) > 1 else 'loop'
    if mode == 'once':
        print("processed", one_pass(conn), "canary bid(s)")
        return
    print("[vauto-api] poller started (poll=%ss, flag=%s)" % (POLL, FLAG), flush=True)
    while True:
        try:
            one_pass(conn)
        except Exception as e:
            print("[vauto-api] pass err:", e, flush=True)
            try: conn.rollback()
            except Exception: pass
        time.sleep(POLL)


main()
