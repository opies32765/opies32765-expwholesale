"""patch_9b_auction_comps.py — feed real auction sales into the 9B assessment.

Runs ON C1. Idempotent, backs both files up, aborts if an anchor isn't unique.
Marker: AUCTION_COMPS_PROMPT_2026_09_03

Design constraints, all learned the hard way this week:
  * Comps are a FEATURE, never a LABEL. The target stays what the car really did.
  * The section states plainly that these are COMPLETED SALES, so the model can
    weight them against book values, which are explicitly "reference, not anchor".
  * If there are no comps the section says so in one line. It must never render a
    placeholder that reads like data -- the 9B invents corroborating detail for
    absent fields (it cited a clean Carfax it was never given).
  * A KEY QUESTION is added, because injection is not answer: the CWK 4B ignored
    facts it was handed. If the model won't use it, we need to see that.
"""
import io, os, shutil, sys, time

BASE = '/opt/expwholesale'
MARK = 'AUCTION_COMPS_PROMPT_2026_09_03'
STAMP = time.strftime('%Y%m%d-%H%M%S')

AI = os.path.join(BASE, 'ai_assessment_v2.py')
APP = os.path.join(BASE, 'app.py')

# ---------------------------------------------------------------- section fn
SECTION_FN = '''

def _auction_comps_section(auction_comps: dict | None) -> str:
    """''' + MARK + ''': real hammer prices for the SAME year/model/trim.

    Source: auction_comps on C1, built from EDGE Pipeline post-sale reports for
    13 Florida auctions. Every figure is a COMPLETED SALE at a physical auction,
    not a valuation. That is why this sits above BOOK VALUES in the prompt.
    """
    ac = auction_comps or {}
    rows = ac.get('sold') or []
    out = ['\\u2550\\u2550\\u2550 RECENT AUCTION SALES \\u2014 same year, model and trim (COMPLETED TRANSACTIONS) \\u2550\\u2550\\u2550']
    if not rows:
        out.append('  (no same-trim auction sale found in the last 6 weeks)')
        return '\\n'.join(out)
    for r in rows:
        try:
            bits = [f"  {r['year']} {r.get('make','')} {r.get('model','')}"
                    f"{(' ' + r['style']) if r.get('style') else ''}".rstrip()]
            bits.append(f"{int(r['odometer']):,} mi")
            bits.append(f"grade {r['grade']}" if r.get('grade') is not None else 'no CR')
            bits.append(f"{r.get('auction_label') or r.get('auction_slug')} {r['sale_date']}")
            line = ' | '.join(bits) + f"  \\u2192 SOLD ${int(r['price']):,}"
            if r.get('d_miles') is not None:
                line += f"  ({int(r['d_miles']):+,} mi vs subject)"
            out.append(line)
        except (KeyError, TypeError, ValueError):
            continue
    ns = ac.get('ns_rate')
    if ns is not None:
        out.append(f"  Market resistance: {ns * 100:.1f}% of this make/year "
                   f"ran and did NOT sell.")
    out.append('  These are hammer prices actually paid, not estimates.')
    return '\\n'.join(out)
'''

# ------------------------------------------------------------------- anchors
AI_EDITS = [
    # 1. template slot, directly under the market stack and ABOVE book values
    ('{market_stack}\n',
     '{market_stack}\n\n{auction_comps_section}\n'),
    # 2. a key question, so we can see whether it actually uses them
    ('3. Does this car\'s condition',
     "3. What did IDENTICAL cars (same year, model and trim) actually bring at auction in the last 6 weeks? "
     "Those are completed transactions, not valuations \\u2014 weight them accordingly against the book values, "
     "and say in your reasoning whether they moved your number.\n"
     "4. Does this car's condition"),
    # 3. signature
    ("                 voice_master: dict | None = None) -> str:",
     "                 voice_master: dict | None = None,\n"
     "                 auction_comps: dict | None = None) -> str:"),
    # 4. format kwarg
    ("        market_stack=_market_stack(market_intel, dealer_intel, buyer_intel,\n"
     "                                   subject_miles=bid.get('mileage')),",
     "        market_stack=_market_stack(market_intel, dealer_intel, buyer_intel,\n"
     "                                   subject_miles=bid.get('mileage')),\n"
     "        auction_comps_section=_auction_comps_section(auction_comps),"),
]

APP_EDITS = [
    ("    if _v2_build_prompt:\n        prompt = _v2_build_prompt(\n",
     "    # " + MARK + ": real auction sales for the same year/model/trim.\n"
     "    # Wrapped so a failure can NEVER block an assessment -- comps are\n"
     "    # supporting evidence, not a required leg.\n"
     "    _auction_comps = None\n"
     "    try:\n"
     "        from comps_lookup import for_bid as _ac_for_bid\n"
     "        _auction_comps = _ac_for_bid(dict(bid))\n"
     "        if _auction_comps and _auction_comps.get('sold'):\n"
     "            print('[ASSESS] Bid %s auction comps: n=%d' %\n"
     "                  (bid_id, len(_auction_comps['sold'])), flush=True)\n"
     "    except Exception as _ac_e:\n"
     "        print('[ASSESS] auction_comps err: %s' % _ac_e, flush=True)\n"
     "\n"
     "    if _v2_build_prompt:\n        prompt = _v2_build_prompt(\n"),
    ("            voice_master=_voice_master,\n        )",
     "            voice_master=_voice_master,\n"
     "            auction_comps=_auction_comps,\n        )"),
]


def apply(path, edits, extra_append=None):
    src = io.open(path, encoding='utf-8').read()
    if MARK in src:
        print('  %-22s already patched' % os.path.basename(path))
        return True
    for old, _new in edits:
        if src.count(old) != 1:
            print('  %-22s ANCHOR NOT UNIQUE (%d): %r' %
                  (os.path.basename(path), src.count(old), old[:60]))
            return False
    shutil.copy2(path, path + '.bak.' + STAMP + '-preauctionprompt')
    for old, new in edits:
        src = src.replace(old, new, 1)
    if extra_append:
        src += extra_append
    io.open(path, 'w', encoding='utf-8').write(src)
    print('  %-22s patched' % os.path.basename(path))
    return True


ok = apply(AI, AI_EDITS, extra_append=SECTION_FN)
ok = apply(APP, APP_EDITS) and ok
sys.exit(0 if ok else 1)
