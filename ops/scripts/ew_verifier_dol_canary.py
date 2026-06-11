#!/usr/bin/env python3
"""EW verifier DOL canary (2026-06-11).

Hourly: confirm the shared vAuto pool session still authenticates the BFF DOL
endpoint. If it goes dead (401), Telegram the operator immediately so a stale
session is caught proactively — NOT discovered mid-onboard (the recurring
"did my verifiers verify the DOL" problem).

Verdict from the same call the verifiers use (competition/vehicles):
  alive (HTTP 200) -> silent, clears alert latch
  dead  (HTTP 401) -> Telegram (cooldown), updates latch
  unknown (timeout/5xx/no cookies) -> log only, no alert (avoid false alarms)

Creds: Telegram from /etc/ew_failover.env (never hardcoded). Pool session read
straight from state/vauto_session.json (no secret needed). Log:
/var/log/ew_verifier_dol_canary.log. Latch: /run/ew_verifier_dol_canary.state.
Standby-safe: exits quietly if the pool file is absent (only primary has it).
"""
import json, os, time, sys

POOL_FILE = '/opt/expwholesale/state/vauto_session.json'
STATE = '/run/ew_verifier_dol_canary.state'
FAILOVER_ENV = '/etc/ew_failover.env'
COOLDOWN_S = 6 * 3600  # at most one Telegram per 6h while dead
COMP = 'https://slot2.bff.megazord.vauto.app.coxautoinc.com/api/competition/vehicles'
WANTED = ('coxautoinc.com', 'vauto.com', 'vauto.app.coxautoinc.com', 'okta', 'megazord')


def log(msg):
    line = time.strftime('%Y-%m-%d %H:%M:%S') + ' ' + msg
    print(line, flush=True)


def load_env_file(path):
    out = {}
    try:
        for ln in open(path):
            ln = ln.strip()
            if ln and not ln.startswith('#') and '=' in ln:
                k, v = ln.split('=', 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def telegram(msg):
    env = load_env_file(FAILOVER_ENV)
    tok = env.get('TELEGRAM_BOT_TOKEN') or os.environ.get('TELEGRAM_BOT_TOKEN')
    chat = env.get('TELEGRAM_CHAT_ID') or os.environ.get('TELEGRAM_CHAT_ID')
    if not (tok and chat):
        log('WARN no telegram creds; skipping alert')
        return
    try:
        import requests
        requests.post('https://api.telegram.org/bot%s/sendMessage' % tok,
                      data={'chat_id': chat, 'text': msg}, timeout=12)
    except Exception as e:
        log('WARN telegram failed: %s' % e)


def read_latch():
    try:
        return json.load(open(STATE))
    except Exception:
        return {'last_verdict': None, 'last_alert_ts': 0}


def write_latch(d):
    try:
        json.dump(d, open(STATE, 'w'))
    except Exception as e:
        log('WARN latch write failed: %s' % e)


def main():
    if not os.path.exists(POOL_FILE):
        # standby host (no pool) — nothing to check
        return 0
    try:
        pool = json.load(open(POOL_FILE))
    except Exception as e:
        log('unknown: pool file unreadable: %s' % e)
        return 0

    cookies = {c['name']: c['value'] for c in (pool.get('cookies') or [])
               if c.get('name') and c.get('value')
               and any(d in (c.get('domain', '').lstrip('.').lower()) for d in WANTED)}
    headers = dict(pool.get('headers') or {})
    appraisal = pool.get('session_appraisal_id')
    if 'vAutoAuth' not in cookies or not headers or not appraisal:
        log('unknown: pool incomplete (cookies=%d headers=%d appraisal=%s)'
            % (len(cookies), len(headers), bool(appraisal)))
        return 0

    body = {'appraisalId': appraisal,
            'vehicle': {'vin': '1GCUDJEL4RZ296910', 'year': 2024, 'make': 'Chevrolet',
                        'model': 'Silverado 1500', 'mileage': 6876, 'odometer': 6876, 'trim': None},
            'marketCriteria': {'distance': 10000, 'isAutoDistance': False, 'minOdometer': None,
                               'maxOdometer': None, 'marketMode': 'Recent', 'postalCode': None},
            'criteriaOptions': [], 'listPrice': 0, 'shouldShowMarketInfoForMyVehicle': True,
            'useSmarterCompetitiveSet': False, 'optionCodes': None}
    try:
        import requests
        r = requests.post(COMP, json=body, headers=headers, cookies=cookies, timeout=20)
        code = r.status_code
    except Exception as e:
        log('unknown: BFF call error: %s' % type(e).__name__)
        return 0

    captured = pool.get('captured_at', '?')
    latch = read_latch()
    now = time.time()

    if code == 200:
        log('alive: BFF 200 (pool captured %s)' % captured)
        if latch.get('last_verdict') == 'dead':
            telegram('✅ EW vAuto DOL session RECOVERED — verifiers producing real DOL again.')
        write_latch({'last_verdict': 'alive', 'last_alert_ts': 0})
        return 0
    elif code == 401:
        log('DEAD: BFF 401 (pool captured %s)' % captured)
        if now - latch.get('last_alert_ts', 0) >= COOLDOWN_S:
            telegram('🔴 EW vAuto DOL session DEAD (BFF 401). Verifiers are skip-marking, '
                     'no real days-on-lot. Re-login needed on a verifier VM (192.168.1.33) '
                     'before onboarding. Pool last captured %s.' % captured)
            latch['last_alert_ts'] = now
        latch['last_verdict'] = 'dead'
        write_latch(latch)
        return 0
    else:
        log('unknown: BFF HTTP %d' % code)
        return 0


if __name__ == '__main__':
    sys.exit(main())
