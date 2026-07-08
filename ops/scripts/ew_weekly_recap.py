#!/usr/bin/env python3
# EW WEEKLY RECAP (PDF) - Saturday digest to me/joe/todd/gregg.
# SAME content + schedule + recipients as the text version; only the PRESENTATION
# changed from compact GSM-7 text to a hosted PDF (table style). Texts the PDF link
# (short body -> no 30019 length issue). Backup of text version:
#   /usr/local/bin/ew_weekly_recap.py.bak.20260708-textversion
#   python3 ew_weekly_recap.py         -> send to ALL 4 (+ delivery poll)
#   python3 ew_weekly_recap.py test    -> send to operator ('me') only
import os, re, sys, time, uuid, sqlite3
from datetime import datetime
from twilio.rest import Client

RECIPS = {'me': '+14074309675', 'joe': '+13522099696',
          'todd': '+15613018622', 'gregg': '+15166803500'}
DB = "file:/opt/livesaleslog/crm.db?mode=ro"
_MONTHS = ['', 'January', 'February', 'March', 'April', 'May', 'June', 'July',
           'August', 'September', 'October', 'November', 'December']

env = {}
for line in open("/etc/systemd/system/expwholesale.service"):
    m = re.match(r'^Environment=(.*)$', line.strip())
    if m:
        raw = m.group(1).strip().strip('"')
        if '=' in raw:
            k, v = raw.split('=', 1)
            env[k] = v
SID = env.get("TWILIO_ACCOUNT_SID")
TOK = env.get("TWILIO_AUTH_TOKEN")
FROM = env.get("TWILIO_PHONE")


def nice(lo, hi):
    try:
        ly, lm, ld = lo.split('-')
        hy, hm, hd = hi.split('-')
        if lm == hm and ly == hy:
            return '%s %d-%d, %s' % (_MONTHS[int(lm)], int(ld), int(hd), ly)
        return '%s %d - %s %d, %s' % (_MONTHS[int(lm)], int(ld), _MONTHS[int(hm)], int(hd), hy)
    except Exception:
        return '%s to %s' % (lo, hi)


def pct(a, b):
    return ('%+.0f%%' % ((a - b) / b * 100.0)) if b else 'n/a'


def gather():
    c = sqlite3.connect(DB, uri=True)
    cur = c.cursor()
    cur.execute("SELECT date('now','start of month'), date('now'), "
                "date('now','start of month','-1 month'), date('now','-1 month'), "
                "date('now','start of month','-1 year'), date('now','-1 year'), "
                "date('now','-7 days'), date('now','-1 day'), "
                "date('now','-14 days'), date('now','-8 days')")
    (mtd_lo, mtd_hi, pm_lo, pm_hi, ly_lo, ly_hi, tw_lo, tw_hi, pw_lo, pw_hi) = cur.fetchone()

    def st(lo, hi):
        cur.execute("SELECT COUNT(*), COALESCE(SUM(front_value),0) FROM deals "
                    "WHERE substr(sold_at,1,10) BETWEEN ? AND ?", (lo, hi))
        n, p = cur.fetchone()
        n = int(n or 0)
        p = round(float(p or 0))
        return n, p, (round(p / n) if n else 0)

    d = dict(tw=st(tw_lo, tw_hi), pw=st(pw_lo, pw_hi), m=st(mtd_lo, mtd_hi),
             pm=st(pm_lo, pm_hi), ly=st(ly_lo, ly_hi),
             tw_range=nice(tw_lo, tw_hi), mtd_range=nice(mtd_lo, mtd_hi))
    c.close()
    return d


def build_pdf(d):
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
    cap = ParagraphStyle('cap', parent=styles['Normal'], fontSize=9, textColor=colors.HexColor('#888888'), spaceBefore=4, spaceAfter=14)

    def money(v):
        return "$%s" % format(v, ',.0f')

    def table(rows):
        data = [["Period", "Units Sold", "Total Profit", "PVR Avg"]]
        for label, tup in rows:
            n, p, pvr = tup
            data.append([label, str(n), money(p), money(pvr)])
        t = Table(data, colWidths=[2.3 * inch, 1.1 * inch, 1.4 * inch, 1.1 * inch])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1a1a1a')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 10),
            ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 1), (-1, -1), 10),
            ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
            ('ALIGN', (0, 0), (0, -1), 'LEFT'),
            ('TOPPADDING', (0, 0), (-1, -1), 7),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
            ('LEFTPADDING', (0, 0), (-1, -1), 10),
            ('RIGHTPADDING', (0, 0), (-1, -1), 10),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f5f5f5')]),
            ('LINEBELOW', (0, 0), (-1, 0), 1, colors.HexColor('#1a1a1a')),
            ('LINEBELOW', (0, -1), (-1, -1), 1, colors.HexColor('#cccccc')),
        ]))
        return t

    flow = [
        Paragraph("Experience Wholesale", h1),
        Paragraph("Weekly Sales Recap &nbsp;|&nbsp; %s &nbsp;|&nbsp; Generated %s"
                  % (d['tw_range'], datetime.now().strftime('%B %d, %Y at %-I:%M %p')), sub),
        Paragraph("This Week vs Prior Week", h2),
        table([("This Week (%s)" % d['tw_range'], d['tw']), ("Prior Week", d['pw'])]),
        Paragraph("Week-over-week profit: %s" % pct(d['tw'][1], d['pw'][1]), cap),
        Paragraph("Month-to-Date &mdash; %s" % d['mtd_range'], h2),
        table([("This Month MTD", d['m']), ("Last Month (same days)", d['pm']), ("Last Year (same days)", d['ly'])]),
        Paragraph("Profit vs last month: %s &nbsp;&bull;&nbsp; vs last year: %s"
                  % (pct(d['m'][1], d['pm'][1]), pct(d['m'][1], d['ly'][1])), cap),
    ]
    SimpleDocTemplate(path, pagesize=letter, title="EW Weekly Sales Recap",
                      topMargin=0.7 * inch, bottomMargin=0.7 * inch).build(flow)
    return "https://experience-wholesale.net/static/uploads/reports/%s.pdf" % rid, d['tw_range']


def main():
    test = len(sys.argv) > 1 and sys.argv[1] == 'test'
    d = gather()
    url, wk = build_pdf(d)
    print("PDF:", url)
    body = "Experience Wholesale — Weekly Sales Recap\n%s\n\n%s" % (wk, url)
    targets = {'me': RECIPS['me']} if test else RECIPS
    client = Client(SID, TOK)
    sids = {}
    for who, num in targets.items():
        try:
            msg = client.messages.create(to=num, from_=FROM, body=body)
            sids[who] = msg.sid
            print("sent %s %s: %s" % (who, num, msg.sid))
        except Exception as e:
            print("FAILED %s %s: %s" % (who, num, e))
    time.sleep(12)
    for who, sid in sids.items():
        try:
            print("status %s: %s" % (who, client.messages(sid).fetch().status))
        except Exception as e:
            print("status %s: check-failed %s" % (who, e))


if __name__ == "__main__":
    main()
