"""grade_trim_schema.py — grade trim_schema against the independent 245-pair corpus.

Runs ON C1. Test scaffolding only; changes nothing.

The grader calls fn(a, b) with two bare strings, but trim_schema resolves per
(make, model, trim) — so the wrapper recovers make/model for each pair from the
corpus itself, keyed on (make, model, a, b).
"""
import sys
import trim_schema as ts
import trim_match_eval as ev

# find the corpus object whatever it is called
corpus = None
for name in ('CORPUS', '_CORPUS', 'PAIRS', 'ROWS'):
    if hasattr(ev, name):
        corpus = getattr(ev, name)
        break
if corpus is None:
    for name in dir(ev):
        v = getattr(ev, name)
        if isinstance(v, (list, tuple)) and v and isinstance(v[0], dict) \
                and 'a' in v[0] and 'b' in v[0]:
            corpus = v
            print('corpus found as %s' % name)
            break
if corpus is None:
    print('COULD NOT FIND CORPUS. module attrs:',
          [n for n in dir(ev) if not n.startswith('__')][:40])
    sys.exit(1)

print('corpus rows: %d' % len(corpus))
print('row keys   : %s' % sorted(corpus[0].keys()))

MK = next((k for k in corpus[0] if k.lower() in ('make', 'canon_make')), None)
MD = next((k for k in corpus[0] if k.lower() in ('model', 'canon_model')), None)
print('make key=%r  model key=%r' % (MK, MD))

ctx = {}
for r in corpus:
    ctx[(r['a'], r['b'])] = (r.get(MK) if MK else None, r.get(MD) if MD else None)

vocab = ts.load()


def candidate(a, b):
    mk, md = ctx.get((a, b), (None, None))
    matched, _br, _cr = ts.match_bid_to_comp(
        vocab,
        {'trim': a, 'make': mk, 'model': md},
        {'style': b, 'make': mk, 'model': md,
         'canon_make': mk, 'canon_model': md},
    )
    return matched


ev.grade(candidate, name='trim_schema', show_rows=12)
