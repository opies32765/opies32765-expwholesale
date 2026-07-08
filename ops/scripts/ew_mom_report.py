#!/usr/bin/env python3
# EW SALES PERFORMANCE REPORT (PDF) - Saturday digest to me/joe/todd/gregg.
# Replaces the old compact-text ew_weekly_recap. Content = the operator-approved
# MoM + YoY tables (Units Sold / PVR Avg / Total Profit), rendered as a PDF and
# texted as a hosted link (short GSM-7 body -> no 30019 length issue).
# Window = 1st..today's day-of-month, applied consistently across every 2026 month
# (MoM) and every year 2019..now for the current month (YoY) -> fair same-period compare.
#   python3 ew_mom_report.py         -> send to ALL 4 (+ delivery poll)
#   python3 ew_mom_report.py test    -> send to operator ('me') only
import os, re, sys, uuid, sqlite3, time
from datetime import datetime
from twilio.rest import Client

RECIPIENTS = {'me': '+14074309675', 'joe': '+13522099696',
              'todd': '+15613018622', 'gregg': '+15166803500'}

env = {}
for line in open("/etc/systemd/system/expwholesale.service"):
    m = re.match(r"^Environment=(.*)$", line.strip())
    if m:
        raw = m.group(1).strip().strip('"')
        if "=" in raw:
            k, v = raw.split("=", 1)
            env[k] = v
SID = env.get("TWILIO_ACCOUNT_SID"); TOK = env.get("TWILIO_AUTH_TOKEN"); FROM = env.get("TWILIO_PHONE")

# ---- Query LSL (live, read-only) ----
conn = sqlite3.connect("file:/opt/livesaleslog/crm.db?mode=ro", uri=True)
cur = conn.cursor()

def metrics(start, end_excl):
    cur.execute("""
        SELECT COUNT(*), AVG(front_value), SUM(front_value)
        FROM deals WHERE sold_at >= ? AND sold_at < ? AND front_value IS NOT NULL
    """, (f"{start}T00:00:00", f"{end_excl}T00:00:00"))
    r = cur.fetchone()
    return (r[0] or 0, r[1] or 0.0, r[2] or 0.0)

today = datetime.now()
day_cut = today.day                 # window = 1st .. today (inclusive)
end_day = day_cut + 1               # exclusive upper bound

MONTHS = ['', 'January','February','March','April','May','June','July',
          'August','September','October','November','December']

# 2026 Month-over-Month: each month Jan..current, windowed 1..day_cut
mom_rows = []
for mnum in range(1, today.month + 1):
    e = end_day if mnum == today.month else (day_cut + 1)
    mom_rows.append((MONTHS[mnum], *metrics(f"2026-{mnum:02d}-01", f"2026-{mnum:02d}-{e:02d}")))

# YoY: current month, 1..day_cut, across 2019..current year
cm = today.month
yoy_rows = []
for yr in range(2019, today.year + 1):
    yoy_rows.append((str(yr), *metrics(f"{yr}-{cm:02d}-01", f"{yr}-{cm:02d}-{end_day:02d}")))
conn.close()

window_label = f"1st–{day_cut}{'th' if 11<=day_cut<=13 else {1:'st',2:'nd',3:'rd'}.get(day_cut%10,'th')}"

# ---- Build PDF ----
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

rid = uuid.uuid4().hex
rdir = "/opt/expwholesale/static/uploads/reports"
os.makedirs(rdir, exist_ok=True)
path = os.path.join(rdir, rid + ".pdf")

styles = getSampleStyleSheet()
h1 = ParagraphStyle('h1', parent=styles['Title'], fontSize=20, textColor=colors.HexColor('#1a1a1a'), spaceAfter=6)
sub = ParagraphStyle('sub', parent=styles['Normal'], fontSize=10, textColor=colors.HexColor('#666666'), spaceAfter=16)
h2 = ParagraphStyle('h2', parent=styles['Heading2'], fontSize=14, textColor=colors.HexColor('#1a1a1a'), spaceBefore=10, spaceAfter=8)

def money(v): return f"${v:,.0f}"

def build_table(header_label, rows):
    data = [[header_label, "Units Sold", "PVR Avg", "Total Profit"]]
    for name, u, avg, tot in rows:
        data.append([name, str(u), money(avg), money(tot)])
    t = Table(data, colWidths=[1.7*inch, 1.2*inch, 1.3*inch, 1.5*inch])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#1a1a1a')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,0), 10),
        ('FONTNAME', (0,1), (-1,-1), 'Helvetica'),
        ('FONTSIZE', (0,1), (-1,-1), 10),
        ('ALIGN', (1,0), (-1,-1), 'RIGHT'),
        ('ALIGN', (0,0), (0,-1), 'LEFT'),
        ('TOPPADDING', (0,0), (-1,-1), 7),
        ('BOTTOMPADDING', (0,0), (-1,-1), 7),
        ('LEFTPADDING', (0,0), (-1,-1), 10),
        ('RIGHTPADDING', (0,0), (-1,-1), 10),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#f5f5f5')]),
        ('LINEBELOW', (0,0), (-1,0), 1, colors.HexColor('#1a1a1a')),
        ('LINEBELOW', (0,-1), (-1,-1), 1, colors.HexColor('#cccccc')),
    ]))
    return t

flow = [
    Paragraph("Experience Wholesale", h1),
    Paragraph(f"Sales Performance Report &nbsp;|&nbsp; Generated {today.strftime('%B %d, %Y at %-I:%M %p')}", sub),
    Paragraph(f"2026 Month-over-Month ({window_label} of each month)", h2),
    build_table("Month", mom_rows),
    Spacer(1, 0.3*inch),
    Paragraph(f"Year-over-Year &mdash; {MONTHS[cm]} {window_label} (2019 &rarr; {today.year})", h2),
    build_table("Year", yoy_rows),
]
SimpleDocTemplate(path, pagesize=letter, title="EW Sales Performance Report",
                  topMargin=0.7*inch, bottomMargin=0.7*inch).build(flow)

url = f"https://experience-wholesale.net/static/uploads/reports/{rid}.pdf"
print(f"PDF: {url}")

# ---- Send ----
test = len(sys.argv) > 1 and sys.argv[1] == 'test'
targets = {'me': RECIPIENTS['me']} if test else RECIPIENTS
client = Client(SID, TOK)
body = f"Experience Wholesale — Sales Performance\nMoM 2026 + YoY {MONTHS[cm]} {window_label} (since 2019)\n\n{url}"
sids = {}
for who, num in targets.items():
    try:
        msg = client.messages.create(to=num, from_=FROM, body=body)
        sids[who] = msg.sid
        print(f"sent {who} {num}: {msg.sid}")
    except Exception as e:
        print(f"FAILED {who} {num}: {e}")
time.sleep(12)
for who, sid in sids.items():
    try:
        print(f"status {who}: {client.messages(sid).fetch().status}")
    except Exception as e:
        print(f"status {who}: check-failed {e}")
