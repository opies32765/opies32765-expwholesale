#!/usr/bin/env python3
# EW WEEKLY RECAP — texts the Saturday digest to me/joe/todd/gregg.
# Top: Month-to-Date (this MTD vs prior-month MTD vs last-year MTD, same-day aligned).
# Then: This Week vs Prior Week. Plus a verbose narrative. Clean "## + bullets" format.
# Self-contained: read-only LSL + direct Twilio.
#   python3 ew_weekly_recap.py          -> send to ALL 4
#   python3 ew_weekly_recap.py test     -> send to operator ('me') only + print
import os, re, sys, base64, sqlite3, urllib.parse, urllib.request

RECIPS = {'me': '+14074309675', 'joe': '+13522099696',
          'todd': '+15613018622', 'gregg': '+15166803500'}
DB = "file:/opt/livesaleslog/crm.db?mode=ro"
_MONTHS = ['', 'January', 'February', 'March', 'April', 'May', 'June',
           'July', 'August', 'September', 'October', 'November', 'December']

env = {}
try:
    for line in open("/etc/systemd/system/expwholesale.service"):
        m = re.match(r'^Environment=(.*)$', line.strip())
        if m:
            raw = m.group(1).strip().strip('"')
            if '=' in raw:
                k, v = raw.split('=', 1)
                env[k] = v
except Exception:
    pass
SID = env.get("TWILIO_ACCOUNT_SID"); TOK = env.get("TWILIO_AUTH_TOKEN"); FROM = env.get("TWILIO_PHONE")


def nice(lo, hi):
    try:
        ly, lm, ld = lo.split('-'); hy, hm, hd = hi.split('-')
        if lm == hm and ly == hy:
            return '%s %d-%d, %s' % (_MONTHS[int(lm)], int(ld), int(hd), ly)
        return '%s %d - %s %d, %s' % (_MONTHS[int(lm)], int(ld), _MONTHS[int(hm)], int(hd), hy)
    except Exception:
        return '%s to %s' % (lo, hi)


def comma(n):
    return '{:,}'.format(int(round(n)))


def pct(a, b):
    return (' (%+.0f%%)' % ((a - b) / b * 100.0)) if b else ''


def section(label, rng, n, p, v):
    return ["## %s (%s)" % (label, rng),
            "• Units Sold: %d" % n,
            "• Total Profit: $%s" % comma(p),
            "• PVR: $%s" % comma(v), ""]


def build_body():
    c = sqlite3.connect(DB, uri=True); cur = c.cursor()
    cur.execute("SELECT date('now','start of month'), date('now'), "
                "date('now','start of month','-1 month'), date('now','-1 month'), "
                "date('now','start of month','-1 year'), date('now','-1 year'), "
                "date('now','-7 days'), date('now','-1 day'), "
                "date('now','-14 days'), date('now','-8 days')")
    (mtd_lo, mtd_hi, pm_lo, pm_hi, ly_lo, ly_hi, tw_lo, tw_hi, pw_lo, pw_hi) = cur.fetchone()

    def st(lo, hi):
        cur.execute("SELECT COUNT(*), COALESCE(SUM(front_value),0) FROM deals "
                    "WHERE substr(sold_at,1,10) BETWEEN ? AND ?", (lo, hi))
        n, p = cur.fetchone(); n = int(n or 0); p = round(float(p or 0))
        return n, p, (round(p / n) if n else 0)

    m = st(mtd_lo, mtd_hi); pm = st(pm_lo, pm_hi); ly = st(ly_lo, ly_hi)
    tw = st(tw_lo, tw_hi); pw = st(pw_lo, pw_hi)
    c.close()

    out = ["EW Sales Recap", ""]
    out += section("Month-to-Date", nice(mtd_lo, mtd_hi), *m)
    out += section("Prior Month MTD", nice(pm_lo, pm_hi), *pm)
    out += section("Last Year MTD", nice(ly_lo, ly_hi), *ly)
    out += section("This Week", nice(tw_lo, tw_hi), *tw)
    out += section("Prior Week", nice(pw_lo, pw_hi), *pw)

    narr = (
        "Month-to-date we've booked %d units for $%s in front profit (PVR $%s)%s vs the same point last month "
        "($%s) and%s vs the same period last year ($%s). This past week brought %d units / $%s (PVR $%s)%s from "
        "$%s the prior week."
        % (m[0], comma(m[1]), comma(m[2]), pct(m[1], pm[1]), comma(pm[1]),
           pct(m[1], ly[1]), comma(ly[1]),
           tw[0], comma(tw[1]), comma(tw[2]), pct(tw[1], pw[1]), comma(pw[1]))
    )
    out.append(narr)
    return "\n".join(out).strip()


def send(to, text):
    try:
        data = urllib.parse.urlencode({"To": to, "From": FROM, "Body": text}).encode()
        req = urllib.request.Request(
            "https://api.twilio.com/2010-04-01/Accounts/%s/Messages.json" % SID, data=data)
        req.add_header("Authorization", "Basic " +
                       base64.b64encode(("%s:%s" % (SID, TOK)).encode()).decode())
        urllib.request.urlopen(req, timeout=25).read()
        return True
    except Exception as e:
        sys.stderr.write("[weekly-recap] send err %s: %s\n" % (to, e))
        return False


def main():
    test = len(sys.argv) > 1 and sys.argv[1] == 'test'
    body = build_body()
    targets = {'me': RECIPS['me']} if test else RECIPS
    results = [(nm, send(num, body)) for nm, num in targets.items()]
    print("[weekly-recap] %s sent=%s" % ("TEST" if test else "LIVE", results))
    print(body)


if __name__ == "__main__":
    main()
