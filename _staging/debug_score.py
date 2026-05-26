"""Print fuzzy scores for each candidate against raw inputs — no LLM, no cache."""
import sys
sys.path.insert(0, '/opt/expwholesale/_staging')
import psycopg2, psycopg2.extras
from ymmt_match import _norm_tokens, _score, _load_candidates

DB = 'postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale'
conn = psycopg2.connect(DB, cursor_factory=psycopg2.extras.RealDictCursor)

CASES = [
    (2024, 'MERCEDES-BENZ', 'GLS-Class', 'GLS450 4MATIC'),
    (2024, 'MERCEDES-BENZ', 'Gls',       '63 AMG'),
    (2024, 'MERCEDES-BENZ', 'Gls',       'Maybach Gls 600'),
    (2024, 'MERCEDES-BENZ', 'Gls',       '450 4MATIC'),
    (2023, 'PORSCHE',       '911',       'GT3'),
]

for (y, mk, mdl, tr) in CASES:
    print(f"\n=== {y} {mk} {mdl!r} / {tr!r} ===")
    cands = _load_candidates(conn, y, mk)
    print(f"  loaded {len(cands)} candidates")
    raw_combo = _norm_tokens(f"{mdl} {tr}")
    raw_trim_t = _norm_tokens(tr)
    print(f"  raw_tokens: {raw_combo}")
    scored = []
    for c in cands:
        cand_combo = _norm_tokens(f"{c['model']} {c['trim']}")
        cand_trim = _norm_tokens(c['trim'])
        s = _score(raw_combo, cand_combo, raw_trim_t, cand_trim)
        if c['year'] == y:
            s += 0.02
        scored.append((s, c, cand_combo))
    scored.sort(key=lambda x: -x[0])
    for s, c, ctoks in scored[:6]:
        print(f"  {s:.3f}  id={c['id']:5d} y={c['year']} {c['model']!r} / {c['trim']!r}   tokens={ctoks}")
