"""Backfill ymmt_id across dealer_inventory + bids by calling resolve_ymmt
on every row that doesn't already have ymmt_resolved_at set.

Usage:
  python3 backfill_ymmt.py             # dry-run, prints counts only
  python3 backfill_ymmt.py --apply     # writes ymmt_id + ymmt_resolved_at + ymmt_confidence
  python3 backfill_ymmt.py --apply --limit 200   # do at most 200 rows

Resumable: re-running picks up where the last run left off because we
filter on ymmt_resolved_at IS NULL.

The resolve cache de-duplicates work — identical (year, make, raw_model, raw_trim)
strings only hit fuzzy/LLM once. Fuzzy is pure SQL + Python (free); LLM is
Sonnet 4.6 (~$0.001/call) for the cases fuzzy can't resolve cleanly.
"""
import sys, os, re, argparse, time
sys.path.insert(0, '/opt/expwholesale/_staging')
sys.path.insert(0, '/opt/expwholesale')

# Pull ANTHROPIC_API_KEY from the service unit (no shell exposure)
if not os.environ.get('ANTHROPIC_API_KEY'):
    try:
        unit = open('/etc/systemd/system/expwholesale.service').read()
        m = re.search(r'^Environment=ANTHROPIC_API_KEY=(\S+)', unit, re.M)
        if m:
            os.environ['ANTHROPIC_API_KEY'] = m.group(1)
    except Exception as e:
        print(f'(env load skipped: {e})')

import psycopg2, psycopg2.extras
from ymmt_match import resolve_ymmt

DB = 'postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale'
BATCH = 100


def backfill_table(conn, table, id_col='id', limit=None, apply=False):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(f"""
        SELECT {id_col} AS id, year, make, model, trim
          FROM {table}
         WHERE ymmt_resolved_at IS NULL
           AND year IS NOT NULL
           AND make IS NOT NULL
         ORDER BY {id_col}
         {f'LIMIT {int(limit)}' if limit else ''}
    """)
    rows = cur.fetchall()
    cur.close()

    print(f'\n=== {table}: {len(rows)} unresolved rows ===')
    if not rows:
        return {'total': 0, 'resolved': 0, 'null': 0, 'fuzzy': 0,
                'llm': 0, 'cache': 0, 'errors': 0}

    stats = {'total': len(rows), 'resolved': 0, 'null': 0,
             'fuzzy': 0, 'llm': 0, 'cache': 0, 'errors': 0,
             'no_candidates': 0}
    t0 = time.time()

    # New cursor for resolver's read traffic
    resolver_cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    update_cur = conn.cursor()

    for i, row in enumerate(rows, 1):
        try:
            r = resolve_ymmt(row['year'], row['make'], row['model'],
                             row['trim'], db_conn=conn)
        except Exception as e:
            stats['errors'] += 1
            print(f"  [err] id={row['id']} {row['year']} {row['make']} "
                  f"{row['model']!r}/{row['trim']!r}: {e}")
            continue

        src = r.get('source') or '?'
        if src == 'fuzzy' or src == 'fuzzy_degraded':
            stats['fuzzy'] += 1
        elif src == 'claude_sonnet_4_6':
            stats['llm'] += 1
        elif src == 'cache':
            stats['cache'] += 1
        elif src == 'no_candidates':
            stats['no_candidates'] += 1

        if r.get('ymmt_id'):
            stats['resolved'] += 1
        else:
            stats['null'] += 1

        if apply:
            update_cur.execute(
                f"UPDATE {table} SET ymmt_id=%s, ymmt_resolved_at=NOW(), "
                f"ymmt_confidence=%s WHERE {id_col}=%s",
                (r.get('ymmt_id'), float(r.get('confidence') or 0), row['id']))

        if i % BATCH == 0:
            if apply:
                conn.commit()
            elapsed = time.time() - t0
            rate = i / max(elapsed, 0.001)
            print(f"  [{i:5d}/{len(rows)}] resolved={stats['resolved']} "
                  f"null={stats['null']} fuzzy={stats['fuzzy']} "
                  f"llm={stats['llm']} cache={stats['cache']} "
                  f"errs={stats['errors']} ({rate:.1f} rows/s)")

    if apply:
        conn.commit()

    elapsed = time.time() - t0
    print(f"  done: {stats['total']} rows in {elapsed:.1f}s — "
          f"resolved={stats['resolved']} ({100*stats['resolved']/max(stats['total'],1):.0f}%) "
          f"null={stats['null']} "
          f"[fuzzy={stats['fuzzy']} llm={stats['llm']} cache={stats['cache']} "
          f"no_candidates={stats['no_candidates']} err={stats['errors']}]")
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true',
                    help='Actually write to dealer_inventory + bids (default = dry-run)')
    ap.add_argument('--limit', type=int, default=None,
                    help='Cap rows per table (good for sampling)')
    ap.add_argument('--tables', default='dealer_inventory,bids',
                    help='Comma-separated tables to backfill')
    args = ap.parse_args()

    if not args.apply:
        print('*** DRY RUN — pass --apply to write changes ***')

    conn = psycopg2.connect(DB)
    conn.autocommit = False

    for table in args.tables.split(','):
        table = table.strip()
        backfill_table(conn, table, limit=args.limit, apply=args.apply)


if __name__ == '__main__':
    main()
