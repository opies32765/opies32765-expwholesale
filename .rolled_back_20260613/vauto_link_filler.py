# vauto_link_filler.py — fills the "Saved vAuto on <date>" appraisal link for bids
# that have vAuto data but no link yet (new API-cutover bids AND older gaps). Runs
# the production thin-save ONE at a time (headless ProVision on C1), so it never
# spawns a swarm of browsers and never blocks the data poller / any assessment.
# Retries up to MAX_ATTEMPTS then marks '__api_no_link__' so un-appraisable cars
# stop cycling. Gated on VAUTO_API_ON (same killswitch as the poller).
#   venv/bin/python vauto_link_filler.py once   # single bid (testing)
#   venv/bin/python vauto_link_filler.py        # loop (systemd service)
import os, sys, time
sys.path.insert(0, '/opt/expwholesale')
import psycopg2.extras
import shadow_worker as sw
import vauto_api_writer as W

FLAG = "/opt/expwholesale/VAUTO_API_ON"
POLL = float(os.environ.get('VAUTO_LINK_POLL', '10'))
MAX_ATTEMPTS = int(os.environ.get('VAUTO_LINK_MAX_ATTEMPTS', '3'))


def _session(conn):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT label FROM vauto_session ORDER BY refreshed_at DESC LIMIT 1")
    row = cur.fetchone()
    return sw.load_session(conn, row['label']) if row else None


def next_bid(conn):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    # ONLY bids the API cutover itself wrote (source='api_cutover_v1') — never
    # historical/browser bids. No backfill: the filler just attaches the saved
    # link to new cutover bids that don't have one yet.
    cur.execute("""
      SELECT b.id, b.vin, b.mileage, b.year, b.make, b.model
      FROM bids b JOIN vauto_lookups v ON v.bid_id=b.id
      WHERE v.raw_json->>'source' = 'api_cutover_v1'
        AND length(b.vin)=17 AND COALESCE(b.mileage,0)>0
        AND v.appraisal_url IS NULL AND COALESCE(v.link_attempts,0) < %s
      ORDER BY v.looked_up_at DESC LIMIT 1
    """, (MAX_ATTEMPTS,))
    return cur.fetchone()


def one(conn):
    if not os.path.exists(FLAG):
        return False
    bid = next_bid(conn); conn.rollback()
    if not bid:
        return False
    sess = _session(conn); conn.rollback()
    if not sess:
        print("[link-filler] no vauto_session — skip", flush=True)
        return False
    linked = W.attach_appraisal_link(conn, bid, sess)
    if not linked:
        # count the attempt; after MAX_ATTEMPTS, mark so it stops cycling
        with conn.cursor() as c:
            c.execute("""UPDATE vauto_lookups
                         SET link_attempts = COALESCE(link_attempts,0)+1,
                             appraisal_url = CASE WHEN COALESCE(link_attempts,0)+1 >= %s
                                                  THEN '__api_no_link__' ELSE appraisal_url END
                         WHERE bid_id=%s""", (MAX_ATTEMPTS, bid['id']))
        conn.commit()
    return True


def main():
    conn = sw.db(); conn.autocommit = False
    mode = sys.argv[1] if len(sys.argv) > 1 else 'loop'
    if mode == 'once':
        print("processed a bid" if one(conn) else "nothing to do")
        return
    print("[link-filler] started (poll=%ss, max_attempts=%s)" % (POLL, MAX_ATTEMPTS), flush=True)
    while True:
        try:
            did = one(conn)
        except Exception as e:
            print("[link-filler] err:", e, flush=True)
            try:
                conn.rollback()
            except Exception:
                pass
            did = False
        time.sleep(1 if did else POLL)


if __name__ == "__main__":
    main()
