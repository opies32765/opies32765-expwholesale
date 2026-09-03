"""patch_minisite_auction.py — auction comps on the GRANTED mini-site.

Runs ON C1. Idempotent, backs both files up, aborts if an anchor isn't unique.
Marker: AUCTION_COMPS_MINISITE_2026_09_03

OPERATOR DECISION 2026-09-03. I flagged that real hammer prices are the most
sensitive enrichment EW holds — MMR is a number any Manheim subscriber can pull,
but four sold prices on the exact same trim is the number EW is pricing FROM,
and the recipient is the person selling the car. He weighed that and said put it
on the granted mini-site. His call, his data.

Note this does NOT widen who sees the page: /m/<token>/full is already the
granted view, reached only by a token that ENRICHMENT_SMS_DENY_BY_DEFAULT_2026_07_28
gates via _enrichment_sms_allowed(). This adds a card to a page, not a recipient.

Renders ONLY when there is a real same-year/model/trim match with prices. No
match renders nothing at all -- not an empty card, because an empty card on a
customer-facing page invites a question nobody wants to answer.
"""
import io, os, shutil, sys, time

BASE = '/opt/expwholesale'
MARK = 'AUCTION_COMPS_MINISITE_2026_09_03'
STAMP = time.strftime('%Y%m%d-%H%M%S')
APP = os.path.join(BASE, 'app.py')
TPL = os.path.join(BASE, 'templates', 'm_full.html')

APP_ANCHOR = """    return render_template(
        'm_full.html',
        bid=bid, vauto=vauto, accutrade=accutrade, ipacket=ipacket,"""

APP_NEW = """    # """ + MARK + """: real auction sales for the same year/model/trim.
    # Wrapped -- a failure must never break the customer's page.
    _ac_mini = None
    try:
        from comps_lookup import for_bid as _ac_for_bid
        _ac_mini = _ac_for_bid(dict(bid))
    except Exception as _ac_e:
        print('[m_full] auction_comps err: %s' % _ac_e, flush=True)

    return render_template(
        'm_full.html',
        auction_comps=_ac_mini,
        bid=bid, vauto=vauto, accutrade=accutrade, ipacket=ipacket,"""

CSS_ANCHOR = "  details.card.books   { --accent: #94a3b8; }   /* slate */"
CSS_NEW = (CSS_ANCHOR +
           "\n  details.card.auction { --accent: #38bdf8; }   /* sky */")

TPL_ANCHOR = '<details class="card vauto">'

TPL_NEW = """{# """ + MARK + """ — real completed auction sales, same year/model/trim.
   Renders only on a genuine match; no match renders nothing at all. #}
{% if auction_comps and auction_comps.ok and auction_comps.sold %}
<details class="card auction" open>
  <summary>
    <span class="title">Recent Auction Sales</span>
    <span class="summary-text">{{ auction_comps.sold|length }} like {% if auction_comps.sold|length == 1 %}car{% else %}cars{% endif %} sold</span>
  </summary>
  <div class="body">
    <div style="font-size:11.5px;color:#94a3b8;margin-bottom:10px;line-height:1.5">
      Same year, model and trim. These are hammer prices actually paid at auction
      in the last six weeks &mdash; completed sales, not estimates.
    </div>
    {% for c in auction_comps.sold %}
    <div class="row">
      <span class="lbl">
        {{ c.year }} {{ c.model }}{% if c.style %} {{ c.style }}{% endif %}<br>
        <span style="font-size:11px;color:#64748b">
          {{ '{:,}'.format(c.odometer|int) }} mi{% if c.grade %} &middot; grade {{ c.grade }}{% endif %}
          &middot; {{ c.auction_label }} {{ c.sale_date }}
        </span>
      </span>
      <span class="val">${{ '{:,}'.format(c.price|int) }}</span>
    </div>
    {% endfor %}
  </div>
</details>
{% endif %}

""" + TPL_ANCHOR


def apply(path, edits):
    src = io.open(path, encoding='utf-8').read()
    if MARK in src:
        print('  %-14s already patched' % os.path.basename(path))
        return True
    for old, _ in edits:
        if src.count(old) != 1:
            print('  %-14s ANCHOR NOT UNIQUE (%d): %r' %
                  (os.path.basename(path), src.count(old), old[:50]))
            return False
    shutil.copy2(path, path + '.bak.' + STAMP + '-preauctionmini')
    for old, new in edits:
        src = src.replace(old, new, 1)
    io.open(path, 'w', encoding='utf-8').write(src)
    print('  %-14s patched' % os.path.basename(path))
    return True


ok = apply(APP, [(APP_ANCHOR, APP_NEW)])
ok = apply(TPL, [(CSS_ANCHOR, CSS_NEW), (TPL_ANCHOR, TPL_NEW)]) and ok
sys.exit(0 if ok else 1)
