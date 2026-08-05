"""DP_VERIFY_2026-08-05 — MillionVerifier pass, invalid-only.

Benchmarked against 12 ground-truth addresses before trusting it:

  result='invalid'  1 of 1 correct. Caught carlbauer@zeigler.com, which has
                    valid MX and simply no mailbox -- something the free DNS
                    check provably cannot see. This is the ONLY verdict acted on.
  result='ok'       marked two CONFIRMED-DEAD tombush.com addresses "good".
                    So 'ok' does not mean deliverable and buys us nothing.
  result='catch_all' 4 of the 5 it flagged risky had already DELIVERED. Acting
                    on this bucket would discard real dealers to remove one
                    dead one, which is worse than the bounce.

Hence: record every verdict for reference, remove ONLY 'invalid'. A verdict we
cannot act on is still worth storing -- it stops the next person re-litigating
this from scratch.

The API key is read from the environment and never written to disk: this file
lands in /opt/expwholesale, which ew_save.sh commits and pushes to GitHub.
"""
import os, sys, time, json, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, '/opt/expwholesale')
if not os.environ.get('DATABASE_URL'):
    # Read from the running unit rather than hardcoding a password into a file
    # that ew_save.sh pushes to GitHub.
    import subprocess as _sp
    _env = _sp.run(['systemctl', 'show', 'expwholesale',
                    '--property=Environment', '--value'],
                   capture_output=True, text=True).stdout
    for _tok in _env.replace('"', '').split():
        if _tok.startswith('DATABASE_URL='):
            os.environ['DATABASE_URL'] = _tok.split('=', 1)[1]
            break
    else:
        sys.exit('DATABASE_URL not set and not readable from the unit')
import dealerprice_network as N

KEY = os.environ.get('MV_KEY') or sys.exit('MV_KEY not set')
API = 'https://api.millionverifier.com/api/v3/'
DRY = '--send' not in sys.argv
HDRS = {'User-Agent': 'Mozilla/5.0 (compatible; EW-DealerPrice/1.0)'}
WORKERS = 4


def credits():
    req = urllib.request.Request(
        'https://api.millionverifier.com/api/v3/credits?api=%s' % KEY,
        headers=HDRS)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read()).get('credits', 0)


def verify(email):
    q = urllib.parse.urlencode({'api': KEY, 'email': email, 'timeout': 20})
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(API + '?' + q, headers=HDRS)
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read())
        except Exception as e:
            if attempt == 2:
                return {'email': email, 'result': 'error', 'error': str(e)[:120]}
            time.sleep(2)


def main():
    db = N._db(); cur = db.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS dp_email_verify (
            email       text PRIMARY KEY,
            result      text,
            resultcode  int,
            subresult   text,
            quality     text,
            is_role     boolean,
            is_free     boolean,
            didyoumean  text,
            raw         jsonb,
            checked_at  timestamptz NOT NULL DEFAULT now())""")
    db.commit()

    # send order, so verification stays ahead of the sender
    cur.execute("""
        SELECT t.id, t.email, t.name
          FROM dp_outreach_targets t
         WHERE t.removed_at IS NULL
           AND NOT EXISTS (SELECT 1 FROM dp_outreach_email e
                            WHERE lower(e.email)=lower(t.email))
           AND NOT EXISTS (SELECT 1 FROM dp_outreach_suppression s
                            WHERE s.email=lower(t.email))
           AND NOT EXISTS (SELECT 1 FROM dp_email_verify v
                            WHERE v.email=lower(t.email))
         ORDER BY COALESCE(t.total_profit,0) ASC, t.id ASC""")
    rows = cur.fetchall()

    have = credits()
    budget = max(0, have - 20)          # their count lags real usage;
                                        # reserve wider than it claims
    todo = rows[:budget]
    print('credits available : %d  (reserving 20)' % have)
    print('addresses pending : %d' % len(rows))
    print('will verify       : %d%s' % (len(todo), '' if len(todo) == len(rows)
          else '   (%d left unverified - out of credits)' % (len(rows) - len(todo))))
    print('mode              : %s' % ('DRY RUN' if DRY else 'LIVE'))
    print('-' * 78)
    if not todo:
        return

    done = {'n': 0}

    def work(r):
        if done.get('stop'):
            return r, {'email': r['email'], 'result': 'skipped'}
        d = verify(r['email']) or {}
        err = (d.get('error') or '').lower()
        if 'credit' in err or 'limit' in err:
            done['stop'] = True
            print('  STOPPING: API reports %r' % d.get('error'), flush=True)
        done['n'] += 1
        if done['n'] % 50 == 0:
            print('  ... %d/%d' % (done['n'], len(todo)), flush=True)
        return r, d

    results = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for r, d in ex.map(work, todo):
            results.append((r, d))

    db2 = N._db(); c2 = db2.cursor()
    tally, invalid = {}, []
    for r, d in results:
        res = d.get('result') or 'error'
        tally[res] = tally.get(res, 0) + 1
        c2.execute("""INSERT INTO dp_email_verify
                        (email,result,resultcode,subresult,quality,
                         is_role,is_free,didyoumean,raw)
                      VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                      ON CONFLICT (email) DO UPDATE SET
                        result=EXCLUDED.result, resultcode=EXCLUDED.resultcode,
                        subresult=EXCLUDED.subresult, quality=EXCLUDED.quality,
                        didyoumean=EXCLUDED.didyoumean, raw=EXCLUDED.raw,
                        checked_at=now()""",
                   (r['email'].lower(), res, d.get('resultcode'),
                    d.get('subresult'), d.get('quality'),
                    bool(d.get('role')), bool(d.get('free')),
                    d.get('didyoumean') or None, json.dumps(d)))
        if res == 'invalid':
            invalid.append((r, d))
    db2.commit()

    print('\nverdicts:')
    for k in sorted(tally, key=lambda x: -tally[x]):
        note = ''
        if k == 'invalid':   note = '  <- the only verdict we act on'
        elif k == 'catch_all': note = '  <- recorded, NOT acted on (4/5 delivered)'
        elif k == 'ok':      note = '  <- recorded, NOT acted on (blessed 2 dead)'
        print('  %-10s %4d%s' % (k, tally[k], note))

    print('\ninvalid (%d):' % len(invalid))
    for r, d in invalid:
        print('  %-38s %-14s %s' % (r['email'][:38], d.get('subresult') or '',
                                    (r['name'] or '')[:26]))

    if invalid and not DRY:
        c2.execute("""UPDATE dp_outreach_targets SET
                        removed_at=now(), removed_by='system',
                        removed_reason='MillionVerifier: invalid ('||%s||')'
                      WHERE lower(email)=ANY(%s) AND removed_at IS NULL""",
                   ('no mailbox', [r['email'].lower() for r, _ in invalid]))
        db2.commit()
        print('\nremoved %d invalid target(s)' % c2.rowcount)
    elif invalid:
        print('\nDRY RUN - nothing removed. Re-run with --send to apply.')

    sug = [(r, d) for r, d in results if d.get('didyoumean')]
    if sug:
        print('\ntypo suggestions (NOT applied - your call):')
        for r, d in sug:
            print('  %-38s -> %s' % (r['email'][:38], d['didyoumean']))
    print('\ncredits remaining : %d' % credits())


main()
