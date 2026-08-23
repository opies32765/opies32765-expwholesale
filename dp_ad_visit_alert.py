"""dp_ad_visit_alert.py — DP_AD_VISIT_ALERT_2026_08_22

Text the operator when a REAL person arrives on dealerprice.net from a Google
Ads click. Operator ask 2026-08-22: "could i get a text from the 754 number when
someone visits the site from google? no bots real ip's...no datacenter ip's".

Install:
  * * * * * /opt/expwholesale/venv/bin/python /opt/expwholesale/dp_ad_visit_alert.py >> /var/log/dp_ad_visit_alert.log 2>&1

Design notes (deliberate):
  * ONE TEXT PER VISITOR PER DAY. Google forecasts ~29 paid clicks a day at the
    current budget; a text per pageview would be unreadable by Tuesday. The
    ledger's primary key (visitor_key, alert_date) enforces this — it is not a
    best-effort check that can race.
  * DATACENTER AND PROXY IPs ARE SKIPPED. dp_site_visits.is_hosting is filled
    LAZILY (only when someone opens the visitors dashboard), so at alert time it
    is usually NULL. We therefore resolve the IP here, inline, before deciding —
    reading the stored flag alone would let every bot through.
  * Standalone cron, never a hook in the beacon. The beacon is fail-open and
    must stay that way: an alert problem must never cost a pageview.
  * Uses app.send_sms() (never Twilio directly) so bot_mute / STOP / magic
    numbers are still honoured.
  * GATED. Without /opt/expwholesale/DP_AD_ALERT_LIVE it logs what it WOULD send
    and texts nobody — so the destination number can be eyeballed in the log
    before a single message goes out.
"""
import os
import sys
import json
import traceback
import urllib.request

sys.path.insert(0, '/opt/expwholesale')
os.environ.setdefault(
    'DATABASE_URL',
    'postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale')

_TWILIO_KEYS = ('TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_PHONE')
_UNIT = '/etc/systemd/system/expwholesale.service'


