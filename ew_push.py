# ew_push.py — push notifications for the EW partner app. Self-contained Flask blueprint:
# stores native APNs device tokens + pushes directly to Apple (no Expo relay). ADDITIVE —
# touches no existing intake/assessment code. Registered in app.py via one register_blueprint().
#
# Delivery is C1 -> Apple: we sign a short-lived ES256 JWT with the APNs auth key and POST
# to api.push.apple.com over HTTP/2. APNs config is read from /etc/ew-apns.env (or env):
#   APNS_KEY_PATH=/root/.ew_secrets/AuthKey_XXXXXXXXXX.p8
#   APNS_KEY_ID=XXXXXXXXXX
#   APNS_TEAM_ID=GSZ82GAF42
#   APNS_TOPIC=net.experiencewholesale.partner          (defaulted)
#   APNS_HOST=https://api.push.apple.com                (defaulted; production = TestFlight)
import os, json, time, re, base64
from flask import Blueprint, request, jsonify
import psycopg2

bp = Blueprint('ew_push', __name__)
DB = os.environ.get('DATABASE_URL', 'postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale')
APNS_ENV = os.environ.get('APNS_ENV_FILE', '/etc/ew-apns.env')
TOKEN_RE = re.compile(r'^[0-9a-fA-F]{64,200}$')   # native APNs device token (hex)


def _conn():
    return psycopg2.connect(DB)


def ensure_table():
    c = _conn(); cur = c.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS app_push_tokens(
        token text PRIMARY KEY, platform text,
        created_at timestamptz DEFAULT now(), last_seen timestamptz DEFAULT now())""")
    c.commit(); c.close()


def _cfg():
    cfg = {}
    for k in ('APNS_KEY_PATH', 'APNS_KEY_ID', 'APNS_TEAM_ID', 'APNS_TOPIC', 'APNS_HOST'):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    try:
        for line in open(APNS_ENV):
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            cfg.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except Exception:
        pass
    cfg.setdefault('APNS_TOPIC', 'net.experiencewholesale.partner')
    cfg.setdefault('APNS_TEAM_ID', 'GSZ82GAF42')
    cfg.setdefault('APNS_HOST', 'https://api.push.apple.com')
    return cfg


_jwt_cache = {'t': 0, 'v': None}


def _apns_jwt(cfg):
    now = int(time.time())
    if _jwt_cache['v'] and now - _jwt_cache['t'] < 2700:   # reuse <45min (APNs allows <60)
        return _jwt_cache['v']
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, utils
    key = serialization.load_pem_private_key(open(cfg['APNS_KEY_PATH'], 'rb').read(), password=None)
    def b(o):
        return base64.urlsafe_b64encode(json.dumps(o, separators=(',', ':')).encode()).rstrip(b'=')
    signing = b({'alg': 'ES256', 'kid': cfg['APNS_KEY_ID']}) + b'.' + b({'iss': cfg['APNS_TEAM_ID'], 'iat': now})
    der = key.sign(signing, ec.ECDSA(hashes.SHA256()))
    r, s = utils.decode_dss_signature(der)
    sig = base64.urlsafe_b64encode(r.to_bytes(32, 'big') + s.to_bytes(32, 'big')).rstrip(b'=')
    tok = (signing + b'.' + sig).decode()
    _jwt_cache.update(t=now, v=tok)
    return tok


@bp.route('/api/app/register-push', methods=['POST'])
def register_push():
    try:
        d = request.get_json(force=True, silent=True) or {}
        token = (d.get('token') or '').strip()
        if not (TOKEN_RE.match(token) or token.startswith('ExponentPushToken')):
            return jsonify({'ok': False, 'error': 'bad token'}), 400
        c = _conn(); cur = c.cursor()
        cur.execute("""INSERT INTO app_push_tokens(token, platform) VALUES(%s,%s)
            ON CONFLICT(token) DO UPDATE SET last_seen=now()""", (token, d.get('platform', 'ios')))
        c.commit(); c.close()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)[:120]}), 500


def send_push(title, body, data=None):
    """Push to every registered iOS device via APNs. Returns count delivered (HTTP 200)."""
    cfg = _cfg()
    if not cfg.get('APNS_KEY_PATH') or not cfg.get('APNS_KEY_ID') or not os.path.exists(cfg.get('APNS_KEY_PATH', '')):
        print('[ew_push] APNs key not configured yet — skipping send', flush=True)
        return 0
    c = _conn(); cur = c.cursor()
    cur.execute("SELECT token FROM app_push_tokens"); toks = [r[0] for r in cur.fetchall()]; c.close()
    toks = [t for t in toks if t and TOKEN_RE.match(t)]
    if not toks:
        return 0
    import httpx
    jwt = _apns_jwt(cfg)
    payload = json.dumps({'aps': {'alert': {'title': title, 'body': body}, 'sound': 'default', 'badge': 1},
                          **(data or {})}).encode()
    headers = {'authorization': 'bearer ' + jwt, 'apns-topic': cfg['APNS_TOPIC'],
               'apns-push-type': 'alert', 'apns-priority': '10'}
    dead, sent = [], 0
    try:
        with httpx.Client(http2=True, timeout=15) as cl:
            for t in toks:
                try:
                    r = cl.post(cfg['APNS_HOST'] + '/3/device/' + t, content=payload, headers=headers)
                    if r.status_code == 200:
                        sent += 1
                    elif r.status_code in (400, 410):   # BadDeviceToken / Unregistered
                        dead.append(t)
                        print('[ew_push] dead token %s.. (%s) %s' % (t[:12], r.status_code, r.text[:80]), flush=True)
                    else:
                        print('[ew_push] APNs %s for %s.. %s' % (r.status_code, t[:12], r.text[:120]), flush=True)
                except Exception as e:
                    print('[ew_push] send err:', e, flush=True)
    except Exception as e:
        print('[ew_push] client err:', e, flush=True)
    if dead:
        c = _conn(); cur = c.cursor()
        cur.executemany("DELETE FROM app_push_tokens WHERE token=%s", [(t,) for t in dead]); c.commit(); c.close()
        print('[ew_push] pruned %d dead token(s)' % len(dead), flush=True)
    return sent
