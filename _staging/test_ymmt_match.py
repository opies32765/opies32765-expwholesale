"""Quick sanity test for ymmt_match.resolve_ymmt.
Runs against the live ymmt_catalog (read-only). Cache table not required —
cache calls will silently no-op via their exception path.
"""
import sys, os, re
sys.path.insert(0, '/opt/expwholesale/_staging')
sys.path.insert(0, '/opt/expwholesale')
# Load ANTHROPIC_API_KEY from systemd unit (avoids cmd-line credential leak)
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
conn = psycopg2.connect(DB, cursor_factory=psycopg2.extras.RealDictCursor)

CASES = [
    # (label, year, make, raw_model, raw_trim, expected_trim_contains)
    ('Bid 2025 (GLS450)',          2024, 'Mercedes-Benz', 'GLS-Class', 'GLS450 4MATIC',          '450'),
    ('TXTC inv 63 AMG',            2024, 'Mercedes-Benz', 'Gls',        '63 AMG',                'AMG'),
    ('TXTC inv Gls 63 AMG',        2024, 'Mercedes-Benz', 'Gls',        'Gls 63 AMG',            'AMG'),
    ('TXTC inv Maybach Gls 600',   2024, 'Mercedes-Benz', 'Gls',        'Maybach Gls 600',       'Maybach'),
    ('TXTC inv 450 4MATIC bare',   2024, 'Mercedes-Benz', 'Gls',        '450 4MATIC',            '450'),
    ('Mercedes G63 in G-CLASS',    2023, 'Mercedes-Benz', 'G-CLASS',    'CLASS G 63 AMG',        '63'),
    ('Mercedes G550 model-only',   2023, 'Mercedes-Benz', 'G550',       '',                      '550'),
    ('S-Class S 580',              2024, 'Mercedes-Benz', 'S-CLASS',    'CLASS S 580',           '580'),
    ('Porsche 911 GT3',            2023, 'Porsche',       '911',        'GT3',                   'GT3'),
    ('Porsche 911 Turbo S Cabrio', 2023, 'Porsche',       '911',        'Turbo S Cabriolet',     'Turbo'),
    ('Porsche 911 Carrera 4S',     2023, 'Porsche',       '911',        'Carrera 4S',            'Carrera'),
    ('BMW M3 Competition',         2023, 'BMW',           'M3',         'Competition',           'Competition'),
    ('Ambiguous Gls no trim',      2024, 'Mercedes-Benz', 'Gls',        '',                      None),
]

print(f"{'CASE':<32} {'CATALOG MATCH':<40} {'CONF':<6} {'SRC':<22} {'OK'}")
print('-' * 110)
for (label, y, mk, mdl, tr, expect) in CASES:
    r = resolve_ymmt(y, mk, mdl, tr, db_conn=conn)
    match_str = (f"{r['model']!s} / {r['trim']!s}" if r['ymmt_id'] else 'NONE')
    ok = '?'
    if expect is None:
        ok = '✓' if r['ymmt_id'] is None else '✗ should be ambiguous'
    elif r['trim'] and expect.lower() in r['trim'].lower():
        ok = '✓'
    elif r['ymmt_id'] is None:
        ok = '✗ no match'
    else:
        ok = f'✗ got {r["trim"]!r}, want contains {expect!r}'
    print(f"{label:<32} {match_str[:39]:<40} {r['confidence']:<6} {r['source']:<22} {ok}")
    if r.get('reason'):
        print(f"  reason: {r['reason'][:90]}")