def _load_twilio_env():
    """TWILIO_CREDS_2026_08_17 — a cron process does NOT inherit the systemd
    unit's Environment=, and send_sms() returns False *silently* when these are
    empty. Read them from the unit file; never hardcode (GitHub secret scanning
    rejected exactly that once already). Abort loudly if missing, because the
    silent-False failure mode is the whole problem.
    """
    try:
        with open(_UNIT, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line.startswith('Environment='):
                    continue
                kv = line[len('Environment='):].strip().strip('"')
                if '=' not in kv:
                    continue
                k, v = kv.split('=', 1)
                if k in _TWILIO_KEYS:
                    os.environ.setdefault(k, v.strip().strip('"'))
    except Exception as e:
        sys.exit('[dp-ad-alert] FATAL cannot read %s: %s' % (_UNIT, e))
    missing = [k for k in _TWILIO_KEYS if not os.environ.get(k)]
    if missing:
        sys.exit('[dp-ad-alert] FATAL missing %s — send_sms() would return '
                 'False silently and nobody would ever be told.'
                 % ', '.join(missing))


_load_twilio_env()

import app  # noqa: E402 — get_db() and send_sms()

GATE = '/opt/expwholesale/DP_AD_ALERT_LIVE'
# Confirm this against the dry-run log BEFORE creating the gate file.
TO = os.environ.get('DP_AD_ALERT_TO', '4074309675')
BOARD = 'https://experience-wholesale.net/network/visitors'
LOOKBACK_MIN = 30          # a visit older than this is not news
MAX_PER_RUN = 8            # burst cap: a click spike must not become a text storm
BOT_UA = ('bot', 'crawl', 'spider', 'slurp', 'headless', 'python-requests',
          'curl/', 'wget', 'preview', 'monitor', 'scan', 'facebookexternalhit',
          'lighthouse', 'pingdom', 'uptime')


def _looks_like_bot(ua):
    u = (ua or '').lower()
    return (not u) or any(t in u for t in BOT_UA)


def _ip_is_datacenter(ip):
    """True when the IP is hosting/proxy, None when we genuinely cannot tell.

    ip-api.com free tier: no key, 45 req/min, HTTP only. We call it at most
    MAX_PER_RUN times a minute, well inside that.
    """
    if not ip:
        return None
    try:
        url = ('http://ip-api.com/json/%s?fields=status,hosting,proxy,'
               'city,regionName,isp' % ip)
        req = urllib.request.Request(
            url, headers={'User-Agent': 'EW-DealerPriceAdAlert/1.0'})
        with urllib.request.urlopen(req, timeout=6) as r:
            d = json.loads(r.read())
        if d.get('status') != 'success':
            return None
        return bool(d.get('hosting') or d.get('proxy')), d
    except Exception:
        return None


def compose(kw, city, region, isp, nth):
    """LINK_NOT_LAST_2026_08_17 — a trailing URL makes the phone render a
    link-preview card as a SECOND bubble. Keep plain text after the link."""
    where = ', '.join([p for p in (city, region) if p]) or 'location unknown'
    lines = ['Google Ads visit - DealerPrice']
    if kw:
        lines.append('kw: %s' % kw[:60])
    lines.append(where + ((' - ' + isp[:28]) if isp else ''))
    lines.append(BOARD)
    lines.append('paid visit #%d today' % nth)
    return '\n'.join(lines)


def main():
    live = os.path.exists(GATE)
    sent = 0
    with app.get_db() as conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS dp_ad_visit_alerts (
                visitor_key text        NOT NULL,
                alert_date  date        NOT NULL,
                sent_at     timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (visitor_key, alert_date))""")
        conn.commit()

        cur.execute("""
            SELECT COALESCE(v.visitor_id, v.ip) AS who,
                   max(v.ip)         AS ip,
                   max(v.ua)         AS ua,
                   max(v.utm_term)   AS kw,
                   max(v.city)       AS city,
                   max(v.region)     AS region,
                   max(v.isp)        AS isp,
                   count(*)          AS hits
              FROM dp_site_visits v
             WHERE v.gclid IS NOT NULL
               AND v.visited_at > now() - interval '%s minutes'
               AND NOT EXISTS (
                     SELECT 1 FROM dp_ad_visit_alerts a
                      WHERE a.visitor_key = COALESCE(v.visitor_id, v.ip)
                        AND a.alert_date  = current_date)
             GROUP BY 1
             ORDER BY max(v.visited_at)
             LIMIT %s""" % (LOOKBACK_MIN, MAX_PER_RUN))
        rows = cur.fetchall()

    for r in rows:
        who = r['who']
        if _looks_like_bot(r['ua']):
            print('[dp-ad-alert] skip %s — bot UA' % who, flush=True)
            _claim(who)          # claim it so we do not re-check every minute
            continue
        verdict = _ip_is_datacenter(r['ip'])
        city, region, isp = r['city'], r['region'], r['isp']
        if isinstance(verdict, tuple):
            is_dc, geo = verdict
            city = city or geo.get('city')
            region = region or geo.get('regionName')
            isp = isp or geo.get('isp')
            if is_dc:
                print('[dp-ad-alert] skip %s — datacenter/proxy (%s)'
                      % (who, r['ip']), flush=True)
                _claim(who)
                continue
        elif verdict is None:
            # Could not resolve. Send anyway rather than stay silent: a missed
            # real dealer costs more than one wasted text, and the visitors
            # board shows the truth either way.
            print('[dp-ad-alert] %s — IP unresolved, sending anyway' % who,
                  flush=True)

        body = compose(r['kw'], city, region, isp, r['hits'])
        if not live:
            print('[dp-ad-alert] DRY RUN (no %s) would text %s:\n%s\n---'
                  % (GATE, TO, body), flush=True)
            _claim(who)
            continue
        ok = bool(app.send_sms(TO, body))
        print('[dp-ad-alert] %s -> %s sent=%s' % (who, TO, ok), flush=True)
        if ok:
            _claim(who)
            sent += 1

    if rows:
        print('[dp-ad-alert] %d candidate(s), %d texted, live=%s'
              % (len(rows), sent, live), flush=True)


def _claim(who):
    """Record that this visitor has been handled today. ON CONFLICT DO NOTHING
    makes a concurrent run a no-op rather than a double text."""
    try:
        with app.get_db() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO dp_ad_visit_alerts (visitor_key, alert_date)"
                        " VALUES (%s, current_date) ON CONFLICT DO NOTHING", (who,))
            conn.commit()
    except Exception:
        traceback.print_exc()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
