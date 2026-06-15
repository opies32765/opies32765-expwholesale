# notify_bids.py — watches for newly-landed bids and pushes a notification to the EW
# app. Polls only (does NOT touch intake/assessment). On first run it baselines to the
# current max bid id so it never blasts old bids.
#
# Watermark rules (skip-free, noise-free):
#   * ready  = bid has a VIN or make  -> notify, advance watermark
#   * old    = >3 min and still unready -> advance past it (give up, don't notify)
#   * recent & unready -> STOP advancing; re-check next poll (lets a brand-new bid
#                         get enriched before we either notify or skip it)
import os, sys, time
sys.path.insert(0, '/opt/expwholesale')
import psycopg2, psycopg2.extras
from ew_push import send_push

DB = os.environ.get('DATABASE_URL', 'postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale')
STATE = '/opt/expwholesale/.app_push_last_bid'
POLL = float(os.environ.get('APP_PUSH_POLL', '15'))
GIVE_UP_MIN = int(os.environ.get('APP_PUSH_GIVEUP_MIN', '3'))


def last_id():
    try:
        return int(open(STATE).read().strip())
    except Exception:
        return 0


def save(n):
    open(STATE, 'w').write(str(n))


def poll():
    c = psycopg2.connect(DB); cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    last = last_id()
    if last == 0:  # baseline on first run — don't notify for pre-existing bids
        cur.execute("SELECT COALESCE(MAX(id),0) m FROM bids")
        base = cur.fetchone()['m']; save(base); c.close()
        print('baselined at bid %s' % base, flush=True); return
    cur.execute("""SELECT id, year, make, model, vin,
                          (vin IS NOT NULL OR make IS NOT NULL) AS ready,
                          (created_at < now() - interval '%s minutes') AS old
                   FROM bids WHERE id > %%s ORDER BY id ASC LIMIT 50""" % GIVE_UP_MIN, (last,))
    rows = cur.fetchall(); c.close()
    newlast = last
    for r in rows:
        if r['ready']:
            ymm = ' '.join(str(x) for x in [r['year'], r['make'], r['model']] if x).strip() or 'New vehicle'
            send_push('New bid landed', ymm, {'bidId': r['id']})
            newlast = r['id']
            print('pushed bid %s: %s' % (r['id'], ymm), flush=True)
        elif r['old']:
            newlast = r['id']  # stale & still unidentifiable — stop waiting on it
            print('skipped stale unready bid %s' % r['id'], flush=True)
        else:
            break  # brand-new bid not enriched yet — hold here, re-check next poll
    if newlast != last:
        save(newlast)


if __name__ == '__main__':
    print('[app-push-notifier] started (poll=%ss, giveup=%smin)' % (POLL, GIVE_UP_MIN), flush=True)
    while True:
        try:
            poll()
        except Exception as e:
            print('poll err:', e, flush=True)
        time.sleep(POLL)
