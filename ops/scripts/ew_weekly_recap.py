#!/usr/bin/env python3
# EW WEEKLY RECAP - texts the Saturday digest to me/joe/todd/gregg.
# COMPACT_ASCII_2026_06_25: body is ASCII-only (GSM-7) + tightly formatted so it
#   stays ~2-3 SMS segments. The old format used Unicode bullets (U+2022) which
#   forced UCS-2 encoding (70 chars/seg) -> ~12 segments -> Twilio error 30019
#   (content size exceeds carrier limit) -> 3 of 4 recipients silently undelivered.
# DELIVERY_CHECK_2026_06_25: send() returns the Twilio message SID; after a short
#   wait we poll each message's real status so a carrier rejection shows as failed
#   in the log instead of a false "True" (HTTP 200 = queued, NOT delivered).
# Self-contained: read-only LSL + direct Twilio.
#   python3 ew_weekly_recap.py          -> send to ALL 4
#   python3 ew_weekly_recap.py test     -> send to operator ('me') only + print
import os, re, sys, time, json, base64, sqlite3, urllib.parse, urllib.request

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

    # COMPACT, ASCII-only (GSM-7). Keep ~2-3 segments.
    out = []
    out.append("EW Sales Recap (%s)" % nice(tw_lo, tw_hi))
    out.append("")
    out.append("This week: %d units, $%s, PVR $%s%s vs prior wk %d/$%s"
               % (tw[0], comma(tw[1]), comma(tw[2]), pct(tw[1], pw[1]), pw[0], comma(pw[1])))
    out.append("")
    out.append("MTD %s: %d units, $%s, PVR $%s" % (nice(mtd_lo, mtd_hi), m[0], comma(m[1]), comma(m[2])))
    out.append("- vs last month MTD: %d, $%s%s" % (pm[0], comma(pm[1]), pct(m[1], pm[1])))
    out.append("- vs last year MTD: %d, $%s%s" % (ly[0], comma(ly[1]), pct(m[1], ly[1])))
    return "\n".join(out).strip()


def send(to, text):
    """Returns the Twilio message SID on a successful API call, else None."""
    try:
        data = urllib.parse.urlencode({"To": to, "From": FROM, "Body": text}).encode()
        req = urllib.request.Request(
            "https://api.twilio.com/2010-04-01/Accounts/%s/Messages.json" % SID, data=data)
        req.add_header("Authorization", "Basic " +
                       base64.b64encode(("%s:%s" % (SID, TOK)).encode()).decode())
        resp = json.loads(urllib.request.urlopen(req, timeout=25).read())
        return resp.get("sid")
    except Exception as e:
        sys.stderr.write("[weekly-recap] send err %s: %s\n" % (to, e))
        return None


def status(sid):
    """Poll a message's real delivery status (status, error_code)."""
    try:
        req = urllib.request.Request(
            "https://api.twilio.com/2010-04-01/Accounts/%s/Messages/%s.json" % (SID, sid))
        req.add_header("Authorization", "Basic " +
                       base64.b64encode(("%s:%s" % (SID, TOK)).encode()).decode())
        r = json.loads(urllib.request.urlopen(req, timeout=20).read())
        return r.get("status"), r.get("error_code")
    except Exception as e:
        return "status_check_failed", str(e)[:40]


def main():
    test = len(sys.argv) > 1 and sys.argv[1] == 'test'
    body = build_body()
    seg = -(-len(body) // 153)  # GSM-7 concatenated ~153 chars/segment
    targets = {'me': RECIPS['me']} if test else RECIPS
    sent = [(nm, num, send(num, body)) for nm, num in targets.items()]
    time.sleep(15)  # let carriers report delivery / 30019-type rejections
    results = []
    for nm, num, sid in sent:
        if not sid:
            results.append((nm, 'api_error', None)); continue
        stt, ec = status(sid)
        results.append((nm, stt, ec))
    good = {'delivered', 'sent', 'queued', 'accepted'}
    bad = [r for r in results if r[1] not in good]
    print("[weekly-recap] %s segments=%d results=%s" % ("TEST" if test else "LIVE", seg, results))
    if bad:
        print("[weekly-recap] WARNING -- undelivered/failed: %s" % bad)
    print(body)


if __name__ == "__main__":
    main()
