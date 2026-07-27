#!/usr/bin/env python3
"""render_badge_check.py — render the REAL badge block with REAL scored bids, for a screenshot.

Pulls the badge markup straight out of templates/bid.html (between the SCREENER_BADGE marker
and its {% endif %}) so this cannot drift from what actually ships, scores live bids with the
DEPLOYED ew_screener_lib, and writes an HTML file styled like the bid card.

Verifies: the Jinja expression resolves (no undefined attribute after the field rename), the
label, the formatted number, the colour, and the risk-flag line. It does NOT exercise the
full authenticated page — the app log is the check for that.
"""
import os, re, sys
sys.path.insert(0, "/opt/expwholesale")
import psycopg2, psycopg2.extras
from jinja2 import Environment
import ew_screener_lib as scr

TPL = "/opt/expwholesale/templates/bid.html"
src = open(TPL, encoding="utf-8").read()
i = src.find("SCREENER_BADGE_2026_07_26")
i = src.rfind("{#", 0, i)
# The badge contains a NESTED {% if screener.risk_flag %}, so the first {% endif %} closes
# the inner block, not the outer one. Walk forward keeping the if/endif depth balanced.
start = src.find("{% if screener %}", i)
depth, pos = 0, start
for m in re.finditer(r"\{%-?\s*(if|endif)\b", src[start:]):
    depth += 1 if m.group(1) == "if" else -1
    if depth == 0:
        pos = start + src[start:].find("{% endif %}", m.start()) + len("{% endif %}")
        break
block = src[i:pos]
print("extracted %d chars of badge markup from the live template" % len(block))

conn = psycopg2.connect(os.environ.get("SCREENER_DSN", "dbname=expwholesale port=5433"))
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
cur.execute("""
    select b.id, b.year, b.make, b.model, b.mileage, v.mmr, b.ai_price
      from bids b join vauto_lookups v on v.bid_id = b.id
     where v.mmr > 0 and b.ai_price > 0
     order by b.id desc
     limit 400
""")
rows = [dict(r) for r in cur]
conn.close()

# one of each outcome so the screenshot shows every state the operator can see
picked, seen = [], set()
for r in rows:
    s = scr.score_bid(mmr=float(r["mmr"]), bid_price=float(r["ai_price"]), mileage=r["mileage"])
    if not s: continue
    key = (s["recommendation"], bool(s["risk_flag"]))
    if key in seen: continue
    seen.add(key); picked.append((r, s))
    if len(picked) >= 5: break

env = Environment()
tmpl = env.from_string(block)
cards = []
for r, s in picked:
    veh = ("%s %s %s" % (r["year"] or "", r["make"] or "", r["model"] or "")).strip()
    body = tmpl.render(screener=s)
    cards.append(
        '<div style="background:#0f172a;border:1px solid #1e293b;border-radius:10px;'
        'padding:12px 16px;margin:14px 0;max-width:520px">'
        '<div style="color:#e2e8f0;font-size:13px;font-weight:700">%s</div>'
        '<div style="color:#64748b;font-size:11px;font-family:monospace">bid %s · MMR $%s · '
        'bid $%s · %s mi</div>%s</div>'
        % (veh, r["id"], format(int(r["mmr"]), ","), format(int(r["ai_price"]), ","),
           format(int(r["mileage"] or 0), ","), body))
    print("  %-6s %-28s -> %-5s $%-6s %s" % (r["id"], veh[:28], s["recommendation"],
          s["typical_front_dollars"], s["risk_flag"] or ""))

html = ('<html><body style="background:#020617;font-family:system-ui,-apple-system,sans-serif;'
        'padding:18px;margin:0">'
        '<div style="color:#94a3b8;font-size:12px;margin-bottom:6px">'
        'Deal Screen badge — v3 (typical front, front_value 2025+)</div>'
        + "".join(cards) + '</body></html>')
out = "/tmp/badge_check.html"
open(out, "w", encoding="utf-8").write(html)
print("\nwrote %s (%d bytes)" % (out, len(html)))
