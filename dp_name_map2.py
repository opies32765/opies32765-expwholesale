#!/usr/bin/env python3
"""dp_name_map.py v2 — greeting for every staged outreach address.

v1 derived names from the email local-part alone and could only cover 25.5%,
because 58.7% of the list is initial+surname (dalbrecht, bnorton, cwegmann)
where the first name is simply not present in the string.

v2 adds the source that actually matters: lsl_suppliers. EW has BOUGHT FROM
these dealers, so LSL already records a real human per supplier -- 265 rows,
every one with a primary_contact, plus primary_contact_email. That is a
recorded name, not an inference from an email address.

Sources, highest confidence first:

  1. lsl_email  target.email == lsl_suppliers.primary_contact_email
                An exact address match to a named human. As good as it gets.
  2. lsl_name   dealership name matches lsl_suppliers.name
                The recorded contact for that rooftop. Note this is the
                dealership's contact, not necessarily the owner of the address
                we hold -- good, but one step weaker than an email match.
  3. derived    first.last@ or a single token that is a known given name.
  4. none       fallback greeting. Includes all 432 initial+surname addresses,
                role accounts and company mailboxes.

Corroboration seen while building this: LSL says Duval Ford -> "Rickey Bomar"
and the address on file is rickey.bomarjr@. Two independent sources agreeing
is a good sign the LSL contacts are current rather than stale.

Capitalisation in LSL is inconsistent ("tony Coletti", "PARIN SHAH", "Rickey
Bomar " with a trailing space), so names are normalised before use.
"""
import io
import os
import re
import sys
import csv
import psycopg2
import psycopg2.extras

sys.path.insert(0, '/opt/expwholesale')
from dp_name_map import NAMES, ROLE, COMPANYISH, classify   # reuse v1 logic

# tokens that mean the "contact" is not a person
NOT_A_PERSON = ('llc', 'inc', 'corp', 'motors', 'auto', 'sales', 'group',
                'dealer', 'department', 'dept', 'office', 'desk', 'team',
                'accounting', 'title', 'unknown', 'n/a', 'na', 'none')


def first_name_of(full):
    """First name from an LSL primary_contact, or '' if it is not a person."""
    s = (full or '').strip()
    if not s:
        return ''
    low = s.lower()
    if any(t in low for t in NOT_A_PERSON):
        return ''
    tok = re.split(r'[\s,]+', s)[0]
    tok = re.sub(r'[^A-Za-z\'-]', '', tok)
    if len(tok) < 2:
        return ''
    # Mc/O' handled by title(); "PARIN" -> "Parin", "tony" -> "Tony"
    return tok[:1].upper() + tok[1:].lower() if tok.isupper() or tok.islower() else tok


def main():
    dsn = os.environ.get('DATABASE_URL')
    if not dsn:
        print('DATABASE_URL is not set. Refusing to guess credentials.')
        return 2
    c = psycopg2.connect(dsn)
    cur = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        SELECT t.id, t.name AS dealership, t.email, t.total_profit,
               se.primary_contact AS lsl_by_email,
               sn.primary_contact AS lsl_by_name
          FROM dp_outreach_targets t
          LEFT JOIN lsl_suppliers se
                 ON lower(se.primary_contact_email) = lower(t.email)
                AND COALESCE(se.primary_contact,'') <> ''
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

    counts = {}
    out = []
    for r in rows:
        first, src = '', 'none'
        n = first_name_of(r['lsl_by_email'])
        if n:
            first, src = n, 'lsl_email'
        else:
            n = first_name_of(r['lsl_by_name'])
            if n:
                first, src = n, 'lsl_name'
            else:
                b, d = classify((r['email'] or '').split('@')[0])
                if b in ('dot_known', 'single_known'):
                    first, src = d, 'derived'
                else:
                    src = 'none:' + b
        counts[src] = counts.get(src, 0) + 1
        out.append({'id': r['id'], 'dealership': r['dealership'],
                    'email': r['email'], 'first_name': first, 'source': src,
                    'lsl_contact': r['lsl_by_email'] or r['lsl_by_name'] or '',
                    'total_profit': r['total_profit']})

    path = '/tmp/dp_name_map.csv'
    with io.open(path, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['id', 'dealership', 'email',
                                          'first_name', 'source', 'lsl_contact',
                                          'total_profit'])
        w.writeheader()
        w.writerows(out)

    total = len(rows)
    named = sum(1 for o in out if o['first_name'])
    print('TOTAL STAGED: %d\n' % total)
    print('%-18s %6s %7s' % ('SOURCE', 'COUNT', 'SHARE'))
    print('-' * 40)
    for k in sorted(counts, key=lambda x: -counts[x]):
        print('%-18s %6d %6.1f%%' % (k, counts[k], 100.0 * counts[k] / total))
    print('-' * 40)
    print('WITH A NAME   : %d  (%.1f%%)' % (named, 100.0 * named / total))
    print('FALLBACK      : %d  (%.1f%%)' % (total - named,
                                            100.0 * (total - named) / total))
    print('\n=== 12 highest-value targets ===')
    print('%-38s %-26s %-9s %s' % ('DEALERSHIP', 'EMAIL', 'NAME', 'SOURCE'))
    for o in out[:12]:
        print('%-38s %-26s %-9s %s' % ((o['dealership'] or '')[:37],
                                       (o['email'] or '')[:25],
                                       o['first_name'] or '-', o['source']))
    print('\nwrote %s' % path)


if __name__ == '__main__':
    sys.exit(main())
