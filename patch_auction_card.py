"""patch_auction_card.py — wire the Auction Activity card into the bid page.

Runs ON C1. Idempotent: refuses to double-apply. Backs up both files first.
Marker: AUCTION_COMPS_CARD_2026_08_29
"""
import io, os, shutil, sys, time

BASE = '/opt/expwholesale'
STAMP = time.strftime('%Y%m%d-%H%M%S')
MARK = 'AUCTION_COMPS_CARD_2026_08_29'

APP = os.path.join(BASE, 'app.py')
TPL = os.path.join(BASE, 'templates', 'bid.html')

APP_ANCHOR = ("    _rendered = render_template('bid.html', bid=bid, photos=photos, "
              "show_sources=show_sources,\n")

APP_NEW = (
    "    # " + MARK + ": Edge Pipeline like-car comps. This must NEVER gate or\n"
    "    # delay the listing -- for_bid() swallows every exception and returns an\n"
    "    # empty dict, same rule as LISTING_NEVER_WAITS_ON_ASSESSMENT_2026_06_18.\n"
    "    try:\n"
    "        from comps_lookup import for_bid as _ac_for_bid\n"
    "        auction_comps = _ac_for_bid(bid)\n"
    "    except Exception as _ace:\n"
    "        print('[auction-comps] bid=%s err=%s' % (bid_id, _ace), flush=True)\n"
    "        auction_comps = {'sold': [], 'avail': [], 'ns_rate': None, 'ok': False}\n"
    + APP_ANCHOR +
    "                                auction_comps=auction_comps,\n"
)

TPL_ANCHOR = "      {% if show_sources and (rb.n_visible or rb.retail_median or rb.stocking_report) %}"

TPL_NEW = """      {# """ + MARK + """ — Edge Pipeline like-car comps.
         Renders the instant the local query returns; never waits on ANY
         enrichment leg or on the AI assessment. #}
      {% if auction_comps and auction_comps.ok %}
      <div class="card" id="auction-comps-card" style="border-color:#0ea5e9;flex-shrink:0">
        <div class="card-title" style="display:flex;align-items:center;justify-content:space-between">
          <span class="card-title" style="margin:0">Auction Activity</span>
          <span style="font:11px ui-monospace,monospace;color:#64748b">
            EDGE Pipeline{% if auction_comps.ns_rate is not none %} ·
            {{ (auction_comps.ns_rate * 100) | round(1) }}% no-sale{% endif %}
          </span>
        </div>

        {% if auction_comps.avail %}
        {% set a = auction_comps.avail[0] %}
        <div style="padding:12px 14px;border-bottom:1px solid #1e293b">
          <div style="font:600 10px/1 ui-monospace,monospace;letter-spacing:.13em;
                      text-transform:uppercase;color:#94a3b8;margin-bottom:9px">
            Like car — ran, did not sell
          </div>
          <div style="border:1px solid #14532d;border-radius:5px;padding:11px 12px;
                      background:linear-gradient(180deg,rgba(34,197,94,.07),transparent 70%)">
            <div style="font-size:14px;font-weight:600">
              {{ a.year }} {{ a.make }} {{ a.model }}{% if a.style %} <span style="color:#22c55e">{{ a.style }}</span>{% endif %}
            </div>
            <div style="font:12px/1.7 ui-monospace,monospace;color:#94a3b8;margin-top:5px">
              <b style="color:#e2e8f0">{{ '{:,}'.format(a.odometer) }} mi</b>
              {%- if a.d_miles is not none %} ({{ '{:+,}'.format(a.d_miles) }} vs yours){% endif %}
              {%- if a.grade %} · grade <b style="color:#e2e8f0">{{ a.grade }}</b>{% endif %}
              {%- if a.color %} · {{ a.color }}{% endif %}<br>
              {{ a.auction_slug }} · {{ a.sale_date }} · stock {{ a.stock_no }}
              {%- if a.vin %}<br>VIN {{ a.vin }}{% endif %}
            </div>
          </div>
        </div>
        {% endif %}

        {% if auction_comps.sold %}
        <div style="padding:12px 14px">
          <div style="font:600 10px/1 ui-monospace,monospace;letter-spacing:.13em;
                      text-transform:uppercase;color:#94a3b8;margin-bottom:9px">
            Recent like sales
          </div>
          {% for c in auction_comps.sold %}
          <div style="display:flex;justify-content:space-between;gap:12px;align-items:center;
                      padding:8px 0;{% if not loop.last %}border-bottom:1px solid #1e293b;{% endif %}">
            <div style="min-width:0">
              <div style="font-size:13px;font-weight:550">
                {{ c.year }} {{ c.model }}{% if c.style %} <span style="color:#0ea5e9">{{ c.style }}</span>{% endif %}
              </div>
              <div style="font:11.5px/1.6 ui-monospace,monospace;color:#94a3b8">
                {{ '{:,}'.format(c.odometer) }} mi ·
                {% if c.grade %}gr {{ c.grade }}{% else %}no CR{% endif %} ·
                {{ c.auction_slug }} {{ c.sale_date }}
              </div>
            </div>
            <div style="text-align:right;white-space:nowrap">
              <div style="font:650 15px ui-monospace,monospace;font-variant-numeric:tabular-nums">
                ${{ '{:,}'.format(c.price) }}
              </div>
              <div style="font:11px ui-monospace,monospace;color:#64748b;margin-top:3px">
                {% if c.d_year == 0 %}same yr{% else %}{{ '{:+d}'.format(c.d_year) }} yr{% endif %}{% if c.d_miles is not none %} · {{ '{:+,}'.format(c.d_miles) }} mi{% endif %}
              </div>
            </div>
          </div>
          {% endfor %}
        </div>
        {% endif %}
      </div>
      {% endif %}

"""


def patch(path, anchor, new, label):
    src = io.open(path, encoding='utf-8').read()
    if MARK in src:
        print('  %-10s already patched, skipping' % label)
        return False
    if src.count(anchor) != 1:
        print('  %-10s ANCHOR NOT UNIQUE (%d hits) -- ABORT' % (label, src.count(anchor)))
        return None
    bak = path + '.bak.' + STAMP + '-preauctioncard'
    shutil.copy2(path, bak)
    io.open(path, 'w', encoding='utf-8').write(src.replace(anchor, new, 1))
    print('  %-10s patched  (backup: %s)' % (label, os.path.basename(bak)))
    return True


ok_app = patch(APP, APP_ANCHOR, APP_NEW, 'app.py')
ok_tpl = patch(TPL, TPL_ANCHOR, TPL_NEW + TPL_ANCHOR, 'bid.html')
if ok_app is None or ok_tpl is None:
    sys.exit(1)
print('done.')
