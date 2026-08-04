#!/usr/bin/env python3
"""dp_name_map v3 — final greeting map. Email first, LSL only when it CHECKS OUT.

v2 trusted lsl_suppliers over the email address. Measuring the 34 cases where
both sources produced a name showed that was backwards -- they disagreed 20.6%
of the time, and the email was right in most of them:

    brian.bralich@       email Brian       LSL "BrianCheis Bralich"  corrupted
    mohsen@              email Mohsen      LSL "Moshen Moshen"       typo
    shaun.camay@         email Shaun       LSL "Shuan Camay"         typo
    victor.fray@         email Victor      LSL "Ken Fray"            another person
    christopher.mcconnell@ email Christopher LSL "bruce mcconnell"   another person

Two distinct failure modes there: LSL has data-entry corruption, AND its
contact is the ROOFTOP's primary contact, who is often not the owner of the
mailbox we hold. Both argue the same way -- the email address is the person we
are actually writing to.

So:

  1. derived        the email itself yields a given name. Highest trust: it is
                    the mailbox we are mailing.
  2. lsl_validated  the email is initial+surname (dalbrecht) AND the LSL
                    contact's initial+surname reconstructs it exactly
                    (David Albrecht -> d+albrecht == dalbrecht). Two
                    independent sources agreeing on the same human.
  3. none           fallback greeting.

lsl_name WITHOUT that reconstruction is deliberately NOT used. It is what
produced "Harlan" for bos@marshallgoldman.com -- a rooftop contact stapled onto
an unrelated mailbox. Greeting a dealer by the wrong name on a cold email is
worse than not greeting them at all.
"""
import io
import os
import re
import sys
import csv
import psycopg2
import psycopg2.extras

sys.path.insert(0, '/opt/expwholesale')
from dp_name_map import classify
from dp_name_map2 import first_name_of, NOT_A_PERSON

FALLBACK = ''          # empty => the template drops the name entirely


def lsl_reconstructs(local, full):
    """True when LSL's contact rebuilds this exact local-part as
    initial+surname (or first+initial, or first.last)."""
    s = re.sub(r'[^a-z]', '', (local or '').lower())
    parts = [re.sub(r'[^a-z]', '', p.lower())
             for p in re.split(r'[\s,]+', (full or '')) if p.strip()]
    parts = [p for p in parts if len(p) >= 2]
    if not s or len(parts) < 2:
        return False
    first, last = parts[0], parts[-1]
    return s in (first[:1] + last,          # dalbrecht
                 first + last[:1],          # davida
                 first + last,              # davidalbrecht
                 last + first[:1])          # albrechtd


def main():
    dsn = os.environ.get('DATABASE_URL')
    if not dsn:
        print('DATABASE_URL is not set. Refusing to guess credentials.')
        return 2
    c = psycopg2.connect(dsn)
    cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT t.id, t.name AS dealership, t.email, t.total_profit,
               sn.primary_contact AS lsl
          FROM dp_outreach_targets t
          LEFT JOIN LATERAL (
                SELECT s.primary_contact FROM lsl_suppliers s
                 WHERE lower(regexp_replace(s.name,'[^a-z0-9]','','gi'))
                     = lower(regexp_replace(t.name,'[^a-z0-9]','','gi'))
                   AND COALESCE(s.primary_contact,'') <> ''
                 LIMIT 1) sn ON TRUE
         WHERE t.removed_at IS NULL AND t.email IS NOT NULL AND t.email <> ''
         ORDER BY t.total_profit DESC NULLS LAST""")
    rows = cur.fetchall()
    c.close()

    counts, out = {}, []
    for r in rows:
        local = (r['email'] or '').split('@')[0]
        bucket, derived = classify(local)
        first, src = '', 'none:' + bucket

        if bucket in ('dot_known', 'single_known') and derived:
            first, src = derived, 'derived'
        elif r['lsl'] and lsl_reconstructs(local, r['lsl']):
            n = first_name_of(r['lsl'])
            if n:
                first, src = n, 'lsl_validated'

        counts[src] = counts.get(src, 0) + 1
        out.append({'id': r['id'], 'dealership': r['dealership'],
                    'email': r['email'], 'first_name': first or FALLBACK,
                    'source': src, 'lsl_contact': r['lsl'] or '',
                    'total_profit': r['total_profit']})

    path = '/tmp/dp_name_map.csv'
    with io.open(path, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['id', 'dealership', 'email',
                                          'first_name', 'source', 'lsl_contact',
                                          'total_profit'])
        w.writeheader(); w.writerows(out)

    total = len(rows)
    named = sum(1 for o in out if o['first_name'])
    print('TOTAL STAGED: %d\n' % total)
    print('%-20s %6s %7s' % ('SOURCE', 'COUNT', 'SHARE'))
    print('-' * 40)
    for k in sorted(counts, key=lambda x: -counts[x]):
        print('%-20s %6d %6.1f%%' % (k, counts[k], 100.0 * counts[k] / total))
    print('-' * 40)
    print('WITH A NAME : %d  (%.1f%%)' % (named, 100.0 * named / total))
    print('FALLBACK    : %d  (%.1f%%)' % (total - named,
                                          100.0 * (total - named) / total))

    print('\n=== lsl_validated: LSL rebuilt the address exactly ===')
    n = 0
    for o in out:
        if o['source'] == 'lsl_validated' and n < 10:
            print('  %-30s %-9s <- %s' % (o['email'][:29], o['first_name'],
                                          o['lsl_contact']))
            n += 1
    print('\n=== top 12 targets ===')
    print('%-36s %-27s %-9s %s' % ('DEALERSHIP', 'EMAIL', 'NAME', 'SOURCE'))
    for o in out[:12]:
        print('%-36s %-27s %-9s %s' % ((o['dealership'] or '')[:35],
                                       (o['email'] or '')[:26],
                                       o['first_name'] or '(fallback)',
                                       o['source']))
    print('\nwrote %s' % path)


if __name__ == '__main__':
    sys.exit(main())
