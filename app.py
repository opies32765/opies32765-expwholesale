import json
import os
import re
import time
import base64
import uuid
import threading
import traceback
import psycopg2
import psycopg2.extras
import requests
import gate_helpers
from ew_v4_router import should_use_v4, v4_extract
_BIDS_COLUMNS_CACHE = None  # populated on first auto-decode VIN call
from datetime import datetime
from flask import Flask, render_template, request, jsonify, redirect, url_for, send_from_directory, session
from twilio.rest import Client as TwilioClient

app = Flask(__name__)

@app.errorhandler(Exception)
def _global_exception_handler(e):
    import traceback
    print("[FLASK-EXCEPTION] type=" + type(e).__name__ + " msg=" + str(e), flush=True)
    traceback.print_exc()
    return "Internal Server Error", 500



app.secret_key = os.environ.get('SECRET_KEY', 'expwholesale2026!')
app.permanent_session_lifetime = 86400 * 30  # 30 days
# Auto-reload templates on filesystem change so template-only edits don't
# require a gunicorn restart. Jinja bytecode-caches by default in prod;
# this flips it to check mtime every request. Negligible perf cost.
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True

# Dealer DB blueprint (partner inventory scanning + UI)
try:
    from dealer_db import bp as _dealer_bp
    app.register_blueprint(_dealer_bp)
except Exception as _e:
    print(f'[dealer_db] blueprint not loaded: {_e}', flush=True)

# Partner portal blueprint (self-service dashboard for partner dealers)
try:
    from partner_portal import bp as _partner_bp
    app.register_blueprint(_partner_bp)
except Exception as _e:
    print(f'[partner_portal] blueprint not loaded: {_e}', flush=True)

# Cost analysis dashboard (admin-only — /admin/costs)
try:
    from cost_dashboard import bp as _cost_bp
    app.register_blueprint(_cost_bp)
except Exception as _e:
    print(f'[cost_dashboard] blueprint not loaded: {_e}', flush=True)

# Owner portal blueprint — mobile-first read-mostly view for the 3 EW owners
try:
    from owner_portal import bp as _owner_bp, notify_owners_new_bid
    app.register_blueprint(_owner_bp)
except Exception as _e:
    notify_owners_new_bid = None
    print(f'[owner_portal] blueprint not loaded: {_e}', flush=True)

# Enrichment API blueprint — claim/submit endpoints for the dedicated
# enrichment workers on pve-pc1 (rbook + manheim transactions scrapers).
try:
    from enrichment_api import bp as _enrichment_bp
    app.register_blueprint(_enrichment_bp)
except Exception as _e:
    print(f'[enrichment_api] blueprint not loaded: {_e}', flush=True)

# Network Push blueprint — registers Jinja globals (get_bid_network_sold,
# get_bid_network_claim, get_network_claims_banner) the bid.html template
# expects. Without this, every bid page 500s with UndefinedError. The blueprint
# was added 2026-05-12 (commit fc9129f) but its registration line never landed
# in app.py until 2026-05-14. Drift-safe via @bp.record_once.
try:
    from network_push_bp import bp as _network_push_bp
    app.register_blueprint(_network_push_bp)
except Exception as _e:
    print(f'[network_push] blueprint not loaded: {_e}', flush=True)

# ── Dashboard login ───────────────────────────────────────────────────────────
EW_USERNAME = os.environ.get('EW_USERNAME', 'admin')
EW_PASSWORD = os.environ.get('EW_PASSWORD', 'Sedecrem3')

# Paths that don't require login
_PUBLIC_PREFIXES = (
    '/login', '/mobile', '/webhook/', '/static/', '/thumb', '/p/',
    '/vauto_reports/', '/service-worker', '/privacy', '/terms',
    '/api/mobile-submit', '/api/rep-bids', '/api/register-rep',
    '/api/vauto/', '/api/accutrade/', '/accutrade_reports/',
    '/api/ipacket/', '/ipacket_reports/',
    '/api/enrichment/',  # rbook/manheim enrichment workers (oscar VMs)
    '/api/thalist/',     # thalist.com scraper -> EW (shared-secret auth)
    '/api/dealerclub/',  # dealerclub live-auction scraper -> EW (shared-secret auth)
    '/api/comp_msrp/',   # VM 121 comp_msrp worker (claim, submit, jwt, status)
    "/api/internal/",  # internal worker -> SMS bridge (X-Auth gated inside handler)
    '/api/worker/',  # progress, session_lost — worker-facing, no login
    '/api/dealer/vauto_verify', '/api/dealer/vauto_verify_queue',
    '/api/dealer/beelink_scrape_queue', '/api/dealer/beelink_scrape_result',
    '/api/dealer/info/',
    '/partner/',
    '/owner',  # owner portal (own auth layer: owner_user_id session key)
    '/healthz',  # CF Load Balancer health check  # partner portal (own auth layer: partner_user_id session key)
    '/api/bid/external', '/api/push-subscribe',
    '/api/push-unsubscribe', '/api/vapid-public-key',
    '/.well-known/', '/api/tesla-vin/', '/share/', '/m/',
    '/api/quick-extract',
    '/wholesaler-',  # public self-serve signup at /wholesaler-<reviewer>/signup; admin routes still gated by _require_admin().
)


@app.route('/.well-known/appspecific/<path:filename>')
def well_known(filename):
    return send_from_directory(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', '.well-known', 'appspecific'),
        filename,
        mimetype='application/x-pem-file'
    )

@app.route('/healthz')
def healthz():
    """Health check for Cloudflare Load Balancer.

    Returns 200 ONLY when the app can write to its database. A read-only
    standby (pg_is_in_recovery()=true) returns 503 so CF won't route inbound
    writes to it. After a failover promotion on C2, pg_is_in_recovery() flips
    to false and this starts returning 200 — that's the signal CF uses to
    flip traffic to the failover pool.
    """
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT pg_is_in_recovery()')
                in_recovery = cur.fetchone()
                # RealDictCursor returns dict; key may be 'pg_is_in_recovery'
                in_recovery = list(in_recovery.values())[0] if hasattr(in_recovery, 'values') else in_recovery[0]
        if in_recovery:
            return {'ok': False, 'reason': 'standby (read-only)'}, 503
        return {'ok': True, 'role': 'primary'}, 200
    except Exception as e:
        return {'ok': False, 'error': str(e)[:200]}, 503

_PUBLIC_SUFFIXES = ('/rep-message', '/field-update', '/messages', '/messages-poll')


@app.before_request
def require_login():
    if session.get('logged_in'):
        return
    path = request.path
    if any(path.startswith(p) for p in _PUBLIC_PREFIXES):
        return
    if any(path.endswith(s) for s in _PUBLIC_SUFFIXES):
        return
    # AJAX-style API endpoints: return JSON 401 instead of an HTML 302 so
    # the calling JS can see "session expired" rather than getting silently
    # redirected and trying to parse the login page as JSON (which throws
    # and leaves the UI stuck on a spinner).
    if path.startswith('/api/'):
        return jsonify({'error': 'login_required',
                        'redirect': '/login'}), 401
    return redirect('/login')


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        # Case-insensitive username compare (admin / Admin / ADMIN all work).
        # Password stays strict.
        if ((request.form.get('username') or '').lower() == EW_USERNAME.lower() and
                request.form.get('password') == EW_PASSWORD):
            session.permanent = True
            session['logged_in'] = True
            return redirect('/')
        error = 'Invalid credentials'
    return f'''<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Login — Experience Wholesale</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'Inter',system-ui,sans-serif;background:#0b0f19;color:#e2e8f0;display:flex;align-items:center;justify-content:center;height:100vh}}
.login-card{{background:#111827;border:1px solid #1e293b;border-radius:16px;padding:40px;width:360px;max-width:90vw}}
.logo{{text-align:center;margin-bottom:28px}}
.logo-mark{{display:inline-flex;align-items:center;justify-content:center;width:48px;height:48px;background:linear-gradient(135deg,#3b82f6,#1d4ed8);border-radius:12px;font-size:22px;font-weight:800;color:#fff;margin-bottom:10px}}
.logo-text{{display:block;font-size:17px;font-weight:700;color:#f1f5f9}}
.logo-sub{{display:block;font-size:12px;color:#475569;margin-top:2px}}
label{{display:block;font-size:12px;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:.6px;margin-bottom:6px;margin-top:18px}}
input{{width:100%;padding:10px 14px;background:#0f172a;border:1px solid #1e293b;border-radius:8px;color:#e2e8f0;font-size:15px;font-family:inherit}}
input:focus{{outline:none;border-color:#3b82f6}}
button{{width:100%;padding:12px;margin-top:24px;background:#3b82f6;border:none;border-radius:8px;color:#fff;font-size:15px;font-weight:600;cursor:pointer;font-family:inherit}}
button:hover{{background:#2563eb}}
.error{{color:#f87171;font-size:13px;text-align:center;margin-top:12px}}
</style></head><body>
<div class="login-card">
<div class="logo"><div class="logo-mark">EW</div><span class="logo-text">Experience Wholesale</span><span class="logo-sub">Buy Center</span></div>
<form method="post">
<label>Username</label><input type="text" name="username" autofocus>
<label>Password</label><input type="password" name="password">
<button type="submit">Sign In</button>
{'<p class="error">' + error + '</p>' if error else ''}
</form></div></body></html>'''


@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')


_FIELD_PREFIXES = ('Rep:', 'VIN:', 'Mileage:', 'Asking:')

@app.template_filter('msg_display')
def msg_display_filter(raw):
    """Strip field rep metadata tokens from raw_message for clean display."""
    if not raw:
        return ''
    parts = raw.split(' | ')
    kept = [p for p in parts if not any(p.startswith(s) for s in _FIELD_PREFIXES)]
    return ' | '.join(kept).strip()

@app.template_filter('thumb_url')
def thumb_url_filter(src, size='strip'):
    """Build a /thumb?url=...&size=... URL for templates."""
    from urllib.parse import urlencode
    return '/thumb?' + urlencode({'url': src, 'size': size})

DB_URL = os.environ.get('DATABASE_URL', 'postgresql://expuser:ExpWholesale2026!@localhost/expwholesale')
DIA_DB_URL = 'postgresql://scraper@127.0.0.1/dealer_intelligence'
TWILIO_SID = os.environ.get('TWILIO_ACCOUNT_SID', '')
TWILIO_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN', '')
TWILIO_PHONE = os.environ.get('TWILIO_PHONE', '')
ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
UPLOAD_DIR = os.environ.get('UPLOAD_DIR', '/opt/expwholesale/static/uploads')
VAPID_PRIVATE_KEY = os.environ.get('VAPID_PRIVATE_KEY', '')
VAPID_PUBLIC_KEY = os.environ.get('VAPID_PUBLIC_KEY', '')

THUMB_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'thumb_cache')
os.makedirs(THUMB_CACHE_DIR, exist_ok=True)

THUMB_SIZES = {
    'strip': (400, 280),     # photo strip on bid detail
    'mobile': (200, 150),    # mobile My Bids list
    'full': (1400, 1050),    # lightbox
}

VIN_RE = re.compile(r'\b[A-HJ-NPR-Z0-9]{17}\b')


def vin_check_digit_valid(vin):
    """Validate VIN check digit (9th position) per ISO 3779 algorithm.
    Returns True if the VIN math works out. Use this to catch misread VINs."""
    if not vin or len(vin) != 17:
        return False
    vin = vin.upper()
    # Transliteration: letters → numeric weights
    trans = {'A':1,'B':2,'C':3,'D':4,'E':5,'F':6,'G':7,'H':8,
             'J':1,'K':2,'L':3,'M':4,'N':5,'P':7,'R':9,
             'S':2,'T':3,'U':4,'V':5,'W':6,'X':7,'Y':8,'Z':9,
             '0':0,'1':1,'2':2,'3':3,'4':4,'5':5,'6':6,'7':7,'8':8,'9':9}
    # Position weights
    weights = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]
    try:
        total = sum(trans[c] * weights[i] for i, c in enumerate(vin))
    except KeyError:
        return False
    expected = total % 11
    expected_char = 'X' if expected == 10 else str(expected)
    return vin[8] == expected_char

def decode_vin(vin):
    """Call NHTSA vPIC API and return decoded dict.

    Returns: year, make, model, trim (primary, backward-compatible), plus
    `trim_alternatives` (list if NHTSA gave ambiguous "A/B/C" trim), `trim_raw`
    (original NHTSA string), `trim_ambiguous` (bool), and a bunch of extra
    fields NHTSA populates (series, trim2, body_class, doors, drive_type,
    engine_model, engine_cylinders, displacement_l, engine_hp, fuel_type,
    plant_city, plant_country). Callers can use these for richer prompt
    context when the VDS doesn't encode trim deterministically.
    """
    if not vin or len(vin) != 17:
        return {}
    try:
        r = requests.get(
            f'https://vpic.nhtsa.dot.gov/api/vehicles/decodevin/{vin}?format=json',
            timeout=8
        )
        if r.status_code != 200:
            return {}
        items = r.json().get('Results', [])
    except Exception:
        return {}

    # Map of NHTSA Variable → our output key + coerce
    SCALAR_MAP = {
        'Model Year': ('year', 'int'),
        'ModelYear': ('year', 'int'),
        'Make': ('make', 'str'),
        'Model': ('model', 'str'),
        'Series': ('series', 'str'),
        'Trim2': ('trim2', 'str'),
        'Body Class': ('body_class', 'str'),
        'Doors': ('doors', 'int'),
        'Drive Type': ('drive_type', 'str'),
        'Engine Model': ('engine_model', 'str'),
        'Engine Number of Cylinders': ('engine_cylinders', 'int'),
        'Displacement (L)': ('displacement_l', 'float'),
        'Engine Brake (hp) From': ('engine_hp', 'int'),
        'Fuel Type - Primary': ('fuel_type', 'str'),
        'Plant City': ('plant_city', 'str'),
        'Plant Country': ('plant_country', 'str'),
    }

    out = {}
    trim_raw = ''
    for item in items:
        var = item.get('Variable') or ''
        val = (item.get('Value') or '').strip()
        if not val or val.lower() in ('null', 'not applicable'):
            continue
        if var == 'Trim':
            trim_raw = val
            continue
        mapping = SCALAR_MAP.get(var)
        if not mapping:
            continue
        key, coerce = mapping
        try:
            if coerce == 'int':
                out[key] = int(float(val))
            elif coerce == 'float':
                out[key] = float(val)
            else:
                out[key] = val
        except (ValueError, TypeError):
            continue

    # Trim handling: NHTSA often returns ambiguous alternatives like
    # "Base/Big Bend/Badlands/Wildtrak" when the VDS doesn't encode trim
    # (common on Ford/GM/Chrysler). Preserve alternatives so downstream can
    # ask Gemini to pick from photos + sticker, AND keep a single `trim`
    # for backward compat with callers that expect a string.
    if trim_raw:
        out['trim_raw'] = trim_raw
        alternatives = []
        for sep in (' / ', ', ', ' or ', '/'):
            if sep in trim_raw:
                alternatives = [t.strip() for t in trim_raw.split(sep) if t.strip()]
                break
        if len(alternatives) >= 2:
            out['trim'] = alternatives[0]  # backward compat — first option
            out['trim_alternatives'] = alternatives
            out['trim_ambiguous'] = True
        else:
            out['trim'] = trim_raw
            out['trim_alternatives'] = []
            out['trim_ambiguous'] = False

    return out

def dia_vin_lookup(vin):
    """Query DIA database on Contabo 1 for dealer + auction history by VIN. Fast direct DB query."""
    if not vin or len(vin) != 17:
        return None
    try:
        conn = psycopg2.connect(DIA_DB_URL,
                                cursor_factory=psycopg2.extras.RealDictCursor,
                                connect_timeout=5)
        cur = conn.cursor()

        # Dealer inventory history
        cur.execute("""
            SELECT i.status, i.price, i.mileage, i.first_seen, i.last_seen, i.url,
                   d.name as dealer_name, d.city, d.state, d.website
            FROM inventory i
            JOIN dealers d ON i.dealer_id = d.dealer_id
            WHERE i.vin = %s
            ORDER BY i.last_seen DESC NULLS LAST
        """, (vin,))
        dealer_rows = cur.fetchall()

        # Auction history
        cur.execute("""
            SELECT al.hammer_price, al.sold, al.sale_date, al.title as lot_title, al.lot_url,
                   ah.name as house_name, a.title as auction_title
            FROM auction_lots al
            JOIN auctions a ON al.auction_id = a.id
            JOIN auction_houses ah ON al.house_id = ah.id
            WHERE al.vin = %s
            ORDER BY al.sale_date DESC NULLS LAST
        """, (vin,))
        auction_rows = cur.fetchall()
        conn.close()

        def fmt_date(d):
            return d.strftime('%b %d, %Y') if d else None

        dealers = [{
            'dealer_name': r['dealer_name'],
            'city': r['city'],
            'state': r['state'],
            'website': r['website'],
            'status': r['status'],
            'price': r['price'],
            'mileage': r['mileage'],
            'first_seen': fmt_date(r['first_seen']),
            'last_seen': fmt_date(r['last_seen']),
            'url': r['url'],
        } for r in dealer_rows]

        auctions = [{
            'house_name': r['house_name'],
            'hammer_price': r['hammer_price'],
            'sold': r['sold'],
            'sale_date': fmt_date(r['sale_date']),
            'title': r['lot_title'] or r['auction_title'],
            'lot_url': r['lot_url'],
        } for r in auction_rows]

        return {'dealers': dealers, 'auctions': auctions}
    except Exception as e:
        print(f'DIA lookup error: {e}')
        return None


ODO_PROMPT = (
    'Read the TOTAL odometer mileage from this image.\n\n'

    'CRITICAL DISTINCTION — total odometer vs trip odometer:\n'
    '- TOTAL odometer (return this): lifetime vehicle mileage, 4-6 digits, NO decimal. '
    'Labeled: ODO, Odometer, Total, or sometimes unlabeled as the primary large number.\n'
    '- TRIP odometer (ignore): short resettable counter with a decimal point (e.g. 23.4, '
    '102.7) or labeled TRIP, Trip A, Trip B, A, B. These are always much smaller numbers.\n'
    '- RANGE estimate (ignore): on EVs, a number like "287 mi" or "Est. Range" — '
    'this is the estimated driving range remaining, NOT the odometer.\n\n'

    'WHERE TO LOOK BY VEHICLE TYPE:\n\n'

    'Traditional gauge cluster (most domestic/import cars):\n'
    '  - Small digital readout at the bottom of the speedometer face\n'
    '  - Usually labeled "ODO" with a button to toggle between odo and trip\n'
    '  - May show both ODO and TRIP — read the one labeled ODO\n\n'

    'Modern digital cluster (Ford Sync, GM, Honda, Nissan, etc.):\n'
    '  - Odometer appears as a standalone number in the instrument cluster\n'
    '  - May be on a menu screen — look for "Odometer" heading\n'
    '  - Ignore any number with a decimal or labeled "Trip"\n\n'

    'Tesla (Model S/3/X/Y/Cybertruck):\n'
    '  - Main screen: bottom-left area of the car graphic sometimes shows mileage\n'
    '  - Most reliable: Controls > Software — "Odometer" field shown in miles\n'
    '  - Also in: Controls > Service — shows odometer\n'
    '  - The VIN (17 chars) and odometer may both appear on the same screen — '
    'the odometer is the standalone number (digits only), VIN has letters in it\n'
    '  - Do NOT confuse with "Rated Range" or "Battery" percentage\n\n'

    'BMW/Mercedes/Audi/European:\n'
    '  - iDrive/MBUX/MMI menus: Vehicle Info > Odometer or Service Due screen\n'
    '  - Often shows km AND miles — return the MILES value\n'
    '  - If only km shown, convert: km × 0.621371 = miles (round to nearest whole number)\n\n'

    'Hyundai/Kia/Genesis:\n'
    '  - Digital cluster or small LCD at speedometer base\n'
    '  - "ODO" button cycles between total and trip — look for the larger number\n\n'

    'READING TIPS:\n'
    '  - Odometer is always a whole number (no decimal point)\n'
    '  - Typical range: 1,000 to 300,000 miles. Anything outside this range is likely '
    'a trip reading, range estimate, or misread\n'
    '  - If you see both a 6-digit and a 3-digit number, the 6-digit is the odometer\n'
    '  - Comma separators are fine to read through: "47,832" → return 47832\n\n'

    'Reply with ONLY the total odometer as digits, no commas, no units, no text. '
    'Example: 47832\n'
    'If you cannot find a clear total odometer reading, reply NONE.'
)

VIN_PROMPT = (
    'Extract the Vehicle Identification Number (VIN) from this image.\n\n'
    'A VIN is EXACTLY 17 characters: letters A-Z and digits 0-9 only. '
    'The letters I, O, and Q are NEVER used in a VIN.\n\n'

    'WHERE TO LOOK:\n'
    '- Driver-side door jamb sticker (white/yellow label — VIN in large bold font, '
    'often directly below a barcode. Ignore GVWR, tire pressure, model name.)\n'
    '- Monroney window sticker (labeled "VIN:" near top or bottom)\n'
    '- Dashboard plate visible through windshield\n'
    '- Tesla/digital screens: look for "VIN:" label in Settings or About screen\n\n'

    'COMMON MISREADS — double-check these:\n'
    '0 (zero) vs O (letter O — never in VIN)\n'
    '1 (one) vs I (letter I — never in VIN)\n'
    '8 vs B, 5 vs S, 2 vs Z, 6 vs G, 4 vs A\n\n'

    'VIN STRUCTURE (use this to validate your reading):\n'
    'Positions 1-3: WMI — World Manufacturer Identifier. Common ones:\n'
    '  1FA,1FB,1FC,1FD,1FT = Ford (USA)\n'
    '  1G1,1G6,1GC,1GT,2G1,2G2 = GM/Chevrolet\n'
    '  1HG,1HH,2HG,5J8 = Honda\n'
    '  1N4,1NX,3N1,4T3,JN1,JN8 = Nissan\n'
    '  1VW,3VW,WVW = Volkswagen\n'
    '  2T1,4T1,4T3,JTD,JTH,JTM = Toyota\n'
    '  3FA,3FE = Ford (Mexico)\n'
    '  4S3,4S4,JF1,JF2 = Subaru\n'
    '  5FN,5FR,5J6,5J8 = Honda/Acura\n'
    '  5LM,5TD,5XY = Ford/Toyota/Hyundai\n'
    '  5NPE,5NPD,KMHD,KMH = Hyundai\n'
    '  5XXG,KNDJ,KNDE = Kia\n'
    '  5YJ = Tesla\n'
    '  1C3,1C4,2C3,3C4 = Chrysler/Dodge\n'
    '  1B3,2B3,3B7 = Dodge/Ram\n'
    '  3C4,3C6,3D4 = Chrysler (Mexico)\n'
    '  WBA,WBS,WBY = BMW\n'
    '  WDB,WDD,WDC = Mercedes-Benz\n'
    '  WAU,WA1 = Audi\n'
    '  SAL,SAJ = Land Rover/Jaguar\n'
    '  YV1,YV4 = Volvo\n\n'

    'Position 10: Model year code\n'
    '  A=1980,B=1981,C=1982,D=1983,E=1984,F=1985,G=1986,H=1987,J=1988,K=1989\n'
    '  L=1990,M=1991,N=1992,P=1993,R=1994,S=1995,T=1996,V=1997,W=1998,X=1999\n'
    '  Y=2000,1=2001,2=2002,3=2003,4=2004,5=2005,6=2006,7=2007,8=2008,9=2009\n'
    '  A=2010,B=2011,C=2012,D=2013,E=2014,F=2015,G=2016,H=2017,J=2018,K=2019\n'
    '  L=2020,M=2021,N=2022,P=2023,R=2024,S=2025,T=2026\n\n'

    'Position 9: Check digit (0-9 or X). Calculate to validate:\n'
    '  Transliteration: A=1,B=2,C=3,D=4,E=5,F=6,G=7,H=8,J=1,K=2,L=3,M=4,\n'
    '  N=5,P=7,R=9,S=2,T=3,U=4,V=5,W=6,X=7,Y=8,Z=9, digits = face value\n'
    '  Weights by position: 8,7,6,5,4,3,2,10,0,9,8,7,6,5,4,3,2\n'
    '  Sum each (transliterated value × weight), divide by 11, remainder = check digit\n'
    '  If remainder=10, check digit=X. If your reading does not produce this check digit, '
    'you likely misread a character — recheck the ambiguous ones above.\n\n'

    'Reply with ONLY the 17-character VIN. If no VIN is visible, reply NONE.'
)


# ── DB ──────────────────────────────────────────────────────────────────────

def get_db():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


# ── Gemini (Vertex AI) — replaces Claude for text + vision ──────────────────
os.environ.setdefault('GOOGLE_APPLICATION_CREDENTIALS', '/opt/expwholesale/google_vision_key.json')

_gemini_client = None

def _gemini():
    """Lazy-init Gemini client (Vertex AI mode, uses service account JSON)."""
    global _gemini_client
    if _gemini_client is None:
        try:
            from google import genai
            _gemini_client = genai.Client(
                vertexai=True,
                project='my-project-dia-492415',
                location='global',
            )
        except Exception as e:
            print(f'Gemini init failed: {e}', flush=True)
            _gemini_client = False  # poison so we don't retry every call
    return _gemini_client if _gemini_client else None


def _resize_image_for_gemini(image_bytes, max_dim=1536, quality=85):
    """Resize image to keep Gemini token count manageable. Vehicle photos
    from dealer sites often arrive at 4000x3000+ which can push a single-image
    request over Gemini's 1M-token cap (1095944 observed 2026-05-14 on
    dealer_completion_worker).

    Resizes to max_dim on the longest side preserving aspect, re-encodes
    as JPEG (small + universal). Returns (new_bytes, 'image/jpeg').
    Falls through to original bytes if PIL fails or image is already small.
    """
    try:
        from PIL import Image
        import io as _io
        img = Image.open(_io.BytesIO(image_bytes))
        w, h = img.size
        if max(w, h) <= max_dim and len(image_bytes) < 2_000_000:
            return image_bytes, 'image/jpeg'  # already small enough
        if img.mode not in ('RGB', 'L'):
            img = img.convert('RGB')
        img.thumbnail((max_dim, max_dim), Image.LANCZOS)
        buf = _io.BytesIO()
        img.save(buf, format='JPEG', quality=quality, optimize=True)
        return buf.getvalue(), 'image/jpeg'
    except Exception as e:
        print(f'gemini: image resize failed ({e}); sending original', flush=True)
        return image_bytes, 'image/jpeg'


# SONNET_MILES_OCR_2026_05_15: Claude Sonnet 4.6 vision call helper.
# Used as the high-quality fallback for extract_mileage_from_file (Gemini
# Flash hallucinated mileage from non-odometer photos on bid 1501 —
# Sonnet returned NONE correctly on all 5 non-odometer photos and the
# correct integer on the 1 dash photo).
_sonnet_client = None


def _sonnet():
    global _sonnet_client
    if _sonnet_client is None:
        try:
            import anthropic
            _key = os.environ.get('ANTHROPIC_API_KEY', '').strip()
            if not _key:
                print('[sonnet] ANTHROPIC_API_KEY not set', flush=True)
                _sonnet_client = False
                return None
            _sonnet_client = anthropic.Anthropic(api_key=_key)
        except Exception as e:
            print(f'[sonnet] init failed: {e}', flush=True)
            _sonnet_client = False
    return _sonnet_client if _sonnet_client else None


def sonnet_vision_call(prompt, image_bytes, mime='image/jpeg', max_tokens=64,
                      timeout=15.0):
    """One-shot Claude Sonnet 4.6 vision call. Returns response text or None.
    Use this for OCR tasks where Gemini's hallucination cost > 4-5x token cost.
    """
    client = _sonnet()
    if not client:
        return None
    try:
        import base64 as _b64
        img_b64 = _b64.standard_b64encode(image_bytes).decode('utf-8')
        # Run in a thread so we can enforce a hard timeout
        import threading as _th
        _result = {'text': None}
        def _runner():
            try:
                resp = client.messages.create(
                    model='claude-sonnet-4-6',
                    max_tokens=max_tokens,
                    messages=[{
                        'role': 'user',
                        'content': [
                            {'type': 'image',
                             'source': {'type': 'base64',
                                        'media_type': mime,
                                        'data': img_b64}},
                            {'type': 'text', 'text': prompt},
                        ]
                    }]
                )
                if resp.content:
                    _result['text'] = resp.content[0].text.strip()
            except Exception as _e:
                print(f'[sonnet] call error: {_e}', flush=True)
        t = _th.Thread(target=_runner, daemon=True)
        t.start()
        t.join(timeout)
        return _result['text']
    except Exception as e:
        print(f'[sonnet] outer error: {e}', flush=True)
        return None


def gemini_call(prompt, image_bytes=None, mime='image/jpeg', model='gemini-2.5-flash',
                max_tokens=1024, temperature=0.4, disable_thinking=False):
    # GEMINI_FLASH_MILES_OCR_2026_05_17 (param): pass disable_thinking=True for
    # terse-output OCR tasks (single number, 17-char VIN). With thinking enabled
    # the model can burn most of max_tokens on internal reasoning, leaving
    # truncated or empty text in resp.text — that's how Flash "hallucinated"
    # on bid 1501 (it didn't; it just got cut off mid-token).
    """One-shot Gemini call. Returns text response or None on failure.
    Pass image_bytes for vision tasks. Defaults to Flash (cheap).
    Use model='gemini-2.5-pro' for high-quality reasoning (assessments).

    2026-05-09: Auto-retries up to 2 times on 429 RESOURCE_EXHAUSTED with
    exponential backoff (1s, 2s). Per-minute Vertex quota recovers fast,
    so this self-heals burst spikes from concurrent bid assessments.

    2026-05-14: Auto-resize large images (>1536 longest side) to keep
    request under Gemini's 1M-token cap. Was causing 400 INVALID_ARGUMENT
    on dealer photos from premium dealer sites (DealerClub, etc).
    """
    import time as _time
    client = _gemini()
    if not client:
        return None
    from google.genai import types
    if image_bytes:
        # Downsize oversized images before encoding — Gemini's per-request
        # token budget is finite; high-res dealer photos can blow it.
        image_bytes, mime = _resize_image_for_gemini(image_bytes)
        contents = [
            types.Part.from_bytes(data=image_bytes, mime_type=mime),
            prompt,
        ]
    else:
        contents = prompt
    _cfg_kwargs = dict(
        max_output_tokens=max_tokens,
        temperature=temperature,
    )
    if disable_thinking:
        _cfg_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
    cfg = types.GenerateContentConfig(**_cfg_kwargs)

    last_err = None
    for attempt in range(3):
        try:
            resp = client.models.generate_content(
                model=model, contents=contents, config=cfg,
            )
            return resp.text.strip() if resp.text else None
        except Exception as e:
            last_err = e
            msg = str(e)
            if '429' in msg or 'RESOURCE_EXHAUSTED' in msg or 'rate' in msg.lower():
                if attempt < 2:
                    _time.sleep(2 ** attempt)  # 1s, 2s
                    continue
            # Non-retryable or last attempt — fall through to log + None.
            break
    print(f'Gemini call failed ({model}) after retries: {last_err}', flush=True)
    return None


# ── VIN extraction ───────────────────────────────────────────────────────────

def extract_vin_from_text(text):
    match = VIN_RE.search(text.upper())
    return match.group(0) if match else None


_MAT_ICON_RE = re.compile(
    r'\s*(?:keyboard_arrow_right|keyboard_arrow_left|chevron_right|chevron_left|'
    r'arrow_forward|arrow_back|arrow_drop_down|expand_more|more_vert)\s*$',
    re.IGNORECASE)


def normalize_trim_text(s):
    """Strip Material-Icons glyph names + collapse whitespace. Used to clean
    DOM-scraped trim labels from AccuTrade's modal (the Angular widget appends
    chevron font-ligatures that leak through textContent)."""
    if not s:
        return s
    return _MAT_ICON_RE.sub('', (s or '').strip()).strip()


def _ensure_trim_select_cache_clean_trim_column():
    """Idempotent — ensure clean_trim column exists on accutrade_trim_select_cache.
    Older deployments created the table without it; the /api/accutrade/submit
    canon_trim writeback SELECTs the column directly and 500s without it.
    Runs once at module load."""
    try:
        _db = psycopg2.connect(DB_URL)
        _cur = _db.cursor()
        _cur.execute(
            "ALTER TABLE accutrade_trim_select_cache "
            "ADD COLUMN IF NOT EXISTS clean_trim TEXT")
        _db.commit()
        _db.close()
    except Exception as _ee:
        print(f'[boot] clean_trim column ensure failed: {_ee}', flush=True)


def filter_rbook_to_strict_peers(subject_vin, rows, min_kept=5):
    """Drop comp rows whose first 5 VIN chars don't match the subject.

    For makes that VIN-encode trim/body (Porsche, Chevy, GMC, Audi, BMW),
    char-5 differs across trims (WP0AA=Carrera, WP0AH=GTS). Filtering keeps
    only true peers. For makes that DON'T encode trim in VIN (Ford F-150
    XL vs King Ranch share the same chars 1-5), the filter would drop
    nothing — which is correct, since 1-5 == 1-5 means same body+drivetrain.

    Safety net: if the filter drops more than half the rows AND fewer than
    `min_kept` survive, return the original list. That handles weird vAuto
    bleeds (e.g. when a model code shifts mid-year and char-5 changes
    despite same trim). Better to show some loose comps than zero comps.

    Returns (filtered_rows, dropped_count, source_label).
    """
    if not rows or not subject_vin or len(subject_vin) < 5:
        return rows, 0, 'unfiltered_no_subject'
    subj_pfx = subject_vin[:5].upper()
    kept = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        cv = (r.get('vin') or '').upper()
        if not cv or len(cv) < 5:
            kept.append(r)  # incomplete data — keep, don't penalize
            continue
        if cv[:5] == subj_pfx:
            kept.append(r)
    dropped = len(rows) - len(kept)
    if dropped == 0:
        return rows, 0, 'unfiltered_no_drops'
    # If we'd drop more than half AND end up below min_kept, bail.
    if len(kept) < min_kept and dropped > len(rows) / 2:
        return rows, 0, 'unfiltered_would_nuke'
    return kept, dropped, 'strict_vin_pfx5'


# MILES_DECIMAL_K_2026_05_15: added \d{1,3}\.\d{1,2} branch so "15.6k"
# / "1.5k" / "47.5k" parse correctly. Also relaxed \d{3,6} to \d{2,6}
# so bare "15k" works without commas. Decimal-k is converted in the
# parser body (see extract_miles_from_text below).
_MILES_RE_LABELED = re.compile(
    r'(\d{1,3}\.\d{1,2}|\d{1,3}(?:[,. ]\d{3})+|\d{2,6})\s*(?:k\b|mi\b|miles?\b|mileage\b)',
    re.IGNORECASE)
_MILES_RE_KSHORT = re.compile(r'\b(\d{2,3})\s*[kK]\b')
# Bare comma-grouped numbers like "47,000" or "120,500" — used only when a VIN
# is present (otherwise too risky: matches prices, years, addresses)
_MILES_RE_COMMA = re.compile(r'(?<![\d$])(\d{1,3}(?:,\d{3})+)(?!\d)')

_TEXT_EXTRACT_PROMPT = (
    'Extract vehicle info from this SMS forwarded to a wholesale buyer. '
    'It may be free-form text describing a car for sale (year, make, model, '
    'mileage, exterior color, interior color, trim, asking price, condition '
    'notes, VIN). Return ONLY this JSON, no markdown, no commentary:\n'
    '{\n'
    '  "vin": "17-char VIN or null",\n'
    '  "year": 2024 or null,\n'
    '  "make": "Toyota" or null,\n'
    '  "model": "Camry" or null,\n'
    '  "trim": "SE" or null,\n'
    '  "mileage": 45000 or null,\n'
    '  "color": "exterior color (one word ideally) or null",\n'
    '  "int_color": "interior color or null",\n'
    '  "asking_price": 25000 or null\n'
    '}\n'
    'Rules:\n'
    '- VIN is exactly 17 chars (A-Z, 0-9). Letters I, O, Q are never valid.\n'
    '- mileage is the odometer reading; "47k" = 47000.\n'
    '- color = exterior body color, int_color = interior/seats color.\n'
    '- asking_price = price seller is asking; if a sticker price is mentioned '
    'that is NOT what they want for it, set asking_price to null.\n'
    '- If a field is unclear or absent, set it to null.\n'
    'Text:\n'
)


def _summarize_intake(body, vin, miles, text_ai):
    """Build a clean one-liner for the bid_messages thread when the SMS is
    long-form prose. Short messages (<80 chars) pass through unchanged so
    quick texts like '47k miles' or 'VIN xxx asking 24' stay natural.
    Original full body is always preserved in bids.raw_message for audit.
    """
    body = body or ''
    if len(body) <= 80:
        return body
    bits = []
    if vin:
        bits.append(f"VIN {vin}")
    yr = (text_ai or {}).get('year')
    mk = (text_ai or {}).get('make')
    md = (text_ai or {}).get('model')
    tr = (text_ai or {}).get('trim')
    ymm = ' '.join(str(p) for p in (yr, mk, md, tr) if p).strip()
    if ymm:
        bits.append(ymm)
    if miles:
        try:
            bits.append(f"{int(miles):,} mi")
        except (TypeError, ValueError):
            pass
    col = (text_ai or {}).get('color')
    intc = (text_ai or {}).get('int_color')
    if col and intc:
        bits.append(f"{col}/{intc}")
    elif col:
        bits.append(str(col))
    asking = (text_ai or {}).get('asking_price')
    if asking:
        try:
            bits.append(f"ask ${int(float(asking)):,}")
        except (TypeError, ValueError):
            pass
    if not bits:
        # Extraction came up empty — fall back to a truncated body
        return body[:100] + '…' if len(body) > 100 else body
    return ' · '.join(bits)


def extract_vehicle_info_from_text(body):
    """Gemini Flash text-only extraction for free-form SMS prose. Cheap
    (~$0.0003/call). Returns dict with whatever fields it could parse, or {}.
    max_tokens=1500 to leave headroom for Flash's internal thinking tokens."""
    if not body:
        return {}
    try:
        result = gemini_call(_TEXT_EXTRACT_PROMPT + str(body)[:3000],
                             model='gemini-2.5-flash', max_tokens=1500)
        if not result:
            return {}
        raw = result.strip()
        # Strip markdown fences
        if raw.startswith('```'):
            raw = re.sub(r'^```(?:json)?\s*', '', raw)
            raw = re.sub(r'\s*```$', '', raw)
        # Salvage: if it's not pure JSON, find the {...} block
        if not raw.startswith('{'):
            m = re.search(r'\{[\s\S]*\}', raw)
            if m:
                raw = m.group(0)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as je:
            print(f'[text-extract] JSON parse failed: {je}; raw head={raw[:200]!r}', flush=True)
            # One retry with Pro (more reliable on edge cases) if Flash truncated
            try:
                result2 = gemini_call(_TEXT_EXTRACT_PROMPT + str(body)[:3000],
                                       model='gemini-2.5-pro', max_tokens=2000)
                if result2:
                    r2 = result2.strip()
                    if r2.startswith('```'):
                        r2 = re.sub(r'^```(?:json)?\s*', '', r2)
                        r2 = re.sub(r'\s*```$', '', r2)
                    if not r2.startswith('{'):
                        m2 = re.search(r'\{[\s\S]*\}', r2)
                        if m2:
                            r2 = m2.group(0)
                    return json.loads(r2)
            except Exception as e2:
                print(f'[text-extract] Pro retry failed: {e2}', flush=True)
            return {}
    except Exception as e:
        print(f'[text-extract] error: {e}', flush=True)
        return {}


def extract_miles_from_text(text, has_vin=False):
    """Regex miles extraction from SMS body. Handles 47k, 47,000 mi, 47000 miles,
    mileage 47k. If a VIN is present in the message, also accepts bare comma-
    grouped numbers ("VIN 47,000"). When the entire message is just a bare
    number (e.g. partner forwarded a photo with body "10000"), treat as miles
    in the valid range."""
    if not text:
        return None
    for m in _MILES_RE_LABELED.finditer(text):
        raw = m.group(1)
        # Suffix is the LAST char of the full match — k / i / s / e
        # (mi → i, miles → s, mileage → e, k → k).
        suffix = m.group(0).rstrip()[-1].lower()
        # MILES_DECIMAL_K_2026_05_15: handle decimal-k like "15.6k" → 15600.
        # Only when there's a dot AND the suffix is k.
        if '.' in raw and suffix == 'k':
            try:
                n = int(round(float(raw) * 1000))
            except ValueError:
                continue
        else:
            cleaned = raw.replace(',', '').replace('.', '').replace(' ', '')
            try:
                n = int(cleaned)
            except ValueError:
                continue
            if suffix == 'k' and n < 1000:
                n *= 1000
        if 100 <= n <= 999999:
            return n
    m = _MILES_RE_KSHORT.search(text)
    if m:
        n = int(m.group(1)) * 1000
        if 1000 <= n <= 999000:
            return n
    # Body-is-bare-numeric path — partner sent just digits (with optional
    # commas/spaces), no other content. Strong signal it's the mileage.
    stripped = text.strip()
    if re.fullmatch(r'[\d,\s.]{2,12}', stripped):
        digits = re.sub(r'[^\d]', '', stripped)
        if digits:
            try:
                n = int(digits)
                if 100 <= n <= 999999:
                    return n
            except ValueError:
                pass
    if has_vin:
        # When a VIN is present, accept bare "47,000" anywhere in the message
        for m in _MILES_RE_COMMA.finditer(text):
            n = int(m.group(1).replace(',', ''))
            if 100 <= n <= 999999:
                return n
        # Also accept bare digit runs (e.g. "WP0CB2A95MS248271\n\n28138")
        # by stripping the VIN itself first so we don't match its digits.
        cleaned = re.sub(r'[A-HJ-NPR-Z0-9]{17}', ' ', text)
        for m in re.finditer(r'\b(\d{3,6})\b', cleaned):
            n = int(m.group(1))
            if 100 <= n <= 999999:
                return n
    return None


def extract_vin_from_photo(image_url):
    """Read a VIN from a Twilio-hosted photo URL. Google Vision first, Gemini fallback."""
    try:
        resp = requests.get(image_url, auth=(TWILIO_SID, TWILIO_TOKEN), timeout=15)
        if resp.status_code != 200:
            return None
        img_bytes = resp.content
        media_type = resp.headers.get('Content-Type', 'image/jpeg').split(';')[0]
        return extract_vin_from_file(img_bytes, media_type)
    except Exception as e:
        print(f'VIN photo extract error: {e}', flush=True)
    return None


# ── Google Cloud Vision OCR (cheap, fast for pure text extraction) ────────────
GOOGLE_VISION_KEY_PATH = os.environ.get('GOOGLE_VISION_KEY_PATH', '/opt/expwholesale/google_vision_key.json')

def _google_vision_ocr(file_bytes):
    """Run Google Vision TEXT_DETECTION on raw image bytes. Returns all detected text as a single string, or None on failure."""
    if not os.path.exists(GOOGLE_VISION_KEY_PATH):
        return None
    try:
        from google.cloud import vision
        client = vision.ImageAnnotatorClient.from_service_account_json(GOOGLE_VISION_KEY_PATH)
        image = vision.Image(content=file_bytes)
        response = client.text_detection(image=image)
        if response.error.message:
            print(f'Google Vision error: {response.error.message}')
            return None
        if not response.text_annotations:
            return None
        # First annotation is the full detected text block
        return response.text_annotations[0].description
    except Exception as e:
        print(f'Google Vision call failed: {e}')
        return None


# ── iPacket canvas-OCR fallback helpers ──────────────────────────────────────
# When iPacket renders the sticker as <canvas> (pixels, no DOM text), the
# Trainer worker's regex returns 0 fields. These helpers OCR the screenshot
# server-side and re-extract MSRP/base/colors/options from the text output.

def _ipacket_screenshot_path(screenshot_field):
    """Resolve the stored iPacket screenshot reference to an absolute path."""
    if not screenshot_field:
        return None
    s = str(screenshot_field)
    if s.startswith('/ipacket_reports/'):
        return os.path.join(IPACKET_REPORTS_DIR, s[len('/ipacket_reports/'):])
    if os.path.isabs(s):
        return s
    return os.path.join(IPACKET_REPORTS_DIR, os.path.basename(s))


def _ipacket_with_vin_fallback(cur, bid_id, vin):
    """Fetch ipacket_lookups for bid_id; if the row has no parsed data and
    its screenshot is missing/blank (<80KB), substitute the best same-VIN
    row from another bid. Mirrors the /m/<token> mini-page fallback so the
    desktop bid card and the SMS-link mini-page render the same sticker.
    Returns the chosen row dict (possibly a sister bid's), or None."""
    try:
        cur.execute("SELECT * FROM ipacket_lookups WHERE bid_id = %s", (bid_id,))
        ipacket = cur.fetchone()
    except Exception:
        return None
    if not ipacket or not vin:
        return ipacket
    _cur_path = (ipacket.get('screenshot') or '').lstrip('/')
    _cur_size = 0
    try:
        if _cur_path:
            _abs = os.path.join('/opt/expwholesale', _cur_path)
            _cur_size = os.path.getsize(_abs) if os.path.exists(_abs) else 0
    except Exception:
        _cur_size = 0
    _cur_has_data = bool(ipacket.get('total_msrp') or ipacket.get('base_price') or ipacket.get('exterior_color'))
    if _cur_has_data or _cur_size >= 80_000:
        return ipacket
    try:
        cur.execute("""SELECT * FROM ipacket_lookups
                        WHERE vin=%s AND bid_id != %s
                        ORDER BY looked_up_at DESC LIMIT 10""",
                    (vin, bid_id))
        candidates = cur.fetchall()
        best = None
        best_score = 0
        for c in candidates:
            cp = (c.get('screenshot') or '').lstrip('/')
            cs = 0
            try:
                if cp:
                    ap = os.path.join('/opt/expwholesale', cp)
                    cs = os.path.getsize(ap) if os.path.exists(ap) else 0
            except Exception:
                pass
            has_data = bool(c.get('total_msrp') or c.get('base_price') or c.get('exterior_color'))
            score = (1_000_000 if has_data else 0) + cs
            if score > best_score:
                best_score = score
                best = c
        if best and best_score > _cur_size:
            print(f'[ipacket-fallback] bid={bid_id} vin={vin} -> sister bid_id={best["bid_id"]} (score {best_score} vs {_cur_size})', flush=True)
            return best
    except Exception as _e:
        print(f'[ipacket-fallback] error bid={bid_id}: {_e}', flush=True)
    return ipacket


def _extract_sticker_options(text):
    """Parse factory option line items from sticker text (OCR or DOM).
    Mirrors enrich/ipacket.py _extract_options so EW + worker agree."""
    if not text:
        return []
    options = []
    seen = set()
    skip_pat = re.compile(
        r'^\s*('
        r'TOTAL|SUB[-\s]?TOTAL|GRAND\s+TOTAL|'
        r'BASE(?!\s+PRICE\s+INCLUDES)|MSRP|SUGGESTED\s+RETAIL|'
        r'VIN|STOCK|MODEL\s+YEAR|BODY\s+TYPE|'
        r'EXTERIOR|INTERIOR|PAINT|TRIM\s+COLOR|'
        r'DESTINATION|DELIVERY|FREIGHT|HANDLING|GAS\s+GUZZLER|'
        r'MPG|CITY\s+MPG|HWY\s+MPG|COMBINED\s+MPG|'
        r'ENGINE|TRANSMISSION|DRIVE\s+TYPE|FUEL|'
        r'WARRANTY|EPA|EMISSIONS|SAFETY|CRASH|'
        r'STANDARD\s+FEATURES?|STANDARD\s+EQUIPMENT|'
        r'OPTIONAL\s+EQUIPMENT$|FACTORY\s+OPTIONS?$|ADDITIONAL\s+OPTIONS?$|'
        r'ASSEMBLED|MADE\s+IN|BUILT\s+IN|'
        r'PARTS\s+CONTENT|LABOR\s+CONTENT|PARTS\s+AND\s+LABOR|'
        r'COUNTRY\s+OF\s+ORIGIN|FINAL\s+ASSEMBLY'
        r')\b',
        re.I
    )
    line_pat = re.compile(
        r'^\s*'
        r'([A-Za-z][A-Za-z0-9\'"\-\s/&,.+()%]{3,79}?)'
        r'\s*[.\s]{1,}\s*'
        r'\$?\s*(\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d{3,7}(?:\.\d{2})?)'
        r'\s*$'
    )
    for raw_line in text.split('\n'):
        line = raw_line.strip()
        if len(line) < 8 or len(line) > 160:
            continue
        m = line_pat.match(line)
        if not m:
            continue
        name = m.group(1).strip().rstrip('.').strip()
        if len(name) < 4:
            continue
        if skip_pat.match(name):
            continue
        alpha_chars = sum(1 for c in name if c.isalpha())
        if alpha_chars < 4:
            continue
        try:
            price = int(float(m.group(2).replace(',', '')))
        except ValueError:
            continue
        if not (50 <= price <= 100_000):
            continue
        key = (name.upper(), price)
        if key in seen:
            continue
        seen.add(key)
        options.append({'name': name, 'price': price})
    return options


def _parse_sticker_text(text):
    """Regex-extract MSRP / base / colors / options from OCR or DOM sticker text."""
    result = {
        'total_msrp': None, 'base_price': None,
        'exterior_color': None, 'interior_color': None,
        'options': [],
    }
    if not text or len(text) < 50:
        return result

    for pat in (
                # iPacket PDF window-sticker format
                r"TOTAL\s+MANUFACTURER'?S?\s+SUGGESTED\s+RETAIL\s+PRICE\s*[:$]?\s*\$?\s*([\d,]+)",
                r"AS\s+DELIVERED\s+PRICE\s*[:$]?\s*\$?\s*([\d,]+)",
                r"TOTAL\s+VEHICLE\s+PRICE\*?\s*[:$]?\s*\$?\s*([\d,]+)",
                r"Net\s+Total\s*[:$]?\s*\$?\s*([\d,]+)",  # Mercedes-Benz
                # Common formats
                r'TOTAL\s+(?:PREDICTED\s+)?PRICE\s*[:$]?\s*\*?\s*\$?\s*([\d,]+)',  # RAM puts * between : and $
                r'TOTAL\s+MSRP\s*[:$]?\s*\$?\s*([\d,]+)',
                r'(?<!BASE\s)MSRP\s*[:$]?\s*\$?\s*([\d,]+)'):
        m = re.search(pat, text, re.I)
        if m:
            try:
                v = int(m.group(1).replace(',', ''))
                if 1000 < v < 10_000_000:
                    result['total_msrp'] = v
                    break
            except ValueError:
                pass

    for pat in (
                # iPacket PDF window-sticker format
                r"BASE\s+MANUFACTURER'?S?\s+SUGGESTED\s+RETAIL\s+PRICE\s*[:$]?\s*\$?\s*([\d,]+)",
                # Common formats
                r'BASE\s+SUGGESTED\s+PRICE\s*[:$]?\s*\$?\s*([\d,]+)',
                r'BASE\s+PRICE\s*[:$]?\s*\$?\s*([\d,]+)',
                r'BASE\s+MSRP\s*[:$]?\s*\$?\s*([\d,]+)'):
        m = re.search(pat, text, re.I)
        if m:
            try:
                v = int(m.group(1).replace(',', ''))
                if 1000 < v < 10_000_000:
                    result['base_price'] = v
                    break
            except ValueError:
                pass

    m = re.search(r'EXTERIOR(?:\s+COLOR)?[:\s]+([A-Za-z][A-Za-z\s/-]{2,40})', text, re.I)
    if m:
        result['exterior_color'] = m.group(1).strip().split('\n')[0].strip()
    m = re.search(r'INTERIOR(?:\s+COLOR)?[:\s]+([A-Za-z][A-Za-z\s/-]{2,40})', text, re.I)
    if m:
        result['interior_color'] = m.group(1).strip().split('\n')[0].strip()

    result['options'] = _extract_sticker_options(text)
    return result


def extract_vin_from_file(file_bytes, media_type='image/jpeg'):
    """Extract VIN from image.

    Order (2026-05-10): Gemini 2.5 Pro PRIMARY (handwriting prompt + check-digit
    validation), then Google Vision (cheap OCR fallback) and Gemini Flash as
    cross-checks if Pro gave nothing valid. Every layer now gates on
    vin_check_digit_valid() so structurally-valid-but-wrong VINs (e.g. SS read
    as 55) can no longer slip through silently. Pre-2026-05-10 the order was
    Vision → Flash → Pro and only Pro validated; that masked OCR errors as
    "valid 17-char strings" and cost us ingestion accuracy."""
    # v4 routing: if call originated from EW_TEST_USER_PHONE, try the home-machine
    # Qwen2.5-VL-7B + LoRA first. Fall through to Gemini path on miss/timeout.
    if should_use_v4():
        v4_vin = v4_extract(file_bytes, task='vin')
        if v4_vin and VIN_RE.match(v4_vin) and vin_check_digit_valid(v4_vin):
            print(f'[OCR] VIN via v4 (test user, check digit OK): {v4_vin}', flush=True)
            return v4_vin
        print('[OCR] v4 missed/skipped, going to Gemini Pro', flush=True)

    # Primary: Gemini 2.5 Pro with handwriting prompt + 2 attempts.
    hw_prompt = (
        'Read the VIN from this image. The image may contain a handwritten note, '
        'a VIN sticker, a license plate, or any vehicle identifier. Apply strict VIN rules:\n'
        '- Exactly 17 characters (A-Z, 0-9)\n'
        '- Letters I, O, Q are NEVER valid — substitute 1, 0, 0\n'
        '- Handwriting / low-DPI: resolve 1/7, 0/O/Q, 5/S/G, 2/Z, 4/Y/A, 8/B confusion\n'
        '- The 9th character is a math check digit. Common values: 0-9 or X.\n'
        '- Common prefixes: 1G, 1F, 1C, 1H, 2H, 5J, 5Y, 7S, WP, WB, WD, YV\n\n'
        'Reply with ONLY the 17-char VIN. No other text.'
    )
    candidates = []
    for attempt in range(2):
        result = gemini_call(hw_prompt, image_bytes=file_bytes, mime=media_type,
                             model='gemini-2.5-pro', max_tokens=2000,
                             temperature=0.2 + attempt * 0.3)
        if not result:
            continue
        m = re.search(r'\b[A-HJ-NPR-Z0-9]{17}\b', result.strip().upper())
        if not m:
            continue
        vin = m.group(0)
        candidates.append(vin)
        if vin_check_digit_valid(vin):
            print(f'[OCR] VIN via Gemini Pro (primary, check digit OK): {vin}', flush=True)
            return vin
    if candidates:
        print(f'[OCR] Gemini Pro returned candidates but none passed check digit; cross-checking with Vision/Flash. Pro guesses: {candidates}', flush=True)

    # Cross-check 1: Google Vision OCR (cheap). Accept ONLY if check digit valid.
    text = _google_vision_ocr(file_bytes)
    if text:
        up = text.upper()
        for m in re.finditer(r'\b[A-HJ-NPR-Z0-9]{17}\b', up):
            cand = m.group(0)
            if vin_check_digit_valid(cand):
                print(f'[OCR] VIN via Google Vision cross-check (check digit OK): {cand}', flush=True)
                return cand
        # I/O/Q substitution salvage
        for m in re.finditer(r'\b[A-Z0-9]{17}\b', up):
            cand = m.group(0).replace('O', '0').replace('I', '1').replace('Q', '0')
            if VIN_RE.match(cand) and vin_check_digit_valid(cand):
                print(f'[OCR] VIN via Google Vision (O->0 recovered, check digit OK): {cand}', flush=True)
                return cand

    # Cross-check 2: Gemini Flash. Accept ONLY if check digit valid.
    flash_result = gemini_call(VIN_PROMPT, image_bytes=file_bytes, mime=media_type,
                               model='gemini-2.5-flash', max_tokens=100)
    if flash_result:
        flash_vin = flash_result.strip().upper()
        m = re.search(r'\b[A-HJ-NPR-Z0-9]{17}\b', flash_vin)
        if m and vin_check_digit_valid(m.group(0)):
            print(f'[OCR] VIN via Gemini Flash cross-check (check digit OK): {m.group(0)}', flush=True)
            return m.group(0)

    # No layer produced a check-digit-valid VIN. Return the first Pro candidate
    # (best informed guess) with a manual-review log line so the bid still has
    # SOMETHING to work from instead of NULL.
    if candidates:
        print(f'[OCR] All layers failed check digit; returning Pro best-guess for manual review: {candidates[0]}', flush=True)
        return candidates[0]
    return None


def extract_mileage_from_file(file_bytes, media_type='image/jpeg'):
    """Extract odometer mileage. Google Vision first, Claude fallback."""
    # v4 routing: same rule as VIN — only the test user routes to home v4.
    if should_use_v4():
        v4_miles = v4_extract(file_bytes, task='odometer')
        if v4_miles and v4_miles.isdigit() and 100 <= int(v4_miles) <= 999999:
            print(f'[OCR] miles via v4 (test user): {v4_miles}', flush=True)
            return v4_miles
        print('[OCR] v4 odo missed/skipped, falling back', flush=True)
    text = _google_vision_ocr(file_bytes)
    if text:
        up = text.upper()
        # Hard stop: VIN plates / weight stickers are NOT odometer photos
        # If we see clear VIN-plate indicators, don't guess a mileage
        plate_indicators = ['GVWR', 'GAWR', 'LBS', 'MFD BY', 'DATE OF MANUFACTURE',
                           'VEHICLE SAFETY', 'BUMPER, AND THEFT', 'FEDERAL MOTOR']
        if sum(1 for ind in plate_indicators if ind in up) >= 2:
            print('[OCR] Google Vision: detected VIN plate (not odometer), returning None', flush=True)
            return None

        # First: "Mileage NNN" / "Odometer: NNN" / "ODO NNN" — label BEFORE number.
        # Common in VIN-scanner app screenshots (Carbly, vAuto, Carfax, AutoCheck).
        # Without this, a Carbly screenshot with "In ZIP Code 33426\nMileage 23,576"
        # gets the 23,576 candidate rejected because "ZIP" appears in the 50-char
        # context window of the unlabeled fallback.
        labeled_before = re.findall(
            r'\b(?:MILEAGE|ODOMETER|ODO|MILES)\b[\s:\*\-\.#\(\)]+(\d{1,3}(?:,\d{3})+|\d{3,7})\b',
            up)
        if labeled_before:
            for c in labeled_before:
                n = int(c.replace(',', ''))
                if 100 <= n <= 999999:
                    print(f'[OCR] Mileage via Google Vision (label-before): {n}', flush=True)
                    return n

        # MILES_OCR_DECIMAL_K_2026_05_15: K-suffix shorthand like "47K",
        # "15.6K", "100.5K". Rare on physical dashboards but appears in
        # mobile-app summary screens and typed-note photos. Convert via
        # float * 1000.
        for _cm in re.finditer(r'(\d{1,3}(?:\.\d{1,2})?)\s*K\b', up):
            try:
                _n = int(round(float(_cm.group(1)) * 1000))
            except ValueError:
                continue
            if 100 <= _n <= 999999:
                print(f'[OCR] Mileage via Google Vision (K-suffix): {_n}',
                      flush=True)
                return _n

        # Then: numbers explicitly labeled "mi" / "miles" / "km" — label AFTER number.
        # TESLA_DASH_FIX_2026_05_15: Tesla displays many "mi"-suffixed values
        # (range, trip, battery-to-empty, status-bar range). The literal
        # odometer reading is the LARGEST plausible number on screen — an
        # odometer can't legally go down, so any same-photo competitor like
        # "234 mi range remaining" will be smaller than the real reading.
        # Old code returned the FIRST match, which always picked the
        # status-bar range on Tesla dashes. Fix: collect all, filter Tesla
        # noise contexts, return max.
        _labeled_noise = (
            'RANGE', 'TRIP', 'MI/KWH', 'MIKWH', 'REMAINING', 'REMAIN',
            'BATTERY', 'TO EMPTY', 'UNTIL EMPTY', 'CHARGE',
            'AVERAGE', 'EST. MI', 'EST MI', 'KWH', 'TO GO',
            'EFFICIENCY', 'CONSUMPTION',
        )
        _mi_candidates = []
        for _mm in re.finditer(
                r'(\d{1,3}(?:,\d{3})+|\d{3,7})\s*(?:MI|MILES|KM)\b', up):
            _ns = _mm.group(1)
            _n = int(_ns.replace(',', ''))
            if not (100 <= _n <= 999999):
                continue
            _cs = max(0, _mm.start() - 60)
            _ctx = up[_cs:_mm.end() + 10]
            if any(_noise in _ctx for _noise in _labeled_noise):
                continue
            _mi_candidates.append(_n)
        if _mi_candidates:
            _result = max(_mi_candidates)
            print(f'[OCR] Mileage via Google Vision (labeled, max of '
                  f'{len(_mi_candidates)}): {_result}', flush=True)
            return _result

        # Fallback: any 4-7 digit number but avoid obvious false positives
        # (skip numbers immediately followed/preceded by LBS, KG, $, year contexts)
        candidates = []
        for m in re.finditer(r'\b(\d{1,3}(?:,\d{3})+|\d{3,7})\b', up):
            num_str = m.group(1)
            n = int(num_str.replace(',', ''))
            if not (100 <= n <= 999999):
                continue
            # Check surrounding context (50 chars before, 20 after)
            ctx_start = max(0, m.start() - 50)
            ctx_end = min(len(up), m.end() + 20)
            ctx = up[ctx_start:ctx_end]
            # Skip if near weight / price / year indicators
            bad = ['LBS', 'KG', 'GVWR', 'GAWR', '$', 'MSRP', 'PRICE', 'PROD',
                   'YEAR', 'MODEL YEAR', 'ZIP', 'PHONE', 'STOCK']
            if any(b in ctx for b in bad):
                continue
            # Reject obvious year values (1990-2030)
            if 1990 <= n <= 2030:
                continue
            candidates.append(n)

        if candidates:
            # Prefer the largest (odometers are usually prominent)
            result = max(candidates)
            print(f'[OCR] Mileage via Google Vision: {result}', flush=True)
            return result
    print('[OCR] Google Vision missed mileage, falling back to Gemini Flash', flush=True)

    # GEMINI_FLASH_MILES_OCR_2026_05_17: Gemini 2.5 Flash with thinking
    # disabled. Overnight bench on 72 odometer photos:
    #   Sonnet 4.6                : 29.2%  3.3s/call  ~$10/1000
    #   Gemini Flash (no-think)   : 94.4%  1.9s/call  ~$1/1000
    # Same prompt; the only meaningful difference is the model + disabled
    # thinking. 3x more accurate, 1.7x faster, 10x cheaper.
    _odo_prompt = (
        "Look at this image. Is there a vehicle odometer reading visible? "
        "An odometer is a digital or analog display on the vehicle's "
        "instrument cluster showing total lifetime mileage. NOT a trip "
        "meter, NOT a range estimate, NOT a sticker price, NOT a license "
        "plate, NOT a sale price, NOT a tire spec, NOT an MSRP number. "
        "If you see a CLEAR odometer reading, reply with ONLY the integer "
        "(no commas, no units). If not, reply with the single word NONE."
    )
    gresult = gemini_call(_odo_prompt, image_bytes=file_bytes, mime=media_type,
                          model='gemini-2.5-flash', max_tokens=64,
                          temperature=0, disable_thinking=True)
    if gresult:
        up = gresult.strip().upper()
        if up != 'NONE':
            digits = re.sub(r'[^\d]', '', up)
            if digits:
                n = int(digits)
                if 100 <= n <= 999999:
                    print(f'[OCR] Mileage via Gemini Flash (no-think): {n}',
                          flush=True)
                    return n
    return None


def extract_color_from_file(file_bytes, media_type='image/jpeg'):
    """Identify exterior vehicle color via Gemini Flash."""
    prompt = (
        'What is the exterior color of the vehicle in this photo? '
        'Reply with only the color name (e.g. White, Black, Silver, Gray, Red, Blue, Green, Brown, Gold, Orange, Yellow, Purple). '
        'If you cannot clearly see a vehicle exterior, reply UNKNOWN.'
    )
    result = gemini_call(prompt, image_bytes=file_bytes, mime=media_type,
                         model='gemini-2.5-flash', max_tokens=20)
    if result:
        color = result.strip().title()
        if color.upper() != 'UNKNOWN' and color:
            return color
    return None


# ── Vehicle info extraction (any image with VIN/miles/etc) ──────────────────

CARFAX_PROMPT = (
    'You are analyzing an image that contains vehicle information.\n'
    'The image could be ANY of these:\n'
    '- A CARFAX or AutoCheck vehicle history report\n'
    '- A phone photo of a VIN sticker (door jamb, windshield, dashboard)\n'
    '- An odometer / dashboard photo\n'
    '- A Monroney window sticker\n'
    '- A dealer inventory listing (Autotrader, Cars.com, CarGurus)\n'
    '- A private-party listing screenshot\n'
    '- A photo of a car exterior (just to identify color)\n'
    '- A HANDWRITTEN note or paper with VIN, miles, price, etc.\n'
    '- A whiteboard, notepad, or business card with vehicle info\n'
    '- Any combination of the above\n\n'
    'Extract whatever information is visible and return JSON with these exact keys:\n'
    '{\n'
    '  "vin": "17-char VIN or null",\n'
    '  "year": 2024 or null,\n'
    '  "make": "Toyota" or null,\n'
    '  "model": "Camry" or null,\n'
    '  "trim": "SE" or null,\n'
    '  "mileage": 45000 or null,\n'
    '  "title_status": "Clean" or "Salvage" or "Rebuilt" or null,\n'
    '  "accidents": 0 or null,\n'
    '  "owners": 2 or null,\n'
    '  "color": "White" or null,\n'
    '  "asking_price": 25000 or null\n'
    '}\n\n'
    'RULES:\n'
    '- VIN MUST be exactly 17 characters (A-Z, 0-9). Count the characters carefully — if you only see 16 or have 18, re-read the image.\n'
    '- Letters I, O, Q are NEVER valid in a VIN. If you see what looks like O, it is 0. If you see I, it is 1. If you see Q, it is 0.\n'
    '- For HANDWRITTEN VINs: handwriting often has 1/7 confusion, 0/O/Q confusion, 5/S/G confusion, 2/Z confusion, 4/A confusion. Apply standard VIN rules to disambiguate.\n'
    '- If the handwriting shows crossed-out characters, use the intended (uncrossed) characters only.\n'
    '- Common VIN prefixes: 1G=GM/USA, 1F=Ford, 1C=Chrysler, 1H/2H=Honda, 5J=Acura, 5Y=Tesla, 5Y/JN=Nissan, WP=Porsche, WB=BMW, WD=Mercedes, 7S=Tesla, YV=Volvo. Use this to validate the first 3 chars.\n'
    '- Only extract year from the VIN decode or a clearly labeled year field — do NOT guess from the model.\n'
    '- Mileage: use the LAST/MOST RECENT odometer reading if multiple are shown. Ignore GVWR/GAWR weights.\n'
    '- Accidents: only set this if the image is a Carfax/AutoCheck report (number shown). Otherwise null.\n'
    '- Owners: only set this if the image is a Carfax/AutoCheck report. Otherwise null.\n'
    '- Color: only set if the exterior color is clearly visible (car photo or listing description)\n'
    '- asking_price: only set if a listing/sticker shows an asking price, MSRP, or sale price\n'
    '- If a field is not visible, not applicable, or unclear, set it to null\n'
    '- Return ONLY the JSON object, nothing else. No markdown fences, no commentary.\n'
)


def extract_carfax_info(file_bytes, media_type='image/jpeg'):
    """Extract vehicle info from ANY vehicle-related image via Gemini.
    Works on Carfax/AutoCheck reports, VIN stickers, odometer photos,
    Monroney stickers, listings, handwritten notes, etc.

    Uses Gemini 2.5 Pro for better accuracy on handwriting and ambiguous text."""
    raw = gemini_call(CARFAX_PROMPT, image_bytes=file_bytes, mime=media_type,
                      model='gemini-2.5-pro', max_tokens=3000)
    if not raw:
        return {}
    try:
        # Strip markdown code fences if present
        if raw.startswith('```'):
            raw = re.sub(r'^```(?:json)?\s*', '', raw)
            raw = re.sub(r'\s*```$', '', raw)
        return json.loads(raw)
    except Exception as e:
        print(f'Carfax JSON parse error: {e}', flush=True)
    return {}


def extract_carfax_multi(files_list):
    """Run Carfax extraction on multiple images, merge results (first non-null
    wins). VINs are only accepted if they pass ISO 3779 check digit — protects
    against OCR misreads on glare-y / angled windshield VIN tags shipping the
    wrong car through the pipeline."""
    merged = {}
    fields = ['vin', 'year', 'make', 'model', 'trim', 'mileage',
              'title_status', 'accidents', 'owners', 'color', 'asking_price']
    for file_bytes, media_type in files_list:
        info = extract_carfax_info(file_bytes, media_type)
        # Validate VIN before considering it
        if info.get('vin'):
            _v = str(info['vin']).strip().upper()
            if len(_v) != 17 or not vin_check_digit_valid(_v):
                print(f'[carfax-multi] rejected VIN "{_v}" (check digit fail)', flush=True)
                # Surface the candidate so the async caller can show it to
                # the user (notes badge) + SMS the partner asking for the
                # text VIN. First rejection wins; later-photo VINs override
                # only if shorter/longer attempts came first.
                if not merged.get('_rejected_vin'):
                    merged['_rejected_vin'] = _v
                info.pop('vin', None)
        for f in fields:
            if not merged.get(f) and info.get(f) is not None:
                merged[f] = info[f]
        # If we have VIN + mileage, good enough to stop early
        if merged.get('vin') and merged.get('mileage'):
            break
    return merged


# ── Helpers ──────────────────────────────────────────────────────────────────

_TWILIO_MAGIC_RE = re.compile(r'^\+1555555\d{4}$')

def send_sms(to, body):
    """Send SMS via Twilio. Returns True on success, False on failure. Never raises.

    Twilio reserves +1 (555) 555-XXXX for testing; live accounts reject these
    with HTTP 400 ("Invalid To"). We short-circuit here so test fixtures
    leaking into prod data don't generate noisy retry storms.
    """
    if not to or to.startswith('field:') or not TWILIO_SID or not TWILIO_TOKEN or not TWILIO_PHONE:
        return False
    if _TWILIO_MAGIC_RE.match(to):
        print(f'SMS skipped: Twilio magic number {to[-4:]} (test fixture)', flush=True)
        return False
    try:
        client = TwilioClient(TWILIO_SID, TWILIO_TOKEN)
        client.messages.create(to=to, from_=TWILIO_PHONE, body=body)
        return True
    except Exception as e:
        print(f'SMS send error: {e}')
        return False


# ── Bidder-identity helpers (Phase 1/2/3 gating + name capture) ─────────────
# Phase 1 (mini-page) and Phase 2 (full report with AI price) are restricted
# to a 4-number whitelist of full-broker clients. Phase 3 (single ack only,
# no AI links) is the default for everyone else. The same whitelist gates
# both Phase 1 and Phase 2 via PHASE2_PHONE_GATE (kept as one env to
# avoid drift between the two).

def _phone_digits(p):
    """Last-10-digit normalization. '+14074309675' / '4074309675' / '14074309675'
    all collapse to '4074309675'. Empty/None returns ''."""
    d = ''.join(c for c in (p or '') if c.isdigit())
    if len(d) == 11 and d[0] == '1':
        d = d[1:]
    return d


def _bid_full_gate_digits():
    """Digit-set of phones that get Phase 1 + Phase 2 (mini-page + full
    report). UNION of PHASE2_PHONE_GATE env baseline + active rows in
    gated_phones (gate_type='full_broker'), via gate_helpers (30s cache).
    Empty result = no gating (everyone gets Phase 1+2). Add/remove numbers
    from /admin/phone-gates — no restart needed."""
    digits = gate_helpers.gate_digits('full_broker')
    return digits if digits else None  # None sentinel = open (back-compat)


def _is_full_broker_phone(phone):
    """True if this phone is in the 4-number Phase 1/2 whitelist."""
    allowed = _bid_full_gate_digits()
    if allowed is None:
        return True  # no gating configured → everyone is full-broker
    return _phone_digits(phone) in allowed


def _lookup_bidder(cur, phone):
    """Resolve who a sender is. Returns dict:
        {'kind': 'partner'|'returning'|'unknown',
         'name': str|None,
         'partner_dealer_id': int|None,
         'dealer_name': str|None}

    Lookup precedence (first hit wins):
      1. partner_users  — known dealer partner (auto-link partner_dealer_id)
      2. bidder_contacts — phone→name memory from a prior held-bid release
      3. ANY prior row in bids — they've submitted before (pre-Phase-3 era).
         Classified as 'returning' with name=None so they get the generic
         Phase 3 ack ("Bid #N received — we'll contact you back shortly.")
         and skip the held-bid name-ask flow. This prevents long-standing
         customers (e.g. +19546092424 with 9 prior bids) from being treated
         as first-timers just because we never captured their name.
      4. Else 'unknown' — true first-time sender, held in awaiting_name.
    """
    if not phone:
        return {'kind': 'unknown', 'name': None,
                'partner_dealer_id': None, 'dealer_name': None}
    cur.execute("""SELECT pu.full_name, pu.dealer_id, d.name AS dealer_name
                     FROM partner_users pu
                     JOIN dealers d ON d.id = pu.dealer_id
                    WHERE pu.phone = %s
                    LIMIT 1""", (phone,))
    pu = cur.fetchone()
    if pu:
        return {'kind': 'partner',
                'name': pu.get('full_name'),
                'partner_dealer_id': pu.get('dealer_id'),
                'dealer_name': pu.get('dealer_name')}
    cur.execute("SELECT name FROM bidder_contacts WHERE phone = %s LIMIT 1",
                (phone,))
    bc = cur.fetchone()
    if bc:
        return {'kind': 'returning',
                'name': bc.get('name'),
                'partner_dealer_id': None,
                'dealer_name': None}
    # Final check: prior bid in the bids table? They've submitted before
    # (even if pre-dating the bidder_contacts feature), so treat as a
    # known returning sender. No name yet, but we won't hold them.
    cur.execute("SELECT 1 FROM bids WHERE phone = %s LIMIT 1", (phone,))
    if cur.fetchone():
        return {'kind': 'returning', 'name': None,
                'partner_dealer_id': None, 'dealer_name': None}
    return {'kind': 'unknown', 'name': None,
            'partner_dealer_id': None, 'dealer_name': None}


_NAME_STRIP_RE = re.compile(
    r"^\s*(?:i'?m|im|its|it's|this is|my name is|name is|name:|im called|"
    r"call me|name's|name s)\s+",
    re.IGNORECASE,
)
_NAME_TRAILING_RE = re.compile(
    r"\s+(?:here|speaking|btw|by the way)\s*[.!]?\s*$",
    re.IGNORECASE,
)


def _parse_name_reply(body):
    """Extract a plausible bidder name from a free-form SMS reply.
    Returns the cleaned name string, or None if the body doesn't look like
    a name (no alpha tokens, or pure ack/digit/punctuation noise).

    Conservative on purpose — better to re-ask than to save 'ok' as someone's
    name. Caller is expected to re-prompt the user when this returns None.
    """
    if not body:
        return None
    s = body.strip()
    # Strip common lead-ins.
    s = _NAME_STRIP_RE.sub('', s)
    s = _NAME_TRAILING_RE.sub('', s)
    s = s.strip(' .,!?:-')
    if not s:
        return None
    # Reject single-char, pure-digit, or known noise replies.
    if len(s) < 2:
        return None
    alpha = re.sub(r'[^A-Za-z]', '', s)
    if len(alpha) < 2:
        return None
    _NOISE = {'ok', 'okay', 'k', 'kk', 'yes', 'yeah', 'yep', 'yup',
              'no', 'nope', 'nah', 'sure', 'fine', 'thanks', 'thx',
              'thank you', 'hi', 'hey', 'hello', 'sup'}
    if s.lower() in _NOISE:
        return None
    return s[:64]


_PHASE3_DEFAULT_TEXT = "thanks {name}, bid #{bid_id} received — we'll contact you back shortly."
_PHASE3_NO_NAME_TEXT = "bid #{bid_id} received — we'll contact you back shortly."


def _send_phase3_ack(cur, bid_id, phone, name=None):
    """Send the Phase 3 ack SMS. Idempotent via bids.phase3_notified_at.
    Returns True if SMS sent (or already sent earlier), False on failure.
    Caller commits the transaction."""
    if not phone or phone.startswith('field:'):
        return False
    cur.execute("SELECT phase3_notified_at FROM bids WHERE id = %s", (bid_id,))
    row = cur.fetchone()
    if not row:
        return False
    if row.get('phase3_notified_at'):
        return True  # already sent
    if name:
        body = _PHASE3_DEFAULT_TEXT.format(name=name, bid_id=bid_id)
    else:
        body = _PHASE3_NO_NAME_TEXT.format(bid_id=bid_id)
    sent = send_sms(phone, body)
    if sent:
        cur.execute(
            "UPDATE bids SET phase3_notified_at = NOW() WHERE id = %s",
            (bid_id,),
        )
        print(f'[phase3-ack] bid={bid_id} → {phone} name={name!r}', flush=True)
    else:
        print(f'[phase3-ack] SMS failed bid={bid_id} → {phone}', flush=True)
    return sent


def _upsert_bidder_contact(cur, phone, name):
    """Record (or refresh) a phone→name memory. Increments bid_count + bumps
    last_bid_at on every call. Caller commits."""
    if not phone or not name:
        return
    cur.execute("""
        INSERT INTO bidder_contacts (phone, name, first_bid_at, last_bid_at,
                                     bid_count)
        VALUES (%s, %s, NOW(), NOW(), 1)
        ON CONFLICT (phone) DO UPDATE
           SET name = EXCLUDED.name,
               last_bid_at = NOW(),
               bid_count = bidder_contacts.bid_count + 1
    """, (phone, name))


def time_ago(dt):
    if not dt:
        return ''
    now = datetime.now()
    if dt.tzinfo:
        dt = dt.replace(tzinfo=None)
    diff = now - dt
    s = diff.total_seconds()
    if s < 60:
        return 'just now'
    if s < 3600:
        return f'{int(s//60)}m ago'
    if s < 86400:
        return f'{int(s//3600)}h ago'
    return f'{int(s//86400)}d ago'


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route('/')
def dashboard():
    db = get_db()
    cur = db.cursor()

    # ── 2026-05-12 Network claims: bids dealers said YES to, not yet confirmed sold ──
    cur.execute("""
        SELECT bp.bid_id, bp.dealer_id, bp.claimed_at, bp.score,
               d.name AS dealer_name, d.salesperson AS dealer_salesperson,
               b.year, b.make, b.model, b.network_ask::int AS ask
          FROM bid_pushes bp
          JOIN dealers d ON d.id = bp.dealer_id
          JOIN bids    b ON b.id = bp.bid_id
         WHERE bp.claimed_at IS NOT NULL
           AND bp.claim_late IS NOT TRUE
           AND bp.sold_confirmed_at IS NULL
         ORDER BY bp.claimed_at DESC
    """)
    network_claims = [dict(r) for r in cur.fetchall()]
    network_claims_by_bid = {c['bid_id']: c for c in network_claims}

    cur.execute("SELECT status, COUNT(*) as cnt FROM bids GROUP BY status")
    stats = {'new': 0, 'reviewing': 0, 'bid_sent': 0, 'passed': 0, 'bought': 0, 'total': 0}
    for r in cur.fetchall():
        stats[r['status']] = int(r['cnt'])
        stats['total'] += int(r['cnt'])

    cur.execute("SELECT COUNT(*) as cnt FROM bids WHERE created_at::date = CURRENT_DATE")
    stats['today'] = int(cur.fetchone()['cnt'])

    cur.execute("SELECT COUNT(*) as cnt FROM bids WHERE phone LIKE 'field:%'")
    stats['field'] = int(cur.fetchone()['cnt'])

    # Cross-dealer aged-inventory + price-drop counts (for the 4 dashboard
    # cards that drill into /dealers/aged/<bucket> + /dealers/price-drops).
    # Pulls from the dealer_stats view (auto-aggregates across all active
    # partner dealers — Marino, TXT Charlie, Napletons, and any future add).
    try:
        cur.execute("""
            SELECT COALESCE(SUM(age_30_60),0) AS a30_60,
                   COALESCE(SUM(age_60_90),0) AS a60_90,
                   COALESCE(SUM(age_over_90),0) AS a90_plus
            FROM dealer_stats WHERE active
        """)
        r = cur.fetchone()
        stats['aged_30_60'] = int(r['a30_60'] or 0)
        stats['aged_60_90'] = int(r['a60_90'] or 0)
        stats['aged_90_plus'] = int(r['a90_plus'] or 0)
        # Active cars with a price drop observed scan-over-scan. Sold/missing
        # excluded — drops only matter for cars you can still buy. Sticky
        # for the row's active life: persists until status flips out of active.
        cur.execute("""
            SELECT COUNT(*) AS cnt FROM dealer_inventory i
            JOIN dealers d ON d.id = i.dealer_id
            WHERE i.status='active' AND d.active
              AND i.price_drop_amount IS NOT NULL
        """)
        stats['price_drops'] = int(cur.fetchone()['cnt'])
    except Exception:
        stats['aged_30_60'] = stats['aged_60_90'] = stats['aged_90_plus'] = stats['price_drops'] = 0

    status_filter = request.args.get('status', 'all')
    rep_filter = request.args.get('rep', 'all')

    # Fetch field reps for dropdown
    cur.execute("""
        SELECT phone, name FROM contacts WHERE phone LIKE 'field:%' ORDER BY name
    """)
    reps = [{'phone_key': r['phone'].replace('field:', ''), 'name': r['name'] or r['phone'].replace('field:', '')}
            for r in cur.fetchall()]

    # Build filter conditions
    conditions, params = [], []
    if status_filter == 'field':
        conditions.append("b.phone LIKE 'field:%'")
    elif status_filter != 'all':
        conditions.append("b.status = %s")
        params.append(status_filter)
    if rep_filter != 'all':
        conditions.append("b.phone = %s")
        params.append(f'field:{rep_filter}')

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    q = """
        SELECT b.*, c.name as contact_name, c.company as contact_company,
               c.role as contact_role, d.name as partner_dealer_name,
               dl.current_price       AS dc_current_price,
               dl.end_time            AS dc_end_time,
               dl.is_no_reserve       AS dc_no_reserve,
               dl.reserve_met         AS dc_reserve_met,
               dl.detail_url          AS dc_detail_url,
               dl.status              AS dc_status,
               dl.closed_at           AS dc_closed_at
        FROM bids b
        LEFT JOIN contacts c ON b.contact_id = c.id
        LEFT JOIN dealers d ON b.partner_dealer_id = d.id
        LEFT JOIN dealerclub_lots dl ON dl.bid_id = b.id
        {where}
        ORDER BY b.created_at DESC LIMIT 200
    """
    cur.execute(q.format(where=where), params)
    bids = list(cur.fetchall())

    # Compute opportunity for each DealerClub-sourced bid so the template
    # can render the colored badge without per-row math.
    for bid in bids:
        if not isinstance(bid, dict):
            continue
        if not bid.get('dc_current_price'):
            continue
        ai = bid.get('ai_price')
        if ai is None:
            bid['dc_opp_tier'] = 'gray'
            bid['dc_opp_pct'] = None
            continue
        all_in = float(bid['dc_current_price']) + DEALERCLUB_BUY_FEE_FLAT \
                 + DEALERCLUB_TRANSPORT_EST
        try:
            ai_f = float(ai)
            pct = (ai_f - all_in) / ai_f * 100 if ai_f else None
        except (TypeError, ValueError):
            pct = None
        bid['dc_opp_pct'] = round(pct, 1) if pct is not None else None
        bid['dc_opp_dollars'] = round(float(ai) - all_in) if pct is not None else None
        if pct is None:
            bid['dc_opp_tier'] = 'gray'
        elif pct >= 15:
            bid['dc_opp_tier'] = 'green'
        elif pct >= 5:
            bid['dc_opp_tier'] = 'yellow'
        else:
            bid['dc_opp_tier'] = 'red'

    cur.execute("SELECT bid_id, COUNT(*) as cnt FROM bid_photos GROUP BY bid_id")
    photo_counts = {r['bid_id']: int(r['cnt']) for r in cur.fetchall()}

    # First photo per bid (prefer local copy over Twilio URL) — for the
    # listing thumbnail. Lets ops eyeball what was uploaded without clicking
    # into each bid. ORDER BY id ASC = chronological first attachment.
    cur.execute("""
        SELECT DISTINCT ON (bid_id) bid_id, COALESCE(local_path, url) AS src
        FROM bid_photos ORDER BY bid_id, id
    """)
    first_photos = {r['bid_id']: r['src'] for r in cur.fetchall()}

    # Badge "vA" turns green when the scan pipeline is complete. iPacket runs
    # last (after vAuto + AccuTrade), and the worker always writes a row —
    # success OR not_available=true — so its presence is the cleanest "done".
    # Badge turns green when ALL 3 lookups have rows (iPacket row present even when not_available=true).
    cur.execute("""
        SELECT v.bid_id FROM vauto_lookups v
        JOIN accutrade_lookups a ON a.bid_id = v.bid_id
    """)
    vauto_done = {r['bid_id'] for r in cur.fetchall()}

    # Live worker activity — one row per (bid, worker, job_type) currently
    # in flight. Caps at 60s look-back so a stuck-in-progress row stops
    # showing as "active" instead of looking eternal. Multiple jobs per bid
    # collapse to a single most-recent row in the template.
    cur.execute("""
        SELECT DISTINCT ON (bid_id) bid_id, worker_id, job_type, status, claimed_at, completed_at
          FROM worker_jobs
         WHERE bid_id IS NOT NULL
         ORDER BY bid_id, claimed_at DESC
    """)
    active_workers = {}
    for r in cur.fetchall():
        active_workers.setdefault(r['bid_id'], []).append({
            'worker_id': r['worker_id'],
            'job_type': r['job_type'],
            'status': r.get('status', ''),
            'completed': r.get('completed_at') is not None,
        })

    db.close()
    # Sourcing-bot active requests for the sticky top banner. Stays in
    # priority order (matched first → wishlist last). Pre-computes last
    # user/bot messages so the template stays simple.
    # 2026-05-14: uses its own connection — the main `db` was closed above,
    # so the old `cur.execute` was failing silently against a closed conn,
    # leaving the banner empty while ew_alert_unseen_count (computed by a
    # separate context processor) still incremented. That mismatch caused
    # the Buyer Inbox nav tab to pulse but have no anchor to scroll to.
    sourcing_active = []
    sourcing_unseen_count = 0
    _src_db = None
    try:
        _src_db = get_db()
        _src_cur = _src_db.cursor()
        _src_cur.execute('''
            SELECT id, phone, status, year_min, year_max, make, model, trim,
                   ext_color, miles_max, customer_name, conversation,
                   last_msg_at, last_inbound_at, created_at, seen_at
              FROM sourcing_requests
             WHERE status <> 'archived'
             ORDER BY
               CASE status
                 WHEN 'matched'    THEN 1
                 WHEN 'presented'  THEN 2
                 WHEN 'searching'  THEN 3
                 WHEN 'gathering'  THEN 4
                 WHEN 'wishlist'   THEN 5
               END,
               last_msg_at DESC
             LIMIT 50
        ''')
        rows = _src_cur.fetchall()
        for r in rows:
            r = dict(r)
            conv = r.get('conversation') or []
            last_user = next((t for t in reversed(conv) if t.get('role') == 'user'), None)
            last_bot  = next((t for t in reversed(conv) if t.get('role') == 'bot'),  None)
            r['last_user_text'] = (last_user or {}).get('text', '')
            r['last_bot_text']  = (last_bot  or {}).get('text', '')
            # Unseen = staff hasn't opened the thread, OR new activity
            # has happened since the last time staff opened it. Drives the
            # pulsing yellow Sourcing Alerts banner.
            seen = r.get('seen_at')
            last = r.get('last_msg_at')
            r['unseen'] = bool(seen is None or (last and last > seen))
            if r['unseen']:
                sourcing_unseen_count += 1
            sourcing_active.append(r)
    except Exception as _se:
        print(f'[dashboard] sourcing query error: {_se}', flush=True)
        sourcing_active = []
        sourcing_unseen_count = 0
    finally:
        if _src_db is not None:
            try: _src_db.close()
            except Exception: pass

    # 2026-05-11: partner offer counts per bid for the yellow-star indicator.
    # Fresh connection — the main `db` is already closed by the time we get
    # here (same pattern as bid_detail partner_offers fix).
    partner_offer_counts = {}
    try:
        _poc_db = get_db()
        _poc_cur = _poc_db.cursor()
        _poc_cur.execute("""
            SELECT bid_id, COUNT(*) AS n,
                   COUNT(*) FILTER (WHERE ew_seen_at IS NULL) AS unseen
              FROM bid_partner_offers
             GROUP BY bid_id
        """)
        for r in _poc_cur.fetchall():
            partner_offer_counts[r['bid_id']] = {
                'n': int(r['n']),
                'unseen': int(r['unseen']),
            }
        _poc_db.close()
    except Exception as _poc_err:
        print(f'[index] partner_offer_counts err: {_poc_err}', flush=True)

    return render_template('index.html', bids=bids, stats=stats,
                           status_filter=status_filter, rep_filter=rep_filter,
                           reps=reps, photo_counts=photo_counts,
                           first_photos=first_photos,
                           vauto_done=vauto_done,
                           active_workers=active_workers,
                           sourcing_active=sourcing_active,
                           sourcing_unseen_count=sourcing_unseen_count,
                           partner_offer_counts=partner_offer_counts,
                           time_ago=time_ago,
                           network_claims=network_claims,
                           network_claims_by_bid=network_claims_by_bid)


@app.route('/bid/<int:bid_id>')
def bid_detail(bid_id):
    db = get_db()
    cur = db.cursor()

    cur.execute("""
        SELECT b.*, c.name as contact_name, c.company as contact_company,
               d.name as partner_dealer_name,
               dl.external_id           AS dc_external_id,
               dl.current_price         AS dc_current_price,
               dl.high_bid              AS dc_high_bid,
               dl.bid_count             AS dc_bid_count,
               dl.end_time              AS dc_end_time,
               dl.is_no_reserve         AS dc_no_reserve,
               dl.reserve_met           AS dc_reserve_met,
               dl.reserve_progress_color AS dc_reserve_color,
               dl.status                AS dc_status,
               dl.detail_url            AS dc_detail_url,
               dl.estimated_buy_fee     AS dc_buy_fee,
               dl.estimated_transport   AS dc_transport,
               dl.transport_mileage     AS dc_transport_mileage,
               dl.transport_eta_min     AS dc_transport_eta_min,
               dl.transport_eta_max     AS dc_transport_eta_max,
               dl.transport_enclosed    AS dc_transport_enclosed,
               dl.closed_at             AS dc_closed_at
        FROM bids b
        LEFT JOIN contacts c ON b.contact_id = c.id
        LEFT JOIN dealers d ON b.partner_dealer_id = d.id
        LEFT JOIN dealerclub_lots dl ON dl.bid_id = b.id
        WHERE b.id = %s
    """, (bid_id,))
    bid = cur.fetchone()
    if not bid:
        db.close()
        return 'Not found', 404
    # Compute live opportunity for DealerClub bids so the template can
    # render the prominent green/yellow/red card.
    if bid.get('dc_current_price'):
        bid = dict(bid)
        ai = bid.get('ai_price')
        bf = bid.get('dc_buy_fee') if bid.get('dc_buy_fee') is not None \
             else DEALERCLUB_BUY_FEE_FLAT
        tr = bid.get('dc_transport') if bid.get('dc_transport') is not None \
             else DEALERCLUB_TRANSPORT_EST
        if ai is not None:
            try:
                ai_f = float(ai)
                all_in = float(bid['dc_current_price']) + bf + tr
                pct = (ai_f - all_in) / ai_f * 100 if ai_f else None
                bid['dc_opp_pct'] = round(pct, 1) if pct is not None else None
                bid['dc_opp_gap'] = round(ai_f - all_in) if pct is not None else None
                bid['dc_all_in']  = round(all_in)
                bid['dc_buy_fee_used'] = bf
                bid['dc_transport_used'] = tr
                if pct is None:
                    bid['dc_opp_tier'] = 'gray'
                elif pct >= 15:
                    bid['dc_opp_tier'] = 'green'
                elif pct >= 5:
                    bid['dc_opp_tier'] = 'yellow'
                else:
                    bid['dc_opp_tier'] = 'red'
            except (TypeError, ValueError):
                bid['dc_opp_tier'] = 'gray'
        else:
            bid['dc_opp_tier'] = 'gray'
        # Ensure every dc_* key the template reads exists, even on the
        # try/except + else paths above. Avoids /bid/<id> 500s when
        # ai_price is NULL or pct math bails (DealerClub bids pre-AI).
        for _k in ('dc_opp_pct', 'dc_opp_gap', 'dc_all_in',
                   'dc_buy_fee_used', 'dc_transport_used'):
            bid.setdefault(_k, None)

    cur.execute("SELECT * FROM bid_photos WHERE bid_id = %s ORDER BY id", (bid_id,))
    photos = cur.fetchall()

    cur.execute("SELECT * FROM bid_messages WHERE bid_id = %s ORDER BY created_at", (bid_id,))
    messages = cur.fetchall()

    cur.execute("""
        SELECT bp.bid_id, bp.dealer_id, bp.claimed_at, bp.score, bp.claim_from_phone,
               d.name AS dealer_name, d.salesperson AS dealer_salesperson,
               b.network_ask::int AS ask
          FROM bid_pushes bp
          JOIN dealers d ON d.id = bp.dealer_id
          JOIN bids b    ON b.id = bp.bid_id
         WHERE bp.bid_id = %s AND bp.claimed_at IS NOT NULL
           AND bp.claim_late IS NOT TRUE AND bp.sold_confirmed_at IS NULL
         LIMIT 1
    """, (bid_id,))
    _bnc = cur.fetchone()
    bid_network_claim = dict(_bnc) if _bnc else None

    cur.execute("SELECT * FROM valuations WHERE bid_id = %s ORDER BY fetched_at DESC", (bid_id,))
    valuations = cur.fetchall()

    if bid['status'] == 'new':
        cur.execute("UPDATE bids SET status='reviewing', has_unread=FALSE, updated_at=NOW() WHERE id=%s", (bid_id,))
        db.commit()
    elif bid.get('has_unread'):
        cur.execute("UPDATE bids SET has_unread=FALSE WHERE id=%s", (bid_id,))
        db.commit()

    # Auto-decode VIN if we have one but no make yet
    if bid['vin'] and not bid['make']:
        decoded = decode_vin(bid['vin'])
        if decoded:
            # decode_vin returns NHTSA fields beyond our schema (plant_city,
            # body_class, etc.) — filter to columns that actually exist on bids.
            global _BIDS_COLUMNS_CACHE
            try:
                _BIDS_COLUMNS_CACHE
            except NameError:
                _BIDS_COLUMNS_CACHE = None
            if _BIDS_COLUMNS_CACHE is None:
                cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='bids'")
                _BIDS_COLUMNS_CACHE = {r['column_name'] for r in cur.fetchall()}
            decoded = {k: v for k, v in decoded.items() if k in _BIDS_COLUMNS_CACHE}
            if decoded:
                fields = ', '.join(f'{k}=%s' for k in decoded)
                cur.execute(f"UPDATE bids SET {fields} WHERE id=%s", list(decoded.values()) + [bid_id])
                db.commit()
                bid = dict(bid)
                bid.update(decoded)

    # vAuto lookup data — explicit columns, drops heavy JSONB blobs
    # (rbook_competitive_set + manheim_transactions) which are only used
    # for market_intel and are now read from market_intel_cached.
    cur.execute("""
        SELECT id, bid_id, vin, rbook, mmr, kbb, kbb_com, jd_power, black_book,
               title_status, price_rank, adj_pct_market,
               carfax_screenshot, autocheck_screenshot, carfax_share_url,
               looked_up_at, appraisal_url,
               rbook_completed_at, manheim_completed_at,
               enrichment_state, market_intel_cached,
               api_carfax, api_price_guides, api_refreshed_at
        FROM vauto_lookups WHERE bid_id = %s
    """, (bid_id,))
    vauto_data = cur.fetchone()

    # AccuTrade lookup data
    accutrade_data = None
    try:
        cur.execute("SELECT * FROM accutrade_lookups WHERE bid_id = %s", (bid_id,))
        accutrade_data = cur.fetchone()
    except Exception:
        pass

    # iPacket sticker data — same-VIN fallback for blank/failed captures
    # (mirrors /m/<token> mini-page so desktop bid card matches the SMS link).
    ipacket_data = _ipacket_with_vin_fallback(cur, bid_id, bid.get('vin'))

    # Tesla auto-decode (if VIN is Tesla)
    tesla_data = None
    TESLA_WMIS = ('5YJ', '7SA', '7G2', 'SFZ', 'XP7', 'LRW')
    if bid['vin'] and len(bid['vin']) >= 3 and bid['vin'][:3].upper() in TESLA_WMIS:
        try:
            tesla_data = decode_tesla_vin(bid['vin'])
            # Check cache for Fleet API options
            cur.execute("SELECT options_json, source FROM tesla_vin_cache WHERE vin=%s", (bid['vin'],))
            cached = cur.fetchone()
            if cached and cached.get('options_json'):
                tesla_data['fleet_specs'] = cached['options_json']
            elif not cached:
                # Queue for Fleet API lookup
                cur.execute("INSERT INTO tesla_vin_cache (vin) VALUES (%s) ON CONFLICT DO NOTHING", (bid['vin'],))
                db.commit()
        except Exception:
            pass

    # ── Latest hybrid-assessment log row (bucket + baseline + adjustment) ───
    ai_log = None
    try:
        # Explicit columns — drop the two heavy JSONB blobs (market_intel ~50KB,
        # raw_response variable). market_intel is substituted from
        # vauto_data.market_intel_cached below (same data, faster path).
        cur.execute("""
            SELECT id, bid_id, config_version, bucket, bucket_display,
                   baseline_price, breakdown, llm_adjustment_pct,
                   llm_reasoning, confidence_low, confidence_high,
                   final_price, created_at,
                   dealer_intel, buyer_intel, flags_v2
            FROM ai_assessment_log
            WHERE bid_id = %s
            ORDER BY created_at DESC
            LIMIT 1
        """, (bid_id,))
        ai_log = cur.fetchone()
        # ai_log_uses_cached: substitute market_intel from vauto cache so
        # the template still has it without paying the 50KB TOAST tax.
        if ai_log and vauto_data and vauto_data.get('market_intel_cached'):
            ai_log = dict(ai_log)
            ai_log['market_intel'] = vauto_data['market_intel_cached']
        if ai_log:
            ai_log = dict(ai_log)
            # breakdown is JSONB — may come back as dict/list already or as str
            b = ai_log.get('breakdown')
            if isinstance(b, str):
                try:
                    ai_log['breakdown'] = json.loads(b)
                except Exception:
                    ai_log['breakdown'] = []
            di = ai_log.get('dealer_intel')
            if isinstance(di, str):
                try:
                    ai_log['dealer_intel'] = json.loads(di)
                except Exception:
                    ai_log['dealer_intel'] = None
            bi = ai_log.get('buyer_intel')
            if isinstance(bi, str):
                try:
                    ai_log['buyer_intel'] = json.loads(bi)
                except Exception:
                    ai_log['buyer_intel'] = None
            mi = ai_log.get('market_intel')
            if isinstance(mi, str):
                try:
                    ai_log['market_intel'] = json.loads(mi)
                except Exception:
                    ai_log['market_intel'] = None
    except Exception as _aelog_err:
        print(f'ai_assessment_log read error: {_aelog_err}', flush=True)

    # ── MSRP enrichment for dealer_intel + buyer_intel VINs ───────────────
    # Mirrors the rBook closest_3 pattern: collect VINs from cached
    # dealer/buyer matches, enqueue them in comp_msrps (VM 121 scrapes
    # iPacket), then attach any cached msrp_lookup back to each row so the
    # template renders "MSRP $X". Idempotent — repeat views don't re-queue
    # already-cached VINs.
    try:
        _msrp_vins = []
        if ai_log and isinstance(ai_log.get('dealer_intel'), dict):
            for r in (ai_log['dealer_intel'].get('active') or []):
                if r.get('vin'):
                    _msrp_vins.append(r['vin'])
            for r in (ai_log['dealer_intel'].get('recent_sales') or []):
                if r.get('vin'):
                    _msrp_vins.append(r['vin'])
        if ai_log and isinstance(ai_log.get('buyer_intel'), dict):
            for r in (ai_log['buyer_intel'].get('deals') or []):
                # LSL field is vin_no
                vn = r.get('vin_no') or r.get('vin')
                if vn:
                    _msrp_vins.append(vn)
        _msrp_vins = list({v.upper() for v in _msrp_vins
                           if isinstance(v, str) and len(v) == 17})
        if _msrp_vins:
            _enqueue_msrp_vins(bid_id, _msrp_vins)
            try: _start_comp_msrp_processor()
            except Exception: pass
            _msrp_cache = _load_comp_msrps(_msrp_vins)
            if ai_log and isinstance(ai_log.get('dealer_intel'), dict):
                _attach_msrp_to_rows(ai_log['dealer_intel'].get('active') or [],
                                     'vin', _msrp_cache)
                _attach_msrp_to_rows(ai_log['dealer_intel'].get('recent_sales') or [],
                                     'vin', _msrp_cache)
            if ai_log and isinstance(ai_log.get('buyer_intel'), dict):
                _attach_msrp_to_rows(ai_log['buyer_intel'].get('deals') or [],
                                     'vin_no', _msrp_cache)
    except Exception as _msrp_err:
        print(f'[bid view] dealer/buyer MSRP enrich err: {_msrp_err}', flush=True)

    # Partner-dealer info — show channel-selector UI on Send Bid for any bid
    # tied to a partner dealer. Three resolution paths, in priority order:
    #   1. partner_dealer_id  — explicit (set by Dealer DB Search push)
    #   2. partner_request_id — bid came in via the partner portal
    #   3. VIN match against a partner dealer's dealer_inventory — e.g. a
    #      field rep submits a bid on a car that happens to be in a partner
    #      store, so the bid has neither partner_* column set, but we still
    #      want the dealer to be notifiable.
    partner_info = None
    pd_id = bid.get('partner_dealer_id')
    if not pd_id and bid.get('partner_request_id'):
        cur.execute("""SELECT pu.dealer_id FROM partner_bid_requests pbr
                       JOIN partner_users pu ON pu.id = pbr.partner_user_id
                       WHERE pbr.id = %s""", (bid['partner_request_id'],))
        r = cur.fetchone()
        if r:
            pd_id = r['dealer_id']
    if not pd_id and bid.get('vin'):
        # VIN-in-inventory match. Restrict to dealers that have at least one
        # partner_user (i.e. an actual partner relationship, not a random
        # scraped store). If multiple partners stock the same VIN, pick the
        # most recently-scanned one.
        cur.execute("""SELECT di.dealer_id
                         FROM dealer_inventory di
                         JOIN dealers d ON d.id = di.dealer_id
                        WHERE di.vin = %s
                          AND EXISTS (SELECT 1 FROM partner_users pu
                                       WHERE pu.dealer_id = d.id)
                        ORDER BY di.last_seen_at DESC NULLS LAST,
                                 di.id DESC
                        LIMIT 1""", (bid['vin'],))
        r = cur.fetchone()
        if r:
            pd_id = r['dealer_id']
    if pd_id:
        # Partner dealers are consented via the signed partner agreement,
        # not the SMS double-opt-in handshake — so sms_opt_in alone gates
        # eligibility (no sms_verified_at requirement).
        cur.execute("""SELECT d.id AS dealer_id, d.name AS dealer_name,
                              MAX(CASE WHEN pu.sms_opt_in
                                       THEN 1 ELSE 0 END) AS sms_ok,
                              MAX(CASE WHEN pu.email_bid_alerts THEN 1 ELSE 0 END) AS email_ok,
                              STRING_AGG(DISTINCT pu.phone, ', ') FILTER (WHERE pu.sms_opt_in) AS sms_phones,
                              STRING_AGG(DISTINCT pu.email, ', ') FILTER (WHERE pu.email_bid_alerts) AS emails
                       FROM dealers d
                       LEFT JOIN partner_users pu ON pu.dealer_id = d.id
                       WHERE d.id = %s
                       GROUP BY d.id, d.name""", (pd_id,))
        partner_info = cur.fetchone()

    db.close()

    # Surface a photo-extracted VIN candidate that failed validation so the
    # banner can show "No VIN — best guess: XXX" with a one-click apply.
    # Only meaningful when the bid still has no real VIN.
    vin_candidate = None
    if not bid.get('vin') and bid.get('notes'):
        m = re.search(r'Photo VIN candidate \(verify\):\s*([A-Z0-9]+)', bid['notes'])
        if m:
            vin_candidate = m.group(1)

    # Read cached market_intel from vauto_lookups (populated when rbook
    # completes via vauto_enrichment.kick_direct_enrichment, or lazily
    # on first view of older bids).
    market_intel = None
    try:
        from market_intel import compute_market_intel as _mi
        def _maybe_parse(x):
            if isinstance(x, str):
                try:
                    import json as _j
                    return _j.loads(x)
                except Exception:
                    return None
            return x

        # market_intel_cached_complete check — direct API enrichment only
        # caches rbook (manheim arrives later via VM 120). If manheim has
        # since completed, the cache is stale; force recompute.
        _cached_mi = vauto_data.get('market_intel_cached') if vauto_data else None
        if _cached_mi:
            _parsed_cache = _maybe_parse(_cached_mi) if isinstance(_cached_mi, str) else _cached_mi
            _cache_has_manheim = bool((_parsed_cache or {}).get('manheim'))
            _mh_done = vauto_data and vauto_data.get('manheim_completed_at')
            if _cache_has_manheim or not _mh_done:
                market_intel = _parsed_cache
        if (market_intel is None) and vauto_data:
            # Lazy fill: compute live and persist for next render.
            # One-shot extra fetch of the heavy JSONB columns.
            _db2 = get_db()
            _cur2 = _db2.cursor()
            _cur2.execute("SELECT rbook_competitive_set, manheim_transactions "
                          "FROM vauto_lookups WHERE bid_id=%s", (bid_id,))
            _extra = _cur2.fetchone()
            if _extra:
                _manheim = _maybe_parse(_extra.get('manheim_transactions'))
                _rbook   = _maybe_parse(_extra.get('rbook_competitive_set'))
                market_intel = _mi(
                    {'year': bid.get('year'), 'make': bid.get('make'),
                     'model': bid.get('model'), 'mileage': bid.get('mileage'),
                     'vin': bid.get('vin')},
                    _manheim, _rbook, None,
                )
                if market_intel:
                    import json as _j
                    _cur2.execute("UPDATE vauto_lookups SET market_intel_cached=%s::jsonb "
                                  "WHERE bid_id=%s",
                                  (_j.dumps(market_intel), bid_id))
                    _db2.commit()
            _db2.close()
        # Phase 2: enqueue MSRP lookups for the top-3 closest comp VINs and
        # merge any cached MSRPs into the closest_3 rows so both the UI and
        # (downstream) the AI prompt see them. Idempotent — repeat visits
        # don't re-queue done VINs.
        if market_intel:
            _enqueue_comp_msrps_for_bid(bid_id, market_intel)
            # Make sure the background iPacket-MSRP processor is running.
            # Daemon thread, idempotent — only spawns once per worker.
            try: _start_comp_msrp_processor()
            except Exception: pass
            _closest = (market_intel.get('rbook') or {}).get('closest_3') or []
            _vins = [c.get('vin') for c in _closest if c.get('vin')]
            _msrps = _load_comp_msrps(_vins)
            for c in _closest:
                v = (c.get('vin') or '').upper()
                if v in _msrps:
                    c['msrp_lookup'] = {
                        'msrp':       _msrps[v].get('msrp'),
                        'base_price': _msrps[v].get('base_price'),
                        'status':     _msrps[v].get('status'),
                    }
    except Exception as _mi_err:
        print(f'[bid view] market_intel compute err: {_mi_err}', flush=True)
        market_intel = None

    # ── ML model second opinion (per-make XGBoost) ────────────────────
    # Built from existing market_intel + bid attrs. Models are pre-warmed
    # at gunicorn startup (see ml_predict.preload_all() called from wsgi.py),
    # so each bid card render hits warm cache (~5-30ms typical).
    ml_prediction = None
    try:
        from ml_predict import predict_for_bid
        _mi = market_intel or {}
        _manheim = _mi.get('manheim') or {}
        _rbook = _mi.get('rbook') or {}
        # 2026-05-08: include mmr_median fallback (same fix as in
        # _run_assessment). vauto.mmr is NULL for many exotics; manheim
        # transactions often have real hammer prices whose median is the
        # truest wholesale signal.
        # 2026-05-08: AccuTrade trade_in fallback. For bids where vAuto's MMR
        # feed is empty AND Manheim has zero transactions (Bentley Bentayga,
        # rare exotics), AccuTrade's trade_in value is wholesale-shaped and
        # serves as the last wholesale signal before giving up on ML.
        _est_wholesale = (_manheim.get('adjusted_mmr')
                          or _manheim.get('base_mmr')
                          or _manheim.get('mmr_median')
                          or (accutrade_data or {}).get('trade_in')
                          or (vauto_data or {}).get('mmr'))
        _market_asking = (_rbook.get('avg_price')
                          or _rbook.get('median')
                          or (vauto_data or {}).get('rbook'))
        _ipkt_msrp = (ipacket_data or {}).get('total_msrp')
        if _est_wholesale and bid.get('make'):
            ml_prediction = predict_for_bid({
                'make_name':          bid.get('make') or '',
                'model_name':         bid.get('model'),
                'year':               bid.get('year'),
                'odometer':           bid.get('mileage'),
                'est_wholesale_price': _est_wholesale,
                'market_asking_price': _market_asking,
                'original_msrp':      _ipkt_msrp,
                'base_appraised_value': _market_asking,
                'sale_type':          'Wholesale',
                'vehicle_sale_type':  'Used',
            })
    except Exception as _ml_err:
        print(f'[bid_detail] ml_predict err: {_ml_err}', flush=True)

    # 2026-05-11: offers from subscribed partner dealers on this bid.
    # Uses a fresh connection — the main `db` is already closed at this
    # point in the request, so cursor reuse throws "connection already closed".
    partner_offers = []
    try:
        _po_db = get_db()
        _po_cur = _po_db.cursor()
        _po_cur.execute("""
            SELECT o.id, o.offer_amount, o.message, o.submitted_at,
                   o.ew_seen_at, o.ew_action,
                   d.name AS dealer_name,
                   pu.full_name AS user_name, pu.email AS user_email
              FROM bid_partner_offers o
              JOIN dealers d ON o.dealer_id = d.id
         LEFT JOIN partner_users pu ON o.partner_user_id = pu.id
             WHERE o.bid_id = %s
             ORDER BY o.submitted_at DESC
        """, (bid_id,))
        partner_offers = _po_cur.fetchall()
        _po_db.close()
    except Exception as _po_err:
        print(f'[bid_detail] partner_offers err: {_po_err}', flush=True)

    return render_template('bid.html', bid=bid, photos=photos,
                           messages=messages, valuations=valuations,
                           vauto_data=vauto_data,
                           accutrade_data=accutrade_data,
                           ipacket_data=ipacket_data,
                           tesla_data=tesla_data,
                           ai_assessment=bid.get('ai_assessment'),
                           ai_log=ai_log,
                           partner_info=partner_info,
                           vin_candidate=vin_candidate,
                           market_intel=market_intel,
                           ml_prediction=ml_prediction,
                           partner_offers=partner_offers,
                           bid_network_claim=bid_network_claim, time_ago=time_ago)


# ── SMS intake observability helpers ─────────────────────────────────────────
# Every inbound Twilio webhook gets an sms_intake_log row written at the top
# of the handler with outcome='pending'. As the request walks the partner-
# reply / share-reply / stitch / new-bid paths, we update the row with the
# final outcome + a human-readable reason + the resulting bid_id (if any).
# Lets ops answer "why didn't my text trigger a bid?" without log diving.

def _log_sms_intake(cur, from_phone, body, num_media, media_urls, raw_form):
    cur.execute("""
        INSERT INTO sms_intake_log (from_phone, body, num_media, media_urls,
                                    outcome, raw_form)
        VALUES (%s, %s, %s, %s::jsonb, 'pending', %s::jsonb)
        RETURNING id
    """, (from_phone, body, num_media,
          json.dumps(media_urls or []),
          json.dumps(dict(raw_form or {}))))
    return cur.fetchone()['id']


def _finalize_sms_intake(cur, log_id, outcome, bid_id=None, reason=None,
                         parsed_vin=None, parsed_miles=None):
    if not log_id:
        return
    try:
        cur.execute("""
            UPDATE sms_intake_log
               SET outcome=%s, bid_id=%s, reason=%s,
                   parsed_vin=%s, parsed_miles=%s
             WHERE id=%s
        """, (outcome, bid_id, reason, parsed_vin, parsed_miles, log_id))
    except Exception as _e:
        # Never let intake-log bookkeeping break the actual webhook response.
        print(f'[sms-intake] finalize error log_id={log_id}: {_e}', flush=True)


def _ingest_sms_photo_sync(cur, bid_id, media_url, media_type):
    """Synchronous INSERT + download + save. Same contract as the original
    _ingest_sms_photo. Used by Carfax/AutoCheck handlers that genuinely need
    the bytes inside the request lifecycle. Inbound /webhook/twilio uses the
    background-ingest variant below to stay within Twilio's 15s budget."""
    if not media_url:
        return None
    cur.execute("""INSERT INTO bid_photos (bid_id, url, is_sms_intake)
                   VALUES (%s, %s, TRUE) RETURNING id""", (bid_id, media_url))
    photo_id = cur.fetchone()['id']
    if 'image' not in (media_type or ''):
        return (photo_id, None, None)
    try:
        _resp = requests.get(media_url, auth=(TWILIO_SID, TWILIO_TOKEN), timeout=15)
        if _resp.status_code != 200:
            return (photo_id, None, None)
        mime = (_resp.headers.get('Content-Type') or media_type or 'image/jpeg').split(';')[0]
        local = _save_sms_media_local(bid_id, photo_id, _resp.content, mime)
        if local:
            cur.execute("UPDATE bid_photos SET local_path=%s WHERE id=%s",
                        (local, photo_id))
        return (photo_id, _resp.content, mime)
    except Exception as _e:
        print(f'[sms-photo] download error bid={bid_id} url={media_url[:60]}: {_e}', flush=True)
        return (photo_id, None, None)


def _bg_download_sms_photo(photo_id, bid_id, media_url, media_type, from_phone=None):
    """Background thread target: download MMS bytes after the webhook has
    already responded to Twilio. Opens its own DB connection because the
    request-bound one is closed by the time this runs.

    v4 SMS auto-OCR: every inbound photo is OCR'd for VIN + miles.
    - Test user (from_phone == EW_TEST_USER_PHONE): v4 first, Gemini fallback.
    - Everyone else: extract_vin_from_file() / extract_mileage_from_file()
      (Google Vision -> Gemini Flash -> Gemini Pro).
    Results land on bid_photos.vin_extracted, bids.vin (if NULL), bids.mileage (if NULL).
    """
    try:
        _resp = requests.get(media_url, auth=(TWILIO_SID, TWILIO_TOKEN), timeout=30)
        if _resp.status_code != 200:
            print(f'[sms-photo-bg] HTTP {_resp.status_code} bid={bid_id} photo={photo_id}', flush=True)
            return
        mime = (_resp.headers.get('Content-Type') or media_type or 'image/jpeg').split(';')[0]
        img_bytes = _resp.content
        local = _save_sms_media_local(bid_id, photo_id, img_bytes, mime)

        # OCR pass — runs for EVERY inbound MMS photo.
        vin = None
        miles = None

        # Test user → try v4 first (direct call, no request context needed)
        test_phone = os.environ.get('EW_TEST_USER_PHONE', '').strip()
        is_test = bool(test_phone and from_phone and from_phone.strip() == test_phone)
        if is_test:
            try:
                from ew_v4_router import v4_extract
                v4_vin = v4_extract(img_bytes, task='vin')
                if v4_vin and VIN_RE.match(v4_vin):
                    vin = v4_vin
                    print(f'[v4-sms] VIN={vin} bid={bid_id} photo={photo_id}', flush=True)
                v4_m = v4_extract(img_bytes, task='odometer')
                if v4_m and v4_m.isdigit() and 100 <= int(v4_m) <= 999999:
                    miles = int(v4_m)
                    print(f'[v4-sms] miles={miles} bid={bid_id} photo={photo_id}', flush=True)
            except Exception as _v4e:
                print(f'[v4-sms] err bid={bid_id} photo={photo_id}: {_v4e}', flush=True)

        # Gemini fallback for VIN
        if vin is None:
            try:
                cand = extract_vin_from_file(img_bytes, mime)
                if cand and VIN_RE.match(cand):
                    vin = cand
                    print(f'[sms-ocr] VIN via Gemini bid={bid_id}: {vin}', flush=True)
            except Exception as _e:
                print(f'[sms-ocr] vin err bid={bid_id} photo={photo_id}: {_e}', flush=True)

        # Gemini fallback for miles
        if miles is None:
            try:
                cand = extract_mileage_from_file(img_bytes, mime)
                if cand and str(cand).isdigit() and 100 <= int(cand) <= 999999:
                    miles = int(cand)
                    print(f'[sms-ocr] miles via Gemini bid={bid_id}: {miles}', flush=True)
            except Exception as _e:
                print(f'[sms-ocr] miles err bid={bid_id} photo={photo_id}: {_e}', flush=True)

        # Write everything in one transaction
        with get_db() as conn:
            with conn.cursor() as bg_cur:
                if local:
                    bg_cur.execute("UPDATE bid_photos SET local_path=%s WHERE id=%s",
                                   (local, photo_id))
                if vin:
                    bg_cur.execute("UPDATE bid_photos SET vin_extracted=%s WHERE id=%s",
                                   (vin, photo_id))
                    # VIN_VOTE_2026_05_16: majority vote across all OCR'd
                    # photos in this bid, instead of "first arrival wins".
                    # All bid_photos.vin_extracted values were already gated
                    # on vin_check_digit_valid() upstream in
                    # extract_vin_from_file(), so any non-null row is a
                    # check-digit-valid candidate. Tiebreaker: earliest
                    # arrival. Only sets bids.vin when still NULL/empty,
                    # preserving operator dashboard edits.
                    bg_cur.execute("""
                        SELECT vin_extracted AS v, COUNT(*) AS n
                          FROM bid_photos
                         WHERE bid_id=%s
                           AND vin_extracted IS NOT NULL
                           AND LENGTH(vin_extracted) = 17
                         GROUP BY vin_extracted
                         ORDER BY n DESC, MIN(created_at) ASC
                         LIMIT 1
                    """, (bid_id,))
                    _vrow = bg_cur.fetchone()
                    _winner = (_vrow.get('v') if _vrow else vin) or vin
                    bg_cur.execute("""UPDATE bids SET vin=%s, updated_at=NOW()
                                      WHERE id=%s AND (vin IS NULL OR vin='')""",
                                   (_winner, bid_id))
                    # CLEAR_VERIFY_STATE_BASED_2026_05_16: gate on current
                    # bid state (does it have a vin now? is the flag open
                    # with a vin-related reason?) rather than on whether
                    # this UPDATE moved anything. The voter is no-op when
                    # vin was already set by an earlier photo's OCR, but
                    # we still want to clear the flag in that case.
                    bg_cur.execute("""
                        SELECT vin,
                               needs_verification_at,
                               needs_verification_cleared_at,
                               needs_verification_reason
                          FROM bids WHERE id = %s
                    """, (bid_id,))
                    _vrow = bg_cur.fetchone()
                    _vreason = (_vrow.get('needs_verification_reason') or '').lower() if _vrow else ''
                    _vin_related = any(k in _vreason for k in (
                        'missing_vin', 'vin_invalid', 'invalid_vin',
                        'vin_not_found'))
                    if (_vrow and _vrow.get('vin')
                            and _vrow.get('needs_verification_at')
                            and not _vrow.get('needs_verification_cleared_at')
                            and _vin_related):
                            bg_cur.execute("""
                                UPDATE bids
                                   SET needs_verification_cleared_at = NOW(),
                                       needs_verification_cleared_by = 'auto:photo_ocr_vin'
                                 WHERE id = %s
                            """, (bid_id,))
                            bg_cur.execute(
                                "DELETE FROM ipacket_lookups WHERE bid_id=%s "
                                "AND (looked_up_at IS NULL OR looked_up_at < NOW() - INTERVAL '5 minutes' OR not_available=true)",
                                (bid_id,))
                            bg_cur.execute("DELETE FROM accutrade_lookups WHERE bid_id=%s", (bid_id,))
                            bg_cur.execute("DELETE FROM vauto_lookups WHERE bid_id=%s", (bid_id,))
                            bg_cur.execute(
                                "UPDATE bids SET vauto_claimed_by=NULL, "
                                "vauto_claimed_at=NULL, ai_assessed_at=NULL, "
                                "ai_price=NULL, ai_assessment=NULL, "
                                "miles_audit_at=NULL WHERE id=%s",
                                (bid_id,))
                            print(f'[photo-ocr-vin] bid={bid_id} vin={_winner} '
                                  f'cleared vin verification flag + force-reprocess fired',
                                  flush=True)
                if miles:
                    # PHOTO_OCR_MAX_WINS_2026_05_15: take MAX across all
                    # photo-OCR mileages for this bid. Odometers monotonically
                    # increase, so the largest plausible OCR value across
                    # multiple photos is most likely the real odometer (the
                    # smaller hits are usually trim badges / sale prices /
                    # climate displays that Gemini fell back to).
                    bg_cur.execute("""UPDATE bids SET mileage=%s, updated_at=NOW()
                                      WHERE id=%s AND (mileage IS NULL OR mileage < %s)""",
                                   (miles, bid_id, miles))
                    # CLEAR_VERIFY_STATE_BASED_2026_05_16: state-based gate.
                    # If the bid has miles now (set by us or an earlier
                    # photo's OCR) AND the flag is open AND reason is
                    # missing_miles, clear it — regardless of whether
                    # *this* UPDATE moved the value.
                    bg_cur.execute("""
                        SELECT mileage,
                               needs_verification_at,
                               needs_verification_cleared_at,
                               needs_verification_reason
                          FROM bids WHERE id = %s
                    """, (bid_id,))
                    _vrow = bg_cur.fetchone()
                    if (_vrow and _vrow.get('mileage')
                            and _vrow.get('needs_verification_at')
                            and not _vrow.get('needs_verification_cleared_at')
                            and 'missing_miles' in (_vrow.get('needs_verification_reason') or '')):
                            bg_cur.execute("""
                                UPDATE bids
                                   SET needs_verification_cleared_at = NOW(),
                                       needs_verification_cleared_by = 'auto:photo_ocr_miles'
                                 WHERE id = %s
                            """, (bid_id,))
                            bg_cur.execute(
                                "DELETE FROM ipacket_lookups WHERE bid_id=%s "
                                "AND (looked_up_at IS NULL OR looked_up_at < NOW() - INTERVAL '5 minutes' OR not_available=true)",
                                (bid_id,))
                            bg_cur.execute("DELETE FROM accutrade_lookups WHERE bid_id=%s", (bid_id,))
                            bg_cur.execute("DELETE FROM vauto_lookups WHERE bid_id=%s", (bid_id,))
                            bg_cur.execute(
                                "UPDATE bids SET vauto_claimed_by=NULL, "
                                "vauto_claimed_at=NULL, ai_assessed_at=NULL, "
                                "ai_price=NULL, ai_assessment=NULL, "
                                "miles_audit_at=NULL WHERE id=%s",
                                (bid_id,))
                            print(f'[photo-ocr-miles] bid={bid_id} miles={miles} '
                                  f'cleared missing_miles flag + force-reprocess fired',
                                  flush=True)
            conn.commit()
    except Exception as _e:
        print(f'[sms-photo-bg] error bid={bid_id} photo={photo_id}: {_e}', flush=True)


def _ingest_sms_photo(cur, bid_id, media_url, media_type, from_phone=None):
    """Webhook-side photo ingest. INSERTs bid_photos row synchronously (so
    the bid record is consistent before Twilio gets its 200), then spawns a
    background thread to fetch the actual MMS bytes. Returns (photo_id, None,
    None) — bytes are fetched out-of-band. If a caller genuinely needs the
    bytes inside the request, use _ingest_sms_photo_sync().

    The bg thread will OCR the photo for VIN+miles (Gemini path; v4 first
    for test user) and write back to bid_photos.vin_extracted + bids.vin/mileage
    when those fields are still NULL."""
    if not media_url:
        return None
    cur.execute("""INSERT INTO bid_photos (bid_id, url, is_sms_intake)
                   VALUES (%s, %s, TRUE) RETURNING id""", (bid_id, media_url))
    photo_id = cur.fetchone()['id']
    if 'image' not in (media_type or ''):
        return (photo_id, None, None)
    threading.Thread(
        target=_bg_download_sms_photo,
        args=(photo_id, bid_id, media_url, media_type, from_phone),
        daemon=True,
    ).start()
    return (photo_id, None, None)


def _save_sms_media_local(bid_id, photo_id, content_bytes, mime):
    """Persist Twilio MMS bytes to static/uploads/sms/<bid_id>/<photo_id>.<ext>.
    Returns the URL path (e.g. '/static/uploads/sms/547/12.jpg'), or None on
    failure. Local copy survives Twilio media rotation and works without the
    auth-gated /thumb proxy."""
    try:
        ext_map = {
            'image/jpeg': '.jpg', 'image/jpg': '.jpg', 'image/png': '.png',
            'image/gif': '.gif', 'image/webp': '.webp', 'image/heic': '.heic',
        }
        ext = ext_map.get((mime or '').split(';')[0].strip().lower(), '.bin')
        rel_dir = 'static/uploads/sms/' + str(bid_id)
        abs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), rel_dir)
        os.makedirs(abs_dir, exist_ok=True)
        fname = f'{photo_id}{ext}'
        with open(os.path.join(abs_dir, fname), 'wb') as f:
            f.write(content_bytes)
        return '/' + rel_dir + '/' + fname
    except Exception as _e:
        print(f'[sms-media] save error bid={bid_id} photo={photo_id}: {_e}', flush=True)
        return None


@app.route('/webhook/twilio', methods=['POST'])
def twilio_webhook():
    from_phone = request.form.get('From', '')
    body = request.form.get('Body', '').strip()
    num_media = int(request.form.get('NumMedia', 0))
    _media_urls = [request.form.get(f'MediaUrl{i}') for i in range(num_media)]
    _media_urls = [u for u in _media_urls if u]

    db = get_db()
    cur = db.cursor()

    # Pre-compute VIN/miles from body — used both for routing decisions below
    # AND for the intake-log record so ops can see what we extracted.
    _early_vin = extract_vin_from_text(body) if body else None
    _early_miles = extract_miles_from_text(body, has_vin=bool(_early_vin)) if body else None

    # Record this hit FIRST. Even if downstream code raises, the log row
    # (with outcome='pending') tells ops "we got the text but it blew up."
    intake_log_id = None
    try:
        intake_log_id = _log_sms_intake(cur, from_phone, body, num_media,
                                        _media_urls, request.form)
        db.commit()
    except Exception as _e:
        # Critical: never let intake-log failure poison the request transaction.
        # Without rollback, the cursor stays in aborted state and every
        # downstream cur.execute fails — silently dropping the inbound bid.
        print(f'[sms-intake] log create error: {_e}', flush=True)
        try:
            db.rollback()
        except Exception:
            pass

    # ── 2026-05-12 Network claim reply (YES) — intercept first ──
    try:
        from partner_portal import try_handle_network_claim
        _h, _r = try_handle_network_claim(from_phone, body)
        if _h:
            from html import escape as _esc
            xml = ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
                   "<Response>" + (f"<Message>{_esc(_r)}</Message>" if _r else "") +
                   "</Response>")
            return (xml, 200, {"Content-Type": "application/xml"})
    except Exception as _e:
        print(f"[network-claim] handler error: {_e}", flush=True)

    # ── Sourcing bot router ──
    # Intercepts inbound from gated phones (default: dev phone only) that
    # look like sourcing requests rather than bid intakes. Returns True if
    # handled (we then return empty TwiML); False falls through to existing
    # bid-reply logic untouched. See sourcing_bot.py for full design.
    try:
        from sourcing_bot import try_handle_sourcing
        if try_handle_sourcing(from_phone, body, db, cur,
                               intake_log_id=intake_log_id,
                               num_media=num_media,
                               send_sms=send_sms):
            return ('<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                    200, {'Content-Type': 'application/xml'})
    except Exception as _src_e:
        # Never let the sourcing path break legacy bid intake.
        print(f'[sourcing-bot] router error: {_src_e}', flush=True)
        try:
            db.rollback()
        except Exception:
            pass

    # ── #BIDNUMBER SMS attach (added 2026-05-14) ──
    # Whitelisted operators (in `contacts` table) can text "#1394" to route
    # photos to a specific bid. Opens a 2-minute sliding window that auto-
    # refreshes on each new photo. Photos arriving with no #bid but an
    # active context still route to that bid. Strictly additive to webhook
    # entry; falls through to existing logic when not applicable.
    import re as _re_hb
    _HB_RE = _re_hb.compile(r"#\s*(\d{2,6})\b")
    _hb_match = _HB_RE.search(body) if body else None
    _hb_phone_norm = _re_hb.sub(r"[^0-9]", "", from_phone or "")
    _hb_whitelisted = False
    if _hb_phone_norm:
        try:
            cur.execute("""SELECT 1 FROM contacts
                            WHERE regexp_replace(phone, '[^0-9]', '', 'g') = %s
                            LIMIT 1""", (_hb_phone_norm,))
            _hb_whitelisted = cur.fetchone() is not None
        except Exception as _hb_e:
            print(f"[#bid] whitelist check error: {_hb_e}", flush=True)
            try: db.rollback()
            except Exception: pass
            _hb_whitelisted = False

    def _hb_attach_photos(target_bid_id):
        """Ingest all media_urls from this MMS into target_bid_id. Returns count."""
        n = 0
        for i, media_url in enumerate(_media_urls):
            mt = request.form.get(f"MediaContentType{i}", "image/jpeg")
            if not mt.startswith("image/"):
                continue
            try:
                _ingest_sms_photo(cur, target_bid_id, media_url, mt, from_phone=from_phone)
                n += 1
            except Exception as _ie:
                print(f"[#bid] photo ingest error bid={target_bid_id}: {_ie}", flush=True)
        return n

    def _hb_xml_reply(text):
        from html import escape as _esc
        return ('<?xml version="1.0" encoding="UTF-8"?><Response><Message>' +
                _esc(text) + '</Message></Response>',
                200, {'Content-Type': 'application/xml'})

    # 1) Explicit "#1394" in body
    # HASHBID_OWNER_UNLOCK_2026_05_16: also allow non-whitelisted phones
    # to use #bid for their OWN bid (bids.phone = from_phone). Customers
    # who reply to a verify SMS with "#1504 <photo>" should reach this
    # path even though they're not in the operator contacts table.
    _hb_is_owner = False
    if _hb_match and not _hb_whitelisted:
        try:
            cur.execute("SELECT 1 FROM bids WHERE id=%s AND phone=%s LIMIT 1",
                        (int(_hb_match.group(1)), from_phone))
            _hb_is_owner = cur.fetchone() is not None
        except Exception:
            try: db.rollback()
            except Exception: pass
            _hb_is_owner = False
    if _hb_match and (_hb_whitelisted or _hb_is_owner):
        _hb_bid_id = int(_hb_match.group(1))
        cur.execute("SELECT id, year, make, model FROM bids WHERE id = %s", (_hb_bid_id,))
        _hb_bid = cur.fetchone()
        if _hb_bid:
            _hb_ymm = " ".join(str(x) for x in (_hb_bid.get("year"), _hb_bid.get("make"), _hb_bid.get("model")) if x).strip() or f"bid #{_hb_bid_id}"
            _hb_ingested = _hb_attach_photos(_hb_bid_id)
            cur.execute("""INSERT INTO sms_attach_context (from_phone, bid_id, expires_at)
                            VALUES (%s, %s, NOW() + INTERVAL '2 minutes')
                            ON CONFLICT (from_phone) DO UPDATE
                            SET bid_id = EXCLUDED.bid_id,
                                expires_at = EXCLUDED.expires_at,
                                set_at = NOW()""", (from_phone, _hb_bid_id))
            # HASHBID_RESIDUAL_2026_05_16: also parse VIN/miles from the
            # text content AFTER stripping the "#NNNN" prefix. Lets a
            # single message like "#1504 1504" set miles=1504 on bid 1504
            # in one shot, instead of forcing the customer to send two
            # separate messages.
            _hb_residual = _HB_RE.sub("", body, count=1).strip() if body else ""
            _hb_resid_vin = None
            _hb_resid_miles = None
            if _hb_residual:
                _hb_resid_vin = extract_vin_from_text(_hb_residual)
                _hb_resid_miles = extract_miles_from_text(_hb_residual,
                                                          has_vin=bool(_hb_resid_vin))
                if not _hb_resid_miles:
                    import re as _re_resid
                    _bm = _re_resid.search(r"\b(\d{3,6})\b", _hb_residual)
                    if _bm:
                        _n = int(_bm.group(1))
                        if 100 <= _n <= 999999:
                            _hb_resid_miles = _n
            _hb_resid_applied = []
            if _hb_resid_vin:
                cur.execute(
                    "UPDATE bids SET vin=%s, updated_at=NOW() "
                    "WHERE id=%s AND (vin IS NULL OR vin='')",
                    (_hb_resid_vin, _hb_bid_id))
                if cur.rowcount > 0:
                    _hb_resid_applied.append(f"vin={_hb_resid_vin}")
            if _hb_resid_miles:
                cur.execute(
                    "UPDATE bids SET mileage=%s, updated_at=NOW() "
                    "WHERE id=%s AND (mileage IS NULL OR mileage < %s)",
                    (_hb_resid_miles, _hb_bid_id, _hb_resid_miles))
                if cur.rowcount > 0:
                    _hb_resid_applied.append(f"miles={_hb_resid_miles}")
            # If we just filled a piece of data that had a verification
            # flag open, clear it + force-reprocess (same logic that the
            # SMS stitch + photo OCR paths use).
            if _hb_resid_applied:
                cur.execute("""SELECT needs_verification_at,
                                       needs_verification_cleared_at,
                                       needs_verification_reason
                                  FROM bids WHERE id=%s""", (_hb_bid_id,))
                _hb_vrow = cur.fetchone()
                _hb_vreason = (_hb_vrow.get('needs_verification_reason') or '').lower() if _hb_vrow else ''
                _hb_needs_clear = False
                if _hb_resid_miles and 'missing_miles' in _hb_vreason:
                    _hb_needs_clear = True
                if _hb_resid_vin and any(k in _hb_vreason for k in (
                        'missing_vin', 'vin_invalid', 'invalid_vin',
                        'vin_not_found')):
                    _hb_needs_clear = True
                if (_hb_needs_clear and _hb_vrow.get('needs_verification_at')
                        and not _hb_vrow.get('needs_verification_cleared_at')):
                    cur.execute("""UPDATE bids
                                       SET needs_verification_cleared_at = NOW(),
                                           needs_verification_cleared_by = 'auto:hashbid_residual'
                                     WHERE id = %s""", (_hb_bid_id,))
                    cur.execute(
                        "DELETE FROM ipacket_lookups WHERE bid_id=%s AND "
                        "(looked_up_at IS NULL OR looked_up_at < NOW() - INTERVAL '5 minutes' "
                        "OR not_available=true)", (_hb_bid_id,))
                    cur.execute("DELETE FROM accutrade_lookups WHERE bid_id=%s",
                                (_hb_bid_id,))
                    cur.execute("DELETE FROM vauto_lookups WHERE bid_id=%s",
                                (_hb_bid_id,))
                    cur.execute(
                        "UPDATE bids SET vauto_claimed_by=NULL, "
                        "vauto_claimed_at=NULL, ai_assessed_at=NULL, "
                        "ai_price=NULL, ai_assessment=NULL, "
                        "miles_audit_at=NULL WHERE id=%s",
                        (_hb_bid_id,))
                    print(f'[hashbid-residual] bid={_hb_bid_id} '
                          f'applied={",".join(_hb_resid_applied)} '
                          f'cleared verification + force-reprocess fired',
                          flush=True)
                else:
                    print(f'[hashbid-residual] bid={_hb_bid_id} '
                          f'applied={",".join(_hb_resid_applied)}', flush=True)

            # Compose reply
            _hb_extras = []
            if _hb_ingested:
                _hb_extras.append(f"{_hb_ingested} photo(s)")
            if _hb_resid_vin:
                _hb_extras.append(f"VIN {_hb_resid_vin}")
            if _hb_resid_miles:
                _hb_extras.append(f"{_hb_resid_miles:,} miles")
            if _hb_extras:
                _hb_reply = f"✓ Got {', '.join(_hb_extras)} on bid #{_hb_bid_id} ({_hb_ymm}). Window open 2 min for more."
            else:
                _hb_reply = f"📎 Ready — photos for next 2 min attach to bid #{_hb_bid_id} ({_hb_ymm})."
            _finalize_sms_intake(cur, intake_log_id, "hashbid_attach",
                                 bid_id=_hb_bid_id,
                                 reason=f"#bid prefix matched bid {_hb_bid_id}; ingested {_hb_ingested} photos",
                                 parsed_vin=None, parsed_miles=None)
            db.commit()
            db.close()
            return _hb_xml_reply(_hb_reply)
        else:
            _finalize_sms_intake(cur, intake_log_id, "hashbid_not_found",
                                 reason=f"#bid {_hb_bid_id} not found in bids table",
                                 parsed_vin=None, parsed_miles=None)
            db.commit()
            db.close()
            return _hb_xml_reply(f"Bid #{_hb_bid_id} not found.")

    # 2) MMS with no #bid prefix but active context for this phone
    # HASHBID_OWNER_UNLOCK_2026_05_16: also honor active context for any
    # phone (the context is keyed by phone in sms_attach_context; if a
    # row exists, this phone earned it by sending #bid earlier).
    if num_media > 0 and not _hb_match:
        try:
            cur.execute("""SELECT bid_id FROM sms_attach_context
                            WHERE from_phone = %s AND expires_at > NOW()
                            LIMIT 1""", (from_phone,))
            _hb_ctx = cur.fetchone()
        except Exception:
            _hb_ctx = None
            try: db.rollback()
            except Exception: pass
        if _hb_ctx:
            _hb_bid_id = _hb_ctx["bid_id"]
            cur.execute("SELECT id, year, make, model FROM bids WHERE id = %s", (_hb_bid_id,))
            _hb_bid = cur.fetchone()
            if _hb_bid:
                _hb_ymm = " ".join(str(x) for x in (_hb_bid.get("year"), _hb_bid.get("make"), _hb_bid.get("model")) if x).strip() or f"bid #{_hb_bid_id}"
                _hb_ingested = _hb_attach_photos(_hb_bid_id)
                cur.execute("""UPDATE sms_attach_context
                                  SET expires_at = NOW() + INTERVAL '2 minutes'
                                WHERE from_phone = %s""", (from_phone,))
                _finalize_sms_intake(cur, intake_log_id, "hashbid_attach",
                                     bid_id=_hb_bid_id,
                                     reason=f"active #bid context routed {_hb_ingested} photos to bid {_hb_bid_id}",
                                     parsed_vin=None, parsed_miles=None)
                db.commit()
                db.close()
                return _hb_xml_reply(f"✓ {_hb_ingested} photo(s) attached to bid #{_hb_bid_id} ({_hb_ymm}).")

    # ── Name-reply routing (Phase 3 onboarding) ──
    # If this phone has one or more held bids (status='awaiting_name'),
    # AND the inbound has no VIN and no media (a plausible name-only reply),
    # attempt to parse the body as a name. On success: release ALL held
    # bids for this phone (they may have sent multiple before answering),
    # save the phone→name memory in bidder_contacts, fire the deferred
    # workers + owner push, and send a single combined Phase 3 ack.
    # On unparseable noise: re-ask once.
    #
    # MUST run before the partner-dealer reply path, the share-reply check,
    # and the stitch / new-bid flow — otherwise the name reply would be
    # mis-routed into one of those (creating a duplicate bid or silently
    # stitching the name as a thread message into the held bid forever).
    if from_phone and not num_media and not _early_vin:
        cur.execute("""SELECT id FROM bids
                        WHERE phone = %s
                          AND awaiting_name = TRUE
                          AND created_at > NOW() - INTERVAL '24 hours'
                        ORDER BY id ASC""", (from_phone,))
        _held_rows = cur.fetchall() or []
        if _held_rows:
            _held_ids = [r['id'] for r in _held_rows]
            _name = _parse_name_reply(body)
            if not _name:
                # Inbound was noise ("ok", "?", digits-only). Re-ask once.
                # Don't burn name_nudged_at — that's reserved for the cron
                # nudge (Stage 2f) after long silence.
                try:
                    send_sms(from_phone,
                             "sorry — just need a name we can call you by.")
                except Exception as _ne:
                    print(f'[name-reask] SMS error phone={from_phone!r}: {_ne}', flush=True)
                _finalize_sms_intake(
                    cur, intake_log_id, 'awaiting_name_reask',
                    bid_id=_held_ids[0],
                    reason=(f"Inbound from phone with held bid(s) {_held_ids} "
                            f"did not parse as a name; re-asked"),
                    parsed_vin=None, parsed_miles=None)
                db.commit()
                db.close()
                return ('<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                        200, {'Content-Type': 'text/xml'})

            # Parseable name — release all held bids for this phone.
            _upsert_bidder_contact(cur, from_phone, _name)
            cur.execute("""UPDATE bids
                              SET bidder_name = %s,
                                  awaiting_name = FALSE,
                                  status = 'new',
                                  updated_at = NOW()
                            WHERE id = ANY(%s)""",
                        (_name, _held_ids))

            # Combined Phase 3 ack covering every released bid.
            if len(_held_ids) == 1:
                _ack_body = (f"thanks {_name}, bid #{_held_ids[0]} received — "
                             f"we'll contact you back shortly.")
            else:
                _bid_list = ', '.join('#' + str(i) for i in _held_ids)
                _ack_body = (f"thanks {_name}, bids {_bid_list} received — "
                             f"we'll contact you back shortly.")
            _sent = send_sms(from_phone, _ack_body)
            if _sent:
                cur.execute("""UPDATE bids
                                  SET phase3_notified_at = NOW()
                                WHERE id = ANY(%s)
                                  AND phase3_notified_at IS NULL""",
                            (_held_ids,))
            print(f'[name-captured] phone={from_phone!r} name={_name!r} '
                  f'bids={_held_ids} ack_sent={_sent}', flush=True)
            _finalize_sms_intake(
                cur, intake_log_id, 'name_captured',
                bid_id=_held_ids[0],
                reason=(f"Released held bid(s) {_held_ids} with name={_name!r}; "
                        f"phase 3 ack {'sent' if _sent else 'FAILED'}"),
                parsed_vin=None, parsed_miles=None)
            db.commit()
            db.close()

            # NOW fire the workers + owner push that were deferred when
            # the bids were held. Done AFTER commit + close so DB locks
            # aren't held across slow worker dispatches.
            for _hid in _held_ids:
                try:
                    _fire_owner_new_bid(_hid)
                except Exception as _oe:
                    print(f'[name-released] owner push error bid={_hid}: {_oe}', flush=True)
                try:
                    _replay_held_bid_workers(_hid)
                except Exception as _we:
                    print(f'[name-released] worker fire error bid={_hid}: {_we}', flush=True)
            return ('<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                    200, {'Content-Type': 'text/xml'})

    # ── Stitch precedence over share-reply ──
    # If this phone has a bid created via SMS in the last 60 seconds, that's
    # almost always a follow-up (VIN-then-miles, photo-then-text). Treat it
    # as a stitch BEFORE checking share-reply, otherwise old share_sent rows
    # hijack legitimate stitch follow-ups.
    cur.execute("""
        SELECT 1 FROM bids
        WHERE phone = %s
          AND created_at > NOW() - INTERVAL '60 seconds'
          AND driver_token IS NOT NULL
        LIMIT 1
    """, (from_phone,))
    has_recent_sms_bid = cur.fetchone() is not None

    # ── Partner-dealer reply ──
    # If the From phone matches a partner_user, the inbound MIGHT be a reply
    # to a bid notification we sent that dealer. Route it into that bid's
    # thread. Resolution order:
    #   1. Skip entirely if the body looks like a new bid intake (has a VIN
    #      or media attachment) — partners forwarding inventory texts should
    #      still create new bids.
    #   2. If body contains "#NNN" and bid NNN belongs to this dealer → that bid.
    #   3. Else look up the most recent bid we sent SMS for to this phone
    #      (partner_sms_sent table) within 30 days — this is the strongest
    #      "you replied to that text" anchor.
    #   4. Else most recent dealer-linked bid in last 14 days (last-resort).
    cur.execute("""SELECT pu.id AS pu_id, pu.dealer_id, pu.full_name, d.name AS dealer_name
                     FROM partner_users pu
                     JOIN dealers d ON d.id = pu.dealer_id
                    WHERE pu.phone = %s
                    LIMIT 1""", (from_phone,))
    partner_row = cur.fetchone()
    # Skip the partner-reply path if the body is clearly a new bid intake.
    _partner_looks_like_new_bid = (
        bool(extract_vin_from_text(body)) if body else False
    ) or (num_media > 0)
    if partner_row and not _partner_looks_like_new_bid:
        target_bid_id = None
        # Path 2: explicit "#NNN" — only if the bid is tied to this dealer.
        m = re.search(r'#\s*(\d+)', body or '')
        if m:
            candidate = int(m.group(1))
            cur.execute("""SELECT b.id
                             FROM bids b
                             LEFT JOIN partner_bid_requests pbr
                                    ON pbr.id = b.partner_request_id
                             LEFT JOIN dealer_inventory di
                                    ON di.vin = b.vin AND di.dealer_id = %s
                            WHERE b.id = %s
                              AND (b.partner_dealer_id = %s
                                OR pbr.dealer_id = %s
                                OR di.id IS NOT NULL)
                            LIMIT 1""",
                        (partner_row['dealer_id'], candidate,
                         partner_row['dealer_id'], partner_row['dealer_id']))
            r = cur.fetchone()
            if r:
                target_bid_id = r['id']
        # Path 3: last bid we SMS'd this phone (within 30 days).
        if not target_bid_id:
            cur.execute("""SELECT bid_id FROM partner_sms_sent
                            WHERE phone = %s
                              AND sent_at > NOW() - INTERVAL '30 days'
                            ORDER BY sent_at DESC LIMIT 1""", (from_phone,))
            r = cur.fetchone()
            if r:
                target_bid_id = r['bid_id']
        # Path 4: most recent dealer-linked bid (last-resort fallback).
        if not target_bid_id:
            cur.execute("""SELECT b.id
                             FROM bids b
                             LEFT JOIN partner_bid_requests pbr
                                    ON pbr.id = b.partner_request_id
                             LEFT JOIN dealer_inventory di
                                    ON di.vin = b.vin AND di.dealer_id = %s
                            WHERE b.created_at > NOW() - INTERVAL '14 days'
                              AND (b.partner_dealer_id = %s
                                OR pbr.dealer_id = %s
                                OR di.id IS NOT NULL)
                            ORDER BY b.created_at DESC
                            LIMIT 1""",
                        (partner_row['dealer_id'],
                         partner_row['dealer_id'],
                         partner_row['dealer_id']))
            r = cur.fetchone()
            if r:
                target_bid_id = r['id']
        if target_bid_id:
            sender_name = (partner_row.get('full_name')
                           or partner_row.get('dealer_name')
                           or from_phone)
            if body:
                cur.execute("""INSERT INTO bid_messages (bid_id, direction, message, from_phone)
                               VALUES (%s, 'inbound', %s, %s)""",
                            (target_bid_id, f'[{sender_name}] {body}', from_phone))
            for i in range(num_media):
                media_url = request.form.get(f'MediaUrl{i}')
                media_type = request.form.get(f'MediaContentType{i}', '')
                _ingest_sms_photo(cur, target_bid_id, media_url, media_type, from_phone=from_phone)
            cur.execute("UPDATE bids SET updated_at=NOW(), has_unread=TRUE WHERE id=%s",
                        (target_bid_id,))
            _finalize_sms_intake(
                cur, intake_log_id, 'partner_reply', bid_id=target_bid_id,
                reason=(f"Phone matched partner_user (dealer={partner_row.get('dealer_name')}); "
                        f"body did not look like new-bid intake; routed reply to bid #{target_bid_id}"),
                parsed_vin=_early_vin, parsed_miles=_early_miles)
            db.commit()
            db.close()
            return ('<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                    200, {'Content-Type': 'text/xml'})

    # ── Check if this is a reply to a shared bid ──
    # Find the most recent bid shared to this phone number — but skip the
    # share-reply path when the inbound is clearly a new bid intake (contains
    # a fresh VIN, has photo attachments, or is a follow-up to a recent SMS
    # bid from this same phone). Otherwise a partner forwarding a new VIN
    # or sending miles within the stitch window gets stapled onto whatever
    # bid they last received a share for.
    cur.execute("""
        SELECT bid_id FROM share_sent
        WHERE phone = %s
        ORDER BY sent_at DESC LIMIT 1
    """, (from_phone,))
    share_row = cur.fetchone()

    looks_like_new_bid = bool(extract_vin_from_text(body)) if body else False
    if num_media > 0:
        looks_like_new_bid = True
    if has_recent_sms_bid:
        looks_like_new_bid = True  # let stitch path handle it below

    # VERIFY_STITCH_FRONT_2026_05_15: customer reply to a needs_verification
    # SMS takes priority over share-reply routing. Without this, a bare
    # number reply ("12000") from a phone that also has a previously-shared
    # bid would get share-replied to the WRONG bid. Setting looks_like_new_bid
    # diverts to the normal flow where verify-stitch (line ~3115 area) attaches
    # to the right verification-pending bid.
    cur.execute("""SELECT id FROM bids
                    WHERE phone = %s
                      AND needs_verification_at IS NOT NULL
                      AND needs_verification_cleared_at IS NULL
                      AND created_at > NOW() - INTERVAL '24 hours'
                    ORDER BY id DESC LIMIT 1""", (from_phone,))
    _vp_row = cur.fetchone()
    if _vp_row:
        print(f'[stitch] verify-pending bid #{_vp_row["id"]} found for '
              f'{from_phone}, bypassing share-reply routing', flush=True)
        looks_like_new_bid = True  # bypass share_reply so verify-stitch handles

    if share_row and not looks_like_new_bid:
        # This is a reply to a shared bid — add as message, don't create new bid
        shared_bid_id = share_row['bid_id']

        # Look up contact name from share_contacts
        cur.execute("SELECT name FROM share_contacts WHERE phone = %s", (from_phone,))
        contact_row = cur.fetchone()
        sender_name = contact_row['name'] if contact_row else from_phone

        if body:
            cur.execute("""
                INSERT INTO bid_messages (bid_id, direction, message, from_phone)
                VALUES (%s, 'inbound', %s, %s)
            """, (shared_bid_id, f'[{sender_name}] {body}', from_phone))

        # Attach any photos to the existing bid
        for i in range(num_media):
            media_url = request.form.get(f'MediaUrl{i}')
            media_type = request.form.get(f'MediaContentType{i}', '')
            _ingest_sms_photo(cur, shared_bid_id, media_url, media_type, from_phone=from_phone)

        cur.execute("UPDATE bids SET updated_at=NOW(), has_unread=TRUE WHERE id=%s", (shared_bid_id,))
        _finalize_sms_intake(
            cur, intake_log_id, 'share_reply', bid_id=shared_bid_id,
            reason=(f"Phone has prior shared bid (#{shared_bid_id}); inbound did not look "
                    f"like new-bid intake (no fresh VIN, no media, no recent SMS bid from this phone); "
                    f"routed as reply to that share"),
            parsed_vin=_early_vin, parsed_miles=_early_miles)
        db.commit()
        db.close()

        return ('<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                200, {'Content-Type': 'text/xml'})

    # ── Normal flow: new bid from SMS ──

    # Upsert contact
    cur.execute("""
        INSERT INTO contacts (phone) VALUES (%s)
        ON CONFLICT (phone) DO UPDATE SET phone = EXCLUDED.phone
        RETURNING id
    """, (from_phone,))
    contact_id = cur.fetchone()['id']

    # Extract VIN + miles from text (regex, no AI). When a VIN is present,
    # the miles regex also accepts bare "47,000" patterns.
    vin = extract_vin_from_text(body) if body else None
    miles = extract_miles_from_text(body, has_vin=bool(vin)) if body else None

    # AI text extractor for prose-y SMS bodies. Cheap (~$0.0003) and gets
    # color / int_color / year/make/model / asking that regex can't.
    # Skip when body is just a bare VIN, just digits, or empty — regex
    # already captured everything those forms contain.
    text_ai = {}
    _body_clean = (body or '').strip()
    if (len(_body_clean) > 20 and
            ' ' in _body_clean and
            re.search(r'[A-Za-z]{3,}', _body_clean)):
        text_ai = extract_vehicle_info_from_text(_body_clean) or {}
        if text_ai:
            print(f'[text-extract] body→{ {k: v for k, v in text_ai.items() if v is not None} }', flush=True)

    # Regex VIN/miles always win (definitive). AI fills only the gaps.
    if not vin and text_ai.get('vin'):
        _ai_vin = str(text_ai['vin']).strip().upper()
        if VIN_RE.match(_ai_vin):
            vin = _ai_vin
    if not miles and text_ai.get('mileage'):
        try:
            _m = int(text_ai['mileage'])
            if 100 <= _m <= 999999:
                miles = _m
        except (ValueError, TypeError):
            pass

    # ── Stitching: merge into a recent bid from same phone ──
    # Partners often split a forward into VIN-text-then-miles or photo-then-text.
    # If the same phone has a bid <60s old AND the new VIN doesn't conflict
    # with the existing one, merge into that bid instead of creating new.
    cur.execute("""
        SELECT id, vin, mileage FROM bids
        WHERE phone = %s
          AND created_at > NOW() - INTERVAL '60 seconds'
          AND driver_token IS NOT NULL
        ORDER BY id DESC LIMIT 1
    """, (from_phone,))
    recent = cur.fetchone()

    # VERIFY_STITCH_2026_05_15: when no rapid-stitch match, look for an
    # OPEN needs_verification bid from this phone that the inbound text
    # plausibly satisfies. Window: 24h (covers customer reply latency from
    # busy partners). Match logic:
    #   - inbound has bare miles AND bid has missing_miles flag -> stitch
    #   - inbound has VIN AND bid awaiting VIN verification     -> stitch
    _verify_stitched = False
    if not recent:
        # Re-derive a bare-number miles candidate even if extract_miles_from_text
        # returned None (it requires "k" / "mi" / labels). Bare "12000" by
        # itself doesn't trigger but in verification-reply context it should.
        _bare_miles = None
        if body and not miles:
            _bm = __import__('re').search(r'\b(\d{3,6})\b', body.strip())
            if _bm:
                _n = int(_bm.group(1))
                if 100 <= _n <= 999999:
                    _bare_miles = _n
        cur.execute("""
            SELECT id, vin, mileage, needs_verification_reason
              FROM bids
             WHERE phone = %s
               AND needs_verification_at IS NOT NULL
               AND needs_verification_cleared_at IS NULL
               AND created_at > NOW() - INTERVAL '24 hours'
             ORDER BY id DESC LIMIT 1
        """, (from_phone,))
        _vpend = cur.fetchone()
        if _vpend:
            _vreason = (_vpend.get('needs_verification_reason') or '').lower()
            _can_miles = ('missing_miles' in _vreason
                          and (miles or _bare_miles)
                          and not _vpend.get('mileage'))
            _can_vin = (('vin_invalid' in _vreason
                         or 'vin_not_found' in _vreason
                         or 'invalid_vin' in _vreason
                         or 'missing_vin' in _vreason)
                        and vin)
            # VERIFY_PHOTO_STITCH_2026_05_16: photo-only reply (no text
            # VIN/miles parsed) routes to verify-pending bid. Customer
            # sends photo as the answer — OCR fills the missing data and
            # the VIN_FILL_CLEARS_VERIFY hook downstream kicks off
            # workers. Without this, the photo creates a brand-new bid
            # (observed on bid 1504 → 1505 split).
            _can_photo = (
                num_media > 0
                and not vin and not miles and not _bare_miles
                and any(k in _vreason for k in (
                    'missing_miles', 'missing_vin', 'invalid_vin',
                    'vin_invalid', 'vin_not_found'))
            )
            if _can_miles or _can_vin or _can_photo:
                recent = _vpend
                if _can_miles and not miles:
                    miles = _bare_miles
                _verify_stitched = True
                print(f'[stitch] verify-pending bid={_vpend["id"]} '
                      f'reason={_vreason} miles_added={bool(_can_miles)} '
                      f'vin_added={bool(_can_vin)} '
                      f'photos_only={_can_photo} num_media={num_media}',
                      flush=True)

    can_stitch = False
    if recent:
        existing_vin = (recent['vin'] or '').strip().upper()
        new_vin = (vin or '').strip().upper()
        # Stitch when: new text has no VIN, OR new VIN matches existing,
        # OR existing has no VIN yet (still being extracted async),
        # OR this is a verification-pending stitch (different rules).
        if (_verify_stitched
                or not new_vin or not existing_vin
                or new_vin == existing_vin):
            can_stitch = True

    if can_stitch:
        bid_id = recent['id']
        # In stitch context, a bare 3-6 digit number is almost certainly miles —
        # accept "53472" the same as "53472 miles". Only kick in if the labeled
        # regex didn't already find one.
        if not miles and body:
            bare = re.search(r'\b(\d{3,6})\b', body.strip())
            if bare:
                n = int(bare.group(1))
                if 100 <= n <= 999999:
                    miles = n
        # Fill VIN if existing didn't have one
        if vin and not recent['vin']:
            cur.execute("UPDATE bids SET vin=%s, vauto_priority=TRUE WHERE id=%s",
                        (vin, bid_id))
        # Miles: partner-typed text ALWAYS wins (Carfax history readings are
        # often older than the actual current odometer; the typed value is
        # the partner's explicit statement of fact).
        if miles:
            cur.execute("UPDATE bids SET mileage=%s WHERE id=%s", (miles, bid_id))

        # AI-extracted fields fill NULLs only on stitch (don't overwrite values
        # already set by the original bid or earlier follow-ups).
        if text_ai:
            cur.execute("""SELECT color, int_color, year, make, model, trim, asking_price
                           FROM bids WHERE id=%s""", (bid_id,))
            existing = cur.fetchone() or {}
            _st_sets, _st_vals = [], []
            for src_key, db_col, coerce in [
                ('color',        'color',         lambda v: str(v).strip()[:64]),
                ('int_color',    'int_color',     lambda v: str(v).strip()[:64]),
                ('year',         'year',          lambda v: int(v) if 1900 <= int(v) <= 2100 else None),
                ('make',         'make',          lambda v: str(v).strip()[:64]),
                ('model',        'model',         lambda v: str(v).strip()[:64]),
                ('trim',         'trim',          lambda v: str(v).strip()[:64]),
                ('asking_price', 'asking_price',  lambda v: float(v) if 0 < float(v) < 10_000_000 else None),
            ]:
                if existing.get(db_col) not in (None, ''):
                    continue
                raw = text_ai.get(src_key)
                if raw in (None, '', 'null'):
                    continue
                try:
                    val = coerce(raw)
                    if val is None:
                        continue
                except (ValueError, TypeError):
                    continue
                _st_sets.append(f'{db_col}=%s')
                _st_vals.append(val)
            if _st_sets:
                _st_vals.append(bid_id)
                cur.execute(f"UPDATE bids SET {', '.join(_st_sets)} WHERE id=%s", _st_vals)
        # Append the new message body (summarized when long prose)
        if body:
            _thread_msg = _summarize_intake(body, vin, miles, text_ai)
            cur.execute("""
                INSERT INTO bid_messages (bid_id, direction, message, from_phone)
                VALUES (%s, 'inbound', %s, %s)
            """, (bid_id, _thread_msg, from_phone))
        # Attach photos + queue Carfax-async if any images. Centralized
        # _ingest_sms_photo handles INSERT + download + local-disk persist;
        # bytes are returned so Carfax can run without re-downloading.
        photo_files = []
        for i in range(num_media):
            media_url = request.form.get(f'MediaUrl{i}')
            media_type = request.form.get(f'MediaContentType{i}', '')
            res = _ingest_sms_photo(cur, bid_id, media_url, media_type, from_phone=from_phone)
            if res and res[1]:
                photo_files.append((res[1], res[2]))
        cur.execute("UPDATE bids SET updated_at=NOW() WHERE id=%s", (bid_id,))
        # STITCH_VERIFY_UNIFIED_2026_05_15: clear verification + force-
        # reprocess whenever a stitch satisfies an open verification flag,
        # REGARDLESS of which stitch path matched (60s-window OR verify-
        # pending). Without this, a customer reply that comes within 60s
        # got correctly stitched but the verification flag stayed set
        # forever — workers wouldn't claim and AI wouldn't fire.
        # Check: did we just add miles or VIN to a bid that had an open
        # verification flag? Then clear it.
        _just_added_data = bool(miles or (vin and not recent.get('vin')))
        if _just_added_data and not _verify_stitched:
            try:
                cur.execute(
                    "SELECT id FROM bids WHERE id = %s "
                    "AND needs_verification_at IS NOT NULL "
                    "AND needs_verification_cleared_at IS NULL",
                    (bid_id,))
                if cur.fetchone():
                    _verify_stitched = True  # trigger the block below
                    print(f'[stitch] 60s-window stitch satisfied open '
                          f'verification flag for bid {bid_id}', flush=True)
            except Exception as _vcse:
                print(f'[stitch] verify-check err bid={bid_id}: {_vcse}',
                      flush=True)
        if _verify_stitched:
            try:
                cur.execute("UPDATE bids SET needs_verification_cleared_at = NOW(), "
                            "needs_verification_cleared_by = 'auto:sms_reply' "
                            "WHERE id = %s", (bid_id,))
                # iPacket: keep recent good capture (<5min). Same SQL as
                # /api/admin/bid/<id>/force-reprocess preservation block.
                cur.execute("""DELETE FROM ipacket_lookups
                                  WHERE bid_id = %s
                                    AND (looked_up_at IS NULL
                                         OR looked_up_at < NOW() - INTERVAL '5 minutes'
                                         OR not_available = true)""", (bid_id,))
                cur.execute("DELETE FROM accutrade_lookups WHERE bid_id=%s",
                            (bid_id,))
                cur.execute("DELETE FROM vauto_lookups WHERE bid_id=%s",
                            (bid_id,))
                cur.execute("UPDATE bids SET vauto_claimed_by=NULL, "
                            "vauto_claimed_at=NULL, ai_assessed_at=NULL, "
                            "ai_price=NULL, ai_assessment=NULL, "
                            "miles_audit_at=NULL WHERE id=%s", (bid_id,))
                cur.execute("UPDATE worker_jobs SET completed_at=NOW(), "
                            "status='released_verify_sms_clear', "
                            "duration_ms=EXTRACT(EPOCH FROM (NOW()-claimed_at))::int*1000 "
                            "WHERE bid_id=%s AND completed_at IS NULL",
                            (bid_id,))
                try:
                    _tg_worker_alert(
                        f"\u2705 EW verify cleared by customer SMS reply\n"
                        f"bid <b>#{bid_id}</b> \u00b7 phone {from_phone} \u00b7 force-reprocess fired")
                except Exception:
                    pass
                # IMMEDIATE_RECEIPT_SMS_2026_05_15: instant ack so customer
                # knows we got their reply (avoid 60-90s silence before AI fires).
                try:
                    _parts = []
                    if miles:
                        _parts.append(f"the {miles:,} miles")
                    if vin and not recent.get("vin"):
                        _parts.append(f"VIN {vin}")
                    if _parts:
                        _body = ("Got " + " and ".join(_parts)
                                 + " - working on it now. Standby for the offer!")
                        send_sms(from_phone, _body)
                        print(f"[immediate-receipt-sms] bid={bid_id} sent",
                              flush=True)
                except Exception as _irse:
                    print(f"[immediate-receipt-sms] err bid={bid_id}: {_irse}",
                          flush=True)
            except Exception as _vcse:
                print(f'[verify-stitch-clear] err bid={bid_id}: '
                      f'{type(_vcse).__name__}: {_vcse}', flush=True)
        _finalize_sms_intake(
            cur, intake_log_id, 'stitched', bid_id=bid_id,
            reason=(f"Stitched into bid #{bid_id} "
                    f"({'verification_reply' if _verify_stitched else 'same phone, <60s old'}). "
                    f"vin_added={bool(vin and not recent['vin'])} "
                    f"miles_added={bool(miles and not recent['mileage'])} "
                    f"photos_added={len(photo_files)}"),
            parsed_vin=vin, parsed_miles=miles)
        db.commit()
        db.close()
        if photo_files:
            threading.Thread(
                target=_process_carfax_async,
                args=(bid_id, photo_files),
                daemon=True
            ).start()
        elif vin and not recent['vin']:
            # We just learned the VIN — kick off market check now
            try:
                trigger_market_check(bid_id, vin)
            except Exception:
                pass
        print(f'[stitch] merged into bid #{bid_id} (vin_added={bool(vin and not recent["vin"])} miles_added={bool(miles and not recent["mileage"])} photos={len(photo_files)})', flush=True)
        return ('<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                200, {'Content-Type': 'text/xml'})

    # Resolve sender identity BEFORE the INSERT so we can tag the bid with
    # bidder_name / partner_dealer_id and decide whether to hold it for a
    # name. Three outcomes:
    #   * partner   — phone matches partner_users (e.g. Greg/Marino).
    #                 Auto-link partner_dealer_id and use partner full_name.
    #   * returning — phone is in bidder_contacts (seen on a prior bid).
    #                 Use the stored name.
    #   * unknown   — first-time sender. Bid is held in 'awaiting_name'
    #                 status until they reply with a name. Workers, owner-
    #                 push, and Phase 1/2/3 SMS are all DEFERRED to that
    #                 follow-up turn (Stage 2c name-reply handler).
    bidder = _lookup_bidder(cur, from_phone)
    # Hold only TRUE unknowns. The 4-number full-broker whitelist is
    # trusted by definition — if one of them texts in without a
    # partner_users / bidder_contacts row, process the bid normally
    # (un-named) rather than asking them for a name. Same behavior as
    # the pre-Phase-3 system.
    is_unknown = (bidder['kind'] == 'unknown'
                  and not _is_full_broker_phone(from_phone))

    # Mini-page token: short, URL-safe, unguessable. Used for /m/<token>
    # auto-reply flow so the sender can review + counter from his phone.
    import secrets as _secrets
    driver_token = _secrets.token_urlsafe(8)[:12]

    # Create bid record. Unknown senders land in 'awaiting_name' so the
    # dashboard's main lane (filters on status='new'/'pending'/etc.) hides
    # them until the name lands. Known senders go straight to 'new'.
    initial_status = 'awaiting_name' if is_unknown else 'new'
    cur.execute("""
        INSERT INTO bids (contact_id, phone, vin, mileage, raw_message, status,
                          driver_token, driver_phone,
                          bidder_name, partner_dealer_id,
                          awaiting_name, name_asked_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s,
                CASE WHEN %s THEN NOW() ELSE NULL END) RETURNING id
    """, (contact_id, from_phone, vin, miles, body, initial_status,
          driver_token, from_phone,
          bidder['name'], bidder['partner_dealer_id'],
          is_unknown, is_unknown))
    bid_id = cur.fetchone()['id']

    # Direct API kick removed 2026-05-08: was firing here with stale
    # session_appraisal_id (Phase 1 hadn't saved the real appraisal_url
    # yet). Now fires only from /api/vauto/submit + /api/vauto/url_capture_result
    # AFTER Phase 1 worker writes the real appraisal_url.

    # Apply AI-extracted fields (color, int_color, year/make/model/trim, asking_price).
    # Only fills NULL columns — never overwrites regex-extracted values above.
    if text_ai:
        _ai_sets, _ai_vals = [], []
        for src_key, db_col, coerce in [
            ('color',        'color',         lambda v: str(v).strip()[:64]),
            ('int_color',    'int_color',     lambda v: str(v).strip()[:64]),
            ('year',         'year',          lambda v: int(v) if 1900 <= int(v) <= 2100 else None),
            ('make',         'make',          lambda v: str(v).strip()[:64]),
            ('model',        'model',         lambda v: str(v).strip()[:64]),
            ('trim',         'trim',          lambda v: str(v).strip()[:64]),
            ('asking_price', 'asking_price',  lambda v: float(v) if 0 < float(v) < 10_000_000 else None),
        ]:
            raw = text_ai.get(src_key)
            if raw in (None, '', 'null'):
                continue
            try:
                val = coerce(raw)
                if val is None:
                    continue
            except (ValueError, TypeError):
                continue
            _ai_sets.append(f'{db_col}=%s')
            _ai_vals.append(val)
        if _ai_sets:
            _ai_vals.append(bid_id)
            cur.execute(f"UPDATE bids SET {', '.join(_ai_sets)} WHERE id=%s", _ai_vals)

    # Store inbound message — use structured summary for long prose so the
    # thread stays readable. Original full body lives in bids.raw_message.
    if body:
        _thread_msg = _summarize_intake(body, vin, miles, text_ai)
        cur.execute("""
            INSERT INTO bid_messages (bid_id, direction, message, from_phone)
            VALUES (%s, 'inbound', %s, %s)
        """, (bid_id, _thread_msg, from_phone))

    # Handle photos — _ingest_sms_photo INSERTs the bid_photos row,
    # downloads bytes (auth Twilio MediaUrl), persists to static/uploads/sms/,
    # and returns bytes for Carfax extraction.
    photo_files = []  # (bytes, mime) — fed to extract_carfax_multi async
    for i in range(num_media):
        media_url = request.form.get(f'MediaUrl{i}')
        media_type = request.form.get(f'MediaContentType{i}', '')
        res = _ingest_sms_photo(cur, bid_id, media_url, media_type, from_phone=from_phone)
        if res and res[1]:
            photo_files.append((res[1], res[2]))

    # Decode VIN → populate vehicle fields. NHTSA returns plant_city, body_class
    # etc. which aren't bids columns; filter to actual columns to avoid SQL error.
    if vin:
        decoded = decode_vin(vin)
        _bid_cols = {'year', 'make', 'model', 'trim'}
        decoded = {k: v for k, v in (decoded or {}).items() if k in _bid_cols}
        if decoded:
            fields = ', '.join(f'{k}=%s' for k in decoded)
            cur.execute(f"UPDATE bids SET {fields} WHERE id=%s",
                        list(decoded.values()) + [bid_id])
        cur.execute("UPDATE bids SET vauto_priority=TRUE WHERE id=%s", (bid_id,))

    _finalize_sms_intake(
        cur, intake_log_id,
        'awaiting_name' if is_unknown else 'new_bid', bid_id=bid_id,
        reason=(f"Created bid #{bid_id} from SMS "
                f"(kind={bidder['kind']}). "
                f"vin={'yes' if vin else 'no'} "
                f"miles={'yes' if miles else 'no'} "
                f"photos={len(photo_files)}"),
        parsed_vin=vin, parsed_miles=miles)
    db.commit()

    # ── Held-bid path: first-time unknown sender ─────────────────────────
    # No worker triggers, no owner push, no Phase 1/2/3 SMS. Just ask for a
    # name and stop. Photos are already persisted to disk via the
    # _ingest_sms_photo calls above, so nothing is lost — the name-reply
    # handler (Stage 2c) will flip status='new', fire workers, and send
    # the Phase 3 ack with the captured name.
    if is_unknown:
        db.close()
        print(f'[awaiting-name] held bid={bid_id} phone={from_phone!r}', flush=True)
        if from_phone and not from_phone.startswith('field:'):
            try:
                send_sms(
                    from_phone,
                    "got it — looks like this is your first bid with us. "
                    "what's a name we can use for reference?")
            except Exception as _ne:
                print(f'[awaiting-name] SMS error bid={bid_id}: {_ne}', flush=True)
        return ('<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                200, {'Content-Type': 'text/xml'})

    # ── Known-sender path: partner or returning bidder ───────────────────
    # Send a personalized ack. Wording diverges based on whether this phone
    # is in the Phase 1/2 whitelist:
    #   * full-broker → "give us a minute" (more SMS coming via Phase 1/2)
    #   * Phase 3     → "we'll contact you back shortly" (final SMS) AND
    #                   stamp phase3_notified_at so the cron sweep doesn't
    #                   re-send.
    _full_broker = _is_full_broker_phone(from_phone)
    _name = bidder['name']
    print(f'[bid-ack] entering for bid={bid_id} phone={from_phone!r} '
          f'kind={bidder["kind"]} full_broker={_full_broker}', flush=True)
    if from_phone and not from_phone.startswith('field:'):
        try:
            if _full_broker:
                _ack_body = (f"thanks {_name}, bid #{bid_id} received — "
                             f"give us a minute." if _name
                             else f"Bid #{bid_id} received — give us a minute.")
                _ack_result = send_sms(from_phone, _ack_body)
                print(f'[bid-ack] full-broker sent bid={bid_id} result={_ack_result}', flush=True)
            else:
                # Phase 3 path — _send_phase3_ack handles wording + idempotency.
                _send_phase3_ack(cur, bid_id, from_phone, _name)
                db.commit()
        except Exception as _ack_e:
            print(f'[bid-ack] error bid={bid_id}: {_ack_e}', flush=True)

    db.close()

    # Owner-portal push fan-out (best-effort, never blocks)
    _fire_owner_new_bid(bid_id)

    # Background Carfax-aware extraction across all forwarded images.
    # Same path as Quick Drop: VIN + miles + YMM + trim + title + accidents +
    # owners + color + asking_price in one Gemini Pro pass per image. Threaded
    # so Twilio's webhook returns fast (image extraction can take 10-20s).
    if photo_files:
        threading.Thread(
            target=_process_carfax_async,
            args=(bid_id, photo_files),
            daemon=True
        ).start()
    elif vin:
        # Text-only path: VIN was extracted from message body, fire vAuto now
        trigger_market_check(bid_id, vin)

    return ('<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
            200, {'Content-Type': 'text/xml'})


def _process_carfax_async(bid_id, photo_files):
    """Run extract_carfax_multi on SMS-forwarded images, fold results into the
    bid (VIN, mileage, YMM, trim, color, asking, title/accidents/owners notes),
    then trigger market check + vAuto. Idempotent: only fills NULL fields.

    Photo-extracted VINs MUST pass ISO 3779 check-digit before being saved —
    a misread (e.g. windshield glare turning '1G1' into '1GT') would push the
    wrong car through vAuto/AccuTrade/iPacket and text the partner back the
    wrong vehicle. If the check digit fails we drop the VIN, leave the bid
    photos-only, and SMS the partner asking for the VIN as text."""
    try:
        info = extract_carfax_multi(photo_files) or {}
    except Exception as e:
        print(f'[carfax-async] extract error bid={bid_id}: {e}', flush=True)
        info = {}

    # Reject photo-extracted VINs that fail the check digit. Glare / angle /
    # reflections frequently produce visually-plausible but mathematically
    # invalid VINs. Better to ask the partner for a text VIN than ship a
    # bad one through the pipeline.
    _photo_vin = (info.get('vin') or '').strip().upper()
    _photo_vin_invalid = False
    if _photo_vin and len(_photo_vin) == 17:
        if not vin_check_digit_valid(_photo_vin):
            print(f'[carfax-async] bid={bid_id} VIN check digit FAILED for '
                  f'photo-extracted "{_photo_vin}" — dropping', flush=True)
            info.pop('vin', None)
            _photo_vin_invalid = True
    elif _photo_vin:
        # Wrong length entirely — drop
        info.pop('vin', None)

    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("""
            SELECT vin, mileage, year, make, model, trim, color, asking_price, notes
            FROM bids WHERE id=%s
        """, (bid_id,))
        row = cur.fetchone()
        if not row:
            db.close()
            return

        # Photo extraction policy:
        #   ALLOWED  — VIN, mileage, Carfax-specific fields (title/accidents/
        #              owners). These are objective tokens vision can read
        #              cleanly: 17-char string, odometer digits, fixed labels.
        #   BLOCKED  — year, make, model, trim, color, asking_price. Vision
        #              was misidentifying similar SUVs (Lexus GX → Toyota
        #              RAV4) and overwriting NHTSA's authoritative VIN-decode.
        #              Year/make/model come from NHTSA, period.
        sets, vals = [], []
        for src_key, db_col in [('vin', 'vin'), ('mileage', 'mileage')]:
            if row[db_col] in (None, '') and info.get(src_key) is not None:
                sets.append(f'{db_col}=%s')
                vals.append(info[src_key])

        # Carfax-specific fields go into notes (no dedicated columns)
        carfax_bits = []
        if info.get('title_status'):
            carfax_bits.append(f"Title: {info['title_status']}")
        if info.get('accidents') is not None:
            carfax_bits.append(f"Accidents: {info['accidents']}")
        if info.get('owners') is not None:
            carfax_bits.append(f"Owners: {info['owners']}")
        # Surface a near-VIN that failed validation so the user can verify
        # against the photo and either correct it or text the partner. We
        # only show this when no good VIN ended up on the bid.
        _rejected_vin = info.get('_rejected_vin')
        if _rejected_vin and not row.get('vin'):
            carfax_bits.append(f"Photo VIN candidate (verify): {_rejected_vin}")
        if carfax_bits:
            new_notes = '[Carfax via SMS] ' + ' · '.join(carfax_bits)
            existing = row['notes'] or ''
            if '[Carfax via SMS]' not in existing:
                merged = (existing + '\n' + new_notes).strip() if existing else new_notes
                sets.append('notes=%s')
                vals.append(merged)

        if sets:
            vals.append(bid_id)
            cur.execute(f"UPDATE bids SET {', '.join(sets)}, updated_at=NOW() WHERE id=%s", vals)

        # If we have a VIN now, flag vAuto priority + decode.
        # NHTSA is authoritative for year/make/model — Claude Vision mis-IDs
        # similar SUVs (Lexus GX → Toyota Land Cruiser/RAV4 has happened) so
        # we OVERWRITE those fields when NHTSA returns data, not COALESCE.
        # Trim still uses COALESCE because NHTSA sometimes returns an
        # ambiguous list and a vision-read trim badge can be more specific.
        cur.execute("SELECT vin FROM bids WHERE id=%s", (bid_id,))
        final_vin = (cur.fetchone() or {}).get('vin')
        if final_vin and len(final_vin) == 17:
            cur.execute("UPDATE bids SET vauto_priority=TRUE WHERE id=%s", (bid_id,))
            try:
                decoded = decode_vin(final_vin) or {}
                decoded = {k: v for k, v in decoded.items()
                           if k in ('year', 'make', 'model', 'trim') and v}
                if decoded:
                    decode_sets = []
                    decode_vals = []
                    for k, v in decoded.items():
                        if k == 'trim':
                            decode_sets.append(f'{k}=COALESCE({k}, %s)')
                        else:
                            decode_sets.append(f'{k}=%s')
                        decode_vals.append(v)
                    cur.execute(f"UPDATE bids SET {', '.join(decode_sets)} WHERE id=%s",
                                decode_vals + [bid_id])
            except Exception as e:
                print(f'[carfax-async] decode_vin error: {e}', flush=True)

        db.commit()
        db.close()

        if final_vin:
            try:
                trigger_market_check(bid_id, final_vin)
            except Exception as e:
                print(f'[carfax-async] market_check error: {e}', flush=True)
        elif photo_files and '[Carfax via SMS]' not in (row.get('notes') or ''):
            # No VIN ended up on the bid but we had photos to read. Cover both
            # failure modes: 17-char-but-bad-check-digit (caught at async-level)
            # AND wrong-length / unreadable (caught at carfax-multi level via
            # _rejected_vin). Tell the partner so they can text the VIN
            # instead of waiting on a silent bid. Guard: only fires on the
            # first carfax pass for a bid (existing notes lack our marker)
            # so a second photo upload doesn't double-SMS the partner.
            _candidate = _rejected_vin or (_photo_vin if _photo_vin_invalid else None)
            try:
                _vdb = get_db()
                _vcur = _vdb.cursor()
                _vcur.execute("SELECT driver_phone FROM bids WHERE id=%s", (bid_id,))
                _row = _vcur.fetchone()
                _vdb.close()
                if _row and _row.get('driver_phone'):
                    if _candidate:
                        _msg = (f"Bid #{bid_id} — couldn't read the VIN clearly "
                                f"(best guess: {_candidate}). Please text the "
                                f"17-char VIN and we'll re-process.")
                    else:
                        _msg = (f"Bid #{bid_id} — couldn't read a VIN from the "
                                f"photo. Please text the 17-char VIN and we'll "
                                f"re-process.")
                    send_sms(_row['driver_phone'], _msg)
                    print(f'[carfax-async] bid={bid_id} sent VIN-unclear SMS to driver '
                          f'(candidate={_candidate})', flush=True)
            except Exception as e:
                print(f'[carfax-async] vin-unclear SMS error: {e}', flush=True)

        print(f'[carfax-async] bid={bid_id} extracted={list(info.keys())} '
              f'photo_vin_dropped={_photo_vin_invalid}', flush=True)
    except Exception as e:
        print(f'[carfax-async] db error bid={bid_id}: {e}', flush=True)
        try:
            db.close()
        except Exception:
            pass


def _replay_held_bid_workers(bid_id):
    """Fire the workers that were deferred when this bid was held in
    'awaiting_name' status. Reads VIN + photo paths from DB, reloads photo
    bytes from disk, and dispatches the same async paths the original
    SMS-intake webhook would have hit if the sender had been known.

    Mirrors the photo / VIN branching at the end of twilio_webhook:
      * photos present → _process_carfax_async (which folds in VIN + miles +
        YMM and then triggers market check)
      * no photos but VIN known → trigger_market_check directly
      * neither → nothing fires; bid sits as 'new' for manual handling
    """
    db = get_db()
    cur = db.cursor()
    vin = None
    photo_rows = []
    try:
        cur.execute("SELECT vin FROM bids WHERE id = %s", (bid_id,))
        row = cur.fetchone()
        if not row:
            return
        vin = (row.get('vin') or '').strip() or None
        cur.execute("""SELECT local_path FROM bid_photos
                        WHERE bid_id = %s AND local_path IS NOT NULL
                        ORDER BY id ASC""", (bid_id,))
        photo_rows = cur.fetchall() or []
    finally:
        try:
            db.close()
        except Exception:
            pass

    photo_files = []
    for p in photo_rows:
        rel = p.get('local_path') if isinstance(p, dict) else None
        if not rel:
            continue
        full = rel if os.path.isabs(rel) else os.path.join('/opt/expwholesale', rel)
        if not os.path.exists(full):
            print(f'[replay] missing photo file bid={bid_id} path={full}', flush=True)
            continue
        try:
            with open(full, 'rb') as f:
                data = f.read()
        except Exception as _re:
            print(f'[replay] photo read error bid={bid_id} {full}: {_re}', flush=True)
            continue
        ext = os.path.splitext(full)[1].lower()
        if ext == '.png':
            mime = 'image/png'
        elif ext in ('.heic', '.heif'):
            mime = 'image/heic'
        else:
            mime = 'image/jpeg'
        photo_files.append((data, mime))

    if photo_files:
        threading.Thread(
            target=_process_carfax_async,
            args=(bid_id, photo_files),
            daemon=True,
        ).start()
    elif vin:
        try:
            trigger_market_check(bid_id, vin)
        except Exception as _me:
            print(f'[replay] market check error bid={bid_id}: {_me}', flush=True)
    print(f'[replay] fired workers bid={bid_id} photos={len(photo_files)} '
          f'vin={"yes" if vin else "no"}', flush=True)


@app.route('/api/bid/<int:bid_id>/reply', methods=['POST'])
def send_reply(bid_id):
    data = request.json or {}
    message = data.get('message', '').strip()
    action = data.get('action', 'bid')  # 'bid' or 'pass'

    if not message:
        return jsonify({'error': 'Message required'}), 400

    db = get_db()
    cur = db.cursor()

    cur.execute("SELECT phone, year, make, model FROM bids WHERE id=%s", (bid_id,))
    bid = cur.fetchone()
    if not bid:
        db.close()
        return jsonify({'error': 'Not found'}), 404

    new_status = 'passed' if action == 'pass' else 'bid_sent'
    bid_amount = data.get('bid_amount')

    # Always update the DB first — SMS is best-effort
    cur.execute("""
        UPDATE bids SET status=%s, bid_amount=%s, bid_response=%s,
        bid_sent_at=NOW(), updated_at=NOW() WHERE id=%s
    """, (new_status, bid_amount, message, bid_id))

    cur.execute("""
        INSERT INTO bid_messages (bid_id, direction, message, to_phone)
        VALUES (%s, 'outbound', %s, %s)
    """, (bid_id, message, bid['phone']))

    db.commit()
    db.close()

    # Partner-dealer bids: notify via the channels the client picked on the
    # Send Bid UI (#32). Dashboard notification is always on; text/email are
    # opt-in per dealer + opt-in per click. Safe no-op for non-partner bids.
    notify_text = bool(data.get('notify_text'))
    notify_email = bool(data.get('notify_email'))
    try:
        from partner_portal import notify_partner_of_ew_response
        notify_partner_of_ew_response(bid_id,
                                       send_email=notify_email,
                                       send_text=notify_text)
    except Exception as _e:
        print(f'[partner notify] skipped for bid {bid_id}: {_e}', flush=True)

    # Attempt SMS after DB is committed — failure doesn't affect the response
    sms_sent = send_sms(bid['phone'], message)

    # Push notification to the field rep who submitted this bid (best-effort, never breaks reply)
    try:
        if bid['phone'] and bid['phone'].startswith('field:'):
            vehicle = ' '.join(filter(None, [str(bid.get('year') or ''), bid.get('make', ''), bid.get('model', '')]))
            if new_status == 'bid_sent' and bid_amount:
                push_title = f'Bid #{bid_id} — ${int(float(bid_amount)):,} bid sent'
                push_body = f'{vehicle}: {message}'
            elif new_status == 'passed':
                push_title = f'Bid #{bid_id} — Passed'
                push_body = f'{vehicle}: {message}'
            else:
                push_title = f'Bid #{bid_id} — Update'
                push_body = message
            send_push_to_rep(bid['phone'], push_title, push_body)
    except Exception as e:
        print(f'Push notification error (non-fatal): {e}')

    return jsonify({'success': True, 'status': new_status, 'sms_sent': sms_sent})


@app.route('/api/bid/<int:bid_id>/update', methods=['POST'])
def update_bid(bid_id):
    data = request.json or {}
    db = get_db()
    cur = db.cursor()

    # Detect "VIN was just added" so we can fire the same downstream
    # pipeline the SMS webhook does (NHTSA decode → vAuto/AccuTrade/iPacket
    # priority + market check). Without this, a user fixing a photo VIN
    # candidate manually would leave the bid stuck without lookups.
    cur.execute("SELECT vin FROM bids WHERE id=%s", (bid_id,))
    _existing = cur.fetchone() or {}
    _prev_vin = (_existing.get('vin') or '').strip().upper()
    _new_vin_in = (data.get('vin') or '').strip().upper() if data.get('vin') else ''
    _vin_just_added = (
        'vin' in data
        and _new_vin_in and len(_new_vin_in) == 17
        and vin_check_digit_valid(_new_vin_in)
        and _new_vin_in != _prev_vin
    )

    allowed = ['vin', 'year', 'make', 'model', 'trim', 'mileage', 'color', 'status', 'notes']
    fields, values = [], []
    for f in allowed:
        if f in data:
            fields.append(f'{f}=%s')
            values.append(data[f] or None)

    if fields:
        values.append(bid_id)
        cur.execute(f"UPDATE bids SET {', '.join(fields)}, updated_at=NOW() WHERE id=%s", values)
        db.commit()

    # ── VIN-just-added pipeline ──
    # Run NHTSA decode (fills year/make/model/trim where NULL), flag for
    # vAuto/AccuTrade/iPacket pickup, kick off market-check scrape.
    if _vin_just_added:
        try:
            decoded = decode_vin(_new_vin_in) or {}
            decoded = {k: v for k, v in decoded.items()
                       if k in ('year', 'make', 'model', 'trim') and v}
            if decoded:
                # COALESCE so we never overwrite values the user explicitly
                # filled in this same update call.
                _sets, _vals = [], []
                for k, v in decoded.items():
                    _sets.append(f'{k}=COALESCE({k}, %s)')
                    _vals.append(v)
                _vals.append(bid_id)
                cur.execute(f"UPDATE bids SET {', '.join(_sets)} WHERE id=%s", _vals)
        except Exception as _e:
            print(f'[update_bid] decode_vin error bid={bid_id}: {_e}', flush=True)
        cur.execute("UPDATE bids SET vauto_priority=TRUE WHERE id=%s", (bid_id,))
        db.commit()

    db.close()

    # PHASE_D_VERIFY_GATE_2026_05_15: VIN or miles edit clears open verify
    # flag + force-reprocess. iPacket preserved if <5 min old (operator's
    # quick correction shouldn't lose a good sticker to iPacket rate limit).
    _verif_cleared = False
    if 'vin' in data or 'mileage' in data:
        try:
            _vdb = get_db()
            _vcur = _vdb.cursor()
            _vcur.execute(
                "SELECT needs_verification_at, needs_verification_cleared_at "
                "FROM bids WHERE id = %s", (bid_id,))
            _vrow = _vcur.fetchone()
            if (_vrow and _vrow.get('needs_verification_at')
                    and not _vrow.get('needs_verification_cleared_at')):
                _vcur.execute(
                    "UPDATE bids SET needs_verification_cleared_at = NOW(), "
                    "needs_verification_cleared_by = 'auto:operator_edit' "
                    "WHERE id = %s", (bid_id,))
                # iPacket: keep recent good capture; wipe vauto + accutrade.
                _vcur.execute("""DELETE FROM ipacket_lookups
             WHERE bid_id = %s
               AND (looked_up_at IS NULL
                    OR looked_up_at < NOW() - INTERVAL '5 minutes'
                    OR not_available = true)""", (bid_id,))
                _vcur.execute(
                    'DELETE FROM accutrade_lookups WHERE bid_id=%s', (bid_id,))
                _vcur.execute(
                    'DELETE FROM vauto_lookups WHERE bid_id=%s', (bid_id,))
                _vcur.execute(
                    "UPDATE bids SET vauto_claimed_by=NULL, "
                    "vauto_claimed_at=NULL, ai_assessed_at=NULL, "
                    "ai_price=NULL, ai_assessment=NULL, "
                    "miles_audit_at=NULL WHERE id = %s", (bid_id,))
                _vcur.execute(
                    "UPDATE worker_jobs SET completed_at=NOW(), "
                    "status='released_verify_autoclear', "
                    "duration_ms=EXTRACT(EPOCH FROM (NOW()-claimed_at))::int*1000 "
                    "WHERE bid_id = %s AND completed_at IS NULL", (bid_id,))
                _vdb.commit()
                _verif_cleared = True
                try:
                    _tg_worker_alert(
                        f"\u2705 EW verify flag auto-cleared (VIN/miles edit)\n"
                        f"bid <b>#{bid_id}</b> \u00b7 force-reprocess fired "
                        f"(iPacket preserved if <5min)")
                except Exception:
                    pass
            _vdb.close()
        except Exception as _ace:
            print(f'[update_bid] verify auto-clear error bid={bid_id}: '
                  f'{type(_ace).__name__}: {_ace}', flush=True)

    # Market check fires after DB close so worker thread doesn't share cur.
    if _vin_just_added:
        try:
            trigger_market_check(bid_id, _new_vin_in)
        except Exception as _e:
            print(f'[update_bid] market_check error bid={bid_id}: {_e}', flush=True)

    return jsonify({'success': True,
                    'vin_pipeline_triggered': _vin_just_added,
                    'verification_cleared': _verif_cleared})


def _run_market_check_playwright(bid_id, vin):
    """Scrape Autotrader, Cars.com, CarGurus for a VIN using Playwright. Runs in background thread."""
    import json as _json
    from playwright.sync_api import sync_playwright

    results = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
            ctx = browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
                viewport={'width': 1280, 'height': 800}
            )

            # ── Autotrader ──────────────────────────────────────────────────────
            try:
                page = ctx.new_page()
                page.goto(f'https://www.autotrader.com/cars-for-sale/all-cars?vin={vin}',
                          wait_until='domcontentloaded', timeout=25000)
                page.wait_for_timeout(3000)
                page_text = page.inner_text('body')
                link = page.query_selector('a[href*="vehicledetails"]')
                if link and vin.upper() in page_text.upper():
                    href = link.get_attribute('href') or ''
                    url = href if href.startswith('http') else 'https://www.autotrader.com' + href
                    results['autotrader'] = {'found': True, 'url': url}
                else:
                    results['autotrader'] = {'found': False}
                page.close()
            except Exception as e:
                results['autotrader'] = {'found': None, 'error': str(e)[:80]}

            # ── Cars.com ────────────────────────────────────────────────────────
            try:
                page = ctx.new_page()
                page.goto(f'https://www.cars.com/shopping/results/?keyword={vin}&stock_type=all&maximum_distance=all&zip=10001',
                          wait_until='domcontentloaded', timeout=25000)
                page.wait_for_timeout(2000)
                link = page.query_selector('a[href*="/vehicledetail/"]')
                if link:
                    href = link.get_attribute('href') or ''
                    url = href if href.startswith('http') else 'https://www.cars.com' + href
                    results['cars_com'] = {'found': True, 'url': url}
                else:
                    results['cars_com'] = {'found': False}
                page.close()
            except Exception as e:
                results['cars_com'] = {'found': None, 'error': str(e)[:80]}

            # ── CarGurus ─────────────────────────────────────────────────────────
            try:
                page = ctx.new_page()
                page.goto(f'https://www.cargurus.com/Cars/new/nl?zip=10001&vin={vin}',
                          wait_until='domcontentloaded', timeout=25000)
                page.wait_for_timeout(3000)
                # Only match actual vehicle listing detail links (format: #listing=d_XXXXXXX)
                link = page.query_selector('a[href*="#listing=d_"]')
                # Also verify the VIN appears in page text (confirms it's a real result)
                page_text = page.inner_text('body')
                if link and vin.upper() in page_text.upper():
                    href = link.get_attribute('href') or ''
                    url = href if href.startswith('http') else 'https://www.cargurus.com' + href
                    results['cargurus'] = {'found': True, 'url': url}
                else:
                    results['cargurus'] = {'found': False}
                page.close()
            except Exception as e:
                results['cargurus'] = {'found': None, 'error': str(e)[:80]}

            browser.close()
    except Exception as e:
        # Suppress the "chrome-headless-shell binary missing" noise — the
        # Playwright browsers aren't installed on this server, market_check
        # is a nice-to-have, no client-facing impact. Real errors still log.
        msg = str(e)
        if "Executable doesn't exist" not in msg and 'chrome-headless-shell' not in msg:
            print(f'Market check browser error: {e}')

    # Save results to DB
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute("UPDATE bids SET market_check=%s WHERE id=%s",
                    (_json.dumps(results), bid_id))
        db.commit()
        db.close()
    except Exception as e:
        print(f'Market check DB save error: {e}')


def trigger_market_check(bid_id, vin):
    """Launch background thread to run Playwright market check.

    2026-05-11: also kicks off VIN canonicalization (NHTSA decode + VIN-prefix
    trim lookup → writes canon_year/make/model/trim/source/confidence on the
    bid). This is the single intake hook that every bid creation path runs
    through, so wiring canonicalize here covers SMS, quick-drop, API,
    external, and operator-edit paths in one place.
    """
    if not vin or len(vin) != 17:
        return
    import threading
    t = threading.Thread(target=_run_market_check_playwright, args=(bid_id, vin), daemon=True)
    t.start()
    # Phase 3 canonicalizer — runs in its own daemon thread, opens its own
    # DB conn, never blocks request hot path.
    try:
        from canonicalize_bid import canonicalize_bid_vin_async
        canonicalize_bid_vin_async(bid_id, get_db)
    except Exception as _ce:
        print(f'[canonicalize] kick failed bid={bid_id}: {_ce}', flush=True)


@app.route('/api/bid/<int:bid_id>/market-check', methods=['POST'])
def market_check(bid_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT vin, market_check FROM bids WHERE id=%s", (bid_id,))
    row = cur.fetchone()
    db.close()
    if not row or not row['vin']:
        return jsonify({'error': 'No VIN on this bid'}), 400
    # Clear old results and re-run
    db = get_db(); cur = db.cursor()
    cur.execute("UPDATE bids SET market_check=NULL WHERE id=%s", (bid_id,))
    db.commit(); db.close()
    trigger_market_check(bid_id, row['vin'])
    return jsonify({'status': 'running'})


@app.route('/api/bid/<int:bid_id>/market-status', methods=['GET'])
def market_status(bid_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT market_check FROM bids WHERE id=%s", (bid_id,))
    row = cur.fetchone()
    db.close()
    if not row:
        return jsonify({'error': 'Not found'}), 404
    return jsonify({'ready': row['market_check'] is not None, 'data': row['market_check']})


@app.route('/api/bid/<int:bid_id>/detect-color', methods=['POST'])
def detect_color_route(bid_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT url FROM bid_photos WHERE bid_id=%s ORDER BY id LIMIT 1", (bid_id,))
    row = cur.fetchone()
    if not row:
        db.close()
        return jsonify({'error': 'No photos on this bid'}), 400

    photo_url = row['url']
    # Strip the /static/uploads/ prefix to get the filename
    photo_path = os.path.join(UPLOAD_DIR, os.path.basename(photo_url))

    color = None
    if os.path.exists(photo_path):
        with open(photo_path, 'rb') as f:
            color = extract_color_from_file(f.read())
    else:
        # Twilio-hosted photo — fetch it
        try:
            resp = requests.get(photo_url, timeout=10,
                                auth=(TWILIO_SID, TWILIO_TOKEN) if 'twilio' in photo_url else None)
            if resp.status_code == 200:
                color = extract_color_from_file(resp.content, resp.headers.get('Content-Type', 'image/jpeg'))
        except Exception:
            pass

    if not color:
        db.close()
        return jsonify({'error': 'Could not determine color'}), 400

    cur.execute("UPDATE bids SET color=%s, updated_at=NOW() WHERE id=%s", (color, bid_id))
    db.commit()
    db.close()
    return jsonify({'success': True, 'color': color})


@app.route('/api/bid/<int:bid_id>/decode-vin', methods=['POST'])
def decode_vin_route(bid_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT vin FROM bids WHERE id=%s", (bid_id,))
    row = cur.fetchone()
    if not row or not row['vin']:
        db.close()
        return jsonify({'error': 'No VIN on this bid'}), 400

    decoded = decode_vin(row['vin'])
    if not decoded:
        db.close()
        return jsonify({'error': 'VIN not recognized'}), 400

    fields = ', '.join(f'{k}=%s' for k in decoded)
    cur.execute(f"UPDATE bids SET {fields}, updated_at=NOW() WHERE id=%s",
                list(decoded.values()) + [bid_id])
    db.commit()
    db.close()
    return jsonify({'success': True, **decoded})


# ── _get_thalist_asks_for_bid ─ wholesale-ask context for Gemini ───────
def _get_thalist_asks_for_bid(bid: dict, db=None) -> dict | None:
    """Pull active Thalist posts matching this bid\'s year/make/model
    and aggregate the asking prices into P25/P50/P75.

    Excludes the bid\'s own thalist post (so we don\'t self-anchor).
    Returns None when there\'s nothing useful to show.
    """
    year = bid.get('year')
    make = (bid.get('make') or '').strip()
    model = (bid.get('model') or '').strip()
    if not (year and make and model):
        return None
    own = db is None
    if own:
        db = get_db()
    try:
        cur = db.cursor()
        # Match year+make+model on active posts (not invalidated).
        # The make matching is fuzzy to handle "Mercedes-Benz" vs
        # "MERCEDES-BENZ" vs "Mercedes Benz".
        # Posts have NO direct make column — we look up via title prefix
        # OR via the bid that consumed them (b.make).
        cur.execute("""
            SELECT tp.id, tp.title, tp.asking_price, tp.mileage,
                   tp.poster_company, tp.first_seen_at
              FROM thalist_posts tp
              LEFT JOIN bids b2 ON b2.id = tp.bid_id
             WHERE tp.invalidated_at IS NULL
               AND tp.asking_price IS NOT NULL
               AND tp.asking_price > 0
               AND tp.year = %s
               AND (
                   upper(coalesce(b2.make, '')) = upper(%s)
                   OR upper(tp.title) LIKE upper(%s) || '%%'
                   OR upper(tp.title) LIKE '%%' || upper(%s) || '%%'
               )
               AND (
                   upper(coalesce(b2.model, '')) = upper(%s)
                   OR upper(tp.model) = upper(%s)
                   OR upper(tp.title) LIKE '%%' || upper(%s) || '%%'
               )
               AND (tp.bid_id IS NULL OR tp.bid_id <> %s)
             ORDER BY tp.first_seen_at DESC
             LIMIT 200
        """, (year, make, f'{year} {make}', make, model, model, model, bid.get('id') or 0))
        rows = cur.fetchall()
        if not rows:
            return None
        prices = sorted([int(r['asking_price']) for r in rows
                         if r.get('asking_price')])
        n = len(prices)
        if n == 0:
            return None
        # Percentiles (linear)
        def _pct(p):
            if n == 1:
                return prices[0]
            idx = max(0, min(n - 1, int(round(p * (n - 1)))))
            return prices[idx]
        return {
            'n': n,
            'p25': _pct(0.25),
            'p50': _pct(0.50),
            'p75': _pct(0.75),
            'posts': [dict(r) for r in rows[:5]],
        }
    except Exception as e:
        print(f'[ASSESS] thalist_asks query err: {e}', flush=True)
        return None
    finally:
        if own:
            try: db.close()
            except Exception: pass




def _run_assessment(bid_id):
    """Core assessment logic — callable from endpoint or background thread.
    Returns dict: {'success': True, 'assessment': ..., 'buy_price': ...} or {'error': ...}
    Uses Gemini 2.5 Pro (Vertex AI) for multi-modal reasoning.
    """

    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT b.*, c.name as contact_name FROM bids b
        LEFT JOIN contacts c ON b.contact_id = c.id WHERE b.id = %s
    """, (bid_id,))
    bid = cur.fetchone()
    if not bid:
        db.close()
        return {'error': 'Not found'}

    # Mileage guardrail — refuse to assess if mileage is NULL.
    # Without an odometer reading, the AI hallucinates low_miles / high_miles
    # flags and produces confident dollar figures with no basis. Bid 1012
    # (no photo) and bid 1020 (OCR regex bug) both hit this on 2026-05-07.
    # A human must confirm mileage before a buy recommendation is generated.
    if bid.get('mileage') is None:
        cur.execute(
            "UPDATE bids SET ai_assessment=%s, ai_assessed_at=NOW(), ai_price=NULL "
            "WHERE id=%s",
            ('**MANUAL REVIEW NEEDED**\n\n'
             'Mileage was not captured from the intake. Please add the odometer '
             'reading and re-run the assessment. No buy recommendation will be '
             'generated without verified mileage.',
             bid_id)
        )
        db.commit()
        db.close()
        print(f'[ASSESS] Bid {bid_id} REFUSED -- mileage is NULL '
              f'(human must confirm odometer before pricing)', flush=True)
        return {'success': False, 'error': 'mileage_missing',
                'message': 'Mileage required for assessment'}

    cur.execute("SELECT url FROM bid_photos WHERE bid_id = %s ORDER BY id LIMIT 8", (bid_id,))
    photos = cur.fetchall()

    # iPacket canon_trim extraction now fires at /api/ipacket/submit time
    # (see wire_canon_at_ipacket.py). The pre-step here is intentionally
    # left as a fallback for bids whose canon_trim is still NULL when
    # assessment runs — e.g. ones where the iPacket worker landed before
    # this code shipped. Cheap when canon_trim already populated.
    try:
        from ipacket_trim import extract_and_persist as _ipt_extract
        _ipt_cur = db.cursor()
        _ipt_cur.execute("""
            SELECT b.canon_trim, b.make, b.model,
                   ip.total_msrp, ip.screenshot, ip.raw_json
            FROM bids b
            LEFT JOIN ipacket_lookups ip ON ip.bid_id = b.id
            WHERE b.id = %s
        """, (bid_id,))
        _ipt_pre = _ipt_cur.fetchone()
        if _ipt_pre and not _ipt_pre.get('canon_trim') and _ipt_pre.get('screenshot'):
            _ipt_trim = _ipt_extract(
                bid_id,
                _ipt_pre.get('make'),
                _ipt_pre.get('model'),
                {
                    'total_msrp': _ipt_pre.get('total_msrp'),
                    'screenshot': _ipt_pre.get('screenshot'),
                    'raw_json':   _ipt_pre.get('raw_json') or {},
                },
                db,
                force_ocr=True,
            )
            if _ipt_trim:
                bid = dict(bid)
                bid['canon_trim'] = _ipt_trim
                print(f'[ASSESS] Bid {bid_id} canon_trim fallback-set: '
                      f'{_ipt_trim!r}', flush=True)
    except Exception as _ipt_err:
        print(f'[ASSESS] iPacket trim fallback err on bid {bid_id}: '
              f'{_ipt_err}', flush=True)

    # ── vAuto book values ─────────────────────────────────────────────────────
    cur.execute("SELECT * FROM vauto_lookups WHERE bid_id = %s", (bid_id,))
    vauto = cur.fetchone()

    # ── AccuTrade values ──────────────────────────────────────────────────────
    accutrade = None
    try:
        cur.execute("SELECT * FROM accutrade_lookups WHERE bid_id = %s", (bid_id,))
        accutrade = cur.fetchone()
    except Exception:
        pass

    # ── Tesla decode (if Tesla VIN) ───────────────────────────────────────────
    tesla_data = None
    TESLA_WMIS = ('5YJ', '7SA', '7G2', 'SFZ', 'XP7', 'LRW')
    if bid['vin'] and len(bid['vin']) >= 3 and bid['vin'][:3].upper() in TESLA_WMIS:
        try:
            tesla_data = decode_tesla_vin(bid['vin'])
            cur.execute("SELECT options_json, source FROM tesla_vin_cache WHERE vin=%s", (bid['vin'],))
            cached = cur.fetchone()
            if cached and cached.get('options_json'):
                tesla_data['fleet_specs'] = cached['options_json']
        except Exception:
            pass

    db.close()

    # ── Hybrid assessment: classify segment + compute deterministic baseline ──
    # This runs BEFORE the Gemini call so the baseline + breakdown can be
    # injected into the prompt. Gemini's job shifts from "pick a price cold"
    # to "adjust this baseline by a percentage, with reasoning, within ±cap%".
    try:
        from ai_assessment import (classify_bucket, compute_baseline,
                                    apply_adjustment)
    except Exception as _imp_err:
        print(f'ai_assessment import failed: {_imp_err}', flush=True)
        classify_bucket = None
        compute_baseline = None
        apply_adjustment = None

    try:
        from dealer_match import find_dealer_matches, format_for_prompt
    except Exception as _imp_err:
        print(f'dealer_match import failed: {_imp_err}', flush=True)
        find_dealer_matches = None
        format_for_prompt = None

    try:
        from lsl_buyer_match import find_lsl_buyers, format_for_prompt as _lsl_format
    except Exception as _imp_err:
        print(f'lsl_buyer_match import failed: {_imp_err}', flush=True)
        find_lsl_buyers = None
        _lsl_format = None

    try:
        from market_intel import compute_market_intel, format_for_prompt as _mi_format
    except Exception as _imp_err:
        print(f'market_intel import failed: {_imp_err}', flush=True)
        compute_market_intel = None
        _mi_format = None

    _ai_ver, _ai_cfg = (0, DEFAULT_AI_CONFIG)
    _bucket = None
    _baseline_result = None
    _ai_cap = 15.0
    _dealer_intel = None
    _buyer_intel = None
    _market_intel = None
    if classify_bucket and compute_baseline:
        try:
            _ai_ver, _ai_cfg = get_active_ai_config()
            _ai_cap = float(_ai_cfg.get('llm_adjustment_cap_pct', 15))
            _bid_for_bucket = {
                'make': bid.get('make'),
                'model': bid.get('model'),
                'year': bid.get('year'),
                'asking_price': bid.get('asking_price'),
            }
            _bucket = classify_bucket(_bid_for_bucket, _ai_cfg)
            _baseline_result = compute_baseline(
                _bucket,
                dict(vauto) if vauto else {},
                dict(accutrade) if accutrade else {},
            )
            print(f'[ASSESS] Bid {bid_id} classified as "{_bucket.get("name")}" — '
                  f'baseline ${(_baseline_result or {}).get("baseline_price") or 0:,}',
                  flush=True)
        except Exception as _cls_err:
            print(f'hybrid classify/baseline error: {_cls_err}', flush=True)

    # ── Dealer Network Intel — active + recent sales + top pitch dealers ──
    if find_dealer_matches:
        try:
            _dm_db = get_db()
            _dealer_intel = find_dealer_matches(
                _dm_db,
                bid.get('year'), bid.get('make'), bid.get('model'),
                trim=bid.get("trim"),
                trim_confidence=bid.get("trim_confidence") or "low",
                bid_id=bid_id,
                config=_ai_cfg,
            )
            _dm_db.close()
            _active_n = len(_dealer_intel.get('active', []))
            _sales_n = len(_dealer_intel.get('recent_sales', []))
            _pitch_n = len(_dealer_intel.get('top_pitch', []))
            print(f'[ASSESS] Bid {bid_id} dealer intel: '
                  f'{_active_n} active · {_sales_n} recent sales · {_pitch_n} pitch candidates',
                  flush=True)
        except Exception as _dm_err:
            print(f'dealer intel lookup error: {_dm_err}', flush=True)
            _dealer_intel = None

    # ── LSL Sales-Ledger Buyer Intel — who has actually bought like-vehicles ──
    # from EW over the last 12 months. Reads /opt/livesaleslog/crm.db read-only.
    # 5 years of wholesale ledger data joined with dealer_profile aggregates.
    if find_lsl_buyers:
        try:
            _buyer_intel = find_lsl_buyers(
                bid.get('year'), bid.get('make'), bid.get('model'),
                mileage=bid.get('mileage'),
                trim=bid.get("trim"),
                canon_trim=bid.get("canon_trim"),
                config=_ai_cfg,
            )
            _bn_n = (_buyer_intel.get('patterns') or {}).get('total_deals', 0)
            _bu_n = (_buyer_intel.get('patterns') or {}).get('unique_buyers', 0)
            _bd_n = len(_buyer_intel.get('deals') or [])
            print(f'[ASSESS] Bid {bid_id} same-YMM deals: '
                  f'{_bn_n} total · {_bu_n} unique customers · '
                  f'{_bd_n} rows shown',
                  flush=True)
        except Exception as _bi_err:
            print(f'lsl buyer intel error: {_bi_err}', flush=True)
            _buyer_intel = None

    # ── Market Intel — full stack work-back ──────────────────────────────
    # Combines vauto_lookups.manheim_transactions (wholesale auction floor),
    # vauto_lookups.rbook_competitive_set (retail asking from other dealers),
    # and lsl_buyer_match.patterns.avg_gross (our PVR target). All three are
    # populated live per bid by the enrichment fleet on pve-pc1.
    # Derivation is pure: no hard-coded buyer-margin percentage.
    if compute_market_intel:
        try:
            def _maybe_parse(x):
                """JSONB may come back as dict or JSON string from the driver."""
                if isinstance(x, str):
                    try:
                        import json as _mij
                        return _mij.loads(x)
                    except Exception:
                        return None
                return x
            _manheim_data = _maybe_parse(vauto.get('manheim_transactions')) if vauto else None
            _rbook_data   = _maybe_parse(vauto.get('rbook_competitive_set')) if vauto else None
            _market_intel = compute_market_intel(
                {'year': bid.get('year'), 'make': bid.get('make'),
                 'model': bid.get('model'), 'mileage': bid.get('mileage'),
                 'vin': bid.get('vin')},
                _manheim_data, _rbook_data, _buyer_intel,
            )
            if _market_intel:
                _ntx  = (_market_intel.get('manheim') or {}).get('n_transactions', 0)
                _nrb  = (_market_intel.get('rbook') or {}).get('n_visible', 0)
                _tgt  = _market_intel.get('target_buy')
                _med  = (_market_intel.get('manheim') or {}).get('mmr_median')
                _rmed = (_market_intel.get('rbook') or {}).get('retail_median')
                _spread = _market_intel.get('implied_buyer_gross')
                print(f'[ASSESS] Bid {bid_id} market intel: '
                      f'{_ntx} mmr_tx (med ${_med or 0:,}) · '
                      f'{_nrb} rbook (med ${_rmed or 0:,}) · '
                      f'implied_buyer_gross ${_spread or 0:,} · '
                      f'target_buy ${_tgt or 0:,}', flush=True)
                # Phase 2: enqueue + merge MSRPs onto closest_3 so Gemini
                # sees them in the prompt. Same flow as the bid view.
                try:
                    _enqueue_comp_msrps_for_bid(bid_id, _market_intel)
                    _closest = (_market_intel.get('rbook') or {}).get('closest_3') or []
                    _vins = [c.get('vin') for c in _closest if c.get('vin')]
                    _msrps = _load_comp_msrps(_vins)
                    _msrp_hits = 0
                    for c in _closest:
                        v = (c.get('vin') or '').upper()
                        if v in _msrps:
                            c['msrp_lookup'] = {
                                'msrp':       _msrps[v].get('msrp'),
                                'base_price': _msrps[v].get('base_price'),
                                'status':     _msrps[v].get('status'),
                            }
                            if _msrps[v].get('msrp'):
                                _msrp_hits += 1
                    print(f'[ASSESS] Bid {bid_id} comp_msrps merged: '
                          f'{_msrp_hits}/{len(_closest)} have MSRP', flush=True)
                except Exception as _cmsrp_err:
                    print(f'[ASSESS] comp_msrp merge err: {_cmsrp_err}', flush=True)
        except Exception as _mi_err:
            print(f'market intel error: {_mi_err}', flush=True)
            _market_intel = None

    # ── Partner-Network Velocity Score ───────────────────────────────────────
    # Days-to-sell distribution for this YMM band over the last 90d. Labels:
    # HOT / STEADY / SLOW / STALE / NO_SIGNAL. Gemini uses the label to lean
    # adjustment up (fast → confidence) or down (stale → margin cushion).
    _velocity = None
    try:
        from velocity import compute_velocity, format_for_prompt as _vel_format
        _v_db = get_db()
        _velocity = compute_velocity(
            _v_db,
            year=bid.get('year'),
            make=bid.get('make'),
            model=bid.get('model'),
            mileage=bid.get('mileage'),
            config=_ai_cfg,
        )
        _v_db.close()
        if _velocity:
            print(f'[ASSESS] Bid {bid_id} velocity: {_velocity.get("label")} '
                  f'(sold {_velocity.get("sold_count")} median '
                  f'{_velocity.get("median_days_to_sell")}d · active '
                  f'{_velocity.get("active_count")})', flush=True)
    except Exception as _vel_err:
        print(f'velocity lookup error: {_vel_err}', flush=True)
        _velocity = None

    # ── Build vehicle context ─────────────────────────────────────────────────
    vparts = [str(bid['year'] or ''), bid['make'] or '', bid['model'] or '', bid['trim'] or '']
    vehicle_str = ' '.join(p for p in vparts if p).strip() or 'Unknown vehicle'
    mileage_str = '{:,} miles'.format(bid['mileage']) if bid['mileage'] else 'unknown mileage'
    asking_str = '${:,.0f}'.format(bid['asking_price']) if bid.get('asking_price') else 'not stated'

    ctx = f"VEHICLE: {vehicle_str}\nVIN: {bid['vin'] or 'N/A'}\nMileage: {mileage_str}\n"
    ctx += f"Color: {bid['color'] or 'N/A'}\nAsking price: {asking_str}\n"
    ctx += f"Notes / condition: {bid['notes'] or 'None'}\n"

    # VIN decoder details — NHTSA vPIC returns ~130 fields. When the bid's
    # trim is ambiguous (Ford/GM/Chrysler etc. don't encode trim in VDS),
    # this block tells Gemini explicitly what the alternatives are so it can
    # pick the right one from photos + iPacket sticker + Carfax.
    _nhtsa = {}  # hoisted for v2 prompt builder
    if bid.get('vin') and len(bid['vin']) == 17:
        try:
            _nhtsa = decode_vin(bid['vin']) or {}
            if _nhtsa:
                ctx += "\nVIN DECODER DETAILS (NHTSA vPIC):\n"
                if _nhtsa.get('trim_ambiguous'):
                    _alts = _nhtsa.get('trim_alternatives') or []
                    ctx += (f"  Trim candidates: {' / '.join(_alts)}\n"
                            f"  (NHTSA cannot distinguish trim from VIN for this "
                            f"manufacturer — determine actual trim from photos, "
                            f"iPacket sticker, and Carfax/AutoCheck vehicle descriptions.)\n")
                elif _nhtsa.get('trim'):
                    ctx += f"  Trim: {_nhtsa['trim']}\n"
                for _k, _label in [
                    ('series', 'Series'),
                    ('trim2', 'Trim detail'),
                    ('body_class', 'Body class'),
                    ('doors', 'Doors'),
                    ('drive_type', 'Drive type'),
                    ('engine_model', 'Engine model'),
                    ('engine_cylinders', 'Cylinders'),
                    ('displacement_l', 'Displacement (L)'),
                    ('engine_hp', 'Horsepower'),
                    ('fuel_type', 'Fuel type'),
                    ('plant_city', 'Plant city'),
                    ('plant_country', 'Plant country'),
                ]:
                    _v = _nhtsa.get(_k)
                    if _v not in (None, '', 'null'):
                        ctx += f"  {_label}: {_v}\n"
        except Exception as _vin_err:
            print(f'[ASSESS] VIN details error for bid {bid_id}: {_vin_err}')

    # vAuto book values section
    if vauto:
        ctx += "\nvAUTO BOOK VALUES:\n"
        book_fields = [
            ('rbook', 'rBook'),
            ('black_book', 'Black Book (wholesale)'),
            ('mmr', 'Manheim MMR'),
            ('kbb', 'KBB'),
            ('kbb_com', 'KBB.com'),
            ('jd_power', 'J.D. Power'),
        ]
        for field, label in book_fields:
            val = vauto.get(field)
            if val is not None:
                try:
                    ctx += f"  {label}: ${int(float(val)):,}\n"
                except (ValueError, TypeError):
                    ctx += f"  {label}: {val}\n"
        title = vauto.get('title_status')
        if title:
            ctx += f"  Title Status: {title}\n"

        # vAuto "Like Vehicles" auction comps — actual sold transactions.
        # Strongest single pricing signal: real hammer prices on similar cars
        # at similar miles in recent weeks. Render as table + compute the
        # mileage-banded median so Gemini has a direct market-median anchor.
        _comps = vauto.get('vauto_comps') or []
        if _comps and isinstance(_comps, list):
            # Find comps within ±20k miles of the bid (tighter market signal)
            _bid_miles = None
            try:
                _bid_miles = int(bid.get('mileage') or 0) or None
            except (ValueError, TypeError):
                pass
            _banded = []
            if _bid_miles:
                for c in _comps:
                    try:
                        _m = int(c.get('odometer') or 0)
                        if _m and abs(_m - _bid_miles) <= 20000:
                            _banded.append(c)
                    except (ValueError, TypeError):
                        continue

            ctx += f"\nvAUTO LIKE-VEHICLE AUCTION COMPS ({len(_comps)} recent sales):\n"
            for c in _comps[:12]:
                try:
                    _p = int(c.get('sale_price') or 0)
                    _m = int(c.get('odometer') or 0)
                    _d = c.get('date_sold') or '?'
                    _cn = c.get('condition')
                    _et = c.get('engine_trans') or ''
                    _col = c.get('color') or ''
                    _auc = c.get('auction') or ''
                    _line = (f"  {_d} — ${_p:,} — {_m:,} mi · cond {_cn} · "
                             f"{_et} · {_col} · {_auc}")
                    ctx += _line + "\n"
                except (ValueError, TypeError):
                    continue

            # Summary stats + mileage-banded median
            try:
                _prices = sorted(int(c.get('sale_price') or 0)
                                 for c in _comps if c.get('sale_price'))
                if _prices:
                    _med = _prices[len(_prices)//2]
                    _mean = sum(_prices) // len(_prices)
                    ctx += (f"  → All-comps median ${_med:,} · "
                            f"mean ${_mean:,} · range ${_prices[0]:,}-${_prices[-1]:,}\n")
                if _banded and _bid_miles:
                    _bp = sorted(int(c.get('sale_price') or 0)
                                 for c in _banded if c.get('sale_price'))
                    _bmed = _bp[len(_bp)//2]
                    ctx += (f"  → Within ±20k miles of bid ({_bid_miles:,} mi): "
                            f"{len(_banded)} comps, median ${_bmed:,}\n")
            except Exception:
                pass

    # Market check
    mc = bid.get('market_check') or {}
    if mc:
        ctx += "\nCURRENT MARKET LISTINGS:\n"
        for key, label in [('autotrader','Autotrader'),('cars_com','Cars.com'),('cargurus','CarGurus')]:
            if key in mc:
                found = mc[key].get('found')
                ctx += f"  {label}: {'found listed' if found else 'not listed'}\n"

    # ── DIA RETAIL/AUCTION COMPS — DISABLED 2026-05-06 ─────────────────
    # Per direction, retail/auction comp data should ONLY come from:
    #   1. EW partner-scan dealer_inventory (already injected via the
    #      Dealer Network Intel block above — find_dealer_matches in
    #      dealer_match.py reads /opt/expwholesale's local dealer_inventory)
    #   2. vAuto rBook competitive set ("show my vehicle") — pending the
    #      enrichment_rbook scraper (PR coming once spike lands selectors)
    # vAuto Like-Vehicle MMR auction comps + the new Market Work-Back block
    # (market_intel.py) cover wholesale auction signal.
    # DIA cross-server queries (formerly here) intentionally removed; do
    # NOT re-add without explicit approval — see project_ew_market_workback.md.

    # iPacket sticker data
    ipacket = None
    try:
        db2 = get_db()
        cur2 = db2.cursor()
        cur2.execute("SELECT * FROM ipacket_lookups WHERE bid_id = %s", (bid_id,))
        ipacket = cur2.fetchone()
        db2.close()
    except Exception:
        pass

    # iPacket canvas-rendered sticker fallback — when DOM parsing came up empty
    # but a screenshot exists, OCR the image. Regex-extract what we can
    # (structured), and ALSO stash the raw OCR text for Gemini to read directly
    # (Monroney stickers are column-laid-out; OCR interleaves labels/values
    # in ways regex can't reliably stitch back together, but Gemini handles it).
    if ipacket and ipacket.get('screenshot'):
        _raw = ipacket.get('raw_json') or {}
        if not isinstance(_raw, dict):
            _raw = {}
        _opts_existing = _raw.get('options') or []
        _dom_empty = (not ipacket.get('total_msrp')) and not _opts_existing
        if _dom_empty:
            try:
                _path = _ipacket_screenshot_path(ipacket['screenshot'])
                if _path and os.path.exists(_path):
                    with open(_path, 'rb') as _f:
                        _img_bytes = _f.read()
                    _ocr_text = _google_vision_ocr(_img_bytes)
                    if _ocr_text:
                        _parsed = _parse_sticker_text(_ocr_text)
                        _opts = _parsed.get('options', [])
                        print(f'[ASSESS] Bid {bid_id} iPacket canvas-OCR fallback: '
                              f'regex MSRP=${_parsed.get("total_msrp") or 0:,} · '
                              f'{len(_opts)} options · {len(_ocr_text)} raw chars → '
                              f'dumping OCR text to prompt')
                        for _k in ('total_msrp', 'base_price',
                                   'exterior_color', 'interior_color'):
                                if not ipacket.get(_k) and _parsed.get(_k):
                                    ipacket[_k] = _parsed[_k]
                        if _opts:
                            _raw['options'] = _opts
                        # Always stash the raw OCR (capped) so Gemini gets full
                        # context even when regex stitching fails on Monroney
                        # column layouts.
                        _raw['_ocr_fallback'] = True
                        _raw['_ocr_text'] = _ocr_text[:4000]
                        ipacket['raw_json'] = _raw
            except Exception as _ocr_err:
                print(f'iPacket canvas-OCR error for bid {bid_id}: {_ocr_err}')

    # Level-2 fallback: when canvas-OCR (screenshot) didn't yield MSRP,
    # pull the full iPacket PDF directly and re-parse. The screenshot is
    # often partial (~1.2k chars) while the PDF holds the full sticker
    # (~6-7k chars including the TOTAL MSRP line). Same regex; no new
    # patterns. Triggers only when subject MSRP is still missing.
    if ipacket and bid.get('vin') and not ipacket.get('total_msrp'):
        try:
            _pdf_res = _ipacket_lookup_msrp_for_vin(bid['vin'])
            if (_pdf_res and _pdf_res.get('ok')
                and _pdf_res.get('msrp')):
                ipacket['total_msrp'] = _pdf_res['msrp']
                if _pdf_res.get('base_price') and not ipacket.get('base_price'):
                    ipacket['base_price'] = _pdf_res['base_price']
                # Replace the broken screenshot with a render of the actual
                # PDF page. The worker's screenshot sometimes captures the
                # iPacket UI before the sticker loads — leaving a blank
                # white viewer. Re-pulling here gives us the real sticker
                # image to display on the bid card.
                # GATE: only swap if the existing screenshot is actually
                # blank/missing. A valid canvas screenshot of the iPacket
                # UI looks familiar to users; replacing it with a raw PDF
                # page render confused them ("978 showing something
                # totally different than your normal msrp sticker").
                _existing_blank = True
                try:
                    _existing_path = _ipacket_screenshot_path(ipacket.get('screenshot'))
                    if _existing_path and os.path.exists(_existing_path):
                        # Cheap signal first: real iPacket screenshots are
                        # 100-400KB; blank-white captures are <30KB.
                        _sz = os.path.getsize(_existing_path)
                        if _sz >= 30 * 1024:
                            # Big enough to be real — sample pixels to be sure.
                            try:
                                from PIL import Image as _PILImg
                                with _PILImg.open(_existing_path) as _exi:
                                    _exi = _exi.convert('RGB')
                                    _w, _h = _exi.size
                                    _near_white = 0
                                    _samples = 0
                                    for _gy in range(10):
                                        for _gx in range(10):
                                            _px = _exi.getpixel((
                                                int(_w * (_gx + 0.5) / 10),
                                                int(_h * (_gy + 0.5) / 10),
                                            ))
                                            _samples += 1
                                            if (_px[0] >= 240 and _px[1] >= 240
                                                    and _px[2] >= 240):
                                                _near_white += 1
                                    if _samples and (_near_white / _samples) <= 0.95:
                                        _existing_blank = False
                            except Exception:
                                # If PIL choke, trust the size signal alone.
                                _existing_blank = False
                except Exception:
                    # Any resolution error — fall through and allow the swap
                    # (preserves prior behavior when screenshot is missing).
                    _existing_blank = True

                _new_screenshot_path = None
                if _existing_blank:
                    try:
                        import requests as _rr2
                        import pdfplumber as _pp2
                        import io as _io2
                        # Re-fetch the PDF (already done inside _ipacket_lookup_msrp_for_vin
                        # but not stored); use the viewer URL it returned.
                        _viewer = _pdf_res.get('viewer_url')
                        if _viewer:
                            _vr = _rr2.get(_viewer, timeout=30,
                                           headers={'User-Agent': 'Mozilla/5.0'})
                            if _vr.status_code == 200:
                                with _pp2.open(_io2.BytesIO(_vr.content)) as _pdf:
                                    _pil = _pdf.pages[0].to_image(resolution=200).original
                                    _ts = int(time.time())
                                    _fname = f'ipacket_{bid["vin"]}_{_ts}_pdf.png'
                                    _full = os.path.join(IPACKET_REPORTS_DIR, _fname)
                                    _pil.save(_full, format='PNG')
                                    _new_screenshot_path = f'/ipacket_reports/{_fname}'
                    except Exception as _img_err:
                        print(f'[ASSESS] PDF-fallback image render err: {_img_err}',
                              flush=True)
                # Persist MSRP + (optional) new screenshot to DB
                try:
                    _pdb = get_db()
                    _pcur = _pdb.cursor()
                    if _new_screenshot_path:
                        _pcur.execute("""
                            UPDATE ipacket_lookups
                               SET total_msrp = COALESCE(total_msrp, %s),
                                   base_price = COALESCE(base_price, %s),
                                   screenshot = %s,
                                   looked_up_at = NOW()
                             WHERE bid_id = %s
                        """, (_pdf_res.get('msrp'),
                              _pdf_res.get('base_price'),
                              _new_screenshot_path, bid_id))
                        ipacket['screenshot'] = _new_screenshot_path
                    else:
                        _pcur.execute("""
                            UPDATE ipacket_lookups
                               SET total_msrp = COALESCE(total_msrp, %s),
                                   base_price = COALESCE(base_price, %s),
                                   looked_up_at = NOW()
                             WHERE bid_id = %s
                        """, (_pdf_res.get('msrp'),
                              _pdf_res.get('base_price'), bid_id))
                    _pdb.commit()
                    _pdb.close()
                except Exception as _persist_err:
                    print(f'[ASSESS] PDF-fallback persist err: {_persist_err}',
                          flush=True)
                if _new_screenshot_path:
                    _swap_note = ' · sticker.png replaced (canvas blank)'
                else:
                    _swap_note = ' · kept canvas (MSRP-only update)'
                print(f'[ASSESS] Bid {bid_id} iPacket PDF-fallback: '
                      f'MSRP=${_pdf_res["msrp"]:,} '
                      f'(base ${_pdf_res.get("base_price") or 0:,})'
                      + _swap_note,
                      flush=True)
        except Exception as _pdf_err:
            print(f'[ASSESS] iPacket PDF-fallback err: {_pdf_err}',
                  flush=True)

    # AccuTrade values section
    if accutrade:
        ctx += "\nACCUTRADE VALUES:\n"
        at_fields = [
            ('guaranteed_offer', 'Instant Offer'),
            ('trade_in', 'Target Auction'),
            ('trade_market', 'Target Retail'),
            ('retail', 'Manheim'),
            ('market_avg', 'Wholesale / Average'),
        ]
        for field, label in at_fields:
            val = accutrade.get(field)
            if val is not None:
                try:
                    ctx += f"  {label}: ${int(float(val)):,}\n"
                except (ValueError, TypeError):
                    ctx += f"  {label}: {val}\n"

    # iPacket sticker section
    if ipacket:
        ctx += "\niPACKET OEM STICKER:\n"
        if ipacket.get('total_msrp'):
            ctx += f"  Original MSRP: ${int(ipacket['total_msrp']):,}\n"
        if ipacket.get('base_price'):
            ctx += f"  Base Price: ${int(ipacket['base_price']):,}\n"
        if ipacket.get('exterior_color'):
            ctx += f"  Exterior: {ipacket['exterior_color']}\n"
        if ipacket.get('interior_color'):
            ctx += f"  Interior: {ipacket['interior_color']}\n"
        # Factory option line items — structured text beats relying on vision OCR
        # of the sticker image. Worker populates raw_json.options = [{name,price},...]
        _raw = ipacket.get('raw_json') or {}
        _opts = _raw.get('options') if isinstance(_raw, dict) else None
        if _opts:
            _opt_total = 0
            ctx += "  Factory Options:\n"
            for _o in _opts[:40]:  # cap to avoid context blow-out on outlier stickers
                try:
                    _n = str(_o.get('name', '')).strip()
                    _p = int(_o.get('price') or 0)
                    if _n and _p > 0:
                        ctx += f"    - {_n}: ${_p:,}\n"
                        _opt_total += _p
                except (ValueError, TypeError):
                    continue
            if _opt_total > 0:
                ctx += f"  Options Total (extracted): ${_opt_total:,}\n"
        # Canvas-rendered Monroney stickers: regex can't stitch column-laid-out
        # labels/values back together. Dump the raw OCR text so Gemini can parse
        # it holistically (it has the image too, this is belt-and-suspenders).
        if isinstance(_raw, dict) and _raw.get('_ocr_fallback') and _raw.get('_ocr_text'):
            ctx += ("  Sticker (raw OCR — text order may interleave columns;\n"
                    "    cross-reference with the iPacket screenshot image):\n")
            for _line in str(_raw['_ocr_text']).split('\n'):
                _ln = _line.strip()
                if _ln:
                    ctx += f"    {_ln}\n"

    # Tesla factory data section
    if tesla_data:
        ctx += "\nTESLA FACTORY DATA:\n"
        if tesla_data.get('model'):
            ctx += f"  Model: {tesla_data['model']}\n"
        if tesla_data.get('trim'):
            ctx += f"  Trim: {tesla_data['trim']}\n"
        if tesla_data.get('battery'):
            ctx += f"  Battery: {tesla_data['battery']}\n"
        if tesla_data.get('motor'):
            ctx += f"  Motor: {tesla_data['motor']}\n"
        if tesla_data.get('drive'):
            ctx += f"  Drive: {tesla_data['drive']}\n"
        if tesla_data.get('msrp'):
            ctx += f"  Original MSRP: ${tesla_data['msrp']:,.0f}\n"
        if tesla_data.get('plant'):
            ctx += f"  Plant: {tesla_data['plant']}\n"
        fleet = tesla_data.get('fleet_specs')
        if fleet:
            if isinstance(fleet, str):
                try:
                    fleet = json.loads(fleet)
                except Exception:
                    fleet = None
            if fleet:
                # FSD status
                ap_sw = (fleet.get('autopilotSoftwareCode') or '').lower()
                equip = fleet.get('equipmentPrice') or []
                has_fsd = any(('full self' in (e.get('name') or '').lower() or 'fsd' in (e.get('name') or '').lower()) for e in equip)
                fsd_price = next((e.get('price', 0) for e in equip if 'full self' in (e.get('name') or '').lower() or 'fsd' in (e.get('name') or '').lower()), None)
                if 'premium' in ap_sw or 'full self' in ap_sw:
                    if has_fsd and fsd_price == 0:
                        ctx += "  FSD Status: PURCHASED (transfers with vehicle)\n"
                    else:
                        ctx += "  FSD Status: ACTIVE — likely subscription ($99/mo, does NOT transfer)\n"
                elif has_fsd and fsd_price and fsd_price > 0:
                    ctx += f"  FSD Status: NOT ACTIVE (was ${fsd_price:,} at factory, may have been removed)\n"
                else:
                    ctx += "  FSD Status: Not included\n"
                # Battery health
                soh = fleet.get('batterySoH')
                if soh:
                    ctx += f"  Battery Health: {float(soh):.1f}% SoH\n"
                cap = fleet.get('batteryCapacityKwh')
                if cap:
                    ctx += f"  Battery Capacity: {float(cap):.1f} kWh\n"
                # Equipment list
                if equip:
                    ctx += "  Factory Equipment:\n"
                    for e in equip:
                        name = e.get('name') or e.get('code') or ''
                        price = e.get('price', 0)
                        if name:
                            price_str = f"+${price:,}" if price > 0 else "Included"
                            ctx += f"    {name}: {price_str}\n"

    # ── Load vehicle photos ─────────────────────────────────────────────────
    content = []
    photo_count = 0
    for photo in photos:
        url = photo['url']
        try:
            photo_path = os.path.join(UPLOAD_DIR, os.path.basename(url))
            if os.path.exists(photo_path):
                with open(photo_path, 'rb') as f:
                    img_bytes = f.read()
                media_type = 'image/jpeg'
            else:
                resp = requests.get(url, timeout=10,
                                    auth=(TWILIO_SID, TWILIO_TOKEN) if 'twilio' in url else None)
                if resp.status_code != 200:
                    continue
                img_bytes = resp.content
                media_type = resp.headers.get('Content-Type', 'image/jpeg').split(';')[0]
            try:
                img_bytes, media_type = resize_for_claude(img_bytes)
            except Exception:
                pass
            content.append({'type': 'image', 'source': {
                'type': 'base64', 'media_type': media_type,
                'data': base64.standard_b64encode(img_bytes).decode()
            }})
            photo_count += 1
        except Exception as e:
            print(f'assess photo error: {e}')

    # ── Load Carfax/AutoCheck: OCR to TEXT (avoids Gemini hallucinating details) ─
    # Hoisted vars: also captured below for v2 prompt builder consumption
    _carfax_ocr_text = ''
    _autocheck_ocr_text = ''
    report_count = 0
    max_history_odometer = 0   # highest mileage reading we've seen in Carfax/AutoCheck
    if vauto:
        for report_field, label in [('carfax_screenshot', 'CARFAX REPORT'), ('autocheck_screenshot', 'AUTOCHECK REPORT')]:
            report_path = vauto.get(report_field)
            if not report_path:
                continue
            try:
                if report_path.startswith('/vauto_reports/'):
                    full_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), report_path.lstrip('/'))
                else:
                    full_path = report_path
                if os.path.exists(full_path) and os.path.getsize(full_path) > 1024:
                    with open(full_path, 'rb') as f:
                        img_bytes = f.read()
                    # Use Google Vision OCR for reliable text extraction on dense reports
                    ocr_text = _google_vision_ocr(img_bytes)
                    if ocr_text:
                        # Clean up whitespace
                        clean_text = re.sub(r'[ \t]+', ' ', ocr_text)
                        clean_text = re.sub(r'\n{3,}', '\n\n', clean_text).strip()
                        ctx += f"\n--- {label} (OCR text) ---\n{clean_text}\n"
                        # Also capture into hoisted vars for v2 prompt builder
                        if 'CARFAX' in label.upper():
                            _carfax_ocr_text = clean_text
                        elif 'AUTOCHECK' in label.upper():
                            _autocheck_ocr_text = clean_text
                        report_count += 1
                        # Track the highest odometer reading in the OCR text so
                        # we can flag rollback / listing-understatement after
                        # both reports are processed.
                        for mm in re.finditer(r'(\d{1,3}(?:,\d{3})+|\d{4,7})\s*(?:mi|miles)\b', clean_text, re.I):
                            try:
                                n = int(mm.group(1).replace(',', ''))
                                if 100 <= n <= 999_999 and n > max_history_odometer:
                                    max_history_odometer = n
                            except ValueError:
                                pass
                    else:
                        # Fallback to image if OCR fails
                        content.append({'type': 'text', 'text': f'\n--- {label} (screenshot fallback) ---'})
                        content.append({'type': 'image', 'source': {
                            'type': 'base64', 'media_type': 'image/png',
                            'data': base64.standard_b64encode(img_bytes).decode()
                        }})
                        report_count += 1
                        ctx += f"\n{label}: screenshot attached below (OCR failed)\n"
            except Exception as e:
                print(f'assess report load error ({label}): {e}')

    # ── Odometer sanity check (after Carfax + AutoCheck OCR) ───────────────
    # Compare the highest odometer reading seen in history-report OCR vs. the
    # listing mileage. If history > listing by >1,000 mi, flag it. Common
    # causes ordered by severity: rollback (fraud), stale listing (dealer
    # hasn't updated since servicing), or reporting error. Gemini sees the
    # numbers in ctx already, but an explicit callout affects its adjustment.
    _listing_mi = 0
    try:
        _listing_mi = int(bid.get('mileage') or 0)
    except (TypeError, ValueError):
        _listing_mi = 0
    _odo_flag = None
    if max_history_odometer and _listing_mi and max_history_odometer > _listing_mi + 1000:
        _odo_gap = max_history_odometer - _listing_mi
        _odo_flag = {
            'listing_mileage': _listing_mi,
            'max_history_mileage': max_history_odometer,
            'gap_miles': _odo_gap,
        }
        ctx += (
            f"\n\n⚠️ ODOMETER DISCREPANCY FLAG\n"
            f"Carfax/AutoCheck history contains a recorded odometer reading of "
            f"{max_history_odometer:,} mi, but the current listing reports "
            f"only {_listing_mi:,} mi — a gap of {_odo_gap:,} miles where "
            f"history EXCEEDS the listing. Possible causes, ordered by severity:\n"
            f"  1. Odometer rollback (fraud) — highest risk, material to pricing.\n"
            f"  2. Stale listing — dealer hasn't updated the odometer since a "
            f"recent servicing or drive event.\n"
            f"  3. Reporting error on the history-report side.\n"
            f"Mark the vehicle down UNLESS the history entries all pre-date "
            f"the listing's posting date (in which case it's #2 — stale).\n"
        )

    # ── AccuTrade screenshot — INTENTIONALLY NOT SENT AS IMAGE ──────────────
    # The AccuTrade UI displays a condition-selector panel on every appraisal
    # with labels like "BAD VHR / FRAME DAMAGE / Unknown" whether or not those
    # conditions apply. Gemini mis-reads the label "FRAME DAMAGE" as a fact,
    # causing hallucinated damage claims and double-digit price haircuts on
    # clean vehicles (observed bid 165 / 2023 CHEVROLET TAHOE, 2026-04-22).
    # The actual AccuTrade book values are already injected earlier as clean
    # numeric text (see "ACCUTRADE VALUES:" block in ctx). The screenshot
    # adds no quantitative signal Gemini can rely on. Keeping the variable
    # defined for downstream template parity, but always 0.
    accutrade_report = 0

    # ── Load iPacket sticker screenshot ──────────────────────────────────────
    ipacket_report = 0
    if ipacket and ipacket.get('screenshot'):
        report_path = ipacket['screenshot']
        try:
            if report_path.startswith('/ipacket_reports/'):
                full_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), report_path.lstrip('/'))
            else:
                full_path = report_path
            if os.path.exists(full_path) and os.path.getsize(full_path) > 1024:
                with open(full_path, 'rb') as f:
                    img_bytes = f.read()
                try:
                    img_bytes, media_type = resize_for_claude(img_bytes)
                except Exception:
                    media_type = 'image/png'
                content.append({'type': 'text', 'text': '\n--- iPACKET OEM WINDOW STICKER (screenshot) ---'})
                content.append({'type': 'image', 'source': {
                    'type': 'base64', 'media_type': media_type,
                    'data': base64.standard_b64encode(img_bytes).decode()
                }})
                ipacket_report = 1
                ctx += "\niPACKET OEM STICKER: screenshot attached below\n"
        except Exception as e:
            print(f'assess iPacket screenshot error: {e}')

    # ── Prompt ────────────────────────────────────────────────────────────────
    img_summary = []
    if photo_count:
        img_summary.append(f"{photo_count} vehicle photos")
    if report_count:
        img_summary.append(f"Carfax/AutoCheck report screenshots")
    if accutrade_report:
        img_summary.append(f"AccuTrade appraisal screenshot")
    if ipacket_report:
        img_summary.append(f"iPacket OEM window sticker")
    img_line = "I've attached " + " and ".join(img_summary) + "." if img_summary else "No photos available."

    # Inject dealer network intel (if any matches) — live signal from partner
    # dealer inventory + sold history. Goes above segment/baseline so Gemini
    # sees the real-market context first.
    if _dealer_intel and format_for_prompt:
        try:
            _di_block = format_for_prompt(_dealer_intel)
            if _di_block:
                ctx += '\n\n' + _di_block
        except Exception as _di_fmt_err:
            print(f'dealer intel format error: {_di_fmt_err}', flush=True)

    # Inject LSL sales-ledger buyer intel — real EW transaction history. This
    # is the strongest demand signal for the offset (we know who buys this
    # YMM from us, how often, at what gross). Sits below dealer_intel
    # (retail-side proxy) and above velocity (turnover label).
    if _buyer_intel and _lsl_format:
        try:
            _bi_block = _lsl_format(_buyer_intel)
            if _bi_block:
                ctx += '\n\n' + _bi_block
        except Exception as _bi_fmt_err:
            print(f'lsl buyer intel format error: {_bi_fmt_err}', flush=True)

    # Inject market work-back — Manheim auction floor + LSL gross history.
    # This is the formula-driven target buy price (mmr_median - lsl_avg_gross).
    # Acts as a SECONDARY anchor alongside the bucket-weighted baseline.
    if _market_intel and _mi_format:
        try:
            _mi_block = _mi_format(_market_intel)
            if _mi_block:
                ctx += '\n\n' + _mi_block
        except Exception as _mi_fmt_err:
            print(f'market intel format error: {_mi_fmt_err}', flush=True)

    # Inject partner-network velocity score (HOT/STEADY/SLOW/STALE/NO_SIGNAL)
    if _velocity:
        try:
            _vel_block = _vel_format(_velocity,
                                     year=bid.get('year'),
                                     make=bid.get('make'),
                                     model=bid.get('model'),
                                     mileage=bid.get('mileage'))
            if _vel_block:
                ctx += '\n\n' + _vel_block
        except Exception as _vel_fmt_err:
            print(f'velocity format error: {_vel_fmt_err}', flush=True)

    # ── v2 single-shot synthesis prompt (replaces bucket-baseline + ±cap offset) ──
    # Pivoted 2026-05-06: Gemini receives the full market stack + qualitative
    # data and returns an absolute target_buy in dollars. No bucket
    # classification, no percentage offsets, no hard-coded margins.
    _hybrid_mode = False  # legacy flag — kept for downstream readers, always False now
    try:
        from ai_assessment_v2 import build_prompt as _v2_build_prompt
    except Exception as _v2_imp_err:
        print(f'[ASSESS] v2 import failed: {_v2_imp_err}', flush=True)
        _v2_build_prompt = None

    # Phase 4b (v2): per-YMM retrieval against ai_accuracy. Replaces
    # XGBoost — with only 146 samples a tabular regressor was too noisy.
    # Tiered SQL: tries (Y+M+M+miles) → (M+M) → (M alone), returns first
    # tier with at least one match. Gemini sees sample rows + summary stats.
    _purchase_history = None
    try:
        _purchase_history = _retrieve_purchase_history(
            bid.get('year'), bid.get('make'),
            bid.get('model'), bid.get('mileage'),
            exclude_bid_id=bid_id)
        if _purchase_history:
            print(f'[ASSESS] Bid {bid_id} purchase history: '
                  f'{_purchase_history["n"]} matches at "{_purchase_history["tier"]}" '
                  f'mean=${_purchase_history.get("mean") or 0:,} '
                  f'median=${_purchase_history.get("median") or 0:,}',
                  flush=True)
    except Exception as _ph_e:
        print(f'[ASSESS] purchase history err: {_ph_e}', flush=True)

    # ML model second opinion — feeds Gemini as a calibration anchor.
    # Same predict_for_bid call as the bid card uses.
    # Note: in _run_assessment scope the locals are 'vauto' and 'ipacket'
    # (not 'vauto_data' and 'ipacket_data' as in bid_detail).
    _ml_pred_assess = None
    try:
        from ml_predict import predict_for_bid as _ml_predict_fn
        _mi_for_ml = _market_intel or {}
        _mh = _mi_for_ml.get('manheim') or {}
        _rb = _mi_for_ml.get('rbook') or {}
        # 2026-05-08: added mmr_median fallback. vAuto's published MMR book
        # value (vauto.mmr) is NULL for many exotics/rare cars (Audi R8,
        # Rolls-Royce, etc.) AND adjusted/base_mmr are derived from that
        # missing field. But the actual Manheim transaction scrape
        # (manheim_transactions) often has real hammer prices. mmr_median
        # is the median of those — true wholesale signal. Use it before
        # giving up on ML.
        # 2026-05-08: AccuTrade trade_in fallback (same logic as bid_detail).
        _est_w = (_mh.get('adjusted_mmr') or _mh.get('base_mmr')
                  or _mh.get('mmr_median')
                  or (accutrade or {}).get('trade_in')
                  or (vauto or {}).get('mmr'))
        _mkt_a = (_rb.get('avg_price') or _rb.get('median')
                  or (vauto or {}).get('rbook'))
        _ipkt_msrp = (ipacket or {}).get('total_msrp')
        if _est_w and bid.get('make'):
            _ml_pred_assess = _ml_predict_fn({
                'make_name':           bid.get('make') or '',
                'model_name':          bid.get('model'),
                'year':                bid.get('year'),
                'odometer':            bid.get('mileage'),
                'est_wholesale_price': _est_w,
                'market_asking_price': _mkt_a,
                'original_msrp':       _ipkt_msrp,
                'base_appraised_value': _mkt_a,
                'sale_type':           'Wholesale',
                'vehicle_sale_type':   'Used',
            })
            if _ml_pred_assess and _ml_pred_assess.get('prediction'):
                print(f'[ASSESS] Bid {bid_id} ML model ({_ml_pred_assess["source"]}): '
                      f'${_ml_pred_assess["prediction"]:,} '
                      f'(MAPE {_ml_pred_assess.get("mape_pct", "?")}%)', flush=True)
    except Exception as _ml_e:
        print(f'[ASSESS] ml_predict err: {_ml_e}', flush=True)

    # Thalist live wholesale-ask context for Gemini's prompt. Same-YMM
    # active posts; framed as ceiling, not transaction. Doesn't anchor
    # if framing is preserved.
    _thalist_asks = None
    try:
        _thalist_asks = _get_thalist_asks_for_bid(bid)
        if _thalist_asks:
            print(f'[ASSESS] Bid {bid_id} thalist asks: n={_thalist_asks["n"]} '
                  f'P25=${_thalist_asks.get("p25") or 0:,} '
                  f'P50=${_thalist_asks.get("p50") or 0:,} '
                  f'P75=${_thalist_asks.get("p75") or 0:,}', flush=True)
    except Exception as _ta_e:
        print(f'[ASSESS] thalist_asks err: {_ta_e}', flush=True)

    if _v2_build_prompt:
        prompt = _v2_build_prompt(
            dict(bid),
            vauto=dict(vauto) if vauto else None,
            accutrade=dict(accutrade) if accutrade else None,
            ipacket=dict(ipacket) if ipacket else None,
            photos=list(photos) if photos else None,
            carfax_text=_carfax_ocr_text,
            autocheck_text=_autocheck_ocr_text,
            dealer_intel=_dealer_intel,
            buyer_intel=_buyer_intel,
            market_intel=_market_intel,
            velocity=_velocity,
            nhtsa=_nhtsa,
            tesla=tesla_data,
            purchase_history=_purchase_history,
            ml_prediction=_ml_pred_assess,
            thalist_asks=_thalist_asks,
        )
    else:
        # Module unavailable — emit a minimal prompt so we still return something
        prompt = (f"{ctx}\n\n{img_line}\n\nReturn JSON: "
                  '{"target_buy": <int>, "confidence_low": <int>, '
                  '"confidence_high": <int>, "reasoning": "<text>", "flags": []}')

    content.append({'type': 'text', 'text': prompt})

    try:
        # Convert Claude-formatted `content` array → Gemini parts list
        from google.genai import types as _gtypes
        gemini_parts = []
        for part in content:
            if part.get('type') == 'image':
                img_data = base64.standard_b64decode(part['source']['data'])
                gemini_parts.append(_gtypes.Part.from_bytes(
                    data=img_data, mime_type=part['source']['media_type']))
            elif part.get('type') == 'text':
                gemini_parts.append(part['text'])

        gc = _gemini()
        if not gc:
            raise RuntimeError('Gemini client unavailable')
        resp = gc.models.generate_content(
            model='gemini-2.5-pro',
            contents=gemini_parts,
            config=_gtypes.GenerateContentConfig(max_output_tokens=8000, temperature=0.4),
        )
        assessment = (resp.text or '').strip()
        if not assessment:
            raise RuntimeError('Empty Gemini response')
        print(f'[ASSESS] Bid {bid_id} via Gemini 2.5 Pro ({len(assessment)} chars)', flush=True)

        import re as _re
        import json as _json

        buy_price = None

        # ── v2 path: parse JSON target_buy + INSERT ────────────────────────
        try:
            from ai_assessment_v2 import parse_response as _v2_parse
        except Exception as _v2pe:
            print(f'[ASSESS] v2 parse import failed: {_v2pe}', flush=True)
            _v2_parse = None

        v2_result = _v2_parse(assessment) if _v2_parse else None
        if v2_result:
            gemini_raw_target = v2_result['target_buy']
            buy_price = gemini_raw_target
            # Bias correction layer — segment-level over/under-bid pivot.
            # Reads ai_correction_config (active row); if disabled, returns
            # corrected_target == gemini_raw and tier='none'. See bias_correction.py.
            _bias_result = None
            try:
                import bias_correction as _bc
                _bias_result = _bc.apply_correction(
                    {'make': bid.get('make'), 'model': bid.get('model'),
                     'year': bid.get('year'), 'mileage': bid.get('mileage')},
                    gemini_raw_target)
                if _bias_result and _bias_result.get('strength_applied', 0) > 0:
                    buy_price = _bias_result['corrected_target']
                    print(f'[ASSESS] Bid {bid_id} bias-correction: '
                          f'{_bias_result["reason"]}', flush=True)
            except Exception as _bc_err:
                print(f'[ASSESS] bias_correction err: {_bc_err}', flush=True)
            v2_low   = v2_result.get('confidence_low')
            v2_high  = v2_result.get('confidence_high')
            v2_reason = v2_result.get('reasoning', '')
            v2_flags = v2_result.get('flags', [])

            # Book-value floor sanity check — catches bid 1139-style failures
            # where Gemini recommends far below AccuTrade's offer numbers.
            # If buy_price < 50% of max(trade_in, guaranteed_offer), flag it
            # so a human can review. We don't auto-clamp — just surface.
            try:
                _at_ti = (accutrade or {}).get('trade_in') if accutrade else None
                _at_go = (accutrade or {}).get('guaranteed_offer') if accutrade else None
                _at_ma = (accutrade or {}).get('market_avg') if accutrade else None
                _at_na = bool((accutrade or {}).get('not_available')) if accutrade else False
                _at_top = max([v for v in (_at_ti, _at_go, _at_ma) if v] or [0])
                if (not _at_na and _at_top > 0 and buy_price
                        and float(buy_price) < 0.5 * float(_at_top)):
                    _floor_flag = {
                        'recommended': float(buy_price),
                        'accutrade_trade_in': _at_ti,
                        'accutrade_guaranteed_offer': _at_go,
                        'accutrade_market_avg': _at_ma,
                        'ratio': round(float(buy_price) / float(_at_top), 3),
                    }
                    v2_flags = list(v2_flags) + ['under_book_floor_check']
                    print(f'[ASSESS] Bid {bid_id} UNDER_BOOK_FLOOR: '
                          f'rec=${float(buy_price):,.0f} vs accu top=${_at_top:,.0f} '
                          f'(ratio={_floor_flag["ratio"]:.2f})', flush=True)
                else:
                    _floor_flag = None
            except Exception as _ff_err:
                print(f'[ASSESS] floor-check err: {_ff_err}', flush=True)
                _floor_flag = None

            # Build a simple narrative for the bid card legacy field
            narrative = [f"**RECOMMENDED BUY**: ${buy_price:,}"]
            if v2_low and v2_high:
                narrative.append(f"**RANGE**: ${v2_low:,} – ${v2_high:,}")
            if v2_reason:
                narrative.append(f"**REASONING**: {v2_reason}")
            if v2_flags:
                narrative.append(f"**FLAGS**: {', '.join(v2_flags)}")
            assessment = "\n".join(narrative)

            print(f'[ASSESS] Bid {bid_id} v2 target_buy=${buy_price:,} '
                  f'range ${v2_low or 0:,}-${v2_high or 0:,} '
                  f'flags={v2_flags}', flush=True)

            # Audit log — one row per assessment.
            # New schema: bucket/baseline/llm_adjustment_pct columns stay NULL.
            # final_price = target_buy. reasoning + range stored in their
            # existing columns. flags_v2 + raw v2 result land in JSONB.
            try:
                _db3 = get_db()
                _cur3 = _db3.cursor()
                _cur3.execute("""
                    INSERT INTO ai_assessment_log
                        (bid_id, config_version, llm_reasoning,
                         confidence_low, confidence_high, final_price,
                         gemini_raw_target, raw_response,
                         dealer_intel, buyer_intel, market_intel, flags_v2)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    bid_id, 0, v2_reason,
                    v2_low, v2_high, buy_price,
                    gemini_raw_target,
                    _json.dumps({
                        'v2': v2_result,
                        '_server_flags': (
                            {
                                **({'odometer_discrepancy': _odo_flag} if _odo_flag else {}),
                                **({'under_book_floor_check': _floor_flag} if _floor_flag else {}),
                            } or None
                        ),
                        '_bias_correction': _bias_result,
                    }),
                    _json.dumps(_dealer_intel, default=str) if _dealer_intel else None,
                    _json.dumps(_buyer_intel, default=str) if _buyer_intel else None,
                    _json.dumps(_market_intel, default=str) if _market_intel else None,
                    _json.dumps(v2_flags, default=str) if v2_flags else None,
                ))
                _db3.commit()
                _db3.close()
                # Audit log — every assessment that ran through correction
                if _bias_result is not None:
                    try:
                        import bias_correction as _bc2
                        _bc2.log_correction(bid_id, _bias_result)
                    except Exception as _lc_err:
                        print(f'ai_correction_log write err: {_lc_err}', flush=True)
            except Exception as _log_err:
                print(f'ai_assessment_log v2 write error: {_log_err}', flush=True)
        else:
            print(f'[ASSESS] v2 parse FAILED; raw head={assessment[:300]!r}',
                  flush=True)
            # Last-resort: try to extract target_buy from a partially-truncated
            # JSON before regex-grepping any dollar amount. The $X,XXX grep
            # was too aggressive — caught "$115k MSRP" type numbers in
            # reasoning text and wrote $115 to ai_price (Raptor R bid 974).
            buy_price = None
            tb_match = _re.search(r'"target_buy"\s*:\s*(\d{4,7})', assessment)
            if tb_match:
                buy_price = int(tb_match.group(1))
            else:
                for m in _re.finditer(r'\$([0-9,]+)', assessment):
                    try:
                        v = int(m.group(1).replace(',', ''))
                        if v >= 1000:           # ignore "$115k" etc.
                            buy_price = v
                            break
                    except ValueError:
                        pass
            # Also try to surface a clean reasoning string from the partial
            # JSON so the bid card doesn't display ```json markdown.
            r_match = _re.search(r'"reasoning"\s*:\s*"([^"]+)', assessment)
            partial_reason = r_match.group(1).strip() if r_match else ''
            narrative = []
            if buy_price:
                narrative.append(f"**RECOMMENDED BUY**: ${buy_price:,}")
            if partial_reason:
                narrative.append(f"**REASONING** (partial — response was truncated): {partial_reason}")
            if narrative:
                assessment = "\n".join(narrative)

        db = get_db()
        try:
            cur = db.cursor()

            # Ensure ai_price column exists (migration)
            try:
                cur.execute("ALTER TABLE bids ADD COLUMN IF NOT EXISTS ai_price NUMERIC(10,2)")
                db.commit()
            except Exception:
                db.rollback()

            if buy_price:
                cur.execute("UPDATE bids SET ai_assessment=%s, ai_assessed_at=NOW(), ai_price=%s WHERE id=%s",
                            (assessment, buy_price, bid_id))
            else:
                cur.execute("UPDATE bids SET ai_assessment=%s, ai_assessed_at=NOW() WHERE id=%s",
                            (assessment, bid_id))
            db.commit()
        finally:
            db.close()

        # 2026-05-11: push inbound bid to subscribed partner dealers. Fires
        # from _run_assessment directly so it catches BOTH _auto_assess
        # (SMS intake path) AND /api/bid/<id>/assess (manual re-assess path).
        # Bid 1183 missed the original hook because /api/bid/<id>/assess
        # calls _run_assessment without the _auto_assess wrapper.
        try:
            from partner_portal import _push_bid_to_subscribed_partners
            _push_bid_to_subscribed_partners(bid_id)
        except Exception as _pbpe:
            print(f'[bid-push] outer error bid={bid_id}: {_pbpe}', flush=True)

        # 2026-05-11: refresh ai_accuracy for THIS bid so the training table
        # reflects the corrected assessment immediately (instead of waiting
        # for the 6 AM cron). Required when we re-run an assessment with
        # corrected inputs (e.g. overseer-fixed AccuTrade trim). Spawns a
        # daemon thread so it doesn't slow down the API response. The bias
        # correction layer trains on ai_accuracy, so keeping it current
        # post-reassess is what "clean training data" actually means.
        def _refresh_ai_accuracy():
            try:
                import subprocess as _sp
                _sp.Popen(
                    ['/opt/expwholesale/venv/bin/python3',
                     '/opt/expwholesale/reconcile_ai_accuracy.py',
                     '--bid', str(bid_id)],
                    stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                    close_fds=True,
                )
            except Exception as _re:
                print(f'[reconcile-kick] bid={bid_id}: {_re}', flush=True)
        try:
            import threading as _th
            _th.Thread(target=_refresh_ai_accuracy, daemon=True,
                       name=f'reconcile-{bid_id}').start()
        except Exception as _tke:
            print(f'[reconcile-kick] thread err bid={bid_id}: {_tke}', flush=True)

        return {'success': True, 'assessment': assessment, 'buy_price': buy_price}
    except Exception as e:
        print(f'Assessment error for bid {bid_id}: {e}')
        return {'error': str(e)}


def _auto_assess(bid_id):
    """Background: auto-run AI assessment after vAuto completes."""
    try:
        with app.app_context():
            result = _run_assessment(bid_id)
            if result.get('success'):
                print(f'Auto-assess complete for bid {bid_id}: buy_price={result.get("buy_price")}')
                _notify_driver_if_pending(bid_id)
                # Phase 2 SMS — second text with link to /m/<token>/full.
                # Idempotent via bids.phase2_notified_at (only fires once).
                # 2026-05-09: separate from Phase 1 SMS for now; once Phase 2
                # consistently completes in <1min we can merge into one text.
                try:
                    _notify_driver_phase2(bid_id)
                except Exception as _p2e:
                    print(f'[phase2-notify] outer error bid={bid_id}: {_p2e}', flush=True)
                # Partner push hook moved INTO _run_assessment so both
                # SMS-intake and manual re-assess paths fire it. See there.
            else:
                print(f'Auto-assess failed for bid {bid_id}: {result.get("error")}')
                _release_assessment_claim(bid_id)
    except Exception as e:
        print(f'Auto-assess error for bid {bid_id}: {e}')
        _release_assessment_claim(bid_id)


def _notify_driver_if_pending(bid_id):
    """If this bid came in via SMS (has driver_phone) and we haven't already
    auto-replied, text the sender the mini-page link. Idempotent — sets
    driver_notified_at so a re-run of assessment won't double-text.

    Phase 1 (this) is restricted to the 4-number full-broker whitelist via
    PHASE2_PHONE_GATE (shared with Phase 2). Phones outside that whitelist
    fall into Phase 3 — they received their final ack at bid intake
    (_send_phase3_ack), no mini-page link, no AI links. This function
    no-ops for them so we don't double-message.
    """
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute("""
            SELECT id, driver_token, driver_phone, driver_notified_at,
                   year, make, model
            FROM bids WHERE id=%s
        """, (bid_id,))
        bid = cur.fetchone()
        if not bid or not bid['driver_phone'] or not bid['driver_token']:
            db.close()
            return
        if bid['driver_notified_at'] is not None:
            db.close()
            return
        # Phase 1 gate — only the 4-number full-broker whitelist gets the
        # mini-page link SMS. Others already got the Phase 3 ack at intake.
        if not _is_full_broker_phone(bid['driver_phone']):
            print(f'[driver-notify] gated — bid={bid_id} '
                  f'driver={bid["driver_phone"]} not in Phase 1/2 whitelist '
                  f'(handled by Phase 3 at intake)', flush=True)
            db.close()
            return

        ymm_parts = [str(bid['year']) if bid['year'] else '',
                     bid['make'] or '', bid['model'] or '']
        ymm = ' '.join(p for p in ymm_parts if p).strip() or 'Vehicle'
        base = os.environ.get('PUBLIC_BASE_URL', 'https://experience-wholesale.net')
        link = f"{base}/m/{bid['driver_token']}"
        # Link on its own line — iMessage / Android only auto-linkify URLs
        # preceded by whitespace, and a newline gives a cleaner rich preview.
        body = f"Bid #{bid['id']} {ymm}\n{link}"

        sent = send_sms(bid['driver_phone'], body)
        if sent:
            cur.execute("UPDATE bids SET driver_notified_at=NOW() WHERE id=%s", (bid_id,))
            db.commit()
            print(f'[driver-notify] bid={bid_id} → {bid["driver_phone"]}', flush=True)
        else:
            print(f'[driver-notify] SMS failed bid={bid_id}', flush=True)
        db.close()
    except Exception as e:
        print(f'[driver-notify] error bid={bid_id}: {e}', flush=True)


# ── Assessment-fire coordinator ──────────────────────────────────────────────
# Assessment should only fire ONCE all three book-value sources (vAuto,
# AccuTrade, iPacket) have posted — otherwise the baseline is computed from
# ~30-40% of the intended weight and skews toward whichever sources arrived
# first. A 5-minute fallback timer fires the assessment with whatever's present
# in case a worker is dead or the VIN is ultra-rare.

def _release_assessment_claim(bid_id):
    """Reset ai_assessed_at to NULL so re-run is possible after failure."""
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute("UPDATE bids SET ai_assessed_at=NULL WHERE id=%s AND ai_assessment IS NULL", (bid_id,))
        db.commit()
        db.close()
    except Exception as e:
        print(f'release_assessment_claim error: {e}', flush=True)


def _maybe_fire_assessment(bid_id, require_all=True, source='unknown'):
    """Decide whether to fire AI assessment for a bid.

    require_all=True  → needs vauto, accutrade, AND ipacket rows present.
    require_all=False → timeout fallback; fires if vauto alone is present.

    Atomic claim via UPDATE ai_assessed_at prevents double-fire across the
    gunicorn worker pool. ai_assessment (narrative text) stays NULL until
    _run_assessment completes, so UI polling via /api/assess-status is
    unaffected by the claim.
    """
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute("SELECT ai_assessed_at, ai_assessment, "
                    "needs_verification_at, needs_verification_cleared_at, "
                    "needs_verification_reason "
                    "FROM bids WHERE id=%s", (bid_id,))
        row = cur.fetchone()
        if not row:
            db.close()
            return False
        # If already claimed or assessed, bail
        if row['ai_assessed_at'] is not None:
            db.close()
            return False
        # PHASE_D_VERIFY_GATE_2026_05_15: block AI when bid has open
        # verification flag. Cleared by operator edit on VIN/miles, manual
        # dismiss, or customer SMS reply with corrected data.
        if (row.get('needs_verification_at')
                and not row.get('needs_verification_cleared_at')):
            print(f"assess-gate bid={bid_id} source={source} "
                  f"BLOCKED by needs_verification="
                  f"{row.get('needs_verification_reason') or 'unknown'}",
                  flush=True)
            db.close()
            return False

        cur.execute("SELECT 1, rbook_completed_at IS NOT NULL AS rb_done, "
                    "manheim_completed_at IS NOT NULL AS mh_done "
                    "FROM vauto_lookups WHERE bid_id=%s LIMIT 1", (bid_id,))
        _vrow = cur.fetchone()
        has_vauto = _vrow is not None
        rb_done   = bool(_vrow and _vrow.get('rb_done'))
        mh_done   = bool(_vrow and _vrow.get('mh_done'))
        cur.execute("SELECT 1 FROM accutrade_lookups WHERE bid_id=%s LIMIT 1", (bid_id,))
        has_accu = cur.fetchone() is not None
        cur.execute("SELECT 1 FROM ipacket_lookups WHERE bid_id=%s LIMIT 1", (bid_id,))
        has_ipkt = cur.fetchone() is not None

        if require_all:
            # Wait for the full market stack — rbook + manheim must finish
            # so Gemini gets retail comps + auction floor in the prompt.
            # The 5-minute fallback timer (_schedule_assessment_fallback) will fire
            # with require_all=False if rbook/manheim never land (rare/exotic).
            ready = has_vauto and has_accu and has_ipkt and rb_done and mh_done
        else:
            ready = has_vauto  # fallback: fire with what we have

        if not ready:
            print(f'assess-gate bid={bid_id} source={source} require_all={require_all} '
                  f'vauto={has_vauto} accu={has_accu} ipkt={has_ipkt} '
                  f'rb_done={rb_done} mh_done={mh_done} → wait', flush=True)
            db.close()
            return False

        # Atomic claim — only one caller wins
        cur.execute("""
            UPDATE bids SET ai_assessed_at=NOW()
            WHERE id=%s AND ai_assessed_at IS NULL
            RETURNING id
        """, (bid_id,))
        claimed = cur.fetchone() is not None
        db.commit()
        db.close()
        if not claimed:
            return False
    except Exception as e:
        print(f'_maybe_fire_assessment error bid={bid_id}: {e}', flush=True)
        return False

    print(f'assess-fire bid={bid_id} source={source} require_all={require_all} '
          f'vauto={has_vauto} accu={has_accu} ipkt={has_ipkt}', flush=True)
    threading.Thread(target=_auto_assess, args=(bid_id,), daemon=True).start()
    return True


def _schedule_assessment_fallback(bid_id, delay_sec=300):
    """Arm a one-shot timer that fires assessment with require_all=False if
    AccuTrade/iPacket never landed. Safe to call on every vAuto submit —
    the gate bails if assessment already fired."""
    try:
        t = threading.Timer(
            delay_sec,
            lambda: _maybe_fire_assessment(bid_id, require_all=False, source='fallback_timer')
        )
        t.daemon = True
        t.start()
    except Exception as e:
        print(f'schedule_fallback error bid={bid_id}: {e}', flush=True)


@app.route('/api/bid/<int:bid_id>/assess', methods=['POST'])
def assess_bid(bid_id):
    """Run Claude Opus wholesale assessment using photos, notes, DIA comps, and market data."""
    result = _run_assessment(bid_id)
    if 'error' in result:
        status = 404 if result['error'] == 'Not found' else 500
        return jsonify(result), status
    return jsonify(result)


@app.route('/api/bid/<int:bid_id>/assess-status')
def api_assess_status(bid_id):
    """Check if AI assessment is complete for a bid."""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT ai_assessment IS NOT NULL as done, ai_price FROM bids WHERE id = %s", (bid_id,))
    row = cur.fetchone()
    db.close()
    if row:
        return jsonify({'done': row['done'], 'ai_price': float(row['ai_price']) if row['ai_price'] else None})
    return jsonify({'done': False})


@app.route('/api/bid/<int:bid_id>/contact', methods=['POST'])
def update_contact(bid_id):
    data = request.json or {}
    db = get_db()
    cur = db.cursor()

    cur.execute("SELECT contact_id FROM bids WHERE id=%s", (bid_id,))
    row = cur.fetchone()
    if row:
        cur.execute("""
            UPDATE contacts SET name=%s, company=%s WHERE id=%s
        """, (data.get('name'), data.get('company'), row['contact_id']))
        db.commit()

    db.close()
    return jsonify({'success': True})


@app.route('/api/stats')
def api_stats():
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT status, COUNT(*) as cnt FROM bids GROUP BY status")
    stats = {r['status']: int(r['cnt']) for r in cur.fetchall()}
    cur.execute("SELECT COUNT(*) as cnt FROM bids WHERE created_at::date = CURRENT_DATE")
    stats['today'] = int(cur.fetchone()['cnt'])
    cur.execute("SELECT COUNT(*) as cnt FROM bids WHERE phone LIKE 'field:%'")
    stats['field'] = int(cur.fetchone()['cnt'])
    db.close()
    return jsonify(stats)


@app.route('/api/bids')
def api_bids():
    since_id = request.args.get('since_id', 0, type=int)
    status_filter = request.args.get('status', 'all')
    rep_filter = request.args.get('rep', 'all')
    db = get_db()
    cur = db.cursor()

    # Track dashboard visitor by IP
    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr) or ''
    if ',' in client_ip:
        client_ip = client_ip.split(',')[0].strip()
    if client_ip:
        cur.execute("""
            INSERT INTO dashboard_visitors (ip, last_seen) VALUES (%s, NOW())
            ON CONFLICT (ip) DO UPDATE SET last_seen = NOW()
        """, (client_ip,))
        db.commit()

    cur.execute("SELECT status, COUNT(*) as cnt FROM bids GROUP BY status")
    stats = {'new': 0, 'reviewing': 0, 'bid_sent': 0, 'passed': 0, 'bought': 0, 'total': 0}
    for r in cur.fetchall():
        stats[r['status']] = int(r['cnt'])
        stats['total'] += int(r['cnt'])
    cur.execute("SELECT COUNT(*) as cnt FROM bids WHERE created_at::date = CURRENT_DATE")
    stats['today'] = int(cur.fetchone()['cnt'])
    cur.execute("SELECT status, COUNT(*) as cnt FROM bids WHERE created_at::date = CURRENT_DATE GROUP BY status")
    for r in cur.fetchall():
        stats[f'today_{r["status"]}'] = int(r['cnt'])
    cur.execute("SELECT COUNT(*) as cnt FROM bids WHERE phone LIKE 'field:%'")
    stats['field'] = int(cur.fetchone()['cnt'])

    # Cross-dealer aged + price-drop counts — same as dashboard route, polled
    # every few seconds so the 4 cards stay fresh as scans complete.
    try:
        cur.execute("""
            SELECT COALESCE(SUM(age_30_60),0) AS a30_60,
                   COALESCE(SUM(age_60_90),0) AS a60_90,
                   COALESCE(SUM(age_over_90),0) AS a90_plus
            FROM dealer_stats WHERE active
        """)
        r = cur.fetchone()
        stats['aged_30_60'] = int(r['a30_60'] or 0)
        stats['aged_60_90'] = int(r['a60_90'] or 0)
        stats['aged_90_plus'] = int(r['a90_plus'] or 0)
        # Active cars with a price drop observed scan-over-scan. Sold/missing
        # excluded — drops only matter for cars you can still buy. Sticky
        # for the row's active life: persists until status flips out of active.
        cur.execute("""
            SELECT COUNT(*) AS cnt FROM dealer_inventory i
            JOIN dealers d ON d.id = i.dealer_id
            WHERE i.status='active' AND d.active
              AND i.price_drop_amount IS NOT NULL
        """)
        stats['price_drops'] = int(cur.fetchone()['cnt'])
    except Exception:
        stats['aged_30_60'] = stats['aged_60_90'] = stats['aged_90_plus'] = stats['price_drops'] = 0

    conditions, params = [], []
    if status_filter == 'today':
        conditions.append("b.created_at::date = CURRENT_DATE")
    elif status_filter == 'field':
        conditions.append("b.phone LIKE 'field:%'")
    elif status_filter != 'all':
        conditions.append("b.status = %s")
        params.append(status_filter)
    if rep_filter != 'all':
        conditions.append("b.phone = %s")
        params.append(f'field:{rep_filter}')

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    q = """
        SELECT b.id, b.phone, b.vin, b.year, b.make, b.model, b.mileage,
               b.raw_message, b.status, b.created_at, b.bid_amount, b.ai_price, b.asking_price,
               b.has_unread, b.partner_dealer_id, b.partner_request_id, b.salesperson,
               b.bidder_name, b.awaiting_name, b.vin_invalid_reason,
               b.needs_verification_at, b.needs_verification_cleared_at,
               b.needs_verification_reason,  -- BADGE_NEEDS_VERIFY_API_2026_05_15
               b.damage_signal,  -- DAMAGE_BADGE_API_2026_05_15
               b.carfax_damage, b.autocheck_damage,
               c.name as contact_name, c.company as contact_company, c.role as contact_role,
               d.name as partner_dealer_name,
               dl.current_price       AS dc_current_price,
               dl.end_time            AS dc_end_time,
               dl.is_no_reserve       AS dc_no_reserve,
               dl.reserve_met         AS dc_reserve_met,
               dl.detail_url          AS dc_detail_url,
               dl.status              AS dc_status
        FROM bids b
        LEFT JOIN contacts c ON b.contact_id = c.id
        LEFT JOIN dealers d ON b.partner_dealer_id = d.id
        LEFT JOIN dealerclub_lots dl ON dl.bid_id = b.id
        {where}
        ORDER BY b.created_at DESC LIMIT 200
    """
    cur.execute(q.format(where=where), params)

    bids = []
    for r in cur.fetchall():
        # Per-bid DealerClub opportunity (only when this is a DC lot)
        dc_tier = None
        dc_pct = None
        if r.get('dc_current_price') and r.get('ai_price'):
            try:
                ai_f = float(r['ai_price'])
                all_in = float(r['dc_current_price']) \
                         + DEALERCLUB_BUY_FEE_FLAT \
                         + DEALERCLUB_TRANSPORT_EST
                pct = (ai_f - all_in) / ai_f * 100 if ai_f else None
                if pct is None:
                    dc_tier = 'gray'
                elif pct >= 15:
                    dc_tier = 'green'
                elif pct >= 5:
                    dc_tier = 'yellow'
                else:
                    dc_tier = 'red'
                dc_pct = round(pct, 1) if pct is not None else None
            except (TypeError, ValueError):
                dc_tier = 'gray'
        elif r.get('dc_current_price'):
            dc_tier = 'gray'

        bids.append({
            'id': r['id'],
            'phone': r['phone'],
            'vin': r['vin'],
            'year': r['year'],
            'make': r['make'],
            'model': r['model'],
            'mileage': r['mileage'],
            'raw_message': r['raw_message'],
            'status': r['status'],
            'created_at': r['created_at'].isoformat() if r['created_at'] else None,
            'contact_name': r['contact_name'],
            'contact_company': r['contact_company'],
            'contact_role': r.get('contact_role'),
            'asking_price': float(r['asking_price']) if r['asking_price'] else None,
            'ai_price': float(r['ai_price']) if r['ai_price'] else None,
            'bid_amount': float(r['bid_amount']) if r['bid_amount'] else None,
            'is_field': r['phone'].startswith('field:'),
            'is_db_push': r['phone'] == 'sys:db_push',
            'partner_dealer_id': r.get('partner_dealer_id'),
            'partner_dealer_name': r.get('partner_dealer_name'),
            'partner_request_id': r.get('partner_request_id'),
            'salesperson': r.get('salesperson'),
            'bidder_name': r.get('bidder_name'),
            'awaiting_name': bool(r.get('awaiting_name')),
            'is_new': r['id'] > since_id,
            'has_unread': bool(r.get('has_unread')),
            'dc_current_price': int(r['dc_current_price']) if r.get('dc_current_price') else None,
            'dc_opp_tier': dc_tier,
            'dc_opp_pct':  dc_pct,
            'dc_detail_url': r.get('dc_detail_url'),
            'dc_status': r.get('dc_status'),
            'vin_invalid_reason': r.get('vin_invalid_reason'),
            # BADGE_NEEDS_VERIFY_API_2026_05_15: surface verification
            # flag so dashboard JS poll can keep the yellow badge
            # visible between full-page renders.
            'needs_verification_at': r.get('needs_verification_at').isoformat() if r.get('needs_verification_at') else None,
            'needs_verification_cleared_at': r.get('needs_verification_cleared_at').isoformat() if r.get('needs_verification_cleared_at') else None,
            'needs_verification_reason': r.get('needs_verification_reason'),
            # DAMAGE_BADGE_API_2026_05_15: Carfax/AutoCheck damage cross-check
            'damage_signal': r.get('damage_signal'),
            'carfax_damage': r.get('carfax_damage'),
            'autocheck_damage': r.get('autocheck_damage'),
        })

    cur.execute("SELECT bid_id, COUNT(*) as cnt FROM bid_photos GROUP BY bid_id")
    photo_counts = {r['bid_id']: int(r['cnt']) for r in cur.fetchall()}

    cur.execute("""
        SELECT DISTINCT ON (bid_id) bid_id, COALESCE(local_path, url) AS src
        FROM bid_photos ORDER BY bid_id, id
    """)
    first_photos = {r['bid_id']: r['src'] for r in cur.fetchall()}

    # Badge turns green when iPacket finishes — last in the scan pipeline.
    # Workers always write a row (success OR not_available), so presence = done.
    # Badge turns green when ALL 3 lookups have rows (iPacket row present even when not_available=true).
    cur.execute("""
        SELECT v.bid_id FROM vauto_lookups v
        JOIN accutrade_lookups a ON a.bid_id = v.bid_id
    """)
    vauto_done = {r['bid_id'] for r in cur.fetchall()}

    # Live worker activity — same shape as the dashboard's full-page render
    # (60s look-back, most-recent first). Polled every 3s with the rest of
    # the row data so the chip flips on/off as workers claim/release.
    cur.execute("""
        SELECT DISTINCT ON (bid_id) bid_id, worker_id, job_type, status, completed_at
          FROM worker_jobs
         WHERE bid_id IS NOT NULL
         ORDER BY bid_id, claimed_at DESC
    """)
    active_workers = {}
    for r in cur.fetchall():
        active_workers.setdefault(r['bid_id'], []).append({
            'worker_id': r['worker_id'],
            'job_type': r['job_type'],
            'status': r.get('status', ''),
            'completed': r.get('completed_at') is not None,
        })

    # 2026-05-11: partner offer counts per bid — same shape as the SSR
    # dashboard so the JS auto-poll row-rebuild keeps the Partner Bid
    # column populated. Without this, rebuilt rows drop the column.
    partner_offer_counts = {}
    try:
        cur.execute("""
            SELECT bid_id, COUNT(*) AS n,
                   COUNT(*) FILTER (WHERE ew_seen_at IS NULL) AS unseen
              FROM bid_partner_offers
             GROUP BY bid_id
        """)
        for r in cur.fetchall():
            partner_offer_counts[r['bid_id']] = {
                'n': int(r['n']),
                'unseen': int(r['unseen']),
            }
    except Exception as _poc_err:
        print(f'[api/bids] partner_offer_counts err: {_poc_err}', flush=True)

    db.close()
    return jsonify({'bids': bids, 'stats': stats, 'photo_counts': photo_counts,
                    'first_photos': first_photos,
                    'vauto_done': list(vauto_done),
                    'active_workers': active_workers,
                    'partner_offer_counts': partner_offer_counts})


@app.route('/sms-intake')
def sms_intake_page():
    """Inbound SMS observability — every Twilio webhook hit, with the outcome
    (new_bid / stitched / partner_reply / share_reply) and the reason. Lets
    ops diagnose 'why didn't this text trigger a bid?' without log diving."""
    outcome_filter = request.args.get('outcome', 'all')
    db = get_db()
    cur = db.cursor()
    where, params = '', []
    if outcome_filter and outcome_filter != 'all':
        where = 'WHERE outcome = %s'
        params.append(outcome_filter)
    cur.execute(f"""
        SELECT id, created_at, from_phone, body, num_media, media_urls,
               parsed_vin, parsed_miles, outcome, bid_id, reason
        FROM sms_intake_log
        {where}
        ORDER BY id DESC LIMIT 200
    """, params)
    rows = cur.fetchall()

    # Per-row: pull the LOCAL photo for that bid (if any) so we render the
    # downloaded copy rather than the auth-gated Twilio URL. Falls back to
    # media_urls (raw Twilio) when no bid was created or photos didn't save.
    bid_ids = [r['bid_id'] for r in rows if r['bid_id']]
    locals_by_bid = {}
    if bid_ids:
        cur.execute("""
            SELECT bid_id, local_path, url, id FROM bid_photos
             WHERE bid_id = ANY(%s) AND is_sms_intake = TRUE
             ORDER BY bid_id, id
        """, (bid_ids,))
        for p in cur.fetchall():
            locals_by_bid.setdefault(p['bid_id'], []).append(
                p['local_path'] or p['url'])

    cur.execute("""SELECT outcome, COUNT(*) AS cnt FROM sms_intake_log
                    WHERE created_at > NOW() - INTERVAL '7 days'
                    GROUP BY outcome""")
    counts_7d = {r['outcome']: int(r['cnt']) for r in cur.fetchall()}
    db.close()

    return render_template('sms_intake.html', rows=rows,
                           locals_by_bid=locals_by_bid,
                           outcome_filter=outcome_filter,
                           counts_7d=counts_7d,
                           time_ago=time_ago)


@app.route('/contacts')
def contacts_page():
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT c.id, c.phone, c.name, c.company,
               COUNT(b.id) as bid_count
        FROM contacts c LEFT JOIN bids b ON b.contact_id = c.id
        GROUP BY c.id ORDER BY bid_count DESC, c.id DESC
    """)
    rows = cur.fetchall()
    db.close()
    field_contacts = [r for r in rows if r['phone'].startswith('field:')]
    sms_contacts   = [r for r in rows if not r['phone'].startswith('field:')]
    return render_template('contacts.html',
                           field_contacts=field_contacts,
                           sms_contacts=sms_contacts)


@app.route('/api/contact/<int:contact_id>', methods=['POST'])
def update_contact_direct(contact_id):
    data = request.json or {}
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        UPDATE contacts SET name=%s, company=%s WHERE id=%s
    """, (data.get('name') or None, data.get('company') or None, contact_id))
    db.commit()
    db.close()
    return jsonify({'success': True})


def resize_for_claude(file_bytes, max_bytes=7_000_000, max_dim=3000):
    """Resize image for Gemini (keeps text readable on Carfax/AutoCheck).
    Gemini 2.5 Pro handles up to 20MB/8K images natively, so we only
    resize if huge. Preserves fine text on long Carfax reports."""
    from PIL import Image
    import io
    img = Image.open(io.BytesIO(file_bytes))
    if img.mode not in ('RGB', 'L'):
        img = img.convert('RGB')
    # Resize if too large
    w, h = img.size
    if max(w, h) > max_dim:
        ratio = max_dim / max(w, h)
        img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
    # Compress to JPEG until under max_bytes
    quality = 88
    buf = io.BytesIO()
    while quality >= 40:
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=quality)
        if buf.tell() <= max_bytes:
            break
        quality -= 10
    return buf.getvalue(), 'image/jpeg'


@app.route('/api/verify-photo', methods=['POST'])
def verify_photo():
    """Accept all photos — VIN stickers, documents, damage close-ups, anything.
    The AI assessment on the dashboard sees and evaluates every photo."""
    return jsonify({'ok': True})


@app.route('/api/quick-extract', methods=['POST'])
def quick_extract():
    """Extract VIN or mileage from a single uploaded photo immediately.
    Google Vision first (~$0.0015/call), Claude fallback."""
    extract_type = request.form.get('type', 'vin')  # 'vin' or 'odo'
    f = request.files.get('photo')
    if not f:
        return jsonify({'error': 'No photo'}), 400

    file_bytes = f.read()
    media_type = f.mimetype or 'image/jpeg'

    # Try Google Vision first via the shared helpers
    if extract_type == 'vin':
        vin = extract_vin_from_file(file_bytes, media_type)
        if vin:
            # Flag if check digit fails so UI can prompt user to verify
            valid = vin_check_digit_valid(vin)
            return jsonify({
                'success': True,
                'value': vin,
                'check_digit_valid': valid,
                'warning': None if valid else 'VIN check digit failed — please verify each character'
            })
        return jsonify({'success': False, 'raw': 'Not detected'})
    else:
        miles = extract_mileage_from_file(file_bytes, media_type)
        if miles:
            return jsonify({'success': True, 'value': miles})
        return jsonify({'success': False, 'raw': 'Not detected'})


@app.route('/privacy')
def privacy():
    return render_template('privacy.html')

@app.route('/terms')
def terms():
    return render_template('terms.html')

@app.route('/service-worker.js')
def service_worker():
    """Serve SW from root so its scope covers the entire app."""
    return send_from_directory(app.static_folder, 'service-worker.js',
                               mimetype='application/javascript',
                               max_age=0)

# ── Push Notifications ────────────────────────────────────────────────────────

@app.route('/api/vapid-public-key')
def vapid_public_key():
    return jsonify({'publicKey': VAPID_PUBLIC_KEY})

@app.route('/api/push-subscribe', methods=['POST'])
def push_subscribe():
    """Save a push subscription for a field rep."""
    data = request.json or {}
    rep_name = data.get('rep_name', '').strip()
    subscription = data.get('subscription', {})
    endpoint = subscription.get('endpoint', '')
    keys = subscription.get('keys', {})
    p256dh = keys.get('p256dh', '')
    auth = keys.get('auth', '')

    if not rep_name or not endpoint or not p256dh or not auth:
        return jsonify({'error': 'Missing fields'}), 400

    rep_phone = 'field:' + re.sub(r'\s+', '_', rep_name.lower())
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        INSERT INTO push_subscriptions (rep_phone, endpoint, p256dh, auth)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (endpoint) DO UPDATE SET rep_phone=%s, p256dh=%s, auth=%s
    """, (rep_phone, endpoint, p256dh, auth, rep_phone, p256dh, auth))
    db.commit()
    db.close()
    return jsonify({'success': True})

@app.route('/api/push-unsubscribe', methods=['POST'])
def push_unsubscribe():
    data = request.json or {}
    endpoint = data.get('endpoint', '')
    if not endpoint:
        return jsonify({'error': 'Missing endpoint'}), 400
    db = get_db()
    cur = db.cursor()
    cur.execute("DELETE FROM push_subscriptions WHERE endpoint=%s", (endpoint,))
    db.commit()
    db.close()
    return jsonify({'success': True})

def _fire_owner_new_bid(bid_id):
    """Best-effort fan-out to owner-portal subscribers. Never blocks the
    user-facing bid-create response; owner notification is observability,
    not core flow."""
    if notify_owners_new_bid is None:
        return
    try:
        notify_owners_new_bid(bid_id, send_push_to_rep)
    except Exception as e:
        print(f'[owner-notify] {type(e).__name__}: {e}', flush=True)


def send_push_to_rep(rep_phone, title, body, url='/mobile'):
    """Send push notification to all devices registered for this rep."""
    if not VAPID_PRIVATE_KEY:
        return
    try:
        from pywebpush import webpush, WebPushException
        import json as _json
    except ImportError:
        return

    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE rep_phone=%s", (rep_phone,))
    subs = cur.fetchall()
    db.close()

    for sub in subs:
        try:
            webpush(
                subscription_info={
                    'endpoint': sub['endpoint'],
                    'keys': {'p256dh': sub['p256dh'], 'auth': sub['auth']}
                },
                data=_json.dumps({'title': title, 'body': body, 'url': url}),
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={'sub': 'mailto:admin@experience-wholesale.net'}
            )
        except Exception:
            # Subscription expired or invalid — clean it up
            try:
                db2 = get_db()
                cur2 = db2.cursor()
                cur2.execute("DELETE FROM push_subscriptions WHERE endpoint=%s", (sub['endpoint'],))
                db2.commit()
                db2.close()
            except Exception:
                pass

@app.route('/mobile')
def mobile():
    force_setup = 'reset' in request.args
    # Partner-dealer mobile link — `?p=<token>` resolves to a dealer +
    # their EW salesperson. Template stashes the token in localStorage so
    # subsequent visits stay tagged even if the URL is opened without ?p.
    partner_ctx = None
    token = (request.args.get('p') or '').strip()
    if token:
        try:
            db = get_db()
            cur = db.cursor()
            cur.execute("""SELECT id, name, salesperson, brand FROM dealers
                            WHERE mobile_token = %s AND active = TRUE
                            LIMIT 1""", (token,))
            row = cur.fetchone()
            db.close()
            if row:
                partner_ctx = {'token': token, 'name': row['name'],
                               'salesperson': row['salesperson'],
                               'brand': row.get('brand') or {}}
        except Exception:
            partner_ctx = None
    # `back` URL — sent by the partner portal's "Open Mobile App" button.
    # Only honored when it points at our own host so the back-button
    # can't be used to redirect to an arbitrary external URL.
    back_url = (request.args.get('back') or '').strip()
    if back_url:
        from urllib.parse import urlparse
        try:
            host_ok = urlparse(back_url).netloc == urlparse(request.host_url).netloc
        except Exception:
            host_ok = False
        if not host_ok:
            back_url = None
    return render_template('mobile.html', rep_name='',
                           force_setup=force_setup,
                           partner_ctx=partner_ctx,
                           back_url=back_url)


@app.route('/api/mobile-submit', methods=['POST'])
def mobile_submit():
    os.makedirs(UPLOAD_DIR, exist_ok=True)

    rep_name = request.form.get('rep_name', '').strip()
    notes = request.form.get('notes', '').strip()
    asking_price_raw = request.form.get('asking_price', '').strip()
    manual_vin = request.form.get('manual_vin', '').strip().upper()
    manual_mileage_raw = request.form.get('manual_mileage', '').strip()

    # Asking price is required server-side too — defends against the JS
    # validation being bypassed (curl / older cached bundle / dev tools).
    asking_price = None
    if asking_price_raw:
        try:
            asking_price = float(asking_price_raw)
        except ValueError:
            pass
    if not asking_price or asking_price <= 0:
        return jsonify({'success': False,
                        'error': 'Asking price is required.'}), 400

    manual_mileage = None
    if manual_mileage_raw:
        try:
            manual_mileage = int(manual_mileage_raw)
        except ValueError:
            pass

    # --- Save car photos ---
    car_photo_urls = []
    i = 0
    while True:
        f = request.files.get(f'car_photo_{i}')
        if f is None:
            break
        ext = os.path.splitext(f.filename)[1] or '.jpg'
        fname = f'{uuid.uuid4().hex}{ext}'
        fpath = os.path.join(UPLOAD_DIR, fname)
        f.save(fpath)
        car_photo_urls.append(f'/static/uploads/{fname}')
        i += 1

    # --- VIN photo: Claude Vision ---
    vin = manual_vin if VIN_RE.match(manual_vin) else None
    vin_photo_url = None
    vin_f = request.files.get('vin_photo')
    if vin_f:
        ext = os.path.splitext(vin_f.filename)[1] or '.jpg'
        fname = f'{uuid.uuid4().hex}{ext}'
        fpath = os.path.join(UPLOAD_DIR, fname)
        vin_f.seek(0)
        file_bytes = vin_f.read()
        vin_f.seek(0)
        vin_f.save(fpath)
        vin_photo_url = f'/static/uploads/{fname}'
        if ANTHROPIC_KEY and not vin:
            media_type = vin_f.mimetype or 'image/jpeg'
            extracted = extract_vin_from_file(file_bytes, media_type)
            if extracted:
                vin = extracted

    # --- Odometer photo: Claude Vision ---
    mileage = manual_mileage
    odo_photo_url = None
    odo_f = request.files.get('odo_photo')
    if odo_f:
        ext = os.path.splitext(odo_f.filename)[1] or '.jpg'
        fname = f'{uuid.uuid4().hex}{ext}'
        fpath = os.path.join(UPLOAD_DIR, fname)
        odo_f.seek(0)
        file_bytes = odo_f.read()
        odo_f.seek(0)
        odo_f.save(fpath)
        odo_photo_url = f'/static/uploads/{fname}'
        if ANTHROPIC_KEY and not mileage:
            media_type = odo_f.mimetype or 'image/jpeg'
            mileage = extract_mileage_from_file(file_bytes, media_type)

    # --- Decode VIN ---
    decoded_vin = decode_vin(vin) if vin else {}

    # Color detection from car photos intentionally disabled — only deliberate
    # VIN-sticker / VIN-dashboard / odometer photos should drive vehicle data.
    # Color can still be set manually from the bid detail page if desired.
    detected_color = None

    # --- Build raw_message ---
    parts = []
    if rep_name:
        parts.append(f'Rep: {rep_name}')
    if vin:
        parts.append(f'VIN: {vin}')
    if mileage:
        parts.append(f'Mileage: {mileage:,}')
    if asking_price:
        parts.append(f'Asking: ${asking_price:,.0f}')
    if notes:
        parts.append(notes)
    raw_message = ' | '.join(parts) if parts else 'Mobile field submission'

    # --- Partner-tagged mobile submissions ─────────────────────────────
    # When the mobile PWA is opened via /mobile?p=<token>, the form sends
    # the token back here. If it resolves to a dealer, the bid lands with
    # partner_dealer_id + salesperson snapshot + a partner_bid_requests
    # row (so it shows up in the partner's own dashboard like Quick Drop).
    partner_token = (request.form.get('partner_token') or '').strip()
    partner_dealer = None
    if partner_token:
        db_lookup = get_db()
        lookup_cur = db_lookup.cursor()
        lookup_cur.execute("""SELECT id, name, salesperson FROM dealers
                              WHERE mobile_token = %s AND active = TRUE
                              LIMIT 1""", (partner_token,))
        partner_dealer = lookup_cur.fetchone()
        db_lookup.close()

    # --- DB insert ---
    db = get_db()
    cur = db.cursor()

    # Phone routing depends on whether this is a partner-tagged submission.
    # Partner mobile bids must look identical to Quick Drop bids on the EW
    # dashboard — same `PARTNER{id}` phone sentinel, same shared dealer
    # contact (with company=salesperson driving the blue chip). The rep
    # name still surfaces in raw_message + notes so we know who submitted.
    if partner_dealer:
        rep_phone = f'PARTNER{partner_dealer["id"]}'
        cur.execute("""
            INSERT INTO contacts (phone, name, company)
            VALUES (%s, %s, %s)
            ON CONFLICT (phone) DO UPDATE
              SET name = EXCLUDED.name, company = EXCLUDED.company
            RETURNING id
        """, (rep_phone,
              f'{partner_dealer["name"]} (Partner Portal)',
              (partner_dealer.get('salesperson') or '').strip() or None))
    else:
        rep_phone = f'field:{rep_name.replace(" ", "_").lower() or "rep"}'
        cur.execute("""
            INSERT INTO contacts (phone, name)
            VALUES (%s, %s)
            ON CONFLICT (phone) DO UPDATE SET name = EXCLUDED.name
            RETURNING id
        """, (rep_phone, rep_name or None))
    contact_id = cur.fetchone()['id']

    pbr_id = None
    if partner_dealer:
        # partner_bid_requests row first so the bid can FK back via
        # partner_request_id. inventory_id is NULL (mobile submit isn't
        # tied to scraped inventory).
        cur.execute("""INSERT INTO partner_bid_requests
                         (dealer_id, partner_user_id, inventory_id, vin,
                          target_price, partner_message)
                       VALUES (%s, NULL, NULL, %s, %s, %s)
                       RETURNING id""",
                    (partner_dealer['id'], vin or '', asking_price,
                     f'Mobile by {rep_name}' if rep_name else 'Mobile submission'))
        pbr_id = cur.fetchone()['id']

    notes_full = (f'[Partner Mobile · {partner_dealer["name"]} · Rep: {rep_name or "—"}] {notes}'.strip()
                  if partner_dealer
                  else (f'[Field: {rep_name}] {notes}'.strip() if notes
                        else f'[Field: {rep_name}]'))

    cur.execute("""
        INSERT INTO bids (contact_id, phone, vin, mileage, year, make, model, trim, color,
                          raw_message, asking_price, notes, status, has_unread,
                          partner_dealer_id, partner_request_id, salesperson,
                          field_rep_name)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'new', TRUE,
                %s, %s, %s, %s) RETURNING id
    """, (contact_id, rep_phone, vin, mileage,
          decoded_vin.get('year'), decoded_vin.get('make'),
          decoded_vin.get('model'), decoded_vin.get('trim'),
          detected_color,
          raw_message, asking_price, notes_full,
          (partner_dealer['id'] if partner_dealer else None),
          pbr_id,
          (partner_dealer['salesperson'] if partner_dealer else None),
          rep_name or None))

    bid_id = cur.fetchone()['id']

    # Direct API kick removed 2026-05-08 (see same removal at intake hooks).

    if pbr_id:
        cur.execute("UPDATE partner_bid_requests SET bid_id=%s WHERE id=%s",
                    (bid_id, pbr_id))

    # Store all photos in bid_photos
    all_photos = [(u, None) for u in car_photo_urls]
    if vin_photo_url:
        all_photos.append((vin_photo_url, vin))
    if odo_photo_url:
        all_photos.append((odo_photo_url, None))

    for url, vin_ext in all_photos:
        cur.execute("""
            INSERT INTO bid_photos (bid_id, url, vin_extracted) VALUES (%s, %s, %s)
        """, (bid_id, url, vin_ext))

    # Flag for priority vAuto lookup
    if vin:
        cur.execute("UPDATE bids SET vauto_priority=TRUE WHERE id=%s", (bid_id,))

    db.commit()
    db.close()

    # Owner-portal push fan-out (best-effort, never blocks)
    _fire_owner_new_bid(bid_id)

    # Background market check
    if vin:
        trigger_market_check(bid_id, vin)

    # Telegram alert for partner-tagged mobile submissions so admin sees
    # them land in real time (mirrors the Quick Drop alert format).
    if partner_dealer:
        try:
            from partner_portal import _tg_alert, _notify_salesperson
            _tg_alert(f'📱 <b>{partner_dealer["name"]}</b> mobile · Bid #{bid_id}\n'
                      f'Rep: {rep_name or "—"}\n'
                      f'{vin or "(no VIN)"}'
                      + (f' · {mileage:,} mi' if mileage else '')
                      + (f' · ask ${int(asking_price):,}' if asking_price else ''))
        except Exception:
            pass
        # Re-fetch dealer with salesperson_phone (the partner_dealer dict
        # from earlier didn't include it).
        try:
            from partner_portal import _notify_salesperson
            db_n = get_db()
            cur_n = db_n.cursor()
            cur_n.execute('SELECT name, salesperson, salesperson_phone '
                          'FROM dealers WHERE id = %s', (partner_dealer['id'],))
            dealer_full = cur_n.fetchone()
            db_n.close()
            _notify_salesperson(dealer_full, bid_id, vin,
                                decoded_vin.get('year'), decoded_vin.get('make'),
                                decoded_vin.get('model'), asking_price,
                                source='mobile bid')
        except Exception as e:
            print(f'[salesperson sms] mobile path: {e}')

    return jsonify({
        'success': True,
        'bid_id': bid_id,
        'vin': vin,
        'mileage': mileage,
        'asking_price': asking_price,
        'photos': len(all_photos)
    })


@app.route('/api/bid/<int:bid_id>/field-update', methods=['PATCH'])
def field_update_bid(bid_id):
    rep_name = request.form.get('rep_name', '').strip()
    rep_phone = f'field:{rep_name.replace(" ", "_").lower() or "rep"}'
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id, phone, vin, mileage FROM bids WHERE id = %s", (bid_id,))
    bid = cur.fetchone()
    if not bid or bid['phone'] != rep_phone:
        db.close()
        return jsonify({'error': 'Not found'}), 404

    updates = {}

    manual_vin = request.form.get('manual_vin', '').strip().upper()
    if manual_vin and VIN_RE.match(manual_vin):
        updates['vin'] = manual_vin
        decoded = decode_vin(manual_vin)
        for k in ('year', 'make', 'model', 'trim'):
            if decoded.get(k):
                updates[k] = decoded[k]

    manual_mileage_raw = request.form.get('manual_mileage', '').strip()
    if manual_mileage_raw:
        try:
            updates['mileage'] = int(manual_mileage_raw)
        except ValueError:
            pass

    notes_raw = request.form.get('notes', '').strip()
    if notes_raw:
        updates['notes'] = f'[Field: {rep_name}] {notes_raw}'.strip()

    asking_price_raw = request.form.get('asking_price', '').strip()
    asking_price = None
    if asking_price_raw:
        try:
            asking_price = float(asking_price_raw)
        except ValueError:
            pass

    # Save new car photos
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    new_photo_urls = []
    i = 0
    while True:
        f = request.files.get(f'car_photo_{i}')
        if f is None:
            break
        ext = os.path.splitext(f.filename)[1] or '.jpg'
        fname = f'{uuid.uuid4().hex}{ext}'
        f.save(os.path.join(UPLOAD_DIR, fname))
        new_photo_urls.append(f'/static/uploads/{fname}')
        i += 1

    # Build SET clause
    set_parts = []
    params = []
    for col in ('vin', 'mileage', 'year', 'make', 'model', 'trim', 'notes'):
        if col in updates:
            set_parts.append(f'{col} = %s')
            params.append(updates[col])
    if asking_price is not None:
        set_parts.append('asking_price = %s')
        params.append(asking_price)
    if set_parts:
        set_parts.append('updated_at = NOW()')
        params.append(bid_id)
        cur.execute(f"UPDATE bids SET {', '.join(set_parts)} WHERE id = %s", params)

    for url in new_photo_urls:
        cur.execute("INSERT INTO bid_photos (bid_id, url) VALUES (%s, %s)", (bid_id, url))

    db.commit()
    db.close()
    return jsonify({'success': True, 'bid_id': bid_id})


@app.route('/api/rep-bids')
def rep_bids():
    rep_name = request.args.get('rep', '').strip()
    if not rep_name:
        return jsonify({'bids': []})
    rep_phone = f'field:{rep_name.replace(" ", "_").lower() or "rep"}'
    db = get_db()
    cur = db.cursor()
    # Bump last_seen + IP on every poll (heartbeat)
    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr) or ''
    if ',' in client_ip:
        client_ip = client_ip.split(',')[0].strip()
    cur.execute("UPDATE contacts SET last_seen = NOW(), last_ip = %s WHERE phone = %s", (client_ip, rep_phone))
    db.commit()
    # Match either the legacy field:rep_name phone OR the dedicated
    # field_rep_name column. Partner-tagged mobile bids store phone as
    # PARTNER<id> (so they render like Quick Drop on EW dashboard) but
    # still belong to a specific rep — field_rep_name carries that.
    cur.execute("""
        SELECT b.id, b.vin, b.year, b.make, b.model, b.mileage, b.status,
               b.created_at, b.bid_amount, b.bid_response,
               (SELECT url FROM bid_photos WHERE bid_id = b.id ORDER BY id LIMIT 1) AS first_photo
        FROM bids b
        WHERE b.phone = %s
           OR LOWER(b.field_rep_name) = LOWER(%s)
        ORDER BY b.created_at DESC LIMIT 30
    """, (rep_phone, rep_name))
    rows = cur.fetchall()
    db.close()
    bids = []
    for r in rows:
        bids.append({
            'id': r['id'],
            'vin': r['vin'],
            'year': r['year'],
            'make': r['make'],
            'model': r['model'],
            'mileage': r['mileage'],
            'status': r['status'],
            'created_at': r['created_at'].isoformat() if r['created_at'] else None,
            'bid_amount': float(r['bid_amount']) if r['bid_amount'] else None,
            'bid_response': r['bid_response'],
            'first_photo': r['first_photo'],
        })
    return jsonify({'bids': bids})


@app.route('/api/register-rep', methods=['POST'])
def register_rep():
    """Register a field rep in the system when they first open the app."""
    data = request.json or {}
    rep_name = data.get('rep_name', '').strip()
    if not rep_name or len(rep_name) < 2:
        return jsonify({'error': 'Name required'}), 400

    rep_phone = f'field:{rep_name.replace(" ", "_").lower()}'
    db = get_db()
    cur = db.cursor()
    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr) or ''
    if ',' in client_ip:
        client_ip = client_ip.split(',')[0].strip()
    cur.execute("""
        INSERT INTO contacts (phone, name, last_seen, last_ip)
        VALUES (%s, %s, NOW(), %s)
        ON CONFLICT (phone) DO UPDATE SET name = %s, last_seen = NOW(), last_ip = %s
        RETURNING id
    """, (rep_phone, rep_name, client_ip, rep_name, client_ip))
    contact_id = cur.fetchone()['id']
    db.commit()
    db.close()
    return jsonify({'success': True, 'contact_id': contact_id})


@app.route('/api/active-reps')
def active_reps():
    """Active EW-admin viewers (last 2 minutes). The legacy version of
    this endpoint also surfaced field-rep mobile users by name+IP, but
    that leaked partner-mobile and field-rep names+IPs onto the EW
    dashboard top bar. Reps are no longer returned — the pill now only
    shows authenticated admins (entries land in dashboard_visitors only
    when someone hits /api/bids, which is admin-gated)."""
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT ip, last_seen FROM dashboard_visitors
        WHERE last_seen > NOW() - INTERVAL '2 minutes'
        ORDER BY last_seen DESC
    """)
    viewers = [{'ip': r['ip'],
                'last_seen': r['last_seen'].isoformat() if r['last_seen'] else None}
               for r in cur.fetchall()]
    db.close()
    return jsonify({'viewers': viewers, 'reps': []})


@app.route('/api/bid/<int:bid_id>/messages-poll')
def bid_messages_poll(bid_id):
    """Lightweight poll — returns message count + latest message for the bid page."""
    since = request.args.get('since', 0, type=int)
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT COUNT(*) as cnt FROM bid_messages WHERE bid_id=%s", (bid_id,))
    total = cur.fetchone()['cnt']
    # Get max message id (for client to track)
    cur.execute("SELECT COALESCE(MAX(id), 0) as max_id FROM bid_messages WHERE bid_id=%s", (bid_id,))
    max_id = cur.fetchone()['max_id']

    new_msgs = []
    if since > 0:
        cur.execute("""
            SELECT id, direction, message, created_at FROM bid_messages
            WHERE bid_id=%s AND id > %s ORDER BY created_at
        """, (bid_id, since))
        new_msgs = [{'id': r['id'], 'direction': r['direction'], 'message': r['message'],
                      'created_at': r['created_at'].isoformat() if r['created_at'] else None}
                     for r in cur.fetchall()]
    # Clear unread flag since the manager is actively viewing this bid
    cur.execute("UPDATE bids SET has_unread=FALSE WHERE id=%s AND has_unread=TRUE", (bid_id,))
    db.commit()
    db.close()
    return jsonify({'total': total, 'max_id': max_id, 'new_messages': new_msgs})


@app.route('/api/bid/<int:bid_id>/messages')
def bid_messages(bid_id):
    """Get message thread for a bid (used by mobile app)."""
    rep_name = request.args.get('rep', '').strip()
    rep_phone = f'field:{rep_name.replace(" ", "_").lower() or "rep"}'
    db = get_db()
    cur = db.cursor()
    # Verify this rep owns the bid
    cur.execute("SELECT phone FROM bids WHERE id=%s", (bid_id,))
    bid = cur.fetchone()
    if not bid or bid['phone'] != rep_phone:
        db.close()
        return jsonify({'messages': []})
    cur.execute("SELECT direction, message, created_at FROM bid_messages WHERE bid_id=%s ORDER BY created_at", (bid_id,))
    rows = cur.fetchall()
    db.close()
    return jsonify({'messages': [
        {'direction': r['direction'], 'message': r['message'],
         'created_at': r['created_at'].isoformat() if r['created_at'] else None}
        for r in rows
    ]})


@app.route('/api/bid/<int:bid_id>/rep-message', methods=['POST'])
def rep_message(bid_id):
    """Field rep sends a message on a bid — shows in dashboard thread."""
    data = request.json or {}
    rep_name = data.get('rep_name', '').strip()
    message = data.get('message', '').strip()
    if not rep_name or not message:
        return jsonify({'error': 'Missing rep_name or message'}), 400

    rep_phone = f'field:{rep_name.replace(" ", "_").lower()}'
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT phone FROM bids WHERE id=%s", (bid_id,))
    bid = cur.fetchone()
    if not bid or bid['phone'] != rep_phone:
        db.close()
        return jsonify({'error': 'Not found'}), 404

    cur.execute("""
        INSERT INTO bid_messages (bid_id, direction, message, from_phone)
        VALUES (%s, 'inbound', %s, %s)
    """, (bid_id, message, rep_phone))
    cur.execute("UPDATE bids SET updated_at=NOW(), has_unread=TRUE WHERE id=%s", (bid_id,))
    db.commit()
    db.close()
    return jsonify({'success': True})


# ── vAuto worker API ─────────────────────────────────────────────────────────

VAUTO_REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'vauto_reports')
ACCUTRADE_REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'accutrade_reports')
IPACKET_REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ipacket_reports')


def _live_scan_comps(bid_id):
    """On-demand: find dealers who carried this car type, scrape their sites for fresh comps.
    Updates DIA with fresh data as a side effect."""
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute("SELECT vin, year, make, model, mileage FROM bids WHERE id=%s", (bid_id,))
        bid = cur.fetchone()
        db.close()
        if not bid or not bid['vin'] or len(bid['vin']) < 8:
            return

        vin_prefix = bid['vin'][:8]
        bid_year = bid['year'] or 2023
        make = bid['make'] or ''
        model = bid['model'] or ''

        # Find dealers who've had this VIN prefix — they're likely to have similar cars now
        dia_conn = psycopg2.connect(DIA_DB_URL,
                                    cursor_factory=psycopg2.extras.RealDictCursor,
                                    connect_timeout=5)
        dia_cur = dia_conn.cursor()

        dia_cur.execute("""
            SELECT DISTINCT d.dealer_id, d.name, d.website, i.url as sample_url
            FROM dealers d
            JOIN inventory i ON i.dealer_id = d.dealer_id
            WHERE LEFT(i.vin, 8) = %s
              AND d.website IS NOT NULL AND d.website != ''
              AND i.url IS NOT NULL AND i.url != ''
            LIMIT 20
        """, (vin_prefix,))
        dealers = dia_cur.fetchall()

        if not dealers:
            # Fallback to make/model match
            dia_cur.execute("""
                SELECT DISTINCT d.dealer_id, d.name, d.website, i.url as sample_url
                FROM dealers d
                JOIN inventory i ON i.dealer_id = d.dealer_id
                WHERE i.make ILIKE %s AND i.model ILIKE %s
                  AND d.website IS NOT NULL AND d.website != ''
                  AND i.url IS NOT NULL AND i.url != ''
                LIMIT 20
            """, (make, model))
            dealers = dia_cur.fetchall()

        print(f"  [LiveScan] bid #{bid_id}: found {len(dealers)} dealers to scan")

        from concurrent.futures import ThreadPoolExecutor, as_completed
        from urllib.parse import urlparse
        import re as _re

        def scan_dealer(dealer):
            """Hit a dealer's website search for this make/model/year, extract listings."""
            website = dealer['website'].rstrip('/')
            # Strip UTM params from website URL
            parsed = urlparse(website)
            base_url = f"{parsed.scheme}://{parsed.netloc}"

            # Try common search URL patterns
            search_urls = [
                f"{base_url}/used/{make}/{bid_year}-{make}-{model}/",
                f"{base_url}/inventory/?make={make}&model={model}&year={bid_year}",
                f"{base_url}/used-inventory/?make={make}&model={model}&year_from={bid_year}&year_to={bid_year}",
                f"{base_url}/searchused.aspx?Make={make}&Model={model}&Year={bid_year}",
            ]

            results = []
            for url in search_urls:
                try:
                    r = requests.get(url, timeout=8, headers={
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                    }, allow_redirects=True)
                    if r.status_code != 200:
                        continue

                    text = r.text
                    # Find VINs on the page
                    vins = set(_re.findall(r'\b[A-HJ-NPR-Z0-9]{17}\b', text))
                    # Filter to matching VIN prefix
                    matching = [v for v in vins if v[:8] == vin_prefix and v != bid['vin']]

                    if not matching:
                        # Broader: same make at least
                        matching = [v for v in vins if v != bid['vin']]
                        matching = matching[:3]  # limit

                    # Extract prices near VINs
                    for vin in matching[:5]:
                        # Find price near this VIN in the HTML
                        vin_pos = text.find(vin)
                        if vin_pos < 0:
                            continue
                        chunk = text[max(0, vin_pos-500):vin_pos+500]
                        prices = _re.findall(r'\$\s?([\d,]{5,10})', chunk)
                        price = None
                        for p in prices:
                            try:
                                val = int(p.replace(',', ''))
                                if 2000 < val < 500000:
                                    price = val
                                    break
                            except ValueError:
                                pass
                        # Try to find mileage
                        miles_match = _re.search(r'([\d,]+)\s*(?:mi|miles|mileage)', chunk, _re.I)
                        mileage = None
                        if miles_match:
                            try:
                                mileage = int(miles_match.group(1).replace(',', ''))
                                if mileage > 500000:
                                    mileage = None
                            except ValueError:
                                pass

                        # Build listing URL — try to find a link with this VIN
                        link_match = _re.search(r'href=["\']([^"\']*' + vin + r'[^"\']*)["\']', text, _re.I)
                        listing_url = None
                        if link_match:
                            listing_url = link_match.group(1)
                            if listing_url.startswith('/'):
                                listing_url = base_url + listing_url

                        if price or listing_url:
                            results.append({
                                'vin': vin,
                                'price': price,
                                'mileage': mileage,
                                'dealer_name': dealer['name'],
                                'dealer_id': dealer['dealer_id'],
                                'url': listing_url,
                                'source': 'live_scan',
                            })

                    if results:
                        break  # Found results, no need to try other URL patterns
                except Exception:
                    continue

            return results

        # Scan dealers in parallel (max 8 threads)
        all_results = []
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(scan_dealer, d): d for d in dealers}
            for future in as_completed(futures):
                try:
                    results = future.result()
                    all_results.extend(results)
                except Exception:
                    pass

        print(f"  [LiveScan] bid #{bid_id}: found {len(all_results)} fresh comps from {len(dealers)} dealers")

        # Update DIA with fresh data
        if all_results:
            for comp in all_results:
                try:
                    if comp.get('price') and comp.get('vin'):
                        dia_cur.execute("""
                            UPDATE inventory SET price = %s, last_seen = CURRENT_DATE
                            WHERE vin = %s AND dealer_id = %s
                        """, (comp['price'], comp['vin'], comp['dealer_id']))
                except Exception:
                    pass
            dia_conn.commit()

        dia_conn.close()

    except Exception as e:
        print(f"  [LiveScan] error for bid #{bid_id}: {e}")


# ScrapFly removed 2026-04-23 — service killed after $1,127 runaway overage.
# verify_comp() was the only caller and had no live references anywhere, so
# the whole function was deleted with the service. If live comp verification
# comes back as a need, rebuild against FlareSolverr + DataImpulse instead.
os.makedirs(VAUTO_REPORTS_DIR, exist_ok=True)
os.makedirs(ACCUTRADE_REPORTS_DIR, exist_ok=True)
os.makedirs(IPACKET_REPORTS_DIR, exist_ok=True)


def _ensure_accutrade_table():
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS accutrade_lookups (
                id SERIAL PRIMARY KEY,
                bid_id INTEGER REFERENCES bids(id) ON DELETE CASCADE,
                vin VARCHAR(17) NOT NULL,
                guaranteed_offer INTEGER,
                trade_in INTEGER,
                trade_market INTEGER,
                retail INTEGER,
                market_avg INTEGER,
                local_comps JSONB,
                screenshot TEXT,
                raw_json JSONB,
                looked_up_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_accutrade_bid_id ON accutrade_lookups(bid_id)")
        cur.execute("ALTER TABLE accutrade_lookups ADD COLUMN IF NOT EXISTS not_available BOOLEAN DEFAULT FALSE")
        cur.execute("ALTER TABLE accutrade_lookups ADD COLUMN IF NOT EXISTS unavailable_reason TEXT")
        db.commit()
        db.close()
    except Exception:
        pass


_ensure_accutrade_table()
_ensure_trim_select_cache_clean_trim_column()


def _ensure_ipacket_table():
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ipacket_lookups (
                id SERIAL PRIMARY KEY,
                bid_id INTEGER REFERENCES bids(id) ON DELETE CASCADE,
                vin VARCHAR(17) NOT NULL,
                total_msrp INTEGER,
                base_price INTEGER,
                exterior_color TEXT,
                interior_color TEXT,
                screenshot TEXT,
                raw_json JSONB,
                looked_up_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_ipacket_bid_id ON ipacket_lookups(bid_id)")
        cur.execute("ALTER TABLE ipacket_lookups ADD COLUMN IF NOT EXISTS not_available BOOLEAN DEFAULT FALSE")
        cur.execute("ALTER TABLE ipacket_lookups ADD COLUMN IF NOT EXISTS unavailable_reason TEXT")
        db.commit()
        db.close()
    except Exception:
        pass


_ensure_ipacket_table()


# ── AI Assessment: hybrid bucket-weighted baseline + LLM adjustment ──────────
# Rewire path 2026-04-21: replaces single-shot Gemini prompt with
#   classify_bucket → compute_baseline → LLM percentage adjustment → log.
# ai_config stores versioned settings (buckets, weights, caps) so user can
# tune levers in admin UI without redeploying. Only one row has is_active=TRUE.
# ai_assessment_log records every run with bucket + baseline + adjustment +
# final so we can demo "before/after this lever change" to stakeholders.

DEFAULT_AI_CONFIG = {
    # Weights are wholesale-oriented: Manheim MMR (auction wholesale avg),
    # Black Book Wholesale, AccuTrade Target Auction, AccuTrade Instant Offer,
    # and J.D. Power (trade-in/wholesale-leaning). rBook / KBB are
    # retail-leaning so they get small or zero weight for wholesale pricing.
    # Client can tune every weight from the admin UI.
    "buckets": [
        {
            "name": "exotic_collector",
            "display_name": "Exotic / Collector",
            "description": "Ferrari, Lamborghini, McLaren, Rolls-Royce, Bentley, etc.",
            "rules": {
                "makes": ["FERRARI", "LAMBORGHINI", "MCLAREN", "ROLLS-ROYCE", "ROLLS ROYCE",
                          "BENTLEY", "ASTON MARTIN", "MAYBACH", "BUGATTI", "KOENIGSEGG",
                          "PAGANI", "LOTUS"]
            },
            "weights": {
                "mmr": 0.20,
                "black_book": 0.15,
                "accutrade_target_auction": 0.25,
                "accutrade_instant_offer": 0.20,
                "rbook": 0.20
            }
        },
        {
            "name": "highline",
            "display_name": "High-Line",
            "description": "Luxury brands and $40k+ premium vehicles",
            "rules": {
                "makes": ["PORSCHE", "MASERATI", "BMW", "MERCEDES-BENZ", "MERCEDES",
                          "AUDI", "LEXUS", "JAGUAR", "LAND ROVER", "CADILLAC", "TESLA",
                          "GENESIS", "ALFA ROMEO", "ACURA", "INFINITI", "VOLVO", "LINCOLN"],
                "min_asking_price": 40000
            },
            "weights": {
                "mmr": 0.30,
                "black_book": 0.20,
                "accutrade_target_auction": 0.25,
                "accutrade_instant_offer": 0.15,
                "rbook": 0.10
            }
        },
        {
            "name": "mainstream_sub50k",
            "display_name": "Mainstream (< $50k)",
            "description": "Default catch-all: everyday vehicles",
            "rules": {"catch_all": True},
            "weights": {
                "mmr": 0.35,
                "black_book": 0.25,
                "accutrade_target_auction": 0.20,
                "jd_power": 0.10,
                "rbook": 0.10
            }
        }
    ],
    "llm_adjustment_cap_pct": 15,      # max ± percentage LLM can adjust baseline
    "confidence_range_pct": 5,         # ± range shown as confidence (e.g. ±5%)
    "llm_temperature": 0.4,
    "llm_max_tokens": 3000,
    # Dealer-network matching: how "like vehicles" are identified at partner
    # dealers, and how pitch-candidates are scored.
    "dealer_match": {
        "year_tolerance": 2,           # ± years considered "same vehicle"
        "recent_days": 90,             # window for "recent sales" + pattern calc
        "min_sold_confidence": 0.70,   # sold-signal confidence threshold
        "pitch_weights": {
            "sold_count_multiplier":   10,  # weight of historical sales
            "fast_turnover_bonus_max": 30,  # cap on fast-turnover bonus points
            "active_count_multiplier":  2,  # weight of current stock signal
        },
        "max_active":   5,              # UI + prompt caps
        "max_sales":   10,
        "max_pitch":    3,
    }
}


def _ensure_ai_config_table():
    """Same advisory-lock guard as ai_assessment_log — prevents the gunicorn
    boot-time DDL race that deadlocks when N workers all try to ensure
    the same table simultaneously."""
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute("SELECT pg_try_advisory_lock(8127342902) AS got")
        got = cur.fetchone()['got']
        if not got:
            db.close()
            return
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ai_config (
                    id SERIAL PRIMARY KEY,
                    version INTEGER NOT NULL,
                    config JSONB NOT NULL,
                    is_active BOOLEAN DEFAULT FALSE,
                    description TEXT,
                    created_by TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            # Partial unique index: only one row can be active at a time
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_config_active
                ON ai_config(is_active) WHERE is_active = TRUE
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ai_config_version ON ai_config(version DESC)")

            # Seed if empty
            cur.execute("SELECT COUNT(*) as n FROM ai_config")
            row = cur.fetchone()
            n = (row.get('n') if hasattr(row, 'get') else row[0]) if row else 0
            if n == 0:
                cur.execute("""
                    INSERT INTO ai_config (version, config, is_active, description, created_by)
                    VALUES (1, %s, TRUE, 'Initial defaults', 'system')
                """, (json.dumps(DEFAULT_AI_CONFIG),))
            db.commit()
        finally:
            cur.execute("SELECT pg_advisory_unlock(8127342902)")
            db.close()
    except Exception as e:
        print(f'ai_config ensure error: {e}', flush=True)


def _ensure_ai_assessment_log_table():
    """Idempotent table ensure. Wrapped in a Postgres advisory lock so when
    gunicorn boots N workers simultaneously they don't all race on the
    table-creation/alter DDL — first one to grab the lock does the work,
    the rest skip immediately. Without this guard, concurrent
    `CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS` from
    multiple connections deadlocks on AccessExclusiveLock and strands
    in-flight bid POSTs (observed 2026-04-28: bid #351 stalled for 5min
    after a service restart triggered exactly this race).
    """
    try:
        db = get_db()
        cur = db.cursor()
        # Stable arbitrary 32-bit key for this specific ensure operation.
        # pg_try_advisory_lock returns immediately rather than blocking.
        cur.execute("SELECT pg_try_advisory_lock(8127342901) AS got")
        got = cur.fetchone()['got']
        if not got:
            # Another worker is already doing the ensure — skip without
            # touching DDL. The other worker's ensure is idempotent.
            db.close()
            return
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ai_assessment_log (
                    id SERIAL PRIMARY KEY,
                    bid_id INTEGER REFERENCES bids(id) ON DELETE CASCADE,
                    config_version INTEGER,
                    bucket TEXT,
                    bucket_display TEXT,
                    baseline_price INTEGER,
                    breakdown JSONB,
                    llm_adjustment_pct NUMERIC(6,2),
                    llm_reasoning TEXT,
                    confidence_low INTEGER,
                    confidence_high INTEGER,
                    final_price INTEGER,
                    raw_response JSONB,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_aslog_bid
                ON ai_assessment_log(bid_id, created_at DESC)
            """)
            cur.execute("""
                ALTER TABLE ai_assessment_log
                ADD COLUMN IF NOT EXISTS dealer_intel JSONB
            """)
            cur.execute("""
                ALTER TABLE ai_assessment_log
                ADD COLUMN IF NOT EXISTS buyer_intel JSONB
            """)
            cur.execute("""
                ALTER TABLE ai_assessment_log
                ADD COLUMN IF NOT EXISTS market_intel JSONB
            """)
            cur.execute("""
                ALTER TABLE ai_assessment_log
                ADD COLUMN IF NOT EXISTS flags_v2 JSONB
            """)
            db.commit()
        finally:
            cur.execute("SELECT pg_advisory_unlock(8127342901)")
            db.close()
    except Exception as e:
        print(f'ai_assessment_log ensure error: {e}', flush=True)


def get_active_ai_config():
    """Return the active ai_config dict (or DEFAULT_AI_CONFIG + version=0 fallback)."""
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute("SELECT version, config FROM ai_config WHERE is_active=TRUE LIMIT 1")
        row = cur.fetchone()
        db.close()
        if row:
            cfg = row['config'] if hasattr(row, 'get') else row[1]
            ver = row['version'] if hasattr(row, 'get') else row[0]
            # psycopg2 returns JSONB as dict already; fall back to json.loads for TEXT
            if isinstance(cfg, str):
                cfg = json.loads(cfg)
            return ver, cfg
    except Exception as e:
        print(f'get_active_ai_config error: {e}', flush=True)
    return 0, DEFAULT_AI_CONFIG


_ensure_ai_config_table()
_ensure_ai_assessment_log_table()


# ── VDS unknown log (feeds future table extensions for premium brands) ──────
def _ensure_vds_unknown_table():
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS vds_unknown (
                id SERIAL PRIMARY KEY,
                vin VARCHAR(17) UNIQUE NOT NULL,
                wmi VARCHAR(3),
                vds_slice VARCHAR(5),
                year INTEGER,
                make TEXT,
                model TEXT,
                suggested_trim TEXT,
                resolved BOOLEAN DEFAULT FALSE,
                first_seen_at TIMESTAMP DEFAULT NOW(),
                last_seen_at  TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_vds_unknown_wmi ON vds_unknown(wmi, resolved)")
        db.commit()
        db.close()
    except Exception as e:
        print(f'vds_unknown ensure error: {e}', flush=True)


_ensure_vds_unknown_table()


# ── bids.trim_confidence column (added manually as postgres superuser) ────
# expuser doesn't have ALTER on bids. Ran once via:
#   sudo -u postgres psql -d expwholesale -c \
#     "ALTER TABLE bids ADD COLUMN IF NOT EXISTS trim_confidence TEXT DEFAULT 'low';"


# ── Precise VIN decoder wrapper — wraps decode_vin with VDS tables + auto.dev
def decode_vin_precise_wrapper(vin):
    """claude_vin_decoder_live_marker_20260514 — Now calls Claude Sonnet 4.6
    via decode_vin_smart() FIRST. Falls back to existing VDS+NHTSA cascade
    only if Claude returns nothing or low confidence (<0.5).

    trim_confidence is capped at 'high' (never 'deterministic') so downstream
    Carfax/vAuto/AccuTrade consensus can still override when Claude is wrong.
    """
    if not vin or len(vin) != 17:
        return None

    # ── Primary path: Claude Sonnet 4.6 via decode_vin_smart ──
    try:
        from claude_vin_decoder import decode_vin_smart
        _db_c = get_db()
        try:
            result = decode_vin_smart(vin, _db_c, nhtsa_fallback=decode_vin)
        finally:
            _db_c.close()
        if result and float(result.get('confidence') or 0) >= 0.5:
            conf = float(result.get('confidence') or 0)
            trim_conf = 'high' if conf >= 0.85 else ('medium' if conf >= 0.65 else 'low')
            return {
                'vin': vin,
                'year': result.get('year'),
                'make': (result.get('make') or '').upper() or None,
                'model': result.get('model'),
                'trim': result.get('trim'),
                'style': result.get('body_style'),
                'trim_confidence': trim_conf,
                'source': result.get('source') or 'claude_sonnet_4_6',
            }
    except Exception as e:
        print(f'[decode_vin_precise_wrapper] Claude path error: {e}', flush=True)
        # fall through to legacy cascade

    # ── Legacy fallback: existing VDS+NHTSA cascade ──
    try:
        from vin_precise import decode_vin_precise
    except Exception as e:
        print(f'vin_precise import failed: {e}', flush=True)
        b = decode_vin(vin) or {}
        t = b.get('trim')
        return {
            'vin': vin, 'year': b.get('year'), 'make': b.get('make'),
            'model': b.get('model'), 'trim': t, 'style': None,
            'trim_confidence': 'medium' if t else 'low', 'source': 'nhtsa',
        }
    try:
        _db = get_db()
        r = decode_vin_precise(vin, nhtsa_decoder=decode_vin, db_conn=_db)
        _db.close()
        return r
    except Exception as e:
        print(f'decode_vin_precise_wrapper legacy error: {e}', flush=True)
        b = decode_vin(vin) or {}
        return {
            'vin': vin, 'year': b.get('year'), 'make': b.get('make'),
            'model': b.get('model'), 'trim': b.get('trim'), 'style': None,
            'trim_confidence': 'low', 'source': 'error',
        }


# ── AI Levers admin API ─────────────────────────────────────────────────────

def _validate_ai_config(cfg):
    """Validate config dict. Returns (ok, error_msg)."""
    if not isinstance(cfg, dict):
        return False, 'config must be an object'
    buckets = cfg.get('buckets')
    if not isinstance(buckets, list) or not buckets:
        return False, 'buckets must be a non-empty list'
    seen_names = set()
    for i, b in enumerate(buckets):
        if not isinstance(b, dict):
            return False, f'bucket {i} must be an object'
        name = b.get('name')
        if not name or not isinstance(name, str):
            return False, f'bucket {i} missing name'
        if name in seen_names:
            return False, f'duplicate bucket name: {name}'
        seen_names.add(name)
        weights = b.get('weights')
        if not isinstance(weights, dict) or not weights:
            return False, f'bucket "{name}" missing weights'
        total = 0.0
        for k, v in weights.items():
            try:
                fv = float(v)
            except (TypeError, ValueError):
                return False, f'bucket "{name}" weight "{k}" not numeric'
            if fv < 0 or fv > 1:
                return False, f'bucket "{name}" weight "{k}" = {fv}; must be 0.00 – 1.00'
            total += fv
        if abs(total - 1.0) > 0.01:
            return False, f'bucket "{name}" weights sum to {total:.2f}, must be 1.00 (±0.01)'
    # Last bucket must be catch_all
    last = buckets[-1]
    if not (last.get('rules') or {}).get('catch_all'):
        return False, 'last bucket must have rules.catch_all=true'
    cap = cfg.get('llm_adjustment_cap_pct')
    if cap is None or not isinstance(cap, (int, float)) or cap < 0 or cap > 100:
        return False, 'llm_adjustment_cap_pct must be 0 – 100'
    # Optional dealer_match block — validate if present
    dm = cfg.get('dealer_match')
    if dm is not None:
        if not isinstance(dm, dict):
            return False, 'dealer_match must be an object'
        for k, (lo, hi) in {
            'year_tolerance': (0, 10),
            'recent_days':    (1, 730),
        }.items():
            if k in dm:
                try:
                    v = float(dm[k])
                    if v < lo or v > hi:
                        return False, f'dealer_match.{k} must be {lo}–{hi}'
                except (TypeError, ValueError):
                    return False, f'dealer_match.{k} must be numeric'
        if 'min_sold_confidence' in dm:
            try:
                v = float(dm['min_sold_confidence'])
                if v < 0 or v > 1:
                    return False, 'dealer_match.min_sold_confidence must be 0.00–1.00'
            except (TypeError, ValueError):
                return False, 'dealer_match.min_sold_confidence must be numeric'
    # Optional velocity block — validate if present
    vel = cfg.get('velocity')
    if vel is not None:
        if not isinstance(vel, dict):
            return False, 'velocity must be an object'
        for k, (lo, hi) in {
            'lookback_days':    (1, 730),
            'year_tolerance':   (0, 10),
            'mileage_band':     (0, 500000),
            'hot_max_days':     (1, 365),
            'steady_max_days':  (1, 365),
            'slow_max_days':    (1, 365),
            'stale_dol_floor':  (1, 365),
            'min_sample_size':  (1, 100),
        }.items():
            if k in vel:
                try:
                    v = float(vel[k])
                    if v < lo or v > hi:
                        return False, f'velocity.{k} must be {lo}–{hi}'
                except (TypeError, ValueError):
                    return False, f'velocity.{k} must be numeric'
        # Sanity: hot ≤ steady ≤ slow
        h = vel.get('hot_max_days')
        s = vel.get('steady_max_days')
        sl = vel.get('slow_max_days')
        if h is not None and s is not None and float(h) > float(s):
            return False, 'velocity.hot_max_days must be ≤ steady_max_days'
        if s is not None and sl is not None and float(s) > float(sl):
            return False, 'velocity.steady_max_days must be ≤ slow_max_days'
    return True, None


# ─────────────────────────────────────────────────────────────────────────
# Bulk upload — dealer "needs to go" xlsx/csv lists → individual bids
# Built 2026-05-12. See bulk_upload.py for the parser.
# ─────────────────────────────────────────────────────────────────────────

@app.route('/admin/bulk_upload')
def admin_bulk_upload_page():
    """Render the bulk upload preview/commit page."""
    return render_template('bulk_upload.html')


@app.route('/api/admin/bulk_upload/parse', methods=['POST'])
def api_admin_bulk_upload_parse():
    """Parse an uploaded xlsx/csv. Returns the list of candidate rows + a
    duplicate-VIN check against the existing bids table (open bids only).
    Does NOT insert anything."""
    f = request.files.get('file')
    if not f:
        return jsonify({'error': 'file required'}), 400
    try:
        from bulk_upload import parse_upload
        rows = parse_upload(f.filename, f.read())
    except Exception as e:
        return jsonify({'error': f'parse failed: {type(e).__name__}: {e}'}), 400
    if not rows:
        return jsonify({'error': 'no recognizable rows found in file'}), 400

    # Duplicate check: a row is a dupe if a non-cancelled bid with the same
    # VIN exists. Returns the most recent matching bid's id.
    vins = [r['vin'] for r in rows if r.get('vin')]
    dupe_map = {}
    if vins:
        try:
            db = get_db()
            cur = db.cursor()
            cur.execute("""
                SELECT DISTINCT ON (vin) vin, id
                FROM bids
                WHERE vin = ANY(%s)
                  AND COALESCE(status, '') NOT IN ('cancelled', 'rejected')
                ORDER BY vin, id DESC
            """, (vins,))
            for r in cur.fetchall():
                dupe_map[r['vin']] = r['id']
            db.close()
        except Exception as e:
            print(f'[bulk_upload] dupe check failed: {e}', flush=True)
    for r in rows:
        if r.get('vin') and r['vin'] in dupe_map:
            r['duplicate_of'] = dupe_map[r['vin']]
    return jsonify({'filename': f.filename, 'rows': rows})


def _stagger_kick_market_check(bid_ids_vins, delay_seconds):
    """Fire trigger_market_check for each (bid_id, vin) tuple with a stagger.
    Spawned as a single daemon thread that sleeps between kicks. The first
    bid fires immediately; each subsequent bid waits `delay_seconds`."""
    import threading
    import time as _t
    def _run():
        for i, (bid_id, vin) in enumerate(bid_ids_vins):
            if i > 0:
                _t.sleep(delay_seconds)
            try:
                trigger_market_check(bid_id, vin)
                print(f'[bulk_upload] staggered kick bid={bid_id} '
                      f'vin={vin} idx={i}/{len(bid_ids_vins)}', flush=True)
            except Exception as e:
                print(f'[bulk_upload] kick failed bid={bid_id}: {e}',
                      flush=True)
    threading.Thread(target=_run, daemon=True,
                     name='bulk-upload-stagger').start()


@app.route('/api/admin/bulk_upload/commit', methods=['POST'])
def api_admin_bulk_upload_commit():
    """Commit a confirmed set of bid candidates. Body:
        {rows: [{vin, raw_vehicle, year, make, model, trim, mileage,
                 asking_price, color, body, notes, stock, ...}, ...],
         delay_seconds: 5,
         source_name: "Bob @ ABC Motors"}

    Inserts all bids immediately (single transaction), creates a
    bulk_uploads row, then spawns one stagger thread that fires
    trigger_market_check per bid with the requested delay.
    """
    data = request.get_json(silent=True) or {}
    rows = data.get('rows') or []
    delay_seconds = data.get('delay_seconds')
    try:
        delay_seconds = int(delay_seconds) if delay_seconds is not None else 5
    except (TypeError, ValueError):
        delay_seconds = 5
    delay_seconds = max(0, min(60, delay_seconds))
    source_name = (data.get('source_name') or '').strip()[:200]

    if not source_name:
        return jsonify({'error': 'source_name required (type the dealer or contact who sent the list)'}), 400
    if not isinstance(rows, list) or not rows:
        return jsonify({'error': 'rows array required'}), 400

    # Filter to rows we'll actually insert (valid 17-char VIN, no skip flag).
    keep = []
    for r in rows:
        vin = (r.get('vin') or '').strip().upper()
        if len(vin) != 17:
            continue
        keep.append({**r, 'vin': vin})
    if not keep:
        return jsonify({'error': 'no rows with valid VINs'}), 400

    # Resolve a contact for the batch. We slug the source name into a
    # phone-key like 'bulk:bob_abc_motors' so we can find the same contact
    # next time the same dealer sends a list. If no source, use a generic.
    if source_name:
        import re as _re
        slug = _re.sub(r'[^a-z0-9]+', '_',
                       source_name.lower()).strip('_')[:60] or 'unnamed'
        contact_phone = f'bulk:{slug}'
        contact_name = source_name
    else:
        contact_phone = 'bulk:unnamed'
        contact_name = 'Bulk Upload'

    client_ip = (request.headers.get('X-Forwarded-For', '').split(',')[0].strip()
                 or request.remote_addr or '')

    db = get_db()
    cur = db.cursor()
    try:
        # Upsert the contact
        cur.execute("""
            INSERT INTO contacts (phone, name)
            VALUES (%s, %s)
            ON CONFLICT (phone) DO UPDATE SET name = EXCLUDED.name
            RETURNING id
        """, (contact_phone, contact_name))
        contact_id = cur.fetchone()['id']

        # Create the bulk_uploads grouping row first so each bid links back.
        cur.execute("""
            INSERT INTO bulk_uploads
                (uploaded_by, contact_id, source_name, row_count, delay_seconds)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
        """, (session.get('username') or 'admin', contact_id, source_name,
              len(rows), delay_seconds))
        bulk_upload_id = cur.fetchone()['id']

        created = []
        for r in keep:
            year     = r.get('year')
            make     = (r.get('make') or '').strip()[:60]
            model    = (r.get('model') or '').strip()[:80]
            trim     = (r.get('trim') or '').strip()[:120]
            color    = (r.get('color') or '').strip()[:80]
            body     = (r.get('body') or '').strip()[:80]
            mileage  = r.get('mileage')
            asking   = r.get('asking_price')
            stock    = (r.get('stock') or '').strip()[:40]
            notes_in = (r.get('notes') or '').strip()
            raw_veh  = (r.get('raw_vehicle') or '').strip()

            # Notes field: combine the dealer's note, any stock #, and a
            # bulk-upload header so it's obvious in bid view where this
            # came from.
            note_parts = [f'[Bulk Upload: {source_name or "unnamed"}]']
            if stock:
                note_parts.append(f'Stock #{stock}')
            if body:
                note_parts.append(f'Body: {body}')
            if notes_in:
                note_parts.append(f'Dealer notes: {notes_in}')
            full_notes = ' — '.join(note_parts)

            # Raw message — mimic the quick_drop format so AI prompts and
            # bid_detail render correctly.
            rm_parts = ['[BULK UPLOAD]']
            rm_parts.append(f'VIN: {r["vin"]}')
            if raw_veh:
                rm_parts.append(raw_veh)
            elif year and make and model:
                rm_parts.append(f'{year} {make} {model}')
                if trim:
                    rm_parts.append(trim)
            if mileage:
                rm_parts.append(f'{int(mileage):,} mi')
            raw_message = ' | '.join(rm_parts)

            cur.execute("""
                INSERT INTO bids
                    (contact_id, phone, vin, year, make, model, trim,
                     mileage, color, raw_message, asking_price, notes,
                     status, creation_ip, creation_source, bulk_upload_id,
                     vauto_priority)
                VALUES (%s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        'new', %s, 'bulk_upload', %s,
                        TRUE)
                RETURNING id
            """, (
                contact_id, contact_phone, r['vin'],
                year if year else None,
                make or None, model or None, trim or None,
                int(mileage) if mileage else None,
                color or None, raw_message,
                int(asking) if asking else None, full_notes,
                client_ip, bulk_upload_id,
            ))
            new_bid_id = cur.fetchone()['id']
            created.append((new_bid_id, r['vin']))

        # Update the rollup counts
        cur.execute("""
            UPDATE bulk_uploads
            SET created_count = %s,
                skipped_count = %s
            WHERE id = %s
        """, (len(created), len(rows) - len(created), bulk_upload_id))
        db.commit()
    except Exception as e:
        db.rollback()
        db.close()
        return jsonify({'error': f'commit failed: {type(e).__name__}: {e}'}), 500
    finally:
        try: db.close()
        except Exception: pass

    # Now stagger the enrichment kicks. Single daemon thread sleeps between
    # bids — no thread-per-bid storm.
    _stagger_kick_market_check(created, delay_seconds)

    return jsonify({
        'ok': True,
        'bulk_upload_id': bulk_upload_id,
        'created': [{'bid_id': bid_id, 'vin': vin} for bid_id, vin in created],
        'skipped': len(rows) - len(created),
        'delay_seconds': delay_seconds,
    })


# ─────────────────────────────────────────────────────────────────────────
# thalist.com scraper -> EW bid intake
# Built 2026-05-12. Scraper runs on a Windows VM, fires this 4x/day with
# one JSON payload per Wholesale Inventory post. We dedupe by VIN against
# open bids — new posts spawn a bid (creation_source='thalist'), repeats
# just refresh the ledger row's last_seen_at.
# ─────────────────────────────────────────────────────────────────────────

THALIST_SECRET = os.environ.get(
    'EW_THALIST_SECRET',
    'f1pxdtE9UkMMXslbCI3raenTIOEZ3rIJ4-FaSGt1Iqw')  # default for first deploy
THALIST_ALERT_PHONE = '+14074309675'  # operator's phone for new-post SMS

_THALIST_MAKE_ID_TO_NAME = {
    # Harvested from thalist_posts data 2026-05-13. New IDs auto-discovered
    # via title-parse fallback in api_thalist_post (committed a0f475f) AND
    # the nightly thalist_make_sweep job which backfills any bid that landed
    # with make=NULL.
    4:  'Bentley',
    5:  'BMW',
    11: 'Ferrari',
    17: 'Land Rover',
    18: 'Lexus',
    22: 'Mercedes-Benz',
    32: 'Rolls-Royce',
}


def _thalist_resolve_make(make_id):
    if not make_id:
        return None
    return _THALIST_MAKE_ID_TO_NAME.get(int(make_id))


def _thalist_download_photo(remote_url: str) -> str | None:
    """Pull a thalist blob photo to local /static/uploads/ and return its
    local URL.

    thalist serves blob.core.windows.net images with Content-Type:
    application/octet-stream, which Gemini's vision API refuses with
    "Provided image is not valid." Saving the bytes locally and serving
    them under our own /static/uploads/ (where nginx sets a real
    image/jpeg content type) makes the whole rest of the EW pipeline —
    Gemini, the dashboard thumbnails, the bid view — work normally.

    Returns the local /static/uploads/<uuid>.<ext> URL, or None on
    failure.
    """
    import uuid as _uuid
    try:
        r = requests.get(remote_url, timeout=20, stream=False)
        if r.status_code != 200 or not r.content:
            return None
        data = r.content
        # Sniff extension from magic bytes — Azure blob doesn't tell us.
        if data[:3] == b'\xff\xd8\xff':
            ext = '.jpg'
        elif data[:8] == b'\x89PNG\r\n\x1a\n':
            ext = '.png'
        elif data[:4] == b'RIFF' and data[8:12] == b'WEBP':
            ext = '.webp'
        elif data[:6] in (b'GIF87a', b'GIF89a'):
            ext = '.gif'
        elif data[:4] == b'%PDF':
            return None  # skip PDFs etc. — only photos belong on bids
        else:
            # Unknown signature — assume jpeg, EW dashboard will render it
            # if it's actually a real image of any common format.
            ext = '.jpg'
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        fname = f'thalist_{_uuid.uuid4().hex}{ext}'
        fpath = os.path.join(UPLOAD_DIR, fname)
        with open(fpath, 'wb') as fp:
            fp.write(data)
        return f'/static/uploads/{fname}'
    except Exception as e:
        print(f'[thalist] photo download failed {remote_url}: {e}',
              flush=True)
        return None


def _cleanup_invalid_thalist_bids(min_age_minutes=10) -> dict:
    """Delete thalist-sourced bids whose VIN is structurally invalid.

    Earlier version of this rule was too aggressive: it deleted any
    bid where AccuTrade returned no values. That killed bid 1219
    (Ferrari Roma Spider — valid VIN, AccuTrade just doesn't carry
    pricing for that model). New rule uses VIN check digit as the
    primary signal — only typos (W1NYC6BJ5NX458393 etc.) fail the
    digit check, while exotics that AccuTrade can't price still pass
    and get kept on the dashboard.

    Delete when:
      - AccuTrade explicitly says not_available=TRUE, OR
      - VIN check digit fails AND AccuTrade returned no values.

    Both gates require at least min_age_minutes since the AccuTrade
    lookup — gives the worker a margin to finalize late writes.
    """
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("""
            SELECT b.id AS bid_id,
                   b.vin,
                   tp.id AS ledger_id,
                   tp.post_id,
                   al.not_available,
                   al.unavailable_reason,
                   al.guaranteed_offer,
                   al.trade_in,
                   al.market_avg
            FROM bids b
            JOIN thalist_posts tp ON tp.bid_id = b.id
            JOIN accutrade_lookups al ON al.bid_id = b.id
            WHERE b.creation_source = 'thalist'
              AND tp.invalidated_at IS NULL
              AND al.looked_up_at IS NOT NULL
              AND al.looked_up_at < NOW() - (%s || ' minutes')::interval
        """, (str(min_age_minutes),))
        candidates = list(cur.fetchall())

        victims = []
        for c in candidates:
            no_accutrade_price = (c['guaranteed_offer'] is None
                                  and c['trade_in'] is None
                                  and c['market_avg'] is None)
            # Treat AccuTrade's explicit "not available" as authoritative.
            if c['not_available']:
                victims.append(c)
                continue
            # Otherwise require BOTH a VIN check-digit failure AND no
            # AccuTrade price. check-digit failure alone is enough to
            # confirm the seller typo'd; we still gate on no_price to
            # avoid deleting a bid that AccuTrade was able to value
            # despite a malformed VIN (extremely unlikely but cheap to
            # guard against).
            if no_accutrade_price and c['vin'] \
                    and not vin_check_digit_valid(c['vin']):
                victims.append(c)

        for v in victims:
            reason = (v['unavailable_reason']
                      or 'AccuTrade returned no values (likely invalid VIN)')
            cur.execute("""
                UPDATE thalist_posts
                   SET invalidated_at = NOW(),
                       invalidate_reason = %s,
                       bid_id = NULL
                 WHERE id = %s
            """, (reason, v['ledger_id']))
            # DELETE the bid. CASCADE clears bid_photos, vauto_lookups,
            # accutrade_lookups, ipacket_lookups, ai_assessment_log, etc.
            cur.execute("DELETE FROM bids WHERE id = %s", (v['bid_id'],))
            print(f'[thalist-cleanup] deleted bid #{v["bid_id"]} '
                  f'vin={v["vin"]} post={v["post_id"]} reason="{reason}"',
                  flush=True)
        db.commit()
        return {'deleted': len(victims),
                'victims': [{'bid_id': v['bid_id'],
                             'vin': v['vin'],
                             'post_id': v['post_id']}
                            for v in victims]}
    except Exception as e:
        db.rollback()
        print(f'[thalist-cleanup] error: {e}', flush=True)
        return {'deleted': 0, 'error': f'{type(e).__name__}: {e}'}
    finally:
        try: db.close()
        except Exception: pass


@app.route('/api/thalist/cleanup', methods=['POST'])
def api_thalist_cleanup():
    """Sweep junk thalist bids. Auth: same X-Auth shared secret as
    /api/thalist/post. Called by the scraper at the end of each run."""
    auth = (request.headers.get('X-Auth') or '').strip()
    if not THALIST_SECRET or auth != THALIST_SECRET:
        return jsonify({'error': 'bad auth'}), 401
    result = _cleanup_invalid_thalist_bids()
    return jsonify({'ok': True, **result})


@app.route('/api/thalist/post', methods=['POST'])
def api_thalist_post():
    """Receive one scraped thalist.com Wholesale Inventory post.

    Auth: header X-Auth must match env EW_THALIST_SECRET.
    Body (JSON, all optional except detail_url + post_id):
        {
          post_id:           -304074,
          detail_url:        "https://www.thalist.com/auth/posts/carpost-details/-304074",
          vin:               "JTJTBCDX8T5081902",
          title:             "2026 Lexus GX550 Premium Plus",
          year:              2026, make_id: 18,
          model:             "GX550 Premium Plus",
          asking_price:      86500,
          mileage:            2997,
          location_zip:      "55364",
          description:       "RARE ATOMIC SILVER ...",
          teaser:            "PRICE RECUCED $1000 FOR IMMEDIATE SALE!",
          title_holder:      "LEXUS FS",
          poster_name:       "Billy Ward",
          poster_company:    "Luxury & Exotic Inc.",
          poster_company_id: 1180,
          photos:            ["https://...", "..."]
        }

    Behavior:
      - Upsert thalist_posts row (unique on post_id). On INSERT, run the
        dedupe + bid-create flow. On UPDATE, just bump last_seen_at and
        return 'already_seen'.
      - Dedupe: skip bid creation if an open bid with same VIN exists.
        Record the matching bid in dedupe_target_bid_id and return.
      - On bid creation: fire Telegram + SMS alert, kick canonicalization
        + market check just like a quick_drop.

    Response: {ok, status: 'new'|'dupe'|'already_seen', bid_id, ...}
    """
    auth = (request.headers.get('X-Auth') or '').strip()
    if not THALIST_SECRET or auth != THALIST_SECRET:
        return jsonify({'error': 'bad auth'}), 401

    data = request.get_json(silent=True) or {}
    post_id = data.get('post_id')
    detail_url = (data.get('detail_url') or '').strip()
    if not post_id or not detail_url:
        return jsonify({'error': 'post_id + detail_url required'}), 400
    try:
        post_id = int(post_id)
    except (TypeError, ValueError):
        return jsonify({'error': 'post_id must be int'}), 400

    vin = (data.get('vin') or '').strip().upper() or None
    if vin and len(vin) != 17:
        vin = None  # malformed

    # Decompose for bid-creation path
    year         = data.get('year')
    make_id      = data.get('make_id')
    make_name    = _thalist_resolve_make(make_id)
    model        = (data.get('model') or '').strip() or None
    # Fallback: parse make from the post title when we don't know the
    # make_id mapping. The scraper's structured make_id field is the
    # primary signal; for VIN-bearing posts NHTSA fills canon_make in
    # the canonicalize pipeline. But VIN-less broker listings need this
    # parse so the dashboard isn't blank. Format: "<year> <make> <rest>".
    if not make_name:
        title_raw = (data.get('title') or '').strip()
        tm = re.match(r'^\s*(?:19|20)\d{2}\s+'
                      r'(Mercedes[-\s]Benz|Aston[-\s]Martin|Land[-\s]Rover|'
                      r'Rolls[-\s]Royce|Alfa[-\s]Romeo|\S+)',
                      title_raw, re.IGNORECASE)
        if tm:
            make_name = tm.group(1).strip()
    if not make_name:
        make_name = 'Unknown'
    asking_price = data.get('asking_price')
    mileage      = data.get('mileage')
    title        = (data.get('title') or '').strip() or None
    description  = (data.get('description') or '').strip() or None
    teaser       = (data.get('teaser') or '').strip() or None
    poster_name  = (data.get('poster_name') or '').strip() or None
    poster_company = (data.get('poster_company') or '').strip() or None
    poster_company_id = data.get('poster_company_id')
    location_zip = (data.get('location_zip') or '').strip() or None
    photos       = data.get('photos') or []
    # Post type — 'WI' (Wholesale Inventory) or 'BL' (Broker Listing).
    # Stored on thalist_posts for later filtering/audit. Bids still share
    # one creation_source='thalist' so the dashboard badge stays uniform.
    post_type_code = (data.get('post_type_code') or '').strip().upper() or None
    post_type_name = (data.get('post_type_name') or '').strip() or None
    if post_type_code and post_type_code not in ('WI', 'BL'):
        post_type_code = None  # ignore anything unexpected from the scraper

    db = get_db()
    cur = db.cursor()
    try:
        # UPSERT ledger row first
        cur.execute("""
            INSERT INTO thalist_posts
                (post_id, vin, title, year, make_id, model, asking_price,
                 mileage, location_zip, description, teaser, title_holder,
                 poster_name, poster_company, poster_company_id, photos,
                 detail_url, raw_payload, post_type_code, post_type_name,
                 first_seen_at, last_seen_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s::jsonb,
                    %s, %s::jsonb, %s, %s,
                    NOW(), NOW())
            ON CONFLICT (post_id) DO UPDATE
              SET last_seen_at = NOW(),
                  asking_price = COALESCE(EXCLUDED.asking_price,
                                          thalist_posts.asking_price),
                  raw_payload = EXCLUDED.raw_payload,
                  post_type_code = COALESCE(EXCLUDED.post_type_code,
                                            thalist_posts.post_type_code),
                  post_type_name = COALESCE(EXCLUDED.post_type_name,
                                            thalist_posts.post_type_name)
            RETURNING id, bid_id, dedupe_target_bid_id,
                      (xmax = 0) AS is_insert
        """, (
            post_id, vin, title, year, make_id, model, asking_price,
            mileage, location_zip, description, teaser,
            (data.get('title_holder') or '').strip() or None,
            poster_name, poster_company, poster_company_id,
            json.dumps(photos) if photos else None,
            detail_url, json.dumps(data),
            post_type_code, post_type_name,
        ))
        row = cur.fetchone()
        ledger_id = row['id']
        existing_bid = row['bid_id']
        existing_dupe = row['dedupe_target_bid_id']
        is_insert = bool(row['is_insert'])

        # Repeat scrape — nothing to do
        if not is_insert:
            db.commit()
            return jsonify({
                'ok': True, 'status': 'already_seen',
                'ledger_id': ledger_id,
                'bid_id': existing_bid,
                'dedupe_target_bid_id': existing_dupe,
            })

        # First-time post. If VIN matches an open bid, record as dupe — no
        # new bid + no alert.
        if vin:
            cur.execute("""
                SELECT id FROM bids
                WHERE vin = %s
                  AND COALESCE(status,'') NOT IN ('cancelled', 'rejected')
                ORDER BY id DESC LIMIT 1
            """, (vin,))
            dupe = cur.fetchone()
            if dupe:
                cur.execute("""
                    UPDATE thalist_posts SET dedupe_target_bid_id = %s
                    WHERE id = %s
                """, (dupe['id'], ledger_id))
                db.commit()
                print(f'[thalist] post {post_id} VIN {vin} dedupe -> '
                      f'bid #{dupe["id"]}', flush=True)
                return jsonify({
                    'ok': True, 'status': 'dupe',
                    'ledger_id': ledger_id,
                    'dedupe_target_bid_id': dupe['id'],
                })

        # Create contact for the poster (keyed by their company on thalist
        # so repeat posts from the same dealer roll up into one contact).
        contact_phone = f'thalist:{poster_company_id or "0"}'
        contact_name = poster_name or poster_company or 'Thalist Post'
        cur.execute("""
            INSERT INTO contacts (phone, name, company)
            VALUES (%s, %s, %s)
            ON CONFLICT (phone) DO UPDATE SET
                name    = COALESCE(EXCLUDED.name, contacts.name),
                company = COALESCE(EXCLUDED.company, contacts.company)
            RETURNING id
        """, (contact_phone, contact_name, poster_company))
        contact_id = cur.fetchone()['id']

        # Build the bid's notes + raw_message in EW's house style
        type_label = post_type_name or (
            'Broker Listing' if post_type_code == 'BL'
            else 'Wholesale Inventory' if post_type_code == 'WI'
            else 'Post')
        note_parts = [f'[Thalist {type_label}: '
                      f'{poster_name or poster_company or "?"}]']
        if poster_company and poster_company != poster_name:
            note_parts.append(poster_company)
        if location_zip:
            note_parts.append(f'ZIP {location_zip}')
        if teaser:
            note_parts.append(f'Teaser: {teaser}')
        if description:
            note_parts.append(description)
        note_parts.append(f'thalist post: {detail_url}')
        full_notes = ' — '.join(note_parts)

        rm_parts = ['[THALIST]']
        if vin: rm_parts.append(f'VIN: {vin}')
        if title: rm_parts.append(title)
        elif year and make_name and model:
            rm_parts.append(f'{year} {make_name} {model}')
        if mileage: rm_parts.append(f'{int(mileage):,} mi')
        if asking_price: rm_parts.append(f'${int(asking_price):,}')
        raw_message = ' | '.join(rm_parts)

        cur.execute("""
            INSERT INTO bids
                (contact_id, phone, vin, year, make, model,
                 mileage, raw_message, asking_price, notes,
                 status, creation_source, vauto_priority)
            VALUES (%s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    'new', 'thalist', TRUE)
            RETURNING id
        """, (
            contact_id, contact_phone, vin,
            year if year else None,
            make_name if make_name and make_name != 'Unknown' else None,
            model,
            int(mileage) if mileage else None,
            raw_message,
            int(asking_price) if asking_price else None,
            full_notes,
        ))
        new_bid_id = cur.fetchone()['id']

        # Save photos on the bid. thalist's blob CDN serves images with
        # Content-Type: application/octet-stream, which Gemini's vision
        # endpoint rejects with "Provided image is not valid." We pull
        # each photo down, save it under /static/uploads/ (where nginx
        # serves a real image/jpeg content type), and store the local URL.
        for purl in (photos or [])[:20]:
            if not purl or not isinstance(purl, str):
                continue
            if 'blob.core.windows.net/images/' not in purl:
                continue
            local_url = _thalist_download_photo(purl)
            if not local_url:
                continue
            try:
                cur.execute(
                    "INSERT INTO bid_photos (bid_id, url) VALUES (%s, %s)",
                    (new_bid_id, local_url))
            except Exception:
                pass

        # Link ledger row to the new bid
        cur.execute("""
            UPDATE thalist_posts SET bid_id = %s WHERE id = %s
        """, (new_bid_id, ledger_id))

        db.commit()
    except Exception as e:
        db.rollback()
        db.close()
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500
    finally:
        try: db.close()
        except Exception: pass

    # Fire alerts + downstream enrichment.
    # quiet=True from the scraper (first-run backfill) suppresses the
    # Telegram + SMS so the operator doesn't get text-bombed when the
    # scraper first comes online and finds N already-active cards.
    quiet = bool(data.get('quiet'))
    if quiet:
        print(f'[thalist] quiet=true — skipping alerts for bid #{new_bid_id}',
              flush=True)
    else:
        try:
            title_str = title or f'{year or ""} {make_name or ""} {model or ""}'.strip()
            price_str = f'${int(asking_price):,}' if asking_price else 'no price'
            miles_str = f'{int(mileage):,} mi' if mileage else '? mi'
            type_label = post_type_name or (
                'Broker Listing' if post_type_code == 'BL'
                else 'Wholesale Inventory' if post_type_code == 'WI'
                else 'Post')
            msg_text = (
                f'🚗 New Thalist {type_label} → bid #{new_bid_id}\n'
                f'{title_str}\n'
                f'VIN: {vin or "(none)"}\n'
                f'{price_str} · {miles_str}\n'
                f'by {poster_name or "?"} ({poster_company or "?"})\n'
                f'{detail_url}'
            )
            msg_html = (
                f'<b>🚗 New Thalist {type_label}</b> → bid #<b>{new_bid_id}</b>\n'
                f'{title_str}\n'
                f'VIN: <code>{vin or "(none)"}</code>\n'
                f'{price_str} · {miles_str}\n'
                f'by {poster_name or "?"} ({poster_company or "?"})\n'
                f'<a href="{detail_url}">view on thalist</a>'
            )
            _tg_worker_alert(msg_html)
            send_sms(THALIST_ALERT_PHONE, msg_text[:1200])
        except Exception as e:
            print(f'[thalist] alert error: {e}', flush=True)

    try:
        if vin:
            trigger_market_check(new_bid_id, vin)
    except Exception as e:
        print(f'[thalist] market_check kick failed: {e}', flush=True)

    return jsonify({
        'ok': True, 'status': 'new',
        'ledger_id': ledger_id,
        'bid_id': new_bid_id,
        'vin': vin,
    })


# ─────────────────────────────────────────────────────────────────────────
# DealerClub live-auction integration
# Built 2026-05-12. The scraper (Contabo 1 daemon) polls
# POST api.dealerclub.com/auctions/marketplace/active/ every 30s when
# anything is live, every 5min when idle. For each row it POSTs here.
# We upsert the dealerclub_lots ledger (UNIQUE external_id). First-seen
# creates an EW bid; subsequent polls just refresh current_price /
# end_time / bid_count / status for the live dashboard tiles.
# ─────────────────────────────────────────────────────────────────────────

DEALERCLUB_SECRET = os.environ.get(
    'EW_DEALERCLUB_SECRET',
    'Uu11t87Ki1nrvMEddMX2kHOrfkd_bI4o-iGa5Jsu6yg')  # default for first deploy
DEALERCLUB_ALERT_PHONE = '+14074309675'

# DealerClub serves vehicle photos via imagekit.io / s3-accelerate hosts.
# Both accept anonymous GET (no signed URL needed for thumbnail_url), so
# we can download them locally for Gemini just like the thalist path.
def _dealerclub_download_photo(remote_url: str) -> str | None:
    import uuid as _uuid
    try:
        r = requests.get(remote_url, timeout=20)
        if r.status_code != 200 or not r.content:
            return None
        data = r.content
        if data[:3] == b'\xff\xd8\xff':
            ext = '.jpg'
        elif data[:8] == b'\x89PNG\r\n\x1a\n':
            ext = '.png'
        elif data[:4] == b'RIFF' and data[8:12] == b'WEBP':
            ext = '.webp'
        elif data[:6] in (b'GIF87a', b'GIF89a'):
            ext = '.gif'
        else:
            ext = '.jpg'
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        fname = f'dealerclub_{_uuid.uuid4().hex}{ext}'
        fpath = os.path.join(UPLOAD_DIR, fname)
        with open(fpath, 'wb') as fp:
            fp.write(data)
        return f'/static/uploads/{fname}'
    except Exception as e:
        print(f'[dealerclub] photo download failed {remote_url}: {e}',
              flush=True)
        return None


@app.route('/api/dealerclub/lot', methods=['POST'])
def api_dealerclub_lot():
    """Receive one DealerClub active-auction row.

    Auth: X-Auth header must match EW_DEALERCLUB_SECRET.

    Body (JSON): normalized lot dict from dealerclub_scraper.normalize_lot.
    Always required: external_id + detail_url. VIN may be missing for
    salvage / dealer-block lots and that's fine — bid still gets created.

    Behavior:
      - UPSERT dealerclub_lots on external_id. is_insert distinguishes
        first-seen (create bid) from refresh (just update price/timer).
      - First-seen: dedupe by VIN against open bids; if dupe, skip bid
        creation and just record the dupe target. Otherwise create a bid
        with creation_source='dealerclub', kick canon + market check.
      - On refresh: bump current_price / end_time / bid_count /
        reserve_met / status. If status flipped from active → ended,
        stamp closed_at + close_reason for downstream cleanup.

    Response: {ok, status: 'new'|'updated'|'dupe', lot_id, bid_id?, ...}
    """
    auth = (request.headers.get('X-Auth') or '').strip()
    if not DEALERCLUB_SECRET or auth != DEALERCLUB_SECRET:
        return jsonify({'error': 'bad auth'}), 401

    data = request.get_json(silent=True) or {}
    external_id = (data.get('external_id') or '').strip()
    detail_url = (data.get('detail_url') or '').strip()
    if not external_id or not detail_url:
        return jsonify({'error': 'external_id + detail_url required'}), 400

    vin = (data.get('vin') or '').strip().upper() or None
    if vin and len(vin) != 17:
        vin = None

    year = data.get('year')
    make = (data.get('make') or '').strip() or None
    model = (data.get('model') or '').strip() or None
    trim = (data.get('trim') or '').strip() or None
    odometer = data.get('odometer')
    current_price = data.get('current_price')
    high_bid = data.get('high_bid')
    bid_count = data.get('bid_count') or 0
    unique_bidder_count = data.get('unique_bidder_count') or 0
    end_time = data.get('end_time')
    duration = data.get('duration_in_minutes')
    reserve_met = bool(data.get('reserve_met'))
    is_no_reserve = bool(data.get('is_no_reserve'))
    reserve_price = data.get('reserve_price')
    reserve_color = (data.get('reserve_progress_color') or '').strip() or None
    status = (data.get('status') or '').strip() or None
    featured_image_url = (data.get('featured_image_url') or '').strip() or None
    drivetrain = (data.get('drivetrain') or '').strip() or None

    # Transport-quote fields from the scraper (DealerClub /transportation/quote/)
    transport_price    = data.get('transport_price')
    transport_mileage  = data.get('transport_mileage')
    transport_eta_min  = data.get('transport_eta_min')
    transport_eta_max  = data.get('transport_eta_max')
    transport_enclosed = data.get('transport_enclosed')

    db = get_db()
    cur = db.cursor()
    try:
        # UPSERT the ledger row
        cur.execute("""
            INSERT INTO dealerclub_lots
                (external_id, vin, year, make, model, trim, odometer,
                 drivetrain, current_price, high_bid, bid_count,
                 unique_bidder_count, end_time, duration_in_minutes,
                 reserve_met, is_no_reserve, reserve_price,
                 reserve_progress_color, status, featured_image_url,
                 detail_url, raw_payload,
                 estimated_transport, transport_mileage,
                 transport_eta_min, transport_eta_max, transport_enclosed,
                 first_seen_at, last_seen_at, last_polled_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s::jsonb,
                    %s, %s, %s, %s, %s,
                    NOW(), NOW(), NOW())
            ON CONFLICT (external_id) DO UPDATE SET
                current_price          = EXCLUDED.current_price,
                high_bid               = EXCLUDED.high_bid,
                bid_count              = EXCLUDED.bid_count,
                unique_bidder_count    = EXCLUDED.unique_bidder_count,
                end_time               = EXCLUDED.end_time,
                reserve_met            = EXCLUDED.reserve_met,
                reserve_progress_color = EXCLUDED.reserve_progress_color,
                reserve_price          = EXCLUDED.reserve_price,
                status                 = EXCLUDED.status,
                last_seen_at           = NOW(),
                last_polled_at         = NOW(),
                raw_payload            = EXCLUDED.raw_payload,
                closed_at = CASE
                    WHEN dealerclub_lots.closed_at IS NOT NULL THEN
                        dealerclub_lots.closed_at
                    WHEN EXCLUDED.status IS NOT NULL
                         AND EXCLUDED.status != 'active' THEN NOW()
                    ELSE NULL END,
                close_reason = CASE
                    WHEN dealerclub_lots.close_reason IS NOT NULL THEN
                        dealerclub_lots.close_reason
                    WHEN EXCLUDED.status IS NOT NULL
                         AND EXCLUDED.status != 'active' THEN EXCLUDED.status
                    ELSE NULL END,
                estimated_transport = COALESCE(
                    EXCLUDED.estimated_transport,
                    dealerclub_lots.estimated_transport),
                transport_mileage = COALESCE(
                    EXCLUDED.transport_mileage,
                    dealerclub_lots.transport_mileage),
                transport_eta_min = COALESCE(
                    EXCLUDED.transport_eta_min,
                    dealerclub_lots.transport_eta_min),
                transport_eta_max = COALESCE(
                    EXCLUDED.transport_eta_max,
                    dealerclub_lots.transport_eta_max),
                transport_enclosed = COALESCE(
                    EXCLUDED.transport_enclosed,
                    dealerclub_lots.transport_enclosed)
            RETURNING id, bid_id, (xmax = 0) AS is_insert
        """, (
            external_id, vin, year, make, model, trim, odometer,
            drivetrain, current_price, high_bid, bid_count,
            unique_bidder_count, end_time, duration,
            reserve_met, is_no_reserve, reserve_price,
            reserve_color, status, featured_image_url,
            detail_url, json.dumps(data),
            transport_price, transport_mileage,
            transport_eta_min, transport_eta_max, transport_enclosed,
        ))
        row = cur.fetchone()
        lot_id = row['id']
        existing_bid_id = row['bid_id']
        is_insert = bool(row['is_insert'])

        if not is_insert:
            db.commit()
            return jsonify({
                'ok': True, 'status': 'updated',
                'lot_id': lot_id,
                'bid_id': existing_bid_id,
                'current_price': current_price,
            })

        # First-seen — dedupe by VIN against open bids
        dupe_target = None
        if vin:
            cur.execute("""
                SELECT id FROM bids
                WHERE vin = %s
                  AND COALESCE(status,'') NOT IN ('cancelled', 'rejected')
                ORDER BY id DESC LIMIT 1
            """, (vin,))
            d = cur.fetchone()
            if d:
                dupe_target = d['id']
                cur.execute("""
                    UPDATE dealerclub_lots SET bid_id = NULL
                    WHERE id = %s
                """, (lot_id,))
                db.commit()
                print(f'[dealerclub] lot {external_id} VIN {vin} dupe of '
                      f'bid #{dupe_target}', flush=True)
                return jsonify({
                    'ok': True, 'status': 'dupe',
                    'lot_id': lot_id,
                    'dedupe_target_bid_id': dupe_target,
                })

        # Build the bid's notes + raw_message
        end_str = (end_time or '')[:19].replace('T', ' ') + ' UTC'
        rsv_str = ('no reserve' if is_no_reserve
                   else f'reserve {"met" if reserve_met else "not met"}')
        note_parts = [f'[DealerClub {external_id}]',
                      f'Auction ends {end_str}',
                      rsv_str,
                      f'Current bid ${(current_price or 0):,}',
                      f'{bid_count} bids']
        if odometer:
            note_parts.append(f'{int(odometer):,} mi')
        note_parts.append(f'View: {detail_url}')
        full_notes = ' — '.join(note_parts)

        rm_parts = ['[DEALERCLUB]', f'Lot: {external_id}']
        if vin: rm_parts.append(f'VIN: {vin}')
        if year and make and model:
            ymm = f'{year} {make} {model}'
            if trim:
                ymm += f' {trim}'
            rm_parts.append(ymm)
        if odometer: rm_parts.append(f'{int(odometer):,} mi')
        rm_parts.append(f'Current bid ${(current_price or 0):,}')
        raw_message = ' | '.join(rm_parts)

        # Create / upsert a contact for the DealerClub source
        contact_phone = 'dealerclub:auction'
        contact_name = 'DealerClub Auction'
        cur.execute("""
            INSERT INTO contacts (phone, name)
            VALUES (%s, %s)
            ON CONFLICT (phone) DO UPDATE SET name = EXCLUDED.name
            RETURNING id
        """, (contact_phone, contact_name))
        contact_id = cur.fetchone()['id']

        cur.execute("""
            INSERT INTO bids
                (contact_id, phone, vin, year, make, model, trim,
                 mileage, raw_message, asking_price, notes,
                 status, creation_source, vauto_priority)
            VALUES (%s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    'new', 'dealerclub', TRUE)
            RETURNING id
        """, (
            contact_id, contact_phone, vin,
            year if year else None,
            make, model, trim,
            int(odometer) if odometer else None,
            raw_message,
            int(current_price) if current_price else None,
            full_notes,
        ))
        new_bid_id = cur.fetchone()['id']

        # Featured photo → local upload (Gemini-friendly content-type)
        if featured_image_url:
            local_url = _dealerclub_download_photo(featured_image_url)
            if local_url:
                try:
                    cur.execute(
                        "INSERT INTO bid_photos (bid_id, url) "
                        "VALUES (%s, %s)",
                        (new_bid_id, local_url))
                except Exception:
                    pass

        # Link ledger row to the new bid
        cur.execute("""
            UPDATE dealerclub_lots SET bid_id = %s WHERE id = %s
        """, (new_bid_id, lot_id))

        db.commit()
    except Exception as e:
        db.rollback()
        db.close()
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500
    finally:
        try: db.close()
        except Exception: pass

    # Kick downstream enrichment (canon + vAuto + market check). Gemini
    # assessment will set ai_price; the live dashboard tile compares it
    # against current_price to compute the opportunity score.
    try:
        if vin:
            trigger_market_check(new_bid_id, vin)
    except Exception as e:
        print(f'[dealerclub] market_check kick failed: {e}', flush=True)

    return jsonify({
        'ok': True, 'status': 'new',
        'lot_id': lot_id,
        'bid_id': new_bid_id,
        'vin': vin,
        'current_price': current_price,
    })


# Buy fee + transport assumptions for opportunity scoring. Operator has
# max_buy_fee_override=300 on DealerClub, and transport averages ~$700 for
# domestic moves to FL. Real transport quotes come from DealerClub's
# /transportation/quote/ endpoint later — these are sane defaults.
DEALERCLUB_BUY_FEE_FLAT = 300
DEALERCLUB_TRANSPORT_EST = 700


def _compute_opportunity(current_price, ai_price,
                         buy_fee=None, transport=None):
    """Return (all_in_cost, gap_dollars, gap_pct) for a given lot.

    gap_pct = (ai_price - all_in_cost) / ai_price * 100
    Positive = opportunity. None = not enough data (AI still analyzing).
    """
    if current_price is None or ai_price is None or ai_price <= 0:
        return None, None, None
    bf = buy_fee if buy_fee is not None else DEALERCLUB_BUY_FEE_FLAT
    tr = transport if transport is not None else DEALERCLUB_TRANSPORT_EST
    all_in = float(current_price) + bf + tr
    gap = float(ai_price) - all_in
    pct = gap / float(ai_price) * 100.0
    return round(all_in), round(gap), round(pct, 2)


@app.route('/admin/live_auctions')
def admin_live_auctions_page():
    """Live tile grid of every active DealerClub auction."""
    return render_template('live_auctions.html')


# ── Thalist inventory tile view ──────────────────────────────────────────
#
# Same shape as /admin/live_auctions but for thalist.com wholesale posts.
# Differences from DealerClub: no countdown (these aren't auctions), no
# reserve, no bid count. Opportunity = ai_price - (asking_price +
# THALIST_TRANSPORT_EST). Buy fee is zero (asking IS the price).
#
# Sources for one row:
#   thalist_posts  ledger row (one per active post)
#   bids           AI assessment + canon decode
#   bid_photos     first photo URL for the tile (after local download)

THALIST_TRANSPORT_EST = 700   # flat for now; no API equivalent on thalist


@app.route('/admin/thalist_inventory')
def admin_thalist_inventory_page():
    """Tile grid of every active thalist Wholesale Inventory post."""
    return render_template('thalist_inventory.html')


@app.route('/api/admin/thalist/state')
def api_admin_thalist_state():
    """JSON state for the thalist inventory dashboard. Polled by JS every
    30s. Returns one row per active thalist post (not invalidated, not
    deduped to an old bid), joined with the EW bid's ai_price so the
    client can render the opportunity tier."""
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT tp.id              AS post_row_id,
               tp.post_id,
               tp.vin,
               tp.title,
               tp.year, tp.make_id, tp.model,
               tp.asking_price,
               tp.mileage,
               tp.location_zip,
               tp.description,
               tp.teaser,
               tp.poster_name,
               tp.poster_company,
               tp.poster_company_id,
               tp.post_type_code,
               tp.detail_url,
               tp.first_seen_at,
               tp.bid_id,
               b.ai_price                       AS ai_price,
               b.year                           AS bid_year,
               b.make                           AS bid_make,
               b.model                          AS bid_model,
               b.trim                           AS bid_trim,
               b.status                         AS bid_status,
               (SELECT url FROM bid_photos
                 WHERE bid_id = b.id
                 ORDER BY id ASC LIMIT 1)       AS photo_url
        FROM thalist_posts tp
        JOIN bids b ON b.id = tp.bid_id
        WHERE tp.invalidated_at IS NULL
          AND tp.bid_id IS NOT NULL
          AND COALESCE(b.status,'') NOT IN ('cancelled','rejected','passed','bought')
        ORDER BY tp.first_seen_at DESC
        LIMIT 200
    """)
    rows = list(cur.fetchall())
    db.close()
    posts = []
    for r in rows:
        d = dict(r)
        ai = d.get('ai_price')
        ai_int = int(float(ai)) if ai is not None else None
        ask = d.get('asking_price')
        # Opportunity = AI ceiling - (asking + flat transport).
        # Buy fee is 0 (wholesale offers don't carry one on top).
        all_in = None
        gap = None
        pct = None
        if ask is not None and ai_int is not None and ai_int > 0:
            all_in = int(ask) + THALIST_TRANSPORT_EST
            gap = ai_int - all_in
            pct = round(gap / ai_int * 100, 2)
        d['ai_price'] = ai_int
        d['all_in_cost'] = all_in
        d['opportunity_gap'] = gap
        d['opportunity_pct'] = pct
        # Friendly display fields
        d['ymm'] = (f'{d.get("bid_year") or d.get("year") or ""} '
                    f'{d.get("bid_make") or ""} '
                    f'{d.get("bid_model") or d.get("model") or ""} '
                    f'{d.get("bid_trim") or ""}').strip()
        if d.get('first_seen_at') and hasattr(d['first_seen_at'], 'isoformat'):
            d['first_seen_at'] = d['first_seen_at'].isoformat()
        posts.append(d)
    return jsonify({'posts': posts, 'as_of': time.strftime('%Y-%m-%dT%H:%M:%S')})


@app.route('/api/admin/dealerclub/state')
def api_admin_dealerclub_state():
    """JSON state for the live auction dashboard. Polled by JS every 15s.

    Returns one row per ACTIVE DealerClub lot, joined with the EW bid's
    ai_price so the client can compute opportunity color + gap.
    """
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT dl.id              AS lot_id,
               dl.external_id,
               dl.vin,
               dl.year, dl.make, dl.model, dl.trim,
               dl.odometer,
               dl.current_price,
               dl.high_bid,
               dl.bid_count,
               dl.unique_bidder_count,
               dl.end_time,
               dl.duration_in_minutes,
               dl.reserve_met,
               dl.is_no_reserve,
               dl.reserve_price,
               dl.reserve_progress_color,
               dl.status,
               dl.featured_image_url,
               dl.detail_url,
               dl.estimated_buy_fee,
               dl.estimated_transport,
               dl.bid_id,
               dl.last_polled_at,
               b.ai_price        AS ai_price
        FROM dealerclub_lots dl
        LEFT JOIN bids b ON b.id = dl.bid_id
        WHERE dl.closed_at IS NULL
          AND (dl.end_time IS NULL OR dl.end_time > NOW())
        ORDER BY dl.end_time ASC
    """)
    rows = list(cur.fetchall())
    db.close()
    lots = []
    for r in rows:
        d = dict(r)
        ai = d.get('ai_price')
        ai_int = int(float(ai)) if ai is not None else None
        all_in, gap, pct = _compute_opportunity(
            d.get('current_price'),
            ai_int,
            d.get('estimated_buy_fee'),
            d.get('estimated_transport'),
        )
        d['ai_price'] = ai_int
        d['all_in_cost'] = all_in
        d['opportunity_gap'] = gap
        d['opportunity_pct'] = pct
        # Convert timestamps to ISO so the JS countdown can parse them
        for k in ('end_time', 'last_polled_at'):
            if d.get(k) and hasattr(d[k], 'isoformat'):
                d[k] = d[k].isoformat()
        lots.append(d)
    return jsonify({'lots': lots, 'as_of': time.strftime('%Y-%m-%dT%H:%M:%S')})


@app.route('/admin/ai-levers')
def admin_ai_levers():
    """Render the AI Levers admin page."""
    _ver, _cfg = get_active_ai_config()
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT id, version, is_active, description, created_by, created_at
        FROM ai_config ORDER BY version DESC LIMIT 30
    """)
    versions = [dict(r) for r in cur.fetchall()]
    # Convert timestamps to isoformat for template
    for v in versions:
        if hasattr(v.get('created_at'), 'isoformat'):
            v['created_at_iso'] = v['created_at'].isoformat()
            v['created_at_ago'] = time_ago(v['created_at'])
    db.close()
    return render_template('admin_ai_levers.html',
                           active_version=_ver,
                           active_config=_cfg,
                           versions=versions,
                           time_ago=time_ago)


@app.route('/api/ai-config/active', methods=['GET'])
def api_ai_config_active():
    ver, cfg = get_active_ai_config()
    return jsonify({'version': ver, 'config': cfg})


@app.route('/api/ai-config/versions', methods=['GET'])
def api_ai_config_versions():
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT id, version, is_active, description, created_by, created_at
        FROM ai_config ORDER BY version DESC LIMIT 50
    """)
    rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        if hasattr(r.get('created_at'), 'isoformat'):
            r['created_at'] = r['created_at'].isoformat()
    db.close()
    return jsonify({'versions': rows})


@app.route('/api/ai-config/version/<int:version_id>', methods=['GET'])
def api_ai_config_version(version_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT version, config, description FROM ai_config WHERE id=%s", (version_id,))
    row = cur.fetchone()
    db.close()
    if not row:
        return jsonify({'error': 'not found'}), 404
    cfg = row['config'] if hasattr(row, 'get') else row[1]
    if isinstance(cfg, str):
        cfg = json.loads(cfg)
    return jsonify({
        'version': row['version'] if hasattr(row, 'get') else row[0],
        'config': cfg,
        'description': row['description'] if hasattr(row, 'get') else row[2],
    })


@app.route('/api/ai-config/save', methods=['POST'])
def api_ai_config_save():
    """Save a new config version and mark active. Keeps history for rollback."""
    data = request.json or {}
    cfg = data.get('config')
    description = (data.get('description') or 'User update').strip()[:500]
    ok, err = _validate_ai_config(cfg)
    if not ok:
        return jsonify({'error': err}), 400
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT COALESCE(MAX(version), 0) + 1 AS next_ver FROM ai_config")
    row = cur.fetchone()
    next_ver = row['next_ver'] if hasattr(row, 'get') else row[0]
    cur.execute("UPDATE ai_config SET is_active = FALSE WHERE is_active = TRUE")
    cur.execute("""
        INSERT INTO ai_config (version, config, is_active, description, created_by)
        VALUES (%s, %s, TRUE, %s, %s)
        RETURNING id, version
    """, (next_ver, json.dumps(cfg), description, session.get('user') or 'admin'))
    saved = cur.fetchone()
    db.commit()
    db.close()
    return jsonify({
        'ok': True,
        'id': saved['id'] if hasattr(saved, 'get') else saved[0],
        'version': saved['version'] if hasattr(saved, 'get') else saved[1],
    })


@app.route('/api/ai-config/activate/<int:version_id>', methods=['POST'])
def api_ai_config_activate(version_id):
    """Activate an existing version (rollback)."""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT version FROM ai_config WHERE id=%s", (version_id,))
    row = cur.fetchone()
    if not row:
        db.close()
        return jsonify({'error': 'version not found'}), 404
    cur.execute("UPDATE ai_config SET is_active = FALSE WHERE is_active = TRUE")
    cur.execute("UPDATE ai_config SET is_active = TRUE WHERE id = %s", (version_id,))
    db.commit()
    db.close()
    return jsonify({'ok': True,
                    'activated_version': row['version'] if hasattr(row, 'get') else row[0]})


@app.route('/api/ai-config/preview', methods=['POST'])
def api_ai_config_preview():
    """Preview baseline for a bid with arbitrary config.
    Body: {bid_id: int, config?: dict}.  If config omitted, uses active.
    Returns current baseline vs proposed baseline + breakdowns.
    Does NOT call Gemini — fast deterministic preview only."""
    data = request.json or {}
    bid_id = data.get('bid_id')
    proposed = data.get('config')
    if not bid_id:
        return jsonify({'error': 'missing bid_id'}), 400
    if proposed is not None:
        ok, err = _validate_ai_config(proposed)
        if not ok:
            return jsonify({'error': err}), 400

    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT id, make, model, year, asking_price
        FROM bids WHERE id = %s
    """, (bid_id,))
    bid = cur.fetchone()
    if not bid:
        db.close()
        return jsonify({'error': 'bid not found'}), 404
    cur.execute("SELECT * FROM vauto_lookups WHERE bid_id=%s", (bid_id,))
    vauto = cur.fetchone()
    cur.execute("SELECT * FROM accutrade_lookups WHERE bid_id=%s", (bid_id,))
    accutrade = cur.fetchone()
    db.close()

    try:
        from ai_assessment import classify_bucket, compute_baseline
    except ImportError:
        return jsonify({'error': 'ai_assessment module unavailable'}), 500

    _ver, active_cfg = get_active_ai_config()
    bid_dict = {
        'make': bid.get('make'), 'model': bid.get('model'),
        'year': bid.get('year'), 'asking_price': bid.get('asking_price'),
    }
    vdict = dict(vauto) if vauto else {}
    adict = dict(accutrade) if accutrade else {}

    # Active (current)
    a_bucket = classify_bucket(bid_dict, active_cfg)
    a_result = compute_baseline(a_bucket, vdict, adict)
    result = {
        'bid_id': bid_id,
        'vehicle': f"{bid.get('year') or ''} {bid.get('make') or ''} {bid.get('model') or ''}".strip(),
        'active': {
            'version': _ver,
            'bucket': a_bucket.get('name'),
            'bucket_display': a_bucket.get('display_name'),
            'baseline_price': a_result.get('baseline_price'),
            'breakdown': a_result.get('breakdown'),
            'note': a_result.get('note'),
        }
    }

    if proposed:
        p_bucket = classify_bucket(bid_dict, proposed)
        p_result = compute_baseline(p_bucket, vdict, adict)
        result['proposed'] = {
            'bucket': p_bucket.get('name'),
            'bucket_display': p_bucket.get('display_name'),
            'baseline_price': p_result.get('baseline_price'),
            'breakdown': p_result.get('breakdown'),
            'note': p_result.get('note'),
        }
        if a_result.get('baseline_price') and p_result.get('baseline_price'):
            result['delta_dollars'] = p_result['baseline_price'] - a_result['baseline_price']
            result['delta_pct'] = round(
                100 * (p_result['baseline_price'] - a_result['baseline_price'])
                / a_result['baseline_price'], 1)

    return jsonify(result)


@app.route('/api/vauto/urgent')
def api_vauto_urgent():
    """Fast check: any priority bids waiting? Worker polls this every 5s."""
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT COUNT(*) as cnt FROM bids b
        LEFT JOIN vauto_lookups vl ON vl.bid_id = b.id
        WHERE b.vauto_priority = TRUE
          AND b.vin IS NOT NULL AND length(b.vin) = 17
          AND vl.id IS NULL
    """)
    cnt = cur.fetchone()['cnt']
    db.close()
    return jsonify({'urgent': cnt > 0, 'count': cnt})


@app.route('/api/vauto/pending')
def api_vauto_pending():
    """Return bids that need vAuto lookup, atomically claimed for this worker.

    Two-tier dispatch:
      * priority='primary' (default — Trainer / legacy clients with no
        worker_id passed)  → claims any unclaimed bid, no constraints.
      * priority='standby' (Linux VM workers) → only claims when
        primary is currently working OR primary's heartbeat is
        stale (>90s, dead/stuck).

    Both modes use FOR UPDATE SKIP LOCKED so 4 workers polling at the
    same instant atomically partition the queue with zero races.

    Stale claims (worker died mid-job) auto-release after 5 min.

    Query params:
        worker_id  — defaults to 'trainer' for backward compat with the
                     existing CarScanner script that doesn't pass it.
        priority   — 'primary' (default) or 'standby'.
    """
    worker_id = (request.args.get('worker_id') or 'trainer').strip()
    priority = (request.args.get('priority') or 'primary').strip()

    db = get_db()
    cur = db.cursor()

    # ── Self-healing gate ────────────────────────────────────────────────────
    # If this worker is paused (Cox session-loss self-report) or has been
    # auto-demoted for repeated failures, it gets nothing. It still posts
    # heartbeats; standby logic below treats it as not-primary so other
    # workers take over without any human intervention.
    cur.execute("""
        SELECT paused, effective_priority, consecutive_failures
        FROM workers WHERE worker_id = %s
    """, (worker_id,))
    me = cur.fetchone()
    if me and (me.get('paused') or me.get('effective_priority') == 'degraded'):
        db.close()
        return jsonify({'pending': []})

    # ── Standby gate ─────────────────────────────────────────────────────────
    # Standby workers defer to primary unless primary is busy or silent.
    # Primary liveness uses effective_priority so an auto-demoted primary
    # is treated as silent and standbys take over.
    if priority == 'standby':
        cur.execute("""
            SELECT
                EXISTS(
                    SELECT 1 FROM bids
                    WHERE vauto_claimed_by IN (
                        SELECT worker_id FROM workers
                        WHERE COALESCE(effective_priority, priority) = 'primary'
                    )
                    AND vauto_claimed_at > NOW() - INTERVAL '5 minutes'
                ) AS primary_busy,
                NOT EXISTS(
                    SELECT 1 FROM workers
                    WHERE COALESCE(effective_priority, priority) = 'primary'
                      AND last_heartbeat > NOW() - INTERVAL '90 seconds'
                      AND NOT COALESCE(paused, FALSE)
                ) AS primary_silent
        """)
        state = cur.fetchone()
        if not (state['primary_busy'] or state['primary_silent']):
            # Primary is alive, idle, and capable — let it grab the bid
            # on its next poll. Standby returns empty.
            db.close()
            return jsonify({'pending': []})

    # Atomic claim — primary always, standby only when primary is busy/dead.
    # NOT EXISTS instead of LEFT JOIN: Postgres rejects FOR UPDATE on the
    # nullable side of an outer join. NOT EXISTS gives the same semantics
    # (only bids without a vauto_lookups row) without the join.
    #
    # 2026-05-08 fix: bid is also eligible when a vauto_lookups row exists
    # but books haven't been captured (raw_json IS NULL) AND it isn't the
    # __not_found__ sentinel. This handles the race where Phase 2's
    # kick_direct_enrichment UPSERTed a placeholder row before any Phase 1
    # worker could claim — without the fix, those bids became permanently
    # invisible to /api/vauto/pending. Bids with books OR with the
    # not-found sentinel stay excluded (no re-claim, no infinite retry).
    #
    # 2026-05-08 fix: auto-give-up. Bids that have been claimed 5+ times by
    # Phase 1 workers but never had a successful POST to /api/vauto/submit
    # are clearly broken (vAuto session issue, popup hangs, VIN data issue,
    # etc.). Mark them as __not_found__ so they stop cycling through workers
    # infinitely. Lightweight, idempotent, self-healing — runs each poll.
    # UPSERT so bids that never produced a vauto_lookups row (e.g. every
    # worker error'd before reaching the submit step) still get marked
    # __not_found__ and stop cycling through the fleet. The pre-2026-05-12
    # UPDATE-only version silently no-op'd on those bids (bid 1193: 43
    # consecutive failed claims tripping the 3-failure Telegram alert
    # on every worker that touched it).
    cur.execute("""
        INSERT INTO vauto_lookups (bid_id, vin, appraisal_url, looked_up_at)
        SELECT b.id, b.vin, '__not_found__', NOW()
          FROM bids b
          JOIN (
              SELECT bid_id FROM worker_jobs
               WHERE job_type='vauto'
               GROUP BY bid_id
               HAVING COUNT(*) >= 5
                  AND COUNT(*) FILTER (WHERE status='ok') = 0
          ) wj ON wj.bid_id = b.id
         WHERE b.vin IS NOT NULL
        ON CONFLICT (bid_id) DO UPDATE
           SET appraisal_url = '__not_found__'
         WHERE vauto_lookups.appraisal_url IS NULL
    """)
    cur.execute("""
        WITH eligible AS (
            SELECT b.id
            FROM bids b
            WHERE b.vin IS NOT NULL AND length(b.vin) = 17
              AND b.vin_invalid_reason IS NULL  -- 2026-05-14: skip ISO-3779-invalid VINs (e.g. bid 1438 typo) so workers don't spin
              -- PHASE1_MILES_GATE_2026_05_15: skip bids without mileage so
              -- workers don't waste cycles on AccuTrade that needs miles
              -- to produce useful values. miles_audit_worker SMSes the
              -- bidder; when they reply, update_bid auto-clear fires
              -- force-reprocess and workers re-pick at full eligibility.
              AND b.mileage IS NOT NULL AND b.mileage > 0
              -- PHASE1_MILES_GATE_2026_05_15: skip bids with an open
              -- verification flag (missing_miles / vin_not_found /
              -- vin_invalid / miles_discrepancy). Cleared by operator
              -- edit on VIN/miles (auto-fires force-reprocess), customer
              -- SMS reply, or dashboard Clear button.
              AND (b.needs_verification_at IS NULL
                   OR b.needs_verification_cleared_at IS NOT NULL)
              AND NOT EXISTS (
                  SELECT 1 FROM vauto_lookups vl
                   WHERE vl.bid_id = b.id
                     AND (vl.raw_json IS NOT NULL
                          OR vl.appraisal_url = '__not_found__')
              )
              AND (b.vauto_claimed_at IS NULL
                   OR b.vauto_claimed_at < NOW() - INTERVAL '5 minutes')
            ORDER BY b.vauto_priority DESC, b.created_at DESC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        UPDATE bids
           SET vauto_claimed_by = %s,
               vauto_claimed_at = NOW()
          FROM eligible
         WHERE bids.id = eligible.id
        RETURNING bids.id AS bid_id, bids.vin, bids.mileage, bids.year,
                  bids.make, bids.model,
                  COALESCE(NULLIF(bids.canon_trim, ''), bids.trim) AS trim,
                  bids.vauto_priority
    """, (worker_id,))
    rows = cur.fetchall()

    # Log each claim to worker_jobs (look-back history + dashboard feed).
    for row in rows:
        cur.execute("""
            INSERT INTO worker_jobs (bid_id, worker_id, job_type, status, claimed_at)
            VALUES (%s, %s, 'vauto', 'in_progress', NOW())
        """, (row['bid_id'], worker_id))

    db.commit()
    db.close()
    return jsonify({'pending': [dict(r) for r in rows]})


@app.route('/share/autocheck/<int:bid_id>')
def share_autocheck(bid_id):
    """Public AutoCheck share — server proxies the report HTML using
    Beelink-115's keeper cookies. Client opens this URL with no auth
    required.

    The slot2 BFF returns full HTML; we just stream it through with the
    URL rewritten so static assets still load from autocheck.com.
    """
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT vin FROM vauto_lookups WHERE bid_id=%s", (bid_id,))
    row = cur.fetchone()
    if not row or not row['vin']:
        return ('No VIN on this bid', 404)
    vin = row['vin']

    cur.execute("SELECT cookies, entity_id, platform_user_id, refreshed_at FROM vauto_session WHERE label='oscarpas'")
    sess = cur.fetchone()
    db.close()
    if not sess:
        return ('Cox session unavailable (cookie keeper offline)', 503)

    cookies = sess['cookies']
    if isinstance(cookies, str):
        cookies = json.loads(cookies)

    import requests as _r
    try:
        r = _r.get(
            f'https://slot2.bff.megazord.vauto.app.coxautoinc.com/api/autocheck/getReport?vin={vin}',
            cookies=cookies,
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/json,*/*',
                'appraisalentityid': sess['entity_id'],
                'currententityid': sess['entity_id'],
                'platformuserid': sess['platform_user_id'],
                'Referer': 'https://provision.vauto.app.coxautoinc.com/',
            },
            timeout=15,
        )
    except Exception as e:
        return (f'Upstream error: {e}', 502)

    if r.status_code != 200:
        return (f'Upstream returned {r.status_code}', 502)

    return r.content, 200, {'Content-Type': r.headers.get('content-type', 'text/html')}


@app.route('/share/ipacket/<int:bid_id>')
def share_ipacket(bid_id):
    """iPacket OEM sticker share proxy: PUT to start pull, poll for result,
    redirect client to the public document-viewer URL."""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT vin FROM vauto_lookups WHERE bid_id=%s", (bid_id,))
    row = cur.fetchone()
    if not row or not row['vin']:
        return ('No VIN on this bid', 404)
    vin = row['vin']

    cur.execute("SELECT cookies FROM vauto_session WHERE label='ipacket'")
    sess = cur.fetchone()
    db.close()
    if not sess:
        return ('iPacket token not yet seeded — paste a fresh JWT via /api/ipacket/refresh_token', 503)
    token_blob = sess['cookies']
    if isinstance(token_blob, str):
        token_blob = json.loads(token_blob)
    jwt = token_blob.get('jwt')
    if not jwt:
        return ('iPacket token missing jwt field', 503)

    import requests as _r
    import time as _t
    H = {
        'Authorization': f'bearer {jwt}',
        'Origin': 'https://dpapp.autoipacket.com',
        'Referer': 'https://dpapp.autoipacket.com/',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36',
    }
    try:
        r = _r.put(f'https://djapi.autoipacket.com/v2/sticker-puller/pull/{vin}',
                   headers=H, timeout=15)
        if r.status_code == 401:
            return ('iPacket token expired — paste a fresh JWT via /api/ipacket/refresh_token', 401)
        if r.status_code not in (200, 201):
            return (f'iPacket pull returned {r.status_code}: {r.text[:200]}', 502)
        job_id = r.json().get('id')
        if not job_id:
            return ('iPacket: no job_id in PUT response', 502)
        viewer_url = None
        for _ in range(25):
            pr = _r.get(f'https://djapi.autoipacket.com/v2/sticker-puller/poll/{job_id}',
                        headers=H, timeout=10)
            body = pr.json() if pr.status_code in (200, 201) else {}
            state = body.get('state')
            if state == 'SUCCESS':
                viewer_url = body.get('pdf') or body.get('ipacket_viewer')
                break
            if state in ('FAILED', 'ERROR'):
                return (f'iPacket pull failed: {body.get("detail", "unknown")}', 502)
            _t.sleep(1)
        if not viewer_url:
            return ('iPacket pull timed out after 25s', 504)
    except Exception as e:
        return (f'Upstream error: {e}', 502)

    return f'<html><head><meta http-equiv="refresh" content="0;url={viewer_url}"></head><body>Loading iPacket sticker... <a href="{viewer_url}">click here</a></body></html>', 200, {'Content-Type': 'text/html'}



@app.route('/api/ipacket/refresh_token', methods=['POST'])
def api_ipacket_refresh_token():
    """Store/update iPacket JWT bearer token. Operator pastes JSON:
    {"jwt": "eyJhbGc..."}. Stored in vauto_session table with label='ipacket'.
    Public endpoint (no admin login) so operator can curl from any machine.
    """
    data = request.get_json(silent=True) or {}
    jwt = (data.get('jwt') or '').strip()
    if not jwt or not jwt.startswith('eyJ'):
        return jsonify({'ok': False, 'error': 'jwt (bearer token starting with eyJ) required'}), 400

    db = get_db()
    cur = db.cursor()
    cur.execute("""
        INSERT INTO vauto_session (label, cookies, entity_id, platform_user_id, refreshed_at)
        VALUES ('ipacket', %s::jsonb, 'ipacket', 'ipacket', NOW())
        ON CONFLICT (label) DO UPDATE SET cookies=EXCLUDED.cookies, refreshed_at=NOW()
    """, (json.dumps({'jwt': jwt}),))
    db.commit()
    db.close()
    return jsonify({'ok': True, 'jwt_length': len(jwt)})


@app.route('/api/vauto/refresh_cookies', methods=['POST'])
def api_vauto_refresh_cookies():
    """Cookie keeper endpoint. Beelink-115 + EW workers POST their freshest
    vAuto/Cox cookies here. We UPSERT into vauto_session so the stateless
    api_workers can read them.

    Payload:
        {
          "label": "oscarpas",                    # session label, default oscarpas
          "cookies": {"vAutoAuth": "...", ...},   # dict of cookie name -> value
          "entity_id": "...",                     # dealer entity (vAuto BFF header)
          "platform_user_id": "..."               # platform user (vAuto BFF header)
        }
    """
    data = request.get_json(silent=True) or {}
    label = (data.get('label') or 'oscarpas').strip()[:64]
    cookies = data.get('cookies')
    entity_id = (data.get('entity_id') or '').strip()[:128]
    platform_user_id = (data.get('platform_user_id') or '').strip()[:128]

    if not isinstance(cookies, dict) or not cookies:
        return jsonify({'ok': False, 'error': 'cookies (dict) required'}), 400
    if 'vAutoAuth' not in cookies:
        return jsonify({'ok': False, 'error': 'vAutoAuth cookie missing'}), 400

    db = get_db()
    cur = db.cursor()
    cur.execute("""
        INSERT INTO vauto_session (label, cookies, entity_id, platform_user_id, refreshed_at)
        VALUES (%s, %s::jsonb, %s, %s, NOW())
        ON CONFLICT (label) DO UPDATE SET
            cookies = EXCLUDED.cookies,
            entity_id = COALESCE(NULLIF(EXCLUDED.entity_id, ''), vauto_session.entity_id),
            platform_user_id = COALESCE(NULLIF(EXCLUDED.platform_user_id, ''), vauto_session.platform_user_id),
            refreshed_at = NOW()
    """, (label, json.dumps(cookies), entity_id, platform_user_id))
    db.commit()
    db.close()
    return jsonify({'ok': True, 'cookies': len(cookies), 'label': label})


@app.route('/api/vauto/refresh_session', methods=['POST'])
def api_vauto_refresh_session():
    """Cookie file keeper for the direct BFF API path.

    VM 104 verifier (192.168.1.33) maintains a 24/7 vAuto session and POSTs
    its full session snapshot here every cycle. We atomic-write to
    /opt/expwholesale/state/vauto_session.json so cookie_jar.py picks it up
    on next BFF call (no service restart needed — CookieJar auto-reloads on
    file mtime change).

    Auth: shared secret in `X-Auth` header, matching env var
    EW_VAUTO_REFRESH_SECRET. No session cookie required (route is in
    _PUBLIC_PREFIXES via /api/vauto/).

    Payload (full session JSON, same shape as state/vauto_session.json):
      {
        "captured_at": "2026-05-08T12:34:56",
        "cookies": [{"name", "value", "domain", "path", "expires",
                     "httpOnly", "secure", "sameSite"}, ...],
        "headers": {"platformuserid": "...", "appraisalentityid": "...", ...},
        "session_appraisal_id": "..."
      }
    """
    import json as _json
    import os as _os
    import tempfile as _tempfile

    expected = (_os.environ.get('EW_VAUTO_REFRESH_SECRET') or '').strip()
    if not expected:
        return jsonify({'ok': False, 'error': 'server missing EW_VAUTO_REFRESH_SECRET'}), 500
    provided = (request.headers.get('X-Auth') or '').strip()
    if provided != expected:
        return jsonify({'ok': False, 'error': 'unauthorized'}), 401

    data = request.get_json(silent=True) or {}
    cookies = data.get('cookies')
    if not isinstance(cookies, list) or len(cookies) < 10:
        return jsonify({'ok': False, 'error': 'cookies must be a list of >=10 dicts'}), 400
    headers = data.get('headers')
    if not isinstance(headers, dict) or not headers:
        return jsonify({'ok': False, 'error': 'headers (dict) required'}), 400
    if not data.get('session_appraisal_id'):
        return jsonify({'ok': False, 'error': 'session_appraisal_id required'}), 400

    # Sanity: at least one cookie should be cox/vauto-domain
    domains = {(c.get('domain') or '').lower() for c in cookies if isinstance(c, dict)}
    if not any(any(d in dom for d in ('cox', 'vauto', 'okta', 'megazord'))
               for dom in domains):
        return jsonify({'ok': False, 'error': 'no cox/vauto/okta/megazord domain cookies'}), 400
    # vAutoAuth is Cox's session auth cookie. A push lacking it means the
    # pusher's Cox session has expired and they're sending pre-login junk
    # (analytics/marketing cookies only). Reject — keep the previous valid
    # file rather than overwrite with garbage. Added 2026-05-14.
    cookie_names = {(c.get('name') or '') for c in cookies if isinstance(c, dict)}
    if 'vAutoAuth' not in cookie_names:
        return jsonify({'ok': False, 'error': 'vAutoAuth cookie missing — pre-login junk rejected'}), 400

    target = '/opt/expwholesale/state/vauto_session.json'
    try:
        _os.makedirs(_os.path.dirname(target), exist_ok=True)
        # Atomic write: tmp + rename so cookie_jar.py never reads half-written
        fd, tmp = _tempfile.mkstemp(prefix='vauto_session_', suffix='.tmp',
                                    dir=_os.path.dirname(target))
        try:
            with _os.fdopen(fd, 'w', encoding='utf-8') as fp:
                _json.dump(data, fp, indent=2, ensure_ascii=False)
            _os.replace(tmp, target)
        finally:
            if _os.path.exists(tmp):
                try: _os.remove(tmp)
                except OSError: pass
        try: _os.chmod(target, 0o640)
        except OSError: pass
    except Exception as e:
        return jsonify({'ok': False, 'error': f'write failed: {e}'}), 500

    # Also UPSERT into vauto_session table so cookie_bridge sees a fresh row
    # next poll. Without this, the bridge re-reads its last stale row every
    # 30s and overwrites THIS file write with stale data. (2026-05-15)
    # Label: X-Worker-Id header > payload.label > 'worker_pool'.
    db_upsert_ok = None
    try:
        _label = (request.headers.get('X-Worker-Id') or
                  data.get('label') or 'worker_pool')[:64]
        _cookies_dict = {c['name']: c['value'] for c in cookies
                         if isinstance(c, dict) and c.get('name')}
        _entity_id = (headers.get('appraisalentityid') or
                      headers.get('currententityid') or '')
        _platform_user_id = headers.get('platformuserid') or ''
        _user_agent = headers.get('user-agent') or ''
        _db = get_db()
        _cur = _db.cursor()
        _cur.execute("""
            INSERT INTO vauto_session
                (label, cookies, entity_id, platform_user_id, user_agent,
                 refreshed_at, refreshed_by)
            VALUES (%s, %s, %s, %s, %s, NOW(), %s)
            ON CONFLICT (label) DO UPDATE SET
                cookies          = EXCLUDED.cookies,
                entity_id        = EXCLUDED.entity_id,
                platform_user_id = EXCLUDED.platform_user_id,
                user_agent       = EXCLUDED.user_agent,
                refreshed_at     = NOW(),
                refreshed_by     = EXCLUDED.refreshed_by
        """, (_label, _json.dumps(_cookies_dict), _entity_id,
              _platform_user_id, _user_agent, 'refresh_session_endpoint'))
        _db.commit()
        _db.close()
        db_upsert_ok = True
    except Exception as _e:
        db_upsert_ok = False
        print(f'[refresh_session] db upsert failed: {type(_e).__name__}: {_e}',
              flush=True)

    return jsonify({'ok': True,
                    'cookie_count': len(cookies),
                    'header_count': len(headers),
                    'captured_at': data.get('captured_at'),
                    'written_to': target,
                    'db_upsert_ok': db_upsert_ok})


@app.route('/api/vauto/get_current_cookies', methods=['GET'])
def api_vauto_get_current_cookies():
    """Return the current vauto_session.json payload so consumers (verifiers,
    other workers) can inject fresh cookies into their own Selenium/Playwright
    context. Drops the verifiers' dependency on maintaining their own Cox
    session — they become pool consumers.

    Auth: X-Auth header must match EW_VAUTO_REFRESH_SECRET (same as
    /api/vauto/refresh_session). Added 2026-05-14.
    """
    import json as _json
    import os as _os
    expected = (_os.environ.get('EW_VAUTO_REFRESH_SECRET') or '').strip()
    if not expected:
        return jsonify({'ok': False, 'error': 'server missing EW_VAUTO_REFRESH_SECRET'}), 500
    provided = (request.headers.get('X-Auth') or '').strip()
    if provided != expected:
        return jsonify({'ok': False, 'error': 'unauthorized'}), 401
    target = '/opt/expwholesale/state/vauto_session.json'
    try:
        with open(target, 'r', encoding='utf-8') as fp:
            payload = _json.load(fp)
    except FileNotFoundError:
        return jsonify({'ok': False, 'error': 'no cookies in pool yet'}), 503
    except Exception as e:
        return jsonify({'ok': False, 'error': f'read failed: {type(e).__name__}: {e}'}), 500
    # Quick sanity — make sure pool has vAutoAuth
    cookies = payload.get('cookies') or []
    names = {(c.get('name') or '') for c in cookies if isinstance(c, dict)}
    if 'vAutoAuth' not in names:
        return jsonify({'ok': False, 'error': 'pool missing vAutoAuth'}), 503
    return jsonify(payload)


@app.route('/api/vauto/heartbeat', methods=['POST'])
def api_vauto_heartbeat():
    """Workers POST every 60s with their state. Server tracks for dispatch
    decisions and runs the auto-demote / auto-promote policy that lets the
    queue route around a sessionless or hung worker without human help.

    Body: {worker_id, priority, role, chrome_alive, lookups_done,
           last_lookup_at, last_claim_status, last_claim_duration_ms,
           consecutive_failures, paused, pause_reason, synthetic_ok}

    Auto-demote: if the worker reports paused=true OR consecutive_failures>=3
    OR 2-of-last-3 jobs hit released_stale/released_worker/failed/duration>240s,
    server flips effective_priority='degraded'. The pending endpoint then
    returns empty for this worker until it recovers.

    Auto-promote: a degraded worker that posts synthetic_ok=true on 3
    consecutive heartbeats spanning at least 3 minutes is restored to its
    operator-set priority. synthetic_ok means the worker scraped a known
    vAuto URL and parsed an expected token without errors.
    """
    data = request.get_json(silent=True) or {}
    worker_id = (data.get('worker_id') or 'trainer').strip()
    # === EW_REJECT_OSCAR_WORKERS ===
    # oscar-* workers belong to oscar_intake project and must not poll EW.
    if worker_id.startswith('oscar-'):
        return jsonify({'error': 'wrong_project',
                        'reason': 'oscar-* workers should poll oscar_intake, not EW'}), 410
    priority = (data.get('priority') or 'primary').strip()
    role = (data.get('role') or 'ew_worker').strip()
    chrome_alive = bool(data.get('chrome_alive', True))
    lookups_done = int(data.get('lookups_done') or 0)
    last_lookup_at = data.get('last_lookup_at')
    last_seen_ip = request.headers.get('X-Real-IP') or request.remote_addr

    last_claim_status = (data.get('last_claim_status') or '')[:32] or None
    last_claim_duration_ms = data.get('last_claim_duration_ms')
    try:
        last_claim_duration_ms = int(last_claim_duration_ms) if last_claim_duration_ms is not None else None
    except (TypeError, ValueError):
        last_claim_duration_ms = None
    consecutive_failures = int(data.get('consecutive_failures') or 0)
    paused = bool(data.get('paused', False))
    pause_reason = (data.get('pause_reason') or '')[:128] or None
    synthetic_ok = bool(data.get('synthetic_ok', False))

    db = get_db()
    cur = db.cursor()
    cur.execute("""
        INSERT INTO workers (worker_id, role, priority, effective_priority,
                             last_heartbeat, chrome_alive, lookups_done,
                             last_lookup_at, last_seen_ip,
                             last_claim_status, last_claim_duration_ms,
                             consecutive_failures, paused, pause_reason,
                             updated_at)
        VALUES (%s, %s, %s, %s, NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (worker_id) DO UPDATE SET
            role           = COALESCE(workers.role, EXCLUDED.role),
            priority       = EXCLUDED.priority,
            -- Don't clobber effective_priority on every heartbeat — auto-
            -- demote/promote logic below owns it. Only seed on first insert.
            effective_priority = COALESCE(workers.effective_priority, EXCLUDED.priority),
            last_heartbeat = NOW(),
            chrome_alive   = EXCLUDED.chrome_alive,
            lookups_done   = EXCLUDED.lookups_done,
            last_lookup_at = COALESCE(EXCLUDED.last_lookup_at, workers.last_lookup_at),
            last_seen_ip   = EXCLUDED.last_seen_ip,
            last_claim_status      = COALESCE(EXCLUDED.last_claim_status, workers.last_claim_status),
            last_claim_duration_ms = COALESCE(EXCLUDED.last_claim_duration_ms, workers.last_claim_duration_ms),
            consecutive_failures   = EXCLUDED.consecutive_failures,
            paused                 = EXCLUDED.paused,
            pause_reason           = COALESCE(EXCLUDED.pause_reason, workers.pause_reason),
            updated_at     = NOW()
    """, (worker_id, role, priority, priority, chrome_alive, lookups_done,
          last_lookup_at, last_seen_ip,
          last_claim_status, last_claim_duration_ms,
          consecutive_failures, paused, pause_reason))

    # ── Auto-demote evaluation ───────────────────────────────────────────────
    # Look at the last 3 closed jobs for this worker. Two or more bad
    # outcomes (released_stale/released_worker/failed, or duration>240s)
    # flips us to degraded. consecutive_failures>=3 from worker's own count
    # is a separate trigger for paths the worker can detect that the server
    # can't (in-Chrome timeouts that never reached release_claim).
    cur.execute("""
        SELECT COUNT(*) AS bad
        FROM (
            SELECT status, duration_ms FROM worker_jobs
            WHERE worker_id = %s AND completed_at IS NOT NULL
            ORDER BY completed_at DESC LIMIT 3
        ) t
        WHERE status IN ('released_stale','released_worker','failed')
           OR duration_ms > 240000
    """, (worker_id,))
    bad = cur.fetchone()['bad'] or 0

    should_demote = False  # auto-demote DISABLED per operator request 2026-04-30 - workers stay at declared priority; manual paused/UPDATE still works
    if should_demote:
        cur.execute("""
            UPDATE workers
               SET effective_priority = 'degraded',
                   auto_demoted_at = COALESCE(auto_demoted_at, NOW()),
                   synthetic_ok_count = 0
             WHERE worker_id = %s
               AND effective_priority IS DISTINCT FROM 'degraded'
        """, (worker_id,))

    # ── Auto-promote evaluation ──────────────────────────────────────────────
    # Synthetic checks reset the failure score. After 3 consecutive
    # synthetic_ok heartbeats spanning at least 3 minutes, restore to the
    # operator-set priority and clear the auto_demoted_at marker.
    if synthetic_ok:
        cur.execute("""
            UPDATE workers
               SET synthetic_ok_count = COALESCE(synthetic_ok_count, 0) + 1,
                   last_synthetic_at = NOW()
             WHERE worker_id = %s
        """, (worker_id,))
        cur.execute("""
            UPDATE workers
               SET effective_priority = priority,
                   auto_demoted_at = NULL,
                   consecutive_failures = 0,
                   synthetic_ok_count = 0
             WHERE worker_id = %s
               AND effective_priority = 'degraded'
               AND synthetic_ok_count >= 3
               AND (auto_demoted_at IS NULL OR auto_demoted_at < NOW() - INTERVAL '3 minutes')
        """, (worker_id,))
    elif not paused and not should_demote:
        # Not a synthetic-ok beat and worker is healthy — reset the
        # synthetic streak counter so it has to demonstrate 3 in a row
        # after demotion, not earn credit slowly over a healthy day.
        cur.execute("""
            UPDATE workers SET synthetic_ok_count = 0
             WHERE worker_id = %s AND effective_priority = 'degraded'
        """, (worker_id,))

    db.commit()

    # Return current state so worker can log it.
    cur.execute("""
        SELECT effective_priority, paused, auto_demoted_at, synthetic_ok_count,
               COALESCE(pending_exit, FALSE) AS pending_exit
        FROM workers WHERE worker_id = %s
    """, (worker_id,))
    state = cur.fetchone() or {}
    # If watchdog flagged this worker for exit, deliver the signal exactly
    # once and clear the flag so NSSM-restarted process doesn't immediately
    # exit again on its first heartbeat.
    exit_flag = bool(state.get('pending_exit'))
    if exit_flag:
        cur.execute("""
            UPDATE workers SET pending_exit = FALSE, updated_at = NOW()
             WHERE worker_id = %s
        """, (worker_id,))
        db.commit()
    db.close()
    return jsonify({
        'ok': True,
        'worker_id': worker_id,
        'effective_priority': state.get('effective_priority'),
        'paused': state.get('paused'),
        'auto_demoted_at': (state.get('auto_demoted_at').isoformat()
                            if state.get('auto_demoted_at') else None),
        'synthetic_ok_count': state.get('synthetic_ok_count') or 0,
        'exit': exit_flag,
    })


@app.route('/api/worker/session_lost', methods=['POST'])
def api_worker_session_lost():
    """Worker self-reports Cox/vAuto SSO session loss. The worker's claim
    loop should pause itself the moment it detects a redirect to
    bridge.coxautoinc.com or signin.cox — there's no point claiming bids
    when the next nav will fail. Server marks the worker paused; dispatch
    routes around it; standby takes over within seconds.

    Body: {worker_id, url, detail}
    """
    data = request.get_json(silent=True) or {}
    worker_id = (data.get('worker_id') or '').strip()
    if not worker_id:
        return jsonify({'ok': False, 'error': 'worker_id required'}), 400
    url = (data.get('url') or '')[:256]
    detail = (data.get('detail') or '')[:256]
    reason = f"session_lost: {detail or url}"[:128]

    db = get_db()
    cur = db.cursor()
    # session_lost no longer auto-degrades or pauses (operator request 2026-04-30).
    # Only record the reason for visibility; worker stays primary so dispatch keeps flowing.
    cur.execute("""
        UPDATE workers
           SET pause_reason = %s,
               updated_at = NOW()
         WHERE worker_id = %s
    """, (reason, worker_id))
    # Release any in-flight claim — the worker can't finish it.
    cur.execute("""
        UPDATE bids SET vauto_claimed_by = NULL, vauto_claimed_at = NULL
         WHERE vauto_claimed_by = %s
    """, (worker_id,))
    cur.execute("""
        UPDATE worker_jobs
           SET completed_at = NOW(),
               status = 'released_session_lost',
               error = %s
         WHERE worker_id = %s AND completed_at IS NULL
    """, (reason, worker_id))
    db.commit()
    db.close()
    return jsonify({'ok': True, 'worker_id': worker_id, 'paused': True})


# ─────────────────────────────────────────────────────────────────────────────
# Per-phase progress + aggressive stuck-bid watchdog
#
# Workers POST {worker_id, bid_id, phase, state} at the start and end of each
# of vauto / accutrade / ipacket. Watchdog thread runs every 15s; if a phase
# has been "started" without "done" longer than its budget, it releases the
# claim, marks the worker degraded, signals worker exit on next heartbeat,
# and pings Telegram. This collapses the 5-min stale-claim sweep down to a
# 30-90s recovery for live failures.
# ─────────────────────────────────────────────────────────────────────────────

# Telegram alert helper for watchdog (matches worker bot/chat per spec)
_WATCHDOG_TG_BOT = '8639130743:AAFczHqjWoiUBs7adZwBEJ6217bQzYGhI_o'  # placeholder — overridden by env if set
_WATCHDOG_TG_CHAT = '7985611488'


def _tg_worker_alert(msg):
    """Fire-and-forget Telegram alert. Never raises."""
    try:
        bot = os.environ.get('WATCHDOG_TG_BOT', _WATCHDOG_TG_BOT)
        chat = os.environ.get('WATCHDOG_TG_CHAT', _WATCHDOG_TG_CHAT)
        if not bot or not chat:
            return
        requests.post(
            f'https://api.telegram.org/bot{bot}/sendMessage',
            json={'chat_id': chat, 'text': msg, 'parse_mode': 'HTML'},
            timeout=5,
        )
    except Exception:
        pass


@app.route('/api/worker/progress', methods=['POST'])
def api_worker_progress():
    """Worker reports phase start/done. Lets the watchdog detect stuck bids
    in 30-90s instead of 5min.

    Body: {worker_id, bid_id, phase, state}
      phase: 'vauto' | 'accutrade' | 'ipacket'
      state: 'started' | 'done'
    """
    data = request.get_json(silent=True) or {}
    worker_id = (data.get('worker_id') or '').strip()
    bid_id = data.get('bid_id')
    phase = (data.get('phase') or '').strip()
    state = (data.get('state') or '').strip()

    if not worker_id or not bid_id or phase not in ('vauto', 'accutrade', 'ipacket') \
            or state not in ('started', 'done'):
        return jsonify({'ok': False, 'error': 'missing/invalid fields'}), 400

    db = get_db()
    cur = db.cursor()
    cur.execute("""
        INSERT INTO bid_phase_progress (bid_id, phase, state, worker_id, ts)
        VALUES (%s, %s, %s, %s, NOW())
        ON CONFLICT (bid_id, phase, state) DO UPDATE SET
            ts        = NOW(),
            worker_id = EXCLUDED.worker_id
    """, (bid_id, phase, state, worker_id))
    db.commit()
    db.close()
    return jsonify({'ok': True})



# ── Per-bid live progress (used by dashboard progress bars) ──────────────────
# Phase budgets (seconds): vauto=45, accutrade=55, ipacket=10. Total ~110s.
# pct_complete maps so that pre-vauto/vauto = 0..38, accutrade = 38..84,
# ipacket = 84..99, all-done OR not in_flight = 100.
_PHASE_DURATION_SEC = {'vauto': 45, 'accutrade': 55, 'ipacket': 10}
_PHASE_PCT = {'vauto': (0, 38), 'accutrade': (38, 84), 'ipacket': (84, 99)}
_PHASE_TOTAL_SEC = 110


def _compute_progress_for_bid(bid_id, bid_row, markers, lookups_present):
    """Pure helper — given DB rows, compute the response dict.

    bid_row: dict with keys vauto_claimed_at (or None), vauto_claimed_by.
    markers: list of dicts {phase, state, ts (datetime), age_sec}.
    lookups_present: dict {'vauto': bool, 'accutrade': bool, 'ipacket': bool}.
    """
    in_flight = bool(bid_row and bid_row.get('vauto_claimed_at'))
    claimed_at = bid_row.get('vauto_claimed_at') if bid_row else None
    claimed_by = bid_row.get('vauto_claimed_by') if bid_row else None

    # Index markers by (phase, state)
    by_ps = {(m['phase'], m['state']): m for m in markers}
    v_started = by_ps.get(('vauto', 'started'))
    v_done = by_ps.get(('vauto', 'done'))
    a_started = by_ps.get(('accutrade', 'started'))
    a_done = by_ps.get(('accutrade', 'done'))
    i_started = by_ps.get(('ipacket', 'started'))
    i_done = by_ps.get(('ipacket', 'done'))

    all_done = bool(v_done and a_done and i_done) or (
        lookups_present.get('vauto') and lookups_present.get('accutrade')
        and lookups_present.get('ipacket'))

    # Determine current phase + when it started
    phase = None
    phase_started_at = None
    if i_started and not i_done:
        phase, phase_started_at = 'ipacket', i_started.get('ts')
    elif a_done and not i_done:
        # accutrade done, ipacket not yet started — still "ipacket" pending
        phase, phase_started_at = 'ipacket', a_done.get('ts')
    elif a_started and not a_done:
        phase, phase_started_at = 'accutrade', a_started.get('ts')
    elif v_done and not a_done:
        phase, phase_started_at = 'accutrade', v_done.get('ts')
    elif v_started and not v_done:
        phase, phase_started_at = 'vauto', v_started.get('ts')
    elif in_flight and not v_started:
        phase, phase_started_at = 'vauto', claimed_at
    elif all_done:
        phase = 'done'

    # Elapsed for the row overall (vs claim time)
    elapsed_sec = 0
    if claimed_at:
        try:
            from datetime import datetime, timezone
            now = datetime.now(claimed_at.tzinfo) if getattr(
                claimed_at, 'tzinfo', None) else datetime.utcnow()
            elapsed_sec = max(0, int((now - claimed_at).total_seconds()))
        except Exception:
            elapsed_sec = 0

    # Compute pct
    if all_done or not in_flight:
        pct = 100 if all_done else 0
    elif phase == 'done':
        pct = 100
    else:
        lo, hi = _PHASE_PCT.get(phase, (0, 38))
        # Within-phase progress = min(1, elapsed_in_phase / expected_duration)
        within = 0.0
        if phase_started_at:
            try:
                from datetime import datetime
                now = datetime.now(phase_started_at.tzinfo) if getattr(
                    phase_started_at, 'tzinfo', None) else datetime.utcnow()
                in_phase = max(0, (now - phase_started_at).total_seconds())
                expected = _PHASE_DURATION_SEC.get(phase, 45)
                within = min(1.0, in_phase / max(1.0, expected))
            except Exception:
                within = 0.0
        pct = int(round(lo + (hi - lo) * within))
        # Floor pct at lo of current phase (don't go backwards)
        pct = max(lo, min(hi, pct))

    estimated_total = _PHASE_TOTAL_SEC
    eta = max(5, min(300, estimated_total - elapsed_sec)) if in_flight and not all_done else 0

    return {
        'bid_id': bid_id,
        'in_flight': in_flight,
        'claimed_at': claimed_at.isoformat() if hasattr(claimed_at, 'isoformat') else claimed_at,
        'claimed_by': claimed_by,
        'phase': phase,
        'phase_started_at': phase_started_at.isoformat() if hasattr(phase_started_at, 'isoformat') else phase_started_at,
        'elapsed_sec': elapsed_sec,
        'estimated_total_sec': estimated_total,
        'pct_complete': max(0, min(100, pct)),
        'eta_sec': eta,
        'all_done': bool(all_done),
    }


def _fetch_progress_bulk(cur, bid_ids):
    """Returns {bid_id: progress_dict} for the given ids in a few queries."""
    if not bid_ids:
        return {}
    cur.execute("""
        SELECT id, vauto_claimed_by, vauto_claimed_at
          FROM bids WHERE id = ANY(%s)
    """, (list(bid_ids),))
    bid_rows = {r['id']: dict(r) for r in cur.fetchall()}

    cur.execute("""
        SELECT bid_id, phase, state, ts,
               EXTRACT(EPOCH FROM (NOW() - ts))::int AS age_sec
          FROM bid_phase_progress
         WHERE bid_id = ANY(%s)
    """, (list(bid_ids),))
    markers_by_bid = {}
    for r in cur.fetchall():
        markers_by_bid.setdefault(r['bid_id'], []).append(dict(r))

    # Existence of lookup rows (cheap, indexed PK lookup per table)
    cur.execute("SELECT bid_id FROM vauto_lookups WHERE bid_id = ANY(%s)",
                (list(bid_ids),))
    have_v = {r['bid_id'] for r in cur.fetchall()}
    cur.execute("SELECT bid_id FROM accutrade_lookups WHERE bid_id = ANY(%s)",
                (list(bid_ids),))
    have_a = {r['bid_id'] for r in cur.fetchall()}
    cur.execute("SELECT bid_id FROM ipacket_lookups WHERE bid_id = ANY(%s)",
                (list(bid_ids),))
    have_i = {r['bid_id'] for r in cur.fetchall()}

    out = {}
    for bid_id in bid_ids:
        out[bid_id] = _compute_progress_for_bid(
            bid_id,
            bid_rows.get(bid_id, {}),
            markers_by_bid.get(bid_id, []),
            {'vauto': bid_id in have_v,
             'accutrade': bid_id in have_a,
             'ipacket': bid_id in have_i})
    return out


@app.route('/api/bid/<int:bid_id>/progress')
def api_bid_progress(bid_id):
    db = get_db()
    cur = db.cursor()
    out = _fetch_progress_bulk(cur, [bid_id]).get(bid_id)
    db.close()
    if not out:
        return jsonify({'bid_id': bid_id, 'in_flight': False,
                        'pct_complete': 0, 'all_done': False,
                        'eta_sec': 0, 'elapsed_sec': 0,
                        'phase': None,
                        'estimated_total_sec': _PHASE_TOTAL_SEC}), 200
    return jsonify(out)


@app.route('/api/bids/progress')
def api_bids_progress_batch():
    raw = request.args.get('ids', '').strip()
    if not raw:
        return jsonify({'bids': []})
    ids = []
    for tok in raw.split(','):
        tok = tok.strip()
        if tok.isdigit():
            ids.append(int(tok))
        if len(ids) >= 200:
            break
    if not ids:
        return jsonify({'bids': []})
    db = get_db()
    cur = db.cursor()
    out = _fetch_progress_bulk(cur, ids)
    db.close()
    return jsonify({'bids': [out[i] for i in ids if i in out]})



def _watchdog_evaluate_once():
    # Per-phase watchdog DISABLED 2026-05-01 — was killing healthy workers on slow-Cox bids.
    # Heartbeat-based recovery (NSSM + manual /admin/workers buttons) is the only safety net now.
    return 0

def _watchdog_evaluate_once_DISABLED_REFERENCE():
    """One pass of the stuck-bid evaluator. Returns the count of bids it
    released so callers can log it.

    Per-phase budgets:
      - vAuto:     started → done within 60s
      - AccuTrade: vauto.done → accutrade.done within 70s
      - iPacket:   accutrade.done → ipacket.done within 30s
      - never started: 30s after claim
      - hard cap:  180s on any active claim
    """
    released = 0
    try:
        db = get_db()
        cur = db.cursor()
        # Cluster-wide singleton: only one gunicorn worker actually evaluates
        # per tick. The 5 others get the lock=false and skip — cheap, safe.
        # Magic key 826341 chosen arbitrarily; pg_try_advisory_lock auto-
        # releases on connection close (which happens at the end of this fn).
        cur.execute("SELECT pg_try_advisory_lock(826341) AS got")
        if not cur.fetchone()['got']:
            db.close()
            return 0
        # All in-flight bids: claimed but no vauto_lookups row yet (i.e.
        # vAuto submit hasn't fired and cleared the claim).
        cur.execute("""
            SELECT b.id AS bid_id,
                   b.vauto_claimed_by AS worker_id,
                   b.vauto_claimed_at AS claimed_at,
                   EXTRACT(EPOCH FROM (NOW() - b.vauto_claimed_at))::int AS age_sec
            FROM bids b
            WHERE b.vauto_claimed_at IS NOT NULL
              AND b.vauto_claimed_by IS NOT NULL
        """)
        in_flight = cur.fetchall()

        for row in in_flight:
            bid_id = row['bid_id']
            worker_id = row['worker_id']
            age_sec = row['age_sec'] or 0

            # Pull the latest progress markers for this bid
            cur.execute("""
                SELECT phase, state, ts,
                       EXTRACT(EPOCH FROM (NOW() - ts))::int AS age_sec
                FROM bid_phase_progress
                WHERE bid_id = %s
            """, (bid_id,))
            markers = {(r['phase'], r['state']): r for r in cur.fetchall()}

            v_started = markers.get(('vauto', 'started'))
            v_done = markers.get(('vauto', 'done'))
            a_done = markers.get(('accutrade', 'done'))
            i_done = markers.get(('ipacket', 'done'))

            stuck_rule = None
            stuck_phase = None
            stuck_age = age_sec

            # Rules in priority order (first match wins)
            if v_started and not v_done and (v_started["age_sec"] or 0) > 180:
                stuck_rule = "vauto>180s"
                stuck_phase = 'vauto'
                stuck_age = v_started['age_sec']
            elif v_done and not a_done and (v_done["age_sec"] or 0) > 160:
                stuck_rule = "accutrade>160s"
                stuck_phase = 'accutrade'
                stuck_age = v_done['age_sec']
            elif a_done and not i_done and (a_done["age_sec"] or 0) > 80:
                stuck_rule = "ipacket>80s"
                stuck_phase = 'ipacket'
                stuck_age = a_done['age_sec']
            elif not v_started and age_sec > 30:
                stuck_rule = 'never_started>30s'
                stuck_phase = 'pre_vauto'
                stuck_age = age_sec
            elif age_sec > 180:
                stuck_rule = 'hard_cap>180s'
                stuck_phase = 'unknown'
                stuck_age = age_sec

            if not stuck_rule:
                continue

            # ── Stuck. Mark degraded, release claim, log, signal exit, alert.
            cur.execute("""
                UPDATE workers
                   SET effective_priority = 'degraded',
                       auto_demoted_at = COALESCE(auto_demoted_at, NOW()),
                       synthetic_ok_count = 0,
                       pending_exit = TRUE,
                       pause_reason = %s,
                       updated_at = NOW()
                 WHERE worker_id = %s
            """, (f'watchdog: stuck {stuck_phase} {stuck_age}s', worker_id))

            cur.execute("""
                UPDATE bids
                   SET vauto_claimed_by = NULL,
                       vauto_claimed_at = NULL
                 WHERE id = %s
            """, (bid_id,))

            cur.execute("""
                UPDATE worker_jobs
                   SET completed_at = NOW(),
                       status = 'released_watchdog',
                       error  = %s,
                       duration_ms = EXTRACT(EPOCH FROM (NOW() - claimed_at))::int * 1000
                 WHERE id = (
                     SELECT id FROM worker_jobs
                     WHERE bid_id = %s AND completed_at IS NULL
                     ORDER BY claimed_at DESC LIMIT 1
                 )
            """, (f'watchdog: {stuck_rule}', bid_id))

            cur.execute("""
                INSERT INTO stuck_log (bid_id, worker_id, phase, age_sec, rule)
                VALUES (%s, %s, %s, %s, %s)
            """, (bid_id, worker_id, stuck_phase, stuck_age, stuck_rule))

            db.commit()
            released += 1

            try:
                _tg_worker_alert(
                    f"⚠️ EW watchdog: worker <b>{worker_id}</b> stuck on bid #{bid_id} "
                    f"({stuck_phase}, {stuck_age}s, {stuck_rule}) — claim released, "
                    f"worker flagged for exit"
                )
            except Exception:
                pass
        db.close()
    except Exception as e:
        print(f"[watchdog] error: {e}")
    return released


_watchdog_thread = None
_watchdog_started = threading.Event()


def _watchdog_loop():
    """Poll every 15s. Daemon thread, dies with the gunicorn worker."""
    while True:
        try:
            _watchdog_evaluate_once()
        except Exception:
            traceback.print_exc()
        time.sleep(15)


def _start_watchdog_once():
    """Idempotent: only one watchdog per process. Called lazily on first
    request to dodge the gunicorn pre-fork issue (threads spawned at import
    time get killed by os.fork)."""
    global _watchdog_thread
    if _watchdog_started.is_set():
        return
    _watchdog_started.set()
    _watchdog_thread = threading.Thread(
        target=_watchdog_loop, daemon=True, name='ew_watchdog'
    )
    _watchdog_thread.start()
    print('[ew_watchdog] started')


@app.before_request
def _ensure_watchdog():
    if not _watchdog_started.is_set():
        _start_watchdog_once()


# ─────────────────────────────────────────────────────────────────────────────
# /admin/workers — internal worker monitoring dashboard
# ─────────────────────────────────────────────────────────────────────────────

# ─── /admin/workers Proxmox enrichment (added 2026-05-01) ───
# Maps EW worker_id -> Proxmox vmid. Hardcoded for now; future: small DB table.
# vm-worker-1 currently runs in vmid 9000 (the original template VM).
# Clones for workers 2-5 will be at vmids 100-103.
def _stamp_rbook_direct_started(bid_id):
    """Record the moment we kick off a direct vAuto BFF rbook fetch.

    The legacy enrichment claim path (enrichment_api.claim_job, jtype=rbook)
    reads enrichment_state.rbook.direct_started_at and defers for 60s after
    that stamp, so the legacy oscar-worker on pve-pc1 only races in when
    direct has had its shot AND hasn't completed. This stops the race-loss
    visible bug observed on bid 1192 (2026-05-12) where the slow 7-min
    legacy scrape beat the 5-min assessment fallback timer because direct
    had a transient 401 (now also retried — see commit f605009).

    Called synchronously from /api/vauto/submit and
    /api/vauto/url_capture_result BEFORE spawning the direct daemon
    thread. Idempotent — overwrites prior stamp on retry.
    """
    import time as _time
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute("""
            UPDATE vauto_lookups
               SET enrichment_state =
                   COALESCE(enrichment_state, '{}'::jsonb)
                   || jsonb_build_object(
                          'rbook',
                          COALESCE(enrichment_state->'rbook', '{}'::jsonb)
                          || jsonb_build_object(
                                 'direct_started_at', %s::text,
                                 'status', 'direct_in_flight'))
             WHERE bid_id = %s
               AND rbook_completed_at IS NULL
        """, (_time.strftime('%Y-%m-%dT%H:%M:%S+00:00', _time.gmtime()),
              bid_id))
        db.commit()
        db.close()
    except Exception as e:
        print(f'[direct-stamp] bid={bid_id} stamp failed: {e}', flush=True)


def _stamp_manheim_direct_started(bid_id):
    """Mirror of _stamp_rbook_direct_started for Manheim transactions.

    Stamped synchronously before spawning the direct manheim daemon
    thread. enrichment_api.claim_job reads
    enrichment_state.manheim.direct_started_at and defers EWEnrichMmr
    (VM 120/121) claims for 60s after the stamp, so legacy only races
    in when direct API has had its shot AND hasn't completed.
    """
    import time as _time
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute("""
            UPDATE vauto_lookups
               SET enrichment_state =
                   COALESCE(enrichment_state, '{}'::jsonb)
                   || jsonb_build_object(
                          'manheim',
                          COALESCE(enrichment_state->'manheim', '{}'::jsonb)
                          || jsonb_build_object(
                                 'direct_started_at', %s::text,
                                 'status', 'direct_in_flight'))
             WHERE bid_id = %s
               AND manheim_completed_at IS NULL
        """, (_time.strftime('%Y-%m-%dT%H:%M:%S+00:00', _time.gmtime()),
              bid_id))
        db.commit()
        db.close()
    except Exception as e:
        print(f'[direct-stamp-mh] bid={bid_id} stamp failed: {e}', flush=True)


def _kick_direct_for_intake(bid_id):
    """Kick direct vAuto BFF API enrichment for a freshly-created bid.

    Spawns a daemon thread that:
      1. Inserts an idempotent vauto_lookups placeholder row (so the
         UPDATE inside kick_direct_enrichment has a target).
      2. Waits 2s — gives any in-flight AI vehicle-decode time to write
         year/make/model into the bids row, since direct API needs them.
      3. Calls kick_direct_enrichment(bid_id, get_db).

    Idempotent: if VM 120 EWEnrichRbook also picks up the bid, the final
    UPDATE filters on `rbook_completed_at IS NULL` so whoever finishes
    second is a silent no-op. If direct API can't run (no vehicle data,
    cookies stale, network), the legacy worker remains the fallback.

    Use from every bid-intake INSERT site instead of the URL-capture-only
    hook. Safe to call multiple times for the same bid_id.
    """
    def _go():
        # No placeholder INSERT — that was breaking phase 1 worker
        # eligibility (claim query says NOT EXISTS row, which our
        # placeholder violated). kick_direct_enrichment now upserts
        # the row itself on success.
        # Wait 2s — gives AI vehicle decode time to populate
        # year/make/model on the bids row before direct API tries.
        import time as _t
        _t.sleep(2)
        try:
            from vauto_enrichment import kick_direct_enrichment
            kick_direct_enrichment(int(bid_id), get_db)
        except Exception as _e:
            print(f"[direct-intake] kick failed bid={bid_id}: {_e}",
                  flush=True)

    try:
        import threading
        threading.Thread(
            target=_go, daemon=True, name=f"intake-direct-{bid_id}"
        ).start()
        print(f"[direct-intake] kicked bid={bid_id}", flush=True)
    except Exception as _err:
        print(f"[direct-intake] thread spawn failed bid={bid_id}: {_err}",
              flush=True)


_WORKER_VMID_MAP = {
    'vm-worker-1': 9000,
    'vm-worker-2': 100,
    'vm-worker-4': 102,
    'vm-worker-5': 103,
    'vm-worker-6': 116,
    'vm-worker-7': 111,
    'vm-worker-8': 112,
    'vm-worker-10': 115,
    "vm-worker-11": 122,
    "vm-worker-12": 123,
    "vm-worker-13": 124,
}

def _worker_to_vmid(worker_id):
    """Return the Proxmox vmid for a given EW worker_id, or None."""
    if not worker_id:
        return None
    return _WORKER_VMID_MAP.get(worker_id)

# In-process 5s cache for Proxmox snapshot data so /api/admin/workers/snapshot
# stays cheap when the page polls every 3-5s. Single-flight via a lock.
_PROXMOX_CACHE = {'ts': 0.0, 'data': None}
_PROXMOX_CACHE_LOCK = threading.Lock()
_PROXMOX_CACHE_TTL = 5.0  # seconds
_PROXMOX_FAST_TIMEOUT = 8.0  # seconds — keeps /api/admin/workers/snapshot snappy

def _proxmox_get_fast(path):
    """Like _proxmox_request('GET', path) but with a 3s timeout — for use
    inside the polled workers snapshot endpoint where 8s blocks the UI."""
    if not PROXMOX_API_BASE or not PROXMOX_API_TOKEN:
        return None, 'Proxmox API not configured'
    url = f'{PROXMOX_API_BASE}/api2/json{path}'
    try:
        resp = requests.get(
            url,
            headers={'Authorization': f'PVEAPIToken={PROXMOX_API_TOKEN}'},
            verify=False,
            timeout=_PROXMOX_FAST_TIMEOUT,
        )
        if resp.status_code >= 400:
            return None, f'HTTP {resp.status_code}'
        return resp.json(), None
    except requests.exceptions.Timeout:
        return None, 'Proxmox timeout (>8s)'
    except requests.exceptions.ConnectionError as e:
        return None, f'Cannot reach Proxmox: {str(e)[:120]}'
    except Exception as e:
        return None, f'Request failed: {str(e)[:120]}'

def _get_proxmox_snapshot_cached():
    """Return a dict with proxmox host stats + per-vmid stats + per-vmid
    snapshot info, cached for ~5s. Always returns a dict; on error, the
    'error' key is populated and 'vms'/'host' may be empty.

    Shape:
      {
        'ok': bool,
        'error': str|None,
        'host': { ... same fields as /api/proxmox/host ... } or None,
        'vms_by_id': { vmid_int: {status,uptime_s,cpu_pct,mem,maxmem,...} },
        'snapshots_by_id': { vmid_int: {'count': int, 'last_age_sec': int|None} },
      }
    """
    now = time.time()
    with _PROXMOX_CACHE_LOCK:
        if _PROXMOX_CACHE['data'] is not None and (now - _PROXMOX_CACHE['ts']) < _PROXMOX_CACHE_TTL:
            return _PROXMOX_CACHE['data']

        result = {
            'ok': False,
            'error': None,
            'host': None,
            'vms_by_id': {},
            'snapshots_by_id': {},
            'services_by_id': {},
        }

        if not PROXMOX_API_BASE or not PROXMOX_API_TOKEN:
            result['error'] = 'Proxmox API not configured'
            _PROXMOX_CACHE['ts'] = now
            _PROXMOX_CACHE['data'] = result
            return result

        # 1) Cluster resources (one call gets all VMs with cpu/mem/uptime/status)
        # If this fails (typically tunnel unreachable), short-circuit — don't
        # spend another 8s × N calls discovering the same outage.
        cluster_failed = False
        try:
            vmdata, vmerr = _proxmox_get_fast('/cluster/resources?type=vm')
            if vmerr:
                result['error'] = vmerr
                cluster_failed = True
            else:
                vm_total = 0
                vm_running = 0
                for v in (vmdata or {}).get('data', []) or []:
                    if v.get('type') != 'qemu':
                        continue
                    vmid = v.get('vmid')
                    if vmid is None:
                        continue
                    vm_total += 1
                    if v.get('status') == 'running':
                        vm_running += 1
                    maxmem = v.get('maxmem') or 0
                    mem = v.get('mem') or 0
                    cpu = v.get('cpu') or 0
                    # mem can exceed maxmem when the virtio-balloon driver is not
                    # reporting back — clamp for display so we never show >100%.
                    mem_display = min(mem, maxmem) if maxmem else mem
                    result['vms_by_id'][int(vmid)] = {
                        'vmid': int(vmid),
                        'name': v.get('name') or f"vm-{vmid}",
                        'status': v.get('status') or 'unknown',
                        'uptime_sec': int(v.get('uptime') or 0),
                        'cpu_pct': round(float(cpu) * 100.0, 2),
                        'mem_used_mb': int(mem_display / (1024 * 1024)) if mem_display else 0,
                        'mem_total_mb': int(maxmem / (1024 * 1024)) if maxmem else 0,
                        'mem_pct': round((mem_display / maxmem) * 100.0, 1) if maxmem else 0.0,
                        'template': bool(v.get('template')),
                    }
                result['_vm_total'] = vm_total
                result['_vm_running'] = vm_running
        except Exception as e:
            result['error'] = result['error'] or f'cluster resources failed: {e}'
            cluster_failed = True

        # 2) Host stats (single node 'pve') — skip if cluster call already failed
        try:
            if cluster_failed:
                raise RuntimeError('skip host call — cluster unreachable')
            hdata, herr = _proxmox_get_fast(f'/nodes/{PROXMOX_NODE}/status')
            if not herr and hdata:
                d = (hdata or {}).get('data', {}) or {}
                cpu = float(d.get('cpu') or 0) * 100.0
                mem = d.get('memory', {}) or {}
                mem_total = mem.get('total') or 0
                mem_used = mem.get('used') or 0
                mem_free = max(mem_total - mem_used, 0)
                result['host'] = {
                    'cpu_pct': round(cpu, 2),
                    'mem_total_gb': round(mem_total / (1024**3), 1) if mem_total else 0,
                    'mem_used_gb': round(mem_used / (1024**3), 1) if mem_used else 0,
                    'mem_free_gb': round(mem_free / (1024**3), 1) if mem_free else 0,
                    'vm_total': result.get('_vm_total', 0),
                    'vm_running': result.get('_vm_running', 0),
                    # Capacity = (free_RAM_GB - 4 headroom) / 4 per worker, floored
                    'capacity_more_workers': max(int((mem_free / (1024**3) - 4) // 4), 0) if mem_total else 0,
                }
        except Exception as e:
            # Don't overwrite a more specific earlier error
            if not result['error']:
                result['error'] = f'host status failed: {e}'

        # 3) Per-VM snapshot lists — only if cluster reachable AND vmid was
        #    found in the cluster list (avoids 8s timeouts on phantom vmids
        #    when Proxmox is fully down).
        # Per-VM snapshot fetch DISABLED 2026-05-02 — was fanning out 10×3s = 30s+
        # over the Cloudflare tunnel. VM stats from /cluster/resources are enough.
        mapped_vmids = set()
        if False:
            mapped_vmids = set(int(v) for v in _WORKER_VMID_MAP.values())
        present_vmids = set(result['vms_by_id'].keys())
        if cluster_failed:
            present_vmids = set()  # don't fan out 5 more 8s timeouts
        for vmid in mapped_vmids & present_vmids:
            try:
                sdata, serr = _proxmox_get_fast(
                    f'/nodes/{_node_for_vmid(vmid)}/qemu/{vmid}/snapshot'
                )
                if serr:
                    continue
                snaps = [s for s in ((sdata or {}).get('data', []) or [])
                         if s.get('name') and s.get('name') != 'current']
                # Use snaptime (unix ts) when present
                latest = 0
                for s in snaps:
                    t = s.get('snaptime') or 0
                    if t and t > latest:
                        latest = t
                last_age = int(now - latest) if latest else None
                result['snapshots_by_id'][vmid] = {
                    'count': len(snaps),
                    'last_age_sec': last_age,
                }
            except Exception:
                pass

        # Services = non-worker VMs in the cluster (e.g., vm-verifier, future helpers).
        # Match: name starts with 'vm-' but NOT 'vm-worker-', skip templates.
        try:
            for vmid_int, vm in result.get('vms_by_id', {}).items():
                name = (vm.get('name') or '').lower()
                if not name.startswith('vm-'):
                    continue
                if name.startswith('vm-worker-'):
                    continue
                if vm.get('template'):
                    continue
                node = _NODE_BY_VMID.get(int(vmid_int)) or PROXMOX_NODE
                result['services_by_id'][int(vmid_int)] = {
                    'name': vm.get('name'),
                    'vmid': int(vmid_int),
                    'status': vm.get('status'),
                    'cpu_pct': vm.get('cpu_pct'),
                    'mem_used_mb': vm.get('mem_used_mb'),
                    'mem_total_mb': vm.get('mem_total_mb'),
                    'mem_pct': vm.get('mem_pct'),
                    'uptime_sec': vm.get('uptime_sec'),
                    'node': node,
                }
        except Exception:
            pass

        result['ok'] = result['error'] is None
        # Strip private keys before caching
        result.pop('_vm_total', None)
        result.pop('_vm_running', None)
        _PROXMOX_CACHE['ts'] = now
        _PROXMOX_CACHE['data'] = result
        return result
# ─── end Proxmox enrichment helpers ───


@app.route('/admin/ai-accuracy')
def admin_ai_accuracy():
    """AI vs actual purchase reconciliation dashboard. Reads from
    `ai_accuracy` table populated by /opt/expwholesale/reconcile_ai_accuracy.py
    (cron daily at 6 AM ET, matches bids by VIN to LSL deals.purchase_cost)."""
    return render_template('admin_ai_accuracy.html')


@app.route('/api/admin/ai-accuracy/data')
def api_admin_ai_accuracy_data():
    """Aggregated data for the AI accuracy dashboard.
    Query params: from=YYYY-MM-DD, to=YYYY-MM-DD (inclusive). Falls back to
    last 30 days if not provided.
    Filters on COALESCE(actual_purchased_at, ai_assessed_at)."""
    import re as _re
    DATE_RE = _re.compile(r'^\d{4}-\d{2}-\d{2}$')
    f_arg = request.args.get('from', '').strip()
    t_arg = request.args.get('to', '').strip()
    if DATE_RE.match(f_arg) and DATE_RE.match(t_arg):
        date_from = f_arg
        date_to   = t_arg
    else:
        # Default last 30 days
        from datetime import datetime as _dt, timedelta as _td
        date_to   = _dt.utcnow().date().isoformat()
        date_from = (_dt.utcnow().date() - _td(days=30)).isoformat()

    db = get_db()
    cur = db.cursor()
    try:
        # Use ai_assessed_at when actual_purchased_at is null (rare).
        # Inclusive of both endpoints — date_to is treated as end-of-day.
        cur.execute("""
            SELECT bid_id, vin, year, make, model, mileage,
                   ai_recommendation, ai_confidence_low, ai_confidence_high,
                   ai_assessed_at, actual_purchase_cost, actual_purchased_at,
                   delta, delta_pct, abs_delta_pct, in_confidence_range,
                   lsl_deal_code
            FROM ai_accuracy
            WHERE COALESCE(actual_purchased_at, ai_assessed_at)::date BETWEEN %s::date AND %s::date
              AND delta_pct IS NOT NULL AND bid_id > 0
            ORDER BY actual_purchased_at DESC NULLS LAST, ai_assessed_at DESC
        """, (date_from, date_to))
        rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            for k, v in r.items():
                if hasattr(v, 'isoformat'):
                    r[k] = v.isoformat()
                elif hasattr(v, '__float__') and v is not None and not isinstance(v, bool):
                    try: r[k] = float(v)
                    except (TypeError, ValueError): pass

        # Top-line stats
        n = len(rows)
        if n > 0:
            abs_pcts = sorted(r['abs_delta_pct'] for r in rows
                              if r.get('abs_delta_pct') is not None)
            signed_pcts = [r['delta_pct'] for r in rows if r.get('delta_pct') is not None]
            in_range = sum(1 for r in rows if r.get('in_confidence_range'))
            # Total $ implications. delta = actual - AI rec.
            #   delta > 0 = actual paid MORE than AI rec  → if we'd bid at AI's
            #              number we would have lost the deal (UNDERBID by Δ).
            #   delta < 0 = actual paid LESS than AI rec  → if we'd followed AI
            #              we would have OVERPAID by |Δ|.
            total_underbid = sum(r['delta'] for r in rows
                                 if r.get('delta') is not None and r['delta'] > 0)
            total_overpaid = sum(-r['delta'] for r in rows
                                 if r.get('delta') is not None and r['delta'] < 0)
            n_underbid = sum(1 for r in rows
                             if r.get('delta') is not None and r['delta'] > 0)
            n_overpaid = sum(1 for r in rows
                             if r.get('delta') is not None and r['delta'] < 0)
            stats = {
                'n_matches': n,
                'median_abs_pct':  round(abs_pcts[len(abs_pcts)//2], 2) if abs_pcts else None,
                'mean_signed_pct': round(sum(signed_pcts)/len(signed_pcts), 2) if signed_pcts else None,
                'in_confidence_count': in_range,
                'in_confidence_pct': round(100.0 * in_range / n, 1),
                'p25_abs_pct': round(abs_pcts[len(abs_pcts)//4], 2) if len(abs_pcts) >= 4 else None,
                'p75_abs_pct': round(abs_pcts[(3*len(abs_pcts))//4], 2) if len(abs_pcts) >= 4 else None,
                'within_5pct':  sum(1 for p in abs_pcts if p <= 5),
                'within_10pct': sum(1 for p in abs_pcts if p <= 10),
                'total_underbid_dollars': int(total_underbid),
                'total_overpaid_dollars': int(total_overpaid),
                'n_underbid':            n_underbid,
                'n_overpaid':            n_overpaid,
                'net_dollars':           int(total_underbid - total_overpaid),
            }
        else:
            stats = {'n_matches': 0}

        # Per-make leaderboard
        cur.execute("""
            SELECT make,
                   COUNT(*) AS n,
                   ROUND(AVG(abs_delta_pct), 2) AS mean_abs_pct,
                   ROUND(AVG(delta_pct), 2) AS mean_signed_pct,
                   SUM(CASE WHEN in_confidence_range THEN 1 ELSE 0 END) AS in_range_n
            FROM ai_accuracy
            WHERE COALESCE(actual_purchased_at, ai_assessed_at)::date BETWEEN %s::date AND %s::date
              AND make IS NOT NULL
              AND delta_pct IS NOT NULL AND bid_id > 0
            GROUP BY make
            HAVING COUNT(*) >= 1
            ORDER BY n DESC, mean_abs_pct ASC
        """, (date_from, date_to))
        per_make = [dict(r) for r in cur.fetchall()]
        for m in per_make:
            for k, v in m.items():
                if hasattr(v, '__float__') and v is not None and not isinstance(v, bool):
                    try: m[k] = float(v)
                    except (TypeError, ValueError): pass

        # Histogram bins for abs_delta_pct (0-2, 2-5, 5-10, 10-20, 20+)
        bins = [(0, 2), (2, 5), (5, 10), (10, 20), (20, 999)]
        histogram = []
        for lo, hi in bins:
            c = sum(1 for r in rows
                    if r.get('abs_delta_pct') is not None
                    and lo <= float(r['abs_delta_pct']) < hi)
            histogram.append({'range': f'{lo}-{hi if hi < 999 else "+"}%',
                              'count': c})

        # Trend: rolling 7-day median abs_delta_pct (within selected range)
        cur.execute("""
            WITH per_day AS (
                SELECT date_trunc('day', actual_purchased_at)::date AS day,
                       abs_delta_pct
                FROM ai_accuracy
                WHERE actual_purchased_at IS NOT NULL
                  AND actual_purchased_at::date BETWEEN %s::date AND %s::date
                  AND delta_pct IS NOT NULL AND bid_id > 0
            )
            SELECT day,
                   COUNT(*) AS n,
                   ROUND(percentile_cont(0.5) WITHIN GROUP (ORDER BY abs_delta_pct)::numeric, 2)
                     AS median_abs_pct
            FROM per_day GROUP BY day ORDER BY day
        """, (date_from, date_to))
        trend = [{'day': r['day'].isoformat() if r['day'] else None,
                  'n': r['n'],
                  'median_abs_pct': float(r['median_abs_pct']) if r['median_abs_pct'] is not None else None}
                 for r in cur.fetchall()]

        return jsonify({
            'date_from': date_from,
            'date_to':   date_to,
            'stats':     stats,
            'rows':      rows,
            'per_make':  per_make,
            'histogram': histogram,
            'trend':     trend,
        })
    finally:
        db.close()


# ─── Bias correction admin API ──────────────────────────────────────────
# Reads/writes ai_correction_config (versioned) + bias_segments (read-only).
# UI lives in /admin/ai-accuracy (admin_ai_accuracy.html).

@app.route('/api/correction/active')
def api_correction_active():
    try:
        import bias_correction as _bc
        cfg = _bc.get_active_config()
        # Top segments table for the active panel
        db = get_db()
        cur = db.cursor()
        cur.execute("""
            SELECT make, model, year_band, mileage_band, n, bias_pct,
                   abs_pct, stddev_pct, refreshed_at
              FROM bias_segments
             WHERE window_days = %s AND n >= 2
             ORDER BY n DESC, ABS(bias_pct) DESC
             LIMIT 200
        """, (cfg['config'].get('window_days', 30),))
        segments = []
        for r in cur.fetchall():
            d = dict(r)  # get_db() uses RealDictCursor — r is already a dict
            for k in ('bias_pct','abs_pct','stddev_pct'):
                if d.get(k) is not None: d[k] = float(d[k])
            if d.get('refreshed_at') and hasattr(d['refreshed_at'], 'isoformat'):
                d['refreshed_at'] = d['refreshed_at'].isoformat()
            d['segment_key'] = f"{d['make']}|{d['model']}|{d['year_band']}|{d['mileage_band']}"
            segments.append(d)
        db.close()
        return jsonify({
            'config_id': cfg.get('id'),
            'active': cfg.get('active'),
            'config': cfg.get('config'),
            'segments': segments,
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/correction/preview', methods=['POST'])
def api_correction_preview():
    """Replay PROPOSED config against trailing window without saving.
    Body: {config: {...}, window_days: 30}. Mutates an in-memory copy of
    the config table — no DB write."""
    try:
        import bias_correction as _bc
        body = request.get_json(silent=True) or {}
        proposed = body.get('config') or {}
        window = int(body.get('window_days') or proposed.get('window_days') or 30)
        # Patch get_active_config in-process for the duration of preview by
        # injecting a fake row into a local config eval. Simpler: just re-run
        # apply_correction with the proposed config injected by monkey-patch.
        orig = _bc.get_active_config
        def _stub(conn=None):
            return {'id': None, 'active': True,
                    'config': {**_bc.DEFAULT_CONFIG, **proposed}}
        _bc.get_active_config = _stub
        try:
            r = _bc.preview_impact(window_days=window)
        finally:
            _bc.get_active_config = orig
        return jsonify(r)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/correction/save', methods=['POST'])
def api_correction_save():
    """Save a NEW versioned config row. Optionally activate it."""
    try:
        import json as _json
        body = request.get_json(silent=True) or {}
        config = body.get('config')
        description = body.get('description') or ''
        activate = bool(body.get('activate'))
        if not config:
            return jsonify({'error': 'missing config'}), 400
        # Basic schema validation
        if 'tiers' in config and not isinstance(config['tiers'], list):
            return jsonify({'error': 'tiers must be a list'}), 400
        if 'window_days' in config and not isinstance(config['window_days'], int):
            return jsonify({'error': 'window_days must be int'}), 400
        db = get_db()
        cur = db.cursor()
        if activate:
            cur.execute("UPDATE ai_correction_config SET active = FALSE")
        cur.execute("""
            INSERT INTO ai_correction_config (active, config, description, saved_by)
            VALUES (%s, %s::jsonb, %s, %s) RETURNING id
        """, (activate, _json.dumps(config), description,
              session.get('user') or 'admin'))
        new_row = cur.fetchone()
        new_id = new_row['id'] if isinstance(new_row, dict) else new_row[0]
        db.commit()
        db.close()
        return jsonify({'ok': True, 'id': new_id, 'active': activate})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/correction/segment-override', methods=['POST'])
def api_correction_segment_override():
    """Per-make/model override management.
    Body shapes:
      {make, model, action: 'remove'}
      {make, model, action: 'force_zero'}
      {make, model, action: 'fixed_adjustment', adjustment_pct: -50..+50}
    Backward-compat: {segment_key, action: 'add'|'remove'} also accepted as force_zero.
    """
    try:
        import bias_correction as _bc
        import json as _json
        from datetime import datetime as _dt
        body = request.get_json(silent=True) or {}

        # Resolve make/model key (accept either explicit or legacy segment_key)
        if body.get('make') and body.get('model'):
            key = _bc._make_model_key(body['make'], body['model'])
        elif body.get('segment_key'):
            parts = str(body['segment_key']).split('|')
            key = '|'.join(parts[:2]).upper() if len(parts) >= 2 else body['segment_key'].upper()
        else:
            return jsonify({'error': 'make+model OR segment_key required'}), 400

        action = body.get('action') or 'force_zero'
        # Map legacy 'add' to 'force_zero'
        if action == 'add':
            action = 'force_zero'
        if action not in ('remove', 'force_zero', 'fixed_adjustment'):
            return jsonify({'error': 'action must be remove|force_zero|fixed_adjustment'}), 400

        adj_pct = None
        if action == 'fixed_adjustment':
            try:
                adj_pct = float(body.get('adjustment_pct'))
            except (TypeError, ValueError):
                return jsonify({'error': 'fixed_adjustment requires numeric adjustment_pct'}), 400
            if abs(adj_pct) > 50:
                return jsonify({'error': 'adjustment_pct must be between -50 and +50'}), 400

        db = get_db()
        cur = db.cursor()
        # If no active config, create one (seed from defaults) so the override sticks
        cur.execute("""SELECT id, config FROM ai_correction_config
                       WHERE active = TRUE ORDER BY saved_at DESC LIMIT 1""")
        r = cur.fetchone()
        def _r_get(row, key, idx):
            if row is None: return None
            return row[key] if isinstance(row, dict) else row[idx]
        if not r:
            cfg = dict(_bc.DEFAULT_CONFIG)
            cur.execute("""INSERT INTO ai_correction_config (active, config, description, saved_by)
                           VALUES (TRUE, %s::jsonb, %s, %s) RETURNING id""",
                        (_json.dumps(cfg),
                         'auto-created by per-segment override save',
                         session.get('user') or 'admin'))
            cfg_id = _r_get(cur.fetchone(), 'id', 0)
        else:
            cfg_id = _r_get(r, 'id', 0)
            raw_cfg = _r_get(r, 'config', 1)
            cfg = raw_cfg if isinstance(raw_cfg, dict) else _json.loads(raw_cfg)

        overrides = _bc._normalize_overrides(cfg.get('segment_overrides') or [])
        # Strip any existing entry for this key first
        overrides = [o for o in overrides if (o.get('key') or '').upper() != key]
        # Add new entry unless action is 'remove'
        if action != 'remove':
            entry = {'key': key, 'type': action,
                     'set_at': _dt.utcnow().isoformat() + 'Z',
                     'set_by': session.get('user') or 'admin'}
            if action == 'fixed_adjustment':
                entry['adjustment_pct'] = adj_pct
            overrides.append(entry)
        cfg['segment_overrides'] = overrides
        cur.execute("""UPDATE ai_correction_config SET config = %s::jsonb
                       WHERE id = %s""", (_json.dumps(cfg), cfg_id))
        db.commit()
        db.close()
        return jsonify({'ok': True, 'key': key, 'action': action,
                        'segment_overrides': overrides})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/correction/lookup-segment')
def api_correction_lookup_segment():
    """Drill-down for the per-bid modal.
    Query params: ?make=X&model=Y&year=Z&mileage=W (year/mileage optional).
    Returns: {key, levels: [3-level fallback data], active_override, last_5_bids}
    """
    try:
        import bias_correction as _bc
        make = (request.args.get('make') or '').strip()
        model = (request.args.get('model') or '').strip()
        if not make or not model:
            return jsonify({'error': 'make + model required'}), 400
        year = request.args.get('year')
        try: year = int(year) if year else None
        except (TypeError, ValueError): year = None
        mileage = request.args.get('mileage')
        try: mileage = int(mileage) if mileage else None
        except (TypeError, ValueError): mileage = None

        # 3-level fallback data
        levels = _bc._lookup_all_levels(make, model, year, mileage, window_days=30)
        for lvl in levels:
            for k in ('bias_pct', 'abs_pct', 'stddev_pct'):
                if lvl.get(k) is not None:
                    lvl[k] = float(lvl[k])

        # Active override (if any)
        cfg_row = _bc.get_active_config()
        ov = _bc._get_override(make, model, cfg_row['config'].get('segment_overrides') or [])
        key = _bc._make_model_key(make, model)

        # Last 5 unique-VIN bids in window for this make+model
        db = get_db()
        cur = db.cursor()
        cur.execute("""
            SELECT bid_id, vin, year, mileage,
                   ai_recommendation, actual_purchase_cost, delta_pct,
                   actual_purchased_at::date AS purchased_date
              FROM (
                SELECT DISTINCT ON (vin) bid_id, vin, year, mileage,
                       ai_recommendation, actual_purchase_cost, delta_pct,
                       actual_purchased_at, ai_assessed_at
                  FROM ai_accuracy
                 WHERE UPPER(make) = %s AND UPPER(model) = %s
                   AND delta_pct IS NOT NULL AND bid_id > 0
                   AND vin IS NOT NULL AND vin <> ''
                   AND reconciled_at > NOW() - INTERVAL '60 days'
                   AND actual_purchased_at > NOW() - INTERVAL '60 days'
                 ORDER BY vin, ai_assessed_at DESC NULLS LAST
              ) deduped
             ORDER BY actual_purchased_at DESC LIMIT 5
        """, (make.upper(), model.upper()))
        cols = ['bid_id','vin','year','mileage','ai_recommendation',
                'actual_purchase_cost','delta_pct','purchased_date']
        bids = []
        for r in cur.fetchall():
            d = dict(r) if isinstance(r, dict) else dict(zip(cols, r))
            if d.get('delta_pct') is not None: d['delta_pct'] = float(d['delta_pct'])
            if d.get('purchased_date') and hasattr(d['purchased_date'], 'isoformat'):
                d['purchased_date'] = d['purchased_date'].isoformat()
            bids.append(d)
        db.close()

        return jsonify({
            'key': key,
            'make': make.upper(), 'model': model.upper(),
            'levels': levels,
            'active_override': ov,
            'last_5_bids': bids,
            'config_active': cfg_row.get('active'),
            'config_id': cfg_row.get('id'),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/correction/refresh', methods=['POST'])
def api_correction_refresh():
    """Manual segment refresh — for admin button."""
    try:
        import bias_correction as _bc
        r30 = _bc.refresh_segments(window_days=30)
        r90 = _bc.refresh_segments(window_days=90)
        return jsonify({'30d': r30, '90d': r90})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/admin/workers')
def admin_workers():
    """Render worker monitoring dashboard. Auth handled by global require_login."""
    return render_template('admin_workers.html')


@app.route('/api/admin/workers/snapshot')
def api_admin_workers_snapshot():
    """Aggregated state for /admin/workers. Returns workers, stuck bids, and
    recent activity in one payload to keep the client polling cheap."""
    db = get_db()
    cur = db.cursor()

    # Workers grid — include current claim if any, plus today's lookup count.
    cur.execute("""
        SELECT
            w.worker_id,
            w.role,
            w.priority,
            w.effective_priority,
            w.paused,
            w.pause_reason,
            COALESCE(w.pending_exit, FALSE) AS pending_exit,
            w.last_heartbeat,
            EXTRACT(EPOCH FROM (NOW() - w.last_heartbeat))::int AS last_hb_sec,
            w.chrome_alive,
            w.lookups_done,
            w.last_lookup_at,
            w.last_seen_ip::text AS last_seen_ip,
            w.consecutive_failures,
            w.auto_demoted_at,
            (SELECT COUNT(*) FROM worker_jobs wj
              WHERE wj.worker_id = w.worker_id
                AND wj.status = 'ok'
                AND wj.completed_at::date = (NOW() AT TIME ZONE 'America/New_York')::date
            ) AS lookups_today,
            (SELECT b.id FROM bids b
              WHERE b.vauto_claimed_by = w.worker_id
                AND b.vauto_claimed_at IS NOT NULL
              ORDER BY b.vauto_claimed_at DESC LIMIT 1
            ) AS current_bid_id,
            (SELECT b.vauto_claimed_at FROM bids b
              WHERE b.vauto_claimed_by = w.worker_id
                AND b.vauto_claimed_at IS NOT NULL
              ORDER BY b.vauto_claimed_at DESC LIMIT 1
            ) AS current_claim_at
        FROM workers w
        ORDER BY
            CASE COALESCE(w.effective_priority, w.priority)
                WHEN 'primary' THEN 1
                WHEN 'standby' THEN 2
                WHEN 'degraded' THEN 3
                ELSE 4
            END,
            CAST(NULLIF(REGEXP_REPLACE(w.worker_id, '[^0-9]', '', 'g'), '') AS INTEGER) ASC NULLS LAST
    """)
    workers = []
    for r in cur.fetchall():
        d = dict(r)
        # Per-phase progress for current claim (if any)
        d['phases'] = {'vauto': None, 'accutrade': None, 'ipacket': None}
        d['current_elapsed_sec'] = None
        if d.get('current_bid_id'):
            cur.execute("""
                SELECT phase, state, ts FROM bid_phase_progress
                WHERE bid_id = %s
            """, (d['current_bid_id'],))
            for pr in cur.fetchall():
                key = pr['phase']
                if key in d['phases']:
                    cur_state = d['phases'][key]
                    if cur_state is None or pr['state'] == 'done':
                        d['phases'][key] = pr['state']
            if d.get('current_claim_at'):
                cur.execute("""
                    SELECT EXTRACT(EPOCH FROM (NOW() - %s))::int AS s
                """, (d['current_claim_at'],))
                d['current_elapsed_sec'] = cur.fetchone()['s']

        # ISO-format timestamps for JSON
        for k in ('last_heartbeat', 'last_lookup_at', 'auto_demoted_at',
                  'current_claim_at'):
            v = d.get(k)
            if hasattr(v, 'isoformat'):
                d[k] = v.isoformat()
        workers.append(d)

    # Stuck bids (live snapshot — same predicates as watchdog but no action).
    cur.execute("""
        SELECT b.id AS bid_id, b.vin, b.year, b.make, b.model,
               b.vauto_claimed_by AS worker_id,
               b.vauto_claimed_at AS claimed_at,
               EXTRACT(EPOCH FROM (NOW() - b.vauto_claimed_at))::int AS age_sec
        FROM bids b
        WHERE b.vauto_claimed_at IS NOT NULL
          AND b.vauto_claimed_by IS NOT NULL
        ORDER BY b.vauto_claimed_at ASC
    """)
    in_flight = cur.fetchall()
    stuck = []
    for r in in_flight:
        bid_id = r['bid_id']
        cur.execute("""
            SELECT phase, state, ts,
                   EXTRACT(EPOCH FROM (NOW() - ts))::int AS phase_age_sec
            FROM bid_phase_progress
            WHERE bid_id = %s
            ORDER BY ts DESC
        """, (bid_id,))
        markers = cur.fetchall()
        last_phase = markers[0]['phase'] if markers else 'pre-vauto'
        last_state = markers[0]['state'] if markers else None
        last_age = markers[0]['phase_age_sec'] if markers else r['age_sec']

        # Same tripwires as watchdog
        looks_stuck = False
        age = r['age_sec'] or 0
        # Check for any unbalanced started/done
        marker_map = {(m['phase'], m['state']): m for m in markers}
        v_started = marker_map.get(('vauto', 'started'))
        v_done = marker_map.get(('vauto', 'done'))
        a_done = marker_map.get(('accutrade', 'done'))
        i_done = marker_map.get(('ipacket', 'done'))
        if v_started and not v_done and (v_started['phase_age_sec'] or 0) > 60:
            looks_stuck = True
        elif v_done and not a_done and (v_done['phase_age_sec'] or 0) > 70:
            looks_stuck = True
        elif a_done and not i_done and (a_done['phase_age_sec'] or 0) > 30:
            looks_stuck = True
        elif not v_started and age > 30:
            looks_stuck = True
        elif age > 180:
            looks_stuck = True

        item = {
            'bid_id': bid_id,
            'vin': r['vin'],
            'ymm': ' '.join(filter(None, [str(r.get('year') or '').strip(),
                                          r.get('make') or '',
                                          r.get('model') or ''])).strip(),
            'worker_id': r['worker_id'],
            'claimed_at': r['claimed_at'].isoformat() if r['claimed_at'] else None,
            'age_sec': age,
            'last_phase': last_phase,
            'last_state': last_state,
            'last_phase_age_sec': last_age,
            'stuck': looks_stuck,
        }
        if looks_stuck:
            stuck.append(item)

    # Recent activity timeline — last 50 completed bids
    cur.execute("""
        SELECT wj.bid_id, wj.worker_id, wj.claimed_at, wj.completed_at,
               wj.duration_ms, wj.status,
               b.vin, b.year, b.make, b.model
        FROM worker_jobs wj
        LEFT JOIN bids b ON b.id = wj.bid_id
        WHERE wj.completed_at IS NOT NULL
        ORDER BY wj.completed_at DESC
        LIMIT 50
    """)
    activity = []
    for r in cur.fetchall():
        bid_id = r['bid_id']
        # Pull phase markers
        cur.execute("""
            SELECT phase, state, ts FROM bid_phase_progress
            WHERE bid_id = %s
            ORDER BY ts ASC
        """, (bid_id,))
        markers = []
        for pm in cur.fetchall():
            markers.append({
                'phase': pm['phase'], 'state': pm['state'],
                'ts': pm['ts'].isoformat() if pm['ts'] else None,
            })
        activity.append({
            'bid_id': bid_id,
            'worker_id': r['worker_id'],
            'vin': r['vin'],
            'ymm': ' '.join(filter(None, [str(r.get('year') or '').strip(),
                                          r.get('make') or '',
                                          r.get('model') or ''])).strip(),
            'claimed_at': r['claimed_at'].isoformat() if r['claimed_at'] else None,
            'completed_at': r['completed_at'].isoformat() if r['completed_at'] else None,
            'duration_ms': r['duration_ms'],
            'status': r['status'],
            'phases': markers,
        })

    db.close()

    # ─── Proxmox enrichment (added 2026-05-01) ───
    # Merge per-VM stats into each worker; never let Proxmox failure break the
    # snapshot endpoint. All errors are surfaced in worker['vm']['error'] or
    # the top-level 'proxmox' block.
    try:
        px = _get_proxmox_snapshot_cached()
        for w in workers:
            vmid = _worker_to_vmid(w.get('worker_id'))
            if vmid is None:
                w['vm'] = None
                continue
            vm_stats = px.get('vms_by_id', {}).get(vmid)
            if not vm_stats:
                # VM not yet provisioned (e.g., clones not built yet) or proxmox down
                w['vm'] = {
                    'vmid': vmid,
                    'status': 'absent',
                    'error': px.get('error') or 'VM not found in Proxmox cluster',
                }
                continue
            snap = px.get('snapshots_by_id', {}).get(vmid, {})
            w['vm'] = {
                'vmid': vmid,
                'status': vm_stats.get('status'),
                'uptime_sec': vm_stats.get('uptime_sec'),
                'cpu_pct': vm_stats.get('cpu_pct'),
                'mem_used_mb': vm_stats.get('mem_used_mb'),
                'mem_total_mb': vm_stats.get('mem_total_mb'),
                'mem_pct': vm_stats.get('mem_pct'),
                'snapshot_count': snap.get('count', 0),
                'last_snapshot_age_sec': snap.get('last_age_sec'),
            }
        proxmox_block = {
            'ok': px.get('ok', False),
            'error': px.get('error'),
            'host': px.get('host'),
        }
    except Exception as e:
        # Defensive: never let enrichment break the page
        for w in workers:
            if 'vm' not in w:
                w['vm'] = None
        proxmox_block = {'ok': False, 'error': f'enrichment exception: {e}', 'host': None}
    # ─── end Proxmox enrichment ───

    # Services list (non-worker VMs). Pulled from same Proxmox snapshot
    # cache used above - never adds another API round-trip.
    services_list = []
    try:
        px_for_svc = _get_proxmox_snapshot_cached()
        svc_map = px_for_svc.get('services_by_id', {}) or {}
        for vmid_int in sorted(svc_map.keys()):
            services_list.append(svc_map[vmid_int])
    except Exception:
        services_list = []

    return jsonify({
        'workers': workers,
        'stuck': stuck,
        'activity': activity,
        'proxmox': proxmox_block,
        'services': services_list,
        'now': datetime.now().isoformat(),
    })


@app.route('/api/admin/workers/<worker_id>/pause', methods=['POST'])
def api_admin_worker_pause(worker_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        UPDATE workers SET paused = TRUE, pause_reason = %s, updated_at = NOW()
         WHERE worker_id = %s
    """, ('manual: admin dashboard', worker_id))
    db.commit()
    db.close()
    return jsonify({'ok': True})


@app.route('/api/admin/workers/<worker_id>/unpause', methods=['POST'])
def api_admin_worker_unpause(worker_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        UPDATE workers SET paused = FALSE, pause_reason = NULL, updated_at = NOW()
         WHERE worker_id = %s
    """, (worker_id,))
    db.commit()
    db.close()
    return jsonify({'ok': True})


@app.route('/api/admin/workers/<worker_id>/exit', methods=['POST'])
def api_admin_worker_exit(worker_id):
    """Set pending_exit so worker self-terminates on next heartbeat."""
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        UPDATE workers SET pending_exit = TRUE, updated_at = NOW()
         WHERE worker_id = %s
    """, (worker_id,))
    db.commit()
    db.close()
    return jsonify({'ok': True})


@app.route('/api/admin/workers/release/<int:bid_id>', methods=['POST'])
def api_admin_release_claim(bid_id):
    """Manual claim release from the dashboard."""
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        UPDATE bids SET vauto_claimed_by = NULL, vauto_claimed_at = NULL
         WHERE id = %s
    """, (bid_id,))
    cur.execute("""
        UPDATE worker_jobs
           SET completed_at = NOW(),
               status = 'released_admin',
               duration_ms = EXTRACT(EPOCH FROM (NOW() - claimed_at))::int * 1000
         WHERE id = (
             SELECT id FROM worker_jobs
             WHERE bid_id = %s AND completed_at IS NULL
             ORDER BY claimed_at DESC LIMIT 1
         )
    """, (bid_id,))
    db.commit()
    db.close()
    return jsonify({'ok': True})




@app.route('/api/admin/bid/<int:bid_id>/force-reprocess', methods=['POST'])
def api_admin_force_reprocess(bid_id):
    """Hard reset: wipe all 3 phase-1 lookups + claim + assessment so
    the next /api/vauto/pending poll re-claims this bid fresh from a
    worker. Wired to the 'Force re-process' button on bid.html.

    Does NOT clear driver_notified_at — preserves SMS-back idempotency
    so the customer doesn't get re-texted on every retry.

    Added 2026-05-08 — this endpoint was missing for at least 3 days
    (button was dead-clicking and silently failing on 404).
    """
    db = get_db()
    cur = db.cursor()

    # Verify bid exists first
    cur.execute('SELECT id, vin FROM bids WHERE id=%s', (bid_id,))
    row = cur.fetchone()
    if not row:
        db.close()
        return jsonify({'ok': False, 'error': 'bid not found'}), 404

    # Wipe phase-1 lookups. PHASE_D_VERIFY_GATE_2026_05_15: iPacket has a
    # ~1h rate limit on same-VIN resubmits — a quick reprocess can lose the
    # sticker entirely. Preserve iPacket rows <5min old that succeeded
    # (looked_up_at fresh, not_available=false). Operator can override with
    # ?force_ipacket=1 query string OR JSON body {force_ipacket: true}.
    _force_ipkt = (
        request.args.get('force_ipacket', '').lower() in ('1', 'true', 'yes')
        or bool((request.get_json(silent=True) or {}).get('force_ipacket'))
    )
    if _force_ipkt:
        cur.execute('DELETE FROM ipacket_lookups WHERE bid_id=%s', (bid_id,))
    else:
        cur.execute("""DELETE FROM ipacket_lookups
             WHERE bid_id = %s
               AND (looked_up_at IS NULL
                    OR looked_up_at < NOW() - INTERVAL '5 minutes'
                    OR not_available = true)""", (bid_id,))
    n_ipacket = cur.rowcount
    cur.execute('DELETE FROM accutrade_lookups WHERE bid_id=%s', (bid_id,))
    n_accu = cur.rowcount
    cur.execute('DELETE FROM vauto_lookups WHERE bid_id=%s', (bid_id,))
    n_vauto = cur.rowcount

    # Reset claim + assessment so worker re-claims and assess re-fires
    cur.execute("""
        UPDATE bids
           SET vauto_claimed_by = NULL,
               vauto_claimed_at = NULL,
               ai_assessed_at = NULL,
               ai_price = NULL,
               ai_assessment = NULL
         WHERE id = %s
    """, (bid_id,))

    # Close any in-flight worker_jobs so worker_jobs reflect the reset
    cur.execute("""
        UPDATE worker_jobs
           SET completed_at = NOW(),
               status = 'released_admin_reprocess',
               duration_ms = EXTRACT(EPOCH FROM (NOW()-claimed_at))::int*1000
         WHERE bid_id = %s AND completed_at IS NULL
    """, (bid_id,))
    n_worker_jobs = cur.rowcount

    db.commit()
    db.close()
    return jsonify({
        'ok': True,
        'bid_id': bid_id,
        'wiped': {
            'vauto_lookups': n_vauto,
            'accutrade_lookups': n_accu,
            'ipacket_lookups': n_ipacket,
            'in_flight_worker_jobs_closed': n_worker_jobs,
        },
        'msg': 'Bid reset — next vauto poll will re-claim',
    })


@app.route('/api/vauto/release_claim', methods=['POST'])
def api_vauto_release_claim():
    """Worker hands a claimed bid back when it can't complete (network
    failure, Chrome crash, Cox session lost, etc.). Without this,
    abandoned bids sit `in_progress` until the 5-min stale-claim sweep —
    a noticeable client-visible delay. With this, recovery is immediate:
    the worker calls release_claim, server clears the claim, the next
    poll (theirs or another worker's) re-claims and retries.

    Body: {bid_id, reason}  — reason is informational, logged for debugging.
    """
    data = request.get_json(silent=True) or {}
    try:
        bid_id = int(data.get('bid_id'))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'bid_id required'}), 400
    reason = (data.get('reason') or 'worker_released')[:64]

    db = get_db()
    cur = db.cursor()
    cur.execute("""
        UPDATE bids SET vauto_claimed_by = NULL, vauto_claimed_at = NULL
         WHERE id = %s
    """, (bid_id,))
    cur.execute("""
        UPDATE worker_jobs
           SET completed_at = NOW(), status = 'released_worker', error = %s
         WHERE id = (
             SELECT id FROM worker_jobs
             WHERE bid_id = %s AND completed_at IS NULL
             ORDER BY claimed_at DESC LIMIT 1
         )
    """, (reason, bid_id))
    db.commit()
    db.close()
    return jsonify({'ok': True, 'bid_id': bid_id, 'reason': reason})


@app.route('/api/vauto/find_click_target', methods=['POST'])
def api_vauto_find_click_target():
    """Given a screenshot of the vAuto Appraisals list (filtered to one
    VIN), ask Gemini Vision for the pixel coordinates of the clickable
    Make/Model link. Used by the Beelink verifier when ExtJS grids
    render rows on Canvas (DOM is blind)."""
    image = request.files.get('image')
    vin = (request.form.get('vin') or '').strip().upper()
    label = (request.form.get('label') or '').strip()  # e.g. "2021 Porsche Taycan"
    if not image or not vin:
        return jsonify({'error': 'image and vin required'}), 400
    img_bytes = image.read()
    if not img_bytes:
        return jsonify({'error': 'empty image'}), 400
    prompt = (
        "You are looking at a screenshot of a vAuto Appraisals list page. "
        f"The list is filtered to a specific vehicle with VIN {vin}"
        + (f" (a {label})" if label else "")
        + ". One or more rows will show this VIN. The clickable link for "
        "each row is the Make/Model text (shown in blue, e.g. '2024 BMW "
        "X5 xDrive40i') in the second column.\n"
        "Return ONLY a JSON object — no prose, no markdown fence — with:\n"
        '  {"found": true, "x": <int>, "y": <int>, "label": "<clicked text>"}\n'
        "where x,y are the pixel coordinates (origin top-left) of the "
        "CENTER of the TOPMOST (most recent) Make/Model link for this VIN.\n"
        'If no matching row exists, return: {"found": false}'
    )
    text = gemini_call(prompt, image_bytes=img_bytes, mime='image/png',
                       model='gemini-2.5-flash', max_tokens=200, temperature=0.1)
    if not text:
        return jsonify({'error': 'gemini call failed'}), 502
    raw = text.strip()
    if raw.startswith('```'):
        raw = raw.strip('`').lstrip('json').strip()
    try:
        parsed = json.loads(raw)
    except Exception as e:
        return jsonify({'error': f'parse failed: {e}', 'raw': raw[:200]}), 502
    return jsonify(parsed)


@app.route('/api/vauto/url_capture_result', methods=['POST'])
def api_vauto_url_capture_result():
    """Beelink posts back the captured permalink for a saved vAuto appraisal.
    Accepts {bid_id, vin, appraisal_url}. If appraisal_url is missing/empty,
    marks the row as attempted (by setting a non-null sentinel that the
    queue endpoint filters on) — prevents re-polling forever."""
    data = request.json or {}
    bid_id = data.get('bid_id')
    url = (data.get('appraisal_url') or '').strip()
    if not bid_id:
        return jsonify({'error': 'bid_id required'}), 400
    db = get_db()
    cur = db.cursor()
    if url:
        cur.execute("UPDATE vauto_lookups SET appraisal_url=%s WHERE bid_id=%s",
                    (url, bid_id))
    else:
        # Mark the attempt so the queue endpoint stops returning it.
        cur.execute("UPDATE vauto_lookups SET appraisal_url='__not_found__' "
                    "WHERE bid_id=%s AND appraisal_url IS NULL", (bid_id,))
    db.commit()
    db.close()

    # Direct vAuto BFF enrichment — replaces VM 120's 99s rbook scrape with
    # a ~2s API call. Fires only when a real URL is captured. Best-effort:
    # any failure (auth, timeout, missing session) falls through and legacy
    # EWEnrichRbook on VM 120 picks up the bid via /api/enrichment/claim
    # (which now defers for 60s after _stamp_rbook_direct_started).
    if url:
        _stamp_rbook_direct_started(bid_id)
        _stamp_manheim_direct_started(bid_id)
        try:
            import threading
            from vauto_enrichment import (kick_direct_enrichment,
                                          kick_direct_manheim)
            threading.Thread(
                target=kick_direct_enrichment,
                args=(int(bid_id), get_db),
                daemon=True,
                name=f'direct-enrich-{bid_id}',
            ).start()
            threading.Thread(
                target=kick_direct_manheim,
                args=(int(bid_id), get_db),
                daemon=True,
                name=f'direct-manheim-{bid_id}',
            ).start()
        except Exception as _direct_err:
            print(f'[direct-enrich] kick failed bid={bid_id}: '
                  f'{_direct_err}', flush=True)

    return jsonify({'ok': True, 'url': url or None})


@app.route('/api/dealer/beelink_scrape_queue')
def api_beelink_scrape_queue():
    """Dealers needing browser-based scraping (e.g., Ferrari preowned with
    AWS WAF). Beelink polls this and runs undetected_chromedriver locally."""
    limit = request.args.get('limit', 5, type=int)
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT id, name, url, scrape_config
        FROM dealers
        WHERE active=true
          AND scrape_config IS NOT NULL
          AND scrape_config->>'fetch_strategy' = 'beelink_chrome'
          AND (last_scan_at IS NULL OR last_scan_at < NOW() - INTERVAL '4 hours')
        ORDER BY last_scan_at NULLS FIRST
        LIMIT %s
    """, (limit,))
    rows = cur.fetchall()
    db.close()
    return jsonify({'queue': [dict(r) for r in rows]})


@app.route('/api/dealer/info/<int:dealer_id>')
def api_dealer_info(dealer_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id, name, url, scrape_config, platform FROM dealers WHERE id=%s", (dealer_id,))
    row = cur.fetchone()
    db.close()
    if not row:
        return jsonify({'error': 'not found'}), 404
    return jsonify(dict(row))


@app.route('/api/dealer/beelink_scrape_result', methods=['POST'])
def api_beelink_scrape_result():
    """Receive vehicles scraped by Beelink, upsert into dealer_inventory,
    reconcile missing VINs as sold."""
    data = request.json or {}
    dealer_id = data.get('dealer_id')
    vehicles = data.get('vehicles') or []
    status = data.get('status', 'ok')
    error = data.get('error')
    if not dealer_id:
        return jsonify({'error': 'dealer_id required'}), 400
    db = get_db()
    cur = db.cursor()
    # Open scan record
    cur.execute("INSERT INTO dealer_scans (dealer_id, status) VALUES (%s, 'running') RETURNING id",
                (dealer_id,))
    scan_id = cur.fetchone()['id']
    db.commit()

    # Reuse upsert_vehicle from dealer_scanner
    try:
        from dealer_scanner import upsert_vehicle
    except Exception as e:
        return jsonify({'error': f'cannot import upsert_vehicle: {e}'}), 500

    import re as _re
    VIN_VALID = _re.compile(r'^[A-HJ-NPR-Z0-9]{17}$')
    new_count = 0
    drop_count = 0
    scanned_vins = set()
    for v in vehicles:
        vin = (v.get('vin') or '').upper().strip()
        if not VIN_VALID.match(vin):
            continue
        # Each upsert in its own savepoint so a single bad row doesn't abort
        # the whole transaction.
        cur.execute("SAVEPOINT sp_upsert")
        try:
            inv_id, is_new, drop = upsert_vehicle(cur, dealer_id, scan_id, v)
            cur.execute("RELEASE SAVEPOINT sp_upsert")
        except Exception as e:
            cur.execute("ROLLBACK TO SAVEPOINT sp_upsert")
            print(f'  [beelink_result] upsert error vin={vin}: {e}', flush=True)
            continue
        if inv_id:
            scanned_vins.add(vin)
            if is_new:
                new_count += 1
            if drop:
                drop_count += 1

    # Reconcile: any active VIN we had that wasn't in this scrape → missing/sold
    cur.execute("""
        SELECT id, vin FROM dealer_inventory
        WHERE dealer_id=%s AND status IN ('active','missing')
    """, (dealer_id,))
    rows = cur.fetchall()
    sold_count = 0
    missing_count = 0
    for r in rows:
        if (r['vin'] or '').upper() in scanned_vins:
            continue
        cur.execute("""UPDATE dealer_inventory
                       SET status='missing', missing_scans=COALESCE(missing_scans,0)+1
                       WHERE id=%s""", (r['id'],))
        missing_count += 1

    cur.execute("""UPDATE dealers SET last_scan_at=NOW(), last_scan_status=%s,
                                       last_scan_id=%s, scrape_method='beelink-chrome'
                   WHERE id=%s""", (status, scan_id, dealer_id))
    cur.execute("""UPDATE dealer_scans SET status=%s, finished_at=NOW(),
                                            error_message=%s, vehicles_found=%s,
                                            new_count=%s
                   WHERE id=%s""",
                (status, error, len(scanned_vins), new_count, scan_id))
    db.commit()
    db.close()
    return jsonify({'ok': True, 'scan_id': scan_id, 'inserted': len(scanned_vins),
                    'new': new_count, 'price_drops': drop_count,
                    'missing': missing_count})


@app.route('/api/vauto/url_capture_queue')
def api_vauto_url_capture_queue():
    """Return recent vAuto lookups missing an appraisal_url.
    Beelink verifier polls this and fills in the permalink by walking
    the Appraisals list on vAuto."""
    limit = request.args.get('limit', 10, type=int)
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT vl.bid_id, vl.vin, vl.looked_up_at,
               b.year, b.make, b.model
        FROM vauto_lookups vl
        LEFT JOIN bids b ON b.id = vl.bid_id
        WHERE vl.appraisal_url IS NULL
          AND vl.looked_up_at > NOW() - INTERVAL '2 hours'
        ORDER BY vl.looked_up_at DESC
        LIMIT %s
    """, (limit,))
    # Note: rows where the Beelink tried and failed are stored with the
    # '__not_found__' sentinel — they're excluded by IS NULL above.
    rows = cur.fetchall()
    db.close()
    return jsonify({'queue': [dict(r) for r in rows]})


@app.route('/api/vauto/submit', methods=['POST'])
def api_vauto_submit():
    """Accept vAuto lookup results from worker.

    2026-05-08 partial-success detection: if the worker submitted with
    `appraisal_url` set (got far enough to save) but `carfax_screenshot`
    missing (Carfax popup timed out per worker_vauto.py), DO NOT commit
    the partial row. Release the claim and let another worker retry.
    Capped at 3 attempts to prevent infinite loops on hard-failing bids.
    """
    data = request.json
    if not data or not data.get('bid_id'):
        return jsonify({'error': 'missing bid_id'}), 400

    db = get_db()
    cur = db.cursor()
    bid_id = data['bid_id']
    vin = data.get('vin', '')

    # ── Partial-success guard ─────────────────────────────────────────
    appraisal_url_present = bool((data.get('appraisal_url') or '').strip())
    carfax_present = bool((data.get('carfax_screenshot') or '').strip())
    cur.execute(
        "SELECT COUNT(*) AS n FROM worker_jobs WHERE bid_id=%s AND job_type='vauto'",
        (bid_id,))
    _row = cur.fetchone()
    _attempts = _row['n'] if isinstance(_row, dict) else _row[0]
    if appraisal_url_present and not carfax_present and _attempts < 3:
        cur.execute(
            "UPDATE bids SET vauto_claimed_by=NULL, vauto_claimed_at=NULL WHERE id=%s",
            (bid_id,))
        cur.execute("""
            UPDATE worker_jobs
               SET completed_at = NOW(),
                   status       = 'partial',
                   error        = 'carfax_missing — retry triggered',
                   duration_ms  = EXTRACT(EPOCH FROM (NOW() - claimed_at))::int * 1000
             WHERE id = (
                 SELECT id FROM worker_jobs
                 WHERE bid_id = %s AND job_type='vauto' AND completed_at IS NULL
                 ORDER BY claimed_at DESC LIMIT 1
             )
        """, (bid_id,))
        db.commit()
        db.close()
        print(f'[vauto/submit] bid={bid_id} PARTIAL (carfax missing, attempt '
              f'{_attempts}/3) — claim released for retry', flush=True)
        return jsonify({'ok': True, 'partial': True,
                        'reason': 'carfax_missing',
                        'attempts': _attempts,
                        'retry_triggered': True})

    cur.execute("""
        INSERT INTO vauto_lookups
            (bid_id, vin, rbook, black_book, mmr, kbb, kbb_com, jd_power,
             title_status, price_rank, adj_pct_market,
             carfax_screenshot, autocheck_screenshot, raw_json, appraisal_url,
             looked_up_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (bid_id) DO UPDATE SET
            vin=EXCLUDED.vin, rbook=EXCLUDED.rbook, black_book=EXCLUDED.black_book,
            mmr=EXCLUDED.mmr, kbb=EXCLUDED.kbb, kbb_com=EXCLUDED.kbb_com,
            jd_power=EXCLUDED.jd_power, title_status=EXCLUDED.title_status,
            price_rank=EXCLUDED.price_rank, adj_pct_market=EXCLUDED.adj_pct_market,
            carfax_screenshot=EXCLUDED.carfax_screenshot,
            autocheck_screenshot=EXCLUDED.autocheck_screenshot,
            raw_json=EXCLUDED.raw_json,
            appraisal_url=COALESCE(EXCLUDED.appraisal_url, vauto_lookups.appraisal_url),
            looked_up_at=NOW()
    """, (
        bid_id, vin,
        data.get('rbook'), data.get('wholesale_avg'), data.get('mmr_val'),
        data.get('kbb_val'), data.get('kbb_com_val'), data.get('jd_power_val'),
        data.get('title_status'), data.get('price_rank'), data.get('adj_pct_market'),
        data.get('carfax_screenshot'), data.get('autocheck_screenshot'),
        json.dumps(data.get('raw', {})) if data.get('raw') else None,
        data.get('appraisal_url'),
    ))
    # Clear priority flag + claim now that we have the data. Releasing the
    # claim lets stale-claim recovery never need to fire on this bid.
    cur.execute("""
        UPDATE bids
           SET vauto_priority   = FALSE,
               vauto_claimed_by = NULL,
               vauto_claimed_at = NULL
         WHERE id = %s
    """, (bid_id,))

    # Mark the worker_jobs row complete (the most recent in_progress entry
    # for this bid). Duration = wall-clock from claim to submit. Only
    # touches the most recent open row in case of resubmits.
    cur.execute("""
        UPDATE worker_jobs
           SET completed_at = NOW(),
               status       = 'ok',
               duration_ms  = EXTRACT(EPOCH FROM (NOW() - claimed_at))::int * 1000
         WHERE id = (
             SELECT id FROM worker_jobs
             WHERE bid_id = %s AND completed_at IS NULL
             ORDER BY claimed_at DESC LIMIT 1
         )
    """, (bid_id,))

    db.commit()
    db.close()

    # Direct vAuto BFF enrichment — replaces VM 120's slow rbook scrape.
    # Wave-1 worker writes appraisal_url here on the saved vAuto submit;
    # we fire direct API in a daemon thread. Idempotent on rbook_completed_at
    # IS NULL (legacy claim path defers for 60s after _stamp_rbook_direct_started
    # so it only fires if direct API failed).
    _ap_url = (data.get('appraisal_url') or '').strip()
    if _ap_url.startswith('https://provision.vauto.app.coxautoinc.com/Va/Appraisal/Default.aspx'):
        _stamp_rbook_direct_started(bid_id)
        _stamp_manheim_direct_started(bid_id)
        try:
            import threading
            from vauto_enrichment import (kick_direct_enrichment,
                                          kick_direct_manheim)
            threading.Thread(
                target=kick_direct_enrichment,
                args=(int(bid_id), get_db),
                daemon=True,
                name=f'direct-enrich-{bid_id}',
            ).start()
            threading.Thread(
                target=kick_direct_manheim,
                args=(int(bid_id), get_db),
                daemon=True,
                name=f'direct-manheim-{bid_id}',
            ).start()
        except Exception as _direct_err:
            print(f'[direct-enrich] submit-kick failed bid={bid_id}: '
                  f'{_direct_err}', flush=True)

    # Gate assessment on all three books (vAuto+AccuTrade+iPacket) — fire now
    # if they're all present, otherwise arm a 5-minute fallback timer.
    _maybe_fire_assessment(bid_id, require_all=True, source='vauto')
    _schedule_assessment_fallback(bid_id, delay_sec=300)

    return jsonify({'ok': True, 'bid_id': bid_id})


@app.route('/api/vauto/upload_report', methods=['POST'])
def api_vauto_upload_report():
    """Accept Carfax/AutoCheck screenshot upload from worker."""
    if 'file' not in request.files:
        return jsonify({'error': 'no file'}), 400
    f = request.files['file']
    filename = f.filename
    save_path = os.path.join(VAUTO_REPORTS_DIR, filename)
    f.save(save_path)
    return jsonify({'ok': True, 'filename': filename})


@app.route('/vauto_reports/<path:filename>')
def serve_vauto_report(filename):
    """Serve Carfax/AutoCheck screenshot images."""
    return send_from_directory(VAUTO_REPORTS_DIR, filename)


@app.route('/thumb')
def thumb():
    """On-demand thumbnail proxy with disk cache.

    Works for both local uploads (/static/uploads/...) and external CDN URLs.
    Query params:
        url  = source image URL or path
        size = 'strip' (400x280), 'mobile' (200x150), or 'full' (1400x1050)
    """
    import hashlib
    from io import BytesIO
    from PIL import Image, ImageOps

    src = request.args.get('url', '')
    size_key = request.args.get('size', 'strip')
    if not src or size_key not in THUMB_SIZES:
        return 'Bad request', 400

    max_w, max_h = THUMB_SIZES[size_key]
    cache_key = hashlib.sha1(f'{src}|{size_key}'.encode()).hexdigest()
    cache_path = os.path.join(THUMB_CACHE_DIR, f'{cache_key}.jpg')

    if not os.path.exists(cache_path):
        raw = None
        try:
            if src.startswith('/static/uploads/'):
                # Local upload
                local_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), src.lstrip('/'))
                if os.path.exists(local_path):
                    with open(local_path, 'rb') as f:
                        raw = f.read()
            elif src.startswith('http'):
                # External CDN URL — Twilio MediaUrls need basic auth, other
                # CDNs are public. Twilio rotates media after ~few hours so
                # historical SMS photos may also 404 here; that's fine.
                if 'api.twilio.com' in src and TWILIO_SID and TWILIO_TOKEN:
                    _r = requests.get(src, auth=(TWILIO_SID, TWILIO_TOKEN),
                                      headers={'User-Agent': 'EW-Thumb/1.0'},
                                      timeout=15)
                    if _r.status_code == 200:
                        raw = _r.content
                else:
                    import urllib.request
                    req = urllib.request.Request(src, headers={'User-Agent': 'EW-Thumb/1.0'})
                    with urllib.request.urlopen(req, timeout=15) as r:
                        raw = r.read()
        except Exception:
            pass

        if not raw:
            return 'Source not found', 404

        try:
            img = Image.open(BytesIO(raw))
            img = ImageOps.exif_transpose(img)
            if img.mode not in ('RGB', 'L'):
                img = img.convert('RGB')
            img.thumbnail((max_w, max_h), Image.LANCZOS)
            img.save(cache_path, 'JPEG', quality=80, optimize=True)
        except Exception:
            return 'Resize failed', 500

    resp = send_from_directory(THUMB_CACHE_DIR, f'{cache_key}.jpg', mimetype='image/jpeg')
    resp.headers['Cache-Control'] = 'public, max-age=604800, immutable'
    return resp


@app.route('/p/<int:photo_id>/<size>')
def opaque_photo(photo_id, size):
    """Serve a bid photo by opaque DB id — NO source URL in the request.
    Used on the public share page so viewers can't see which dealer owns
    the car by inspecting the URL (e.g., txtcharlie.com). The image itself
    is re-encoded through PIL which strips EXIF/metadata for free. Content
    of the photo is unchanged; only the identifying URL is hidden."""
    if size not in THUMB_SIZES:
        return 'Bad size', 400
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT url FROM bid_photos WHERE id = %s", (photo_id,))
    row = cur.fetchone()
    db.close()
    if not row:
        return 'Not found', 404
    # Hand off to the existing /thumb logic by synthesizing a request-local
    # call. Simpler: duplicate the tiny core.
    import hashlib
    from io import BytesIO
    from PIL import Image, ImageOps
    src = row['url']
    max_w, max_h = THUMB_SIZES[size]
    # Separate cache namespace from /thumb. The /p/ route strips EXIF via
    # PIL re-encode and serves no source URL in request path, preserving
    # dealer anonymity on the share page.
    cache_key = hashlib.sha1(f'public|{src}|{size}'.encode()).hexdigest()
    cache_path = os.path.join(THUMB_CACHE_DIR, f'{cache_key}.jpg')
    if not os.path.exists(cache_path):
        raw = None
        try:
            if src.startswith('/static/uploads/'):
                local = os.path.join(os.path.dirname(os.path.abspath(__file__)), src.lstrip('/'))
                if os.path.exists(local):
                    with open(local, 'rb') as f: raw = f.read()
            elif src.startswith('http'):
                import urllib.request
                req = urllib.request.Request(src, headers={'User-Agent': 'EW-Thumb/1.0'})
                with urllib.request.urlopen(req, timeout=15) as r: raw = r.read()
        except Exception:
            pass
        if not raw:
            return 'Source fetch failed', 404
        try:
            img = Image.open(BytesIO(raw))
            img = ImageOps.exif_transpose(img)
            if img.mode not in ('RGB', 'L'):
                img = img.convert('RGB')
            img.thumbnail((max_w, max_h), Image.LANCZOS)
            # JPEG save strips EXIF/metadata automatically (no exif= arg).
            img.save(cache_path, 'JPEG', quality=80, optimize=True)
        except Exception:
            return 'Resize failed', 500
    resp = send_from_directory(THUMB_CACHE_DIR, f'{cache_key}.jpg', mimetype='image/jpeg')
    resp.headers['Cache-Control'] = 'public, max-age=604800, immutable'
    return resp


@app.route('/api/vauto/status/<int:bid_id>')
def api_vauto_status(bid_id):
    """Check if vAuto lookup is complete for a bid."""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM vauto_lookups WHERE bid_id = %s", (bid_id,))
    row = cur.fetchone()
    db.close()
    if row:
        d = dict(row)
        # Serialize datetime for JSON
        for k, v in d.items():
            if hasattr(v, 'isoformat'):
                d[k] = v.isoformat()
        return jsonify({'status': 'complete', 'data': d})
    return jsonify({'status': 'pending'})


# ── AccuTrade worker API ────────────────────────────────────────────────────

@app.route('/api/accutrade/pending')
def api_accutrade_pending():
    """Return bids that need AccuTrade lookup."""
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT b.id as bid_id, b.vin, b.mileage, b.year, b.make, b.model
        FROM bids b
        LEFT JOIN accutrade_lookups al ON al.bid_id = b.id
        WHERE b.vin IS NOT NULL AND length(b.vin) = 17
          AND al.id IS NULL
        ORDER BY b.created_at DESC
        LIMIT 20
    """)
    rows = cur.fetchall()
    db.close()
    return jsonify({'pending': [dict(r) for r in rows]})


def _normalize_accutrade_url(url):
    """AccuTrade returns URLs like /appraisal/N?backUrl=%252Freport%252Factive
    where backUrl points at the landing page (/report/active). When a user
    clicks the saved URL without a session, AccuTrade redirects to login,
    then post-login routes to backUrl — landing the user on the active list
    instead of the appraisal. Normalize here at intake so the saved URL has
    backUrl pointing at the appraisal itself; post-login redirects always
    land on the appraisal regardless of which mechanism AccuTrade uses.
    Server-side normalization avoids touching the Trainer worker code."""
    if not url or '/appraisal/' not in url:
        return url
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}{p.path}?backUrl={p.path}"
    except Exception:
        return url


@app.route('/api/accutrade/trim_select', methods=['POST'])
def api_trim_select():
    """AI overseer: pick the correct trim from AccuTrade's modal choices.

    Worker POSTs {vin, bid_id, choices: [{index, text}]}. We cache the result
    by VIN forever — same VIN never re-asks the LLM. On any failure, we tell
    the worker to fall back to its existing fuzzy-match (returns index=null).
    """
    data = request.json or {}
    vin = (data.get('vin') or '').strip().upper()
    bid_id = data.get('bid_id')
    choices = data.get('choices') or []

    if not vin or len(vin) != 17 or not choices:
        return jsonify({'index': None, 'reason': 'bad_request'}), 200

    # Defensive normalize: strip mat-icon glyph names from inbound choice text
    # (older workers may not have the clean-scrape patch yet).
    for c in choices:
        if isinstance(c, dict) and c.get('text'):
            c['text'] = normalize_trim_text(c['text'])

    db = get_db()
    cur = db.cursor()

    # Cache hit by VIN
    cur.execute(
        "SELECT selected_index, selected_text, confidence, model_used, clean_trim "
        "FROM accutrade_trim_select_cache WHERE vin=%s", (vin,)
    )
    row = cur.fetchone()
    if row:
        db.close()
        return jsonify({
            'index': int(row['selected_index']),
            'text': normalize_trim_text(row['selected_text']),
            'clean_trim': row.get('clean_trim') if isinstance(row, dict) else None,
            'confidence': float(row['confidence']) if row['confidence'] is not None else None,
            'source': 'cache',
            'model': row['model_used'],
        })

    # Pull bid context + iPacket signal for the prompt
    bid_trim = None
    bid_year = bid_make = bid_model = None
    sticker_msrp = None
    sticker_base = None
    sticker_ext = None
    sticker_int = None
    try:
        cur.execute(
            "SELECT year, make, model, trim, canon_trim, mileage "
            "FROM bids WHERE id=%s", (bid_id,)
        )
        b = cur.fetchone()
        if b:
            bid_trim = (b.get('canon_trim') or b.get('trim') or '').strip()
            bid_year = b.get('year')
            bid_make = b.get('make')
            bid_model = b.get('model')
        cur.execute(
            "SELECT total_msrp, base_price, exterior_color, interior_color "
            "FROM ipacket_lookups WHERE vin=%s ORDER BY looked_up_at DESC LIMIT 1",
            (vin,)
        )
        s = cur.fetchone()
        if s:
            sticker_msrp = s.get('total_msrp')
            sticker_base = s.get('base_price')
            sticker_ext = s.get('exterior_color')
            sticker_int = s.get('interior_color')
    except Exception as e:
        print(f'trim_select: bid/sticker context fetch failed: {e}', flush=True)

    # Build the prompt for the LLM overseer
    choice_lines = '\n'.join(
        f"  [{c.get('index', i)}] {c.get('text', '')}"
        for i, c in enumerate(choices)
    )
    ctx_lines = [f"VIN: {vin}"]
    if bid_year and bid_make and bid_model:
        ctx_lines.append(f"Vehicle (from seller): {bid_year} {bid_make} {bid_model}")
    if bid_trim:
        ctx_lines.append(f"Trim hint from seller/canon: {bid_trim}")
    if sticker_msrp:
        ctx_lines.append(f"Window-sticker MSRP: ${sticker_msrp:,}")
    if sticker_base:
        ctx_lines.append(f"Window-sticker base price: ${sticker_base:,}")
    if sticker_ext:
        ctx_lines.append(f"Exterior color: {sticker_ext}")
    if sticker_int:
        ctx_lines.append(f"Interior color: {sticker_int}")
    context_block = '\n'.join(ctx_lines)

    prompt = (
        "You are a vehicle-trim disambiguator for an automotive wholesale buyer.\n"
        "AccuTrade has returned multiple possible trim configurations for the same VIN.\n"
        "Pick the SINGLE choice that best matches the actual vehicle.\n\n"
        f"{context_block}\n\n"
        "AccuTrade choices (you must pick exactly one index):\n"
        f"{choice_lines}\n\n"
        "Rules:\n"
        "1. The seller's trim hint is the strongest signal when present.\n"
        "2. Window-sticker MSRP narrows by price tier (higher trims cost more).\n"
        "3. If the seller hint contradicts the sticker MSRP, prefer the hint.\n"
        "4. When in doubt for VINs that don't encode trim (Ford Super Duty, etc.),\n"
        "   prefer the LOWER trim — never assume a high-spec trim without evidence.\n"
        "5. The VIN itself is authoritative when its prefix encodes trim\n"
        "   (Porsche WP0AA=Carrera, WP0AB=Carrera S, WP0AC=GT3/Touring,\n"
        "   WP0AH=GTS variants). When seller hint disagrees with VIN-encoded\n"
        "   trim, TRUST THE VIN.\n"
        "6. Confidence: 0.9+ if VIN or sticker confirms; 0.6-0.8 if reasoned;\n"
        "   below 0.5 if guessing.\n"
        '7. Output a "clean_trim" canonical short label (e.g. "Carrera S",\n'
        '   "GT3", "F-250 XL Crew Cab", "M240i Coupe") — concise, marketable.\n\n'
        'Return ONLY this JSON: {"index": N, "confidence": 0.0-1.0,\n'
        '"clean_trim": "<short label>", "reason": "<one short sentence>"}\n'
        'No markdown, no commentary.'
    )

    model = 'gemini-2.5-flash'
    raw = gemini_call(prompt, model=model, max_tokens=400, temperature=0.1)
    if not raw:
        db.close()
        return jsonify({'index': None, 'reason': 'llm_unavailable'}), 200

    # Parse JSON (tolerate ```json fences and truncated responses).
    # Pull index/confidence/reason via regex fallback if json.loads fails.
    txt = raw.strip()
    if txt.startswith('```'):
        txt = re.sub(r'^```(?:json)?\s*|\s*```$', '', txt, flags=re.MULTILINE).strip()
    idx = None
    conf = 0.5
    reason = ''
    clean_trim = None
    try:
        parsed = json.loads(txt)
        idx = int(parsed.get('index'))
        conf = float(parsed.get('confidence', 0.5))
        reason = (parsed.get('reason') or '')[:200]
        clean_trim = (parsed.get('clean_trim') or '')[:80] or None
    except Exception:
        m_idx = re.search(r'"index"\s*:\s*(\d+)', txt)
        m_conf = re.search(r'"confidence"\s*:\s*([0-9.]+)', txt)
        m_reason = re.search(r'"reason"\s*:\s*"([^"]*)"', txt)
        m_clean = re.search(r'"clean_trim"\s*:\s*"([^"]*)"', txt)
        if m_idx:
            idx = int(m_idx.group(1))
            if m_conf:
                try: conf = float(m_conf.group(1))
                except: pass
            if m_reason:
                reason = m_reason.group(1)[:200]
            if m_clean:
                clean_trim = m_clean.group(1)[:80] or None
        else:
            print(f'trim_select: parse failed | raw={raw[:300]}', flush=True)
            db.close()
            return jsonify({'index': None, 'reason': 'parse_failed', 'raw': raw[:200]}), 200

    if idx < 0 or idx >= len(choices):
        db.close()
        return jsonify({'index': None, 'reason': 'index_out_of_range'}), 200

    selected_text = normalize_trim_text(choices[idx].get('text', ''))

    # Persist to cache
    try:
        cur.execute("ALTER TABLE accutrade_trim_select_cache ADD COLUMN IF NOT EXISTS clean_trim TEXT")
        db.commit()
    except Exception:
        db.rollback()
    try:
        cur.execute("""
            INSERT INTO accutrade_trim_select_cache
                (vin, choices_json, selected_index, selected_text, confidence,
                 model_used, bid_id_first_seen, bid_trim_at_select, clean_trim)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (vin) DO UPDATE SET
                choices_json=EXCLUDED.choices_json,
                selected_index=EXCLUDED.selected_index,
                selected_text=EXCLUDED.selected_text,
                confidence=EXCLUDED.confidence,
                model_used=EXCLUDED.model_used,
                clean_trim=COALESCE(EXCLUDED.clean_trim, accutrade_trim_select_cache.clean_trim),
                updated_at=NOW()
        """, (
            vin, json.dumps(choices), idx, selected_text, conf,
            model, bid_id, bid_trim or None, clean_trim,
        ))
        db.commit()
    except Exception as e:
        print(f'trim_select: cache write failed: {e}', flush=True)
    finally:
        db.close()

    print(f'trim_select VIN={vin} bid={bid_id} chose [{idx}] "{selected_text}" '
          f'clean="{clean_trim}" conf={conf} reason={reason}', flush=True)
    return jsonify({
        'index': idx,
        'text': selected_text,
        'clean_trim': clean_trim,
        'confidence': conf,
        'reason': reason,
        'source': 'llm',
        'model': model,
    })


# PHASE_D_VERIFY_GATE_2026_05_15: miles-discrepancy SMS helper. Triggered by
# miles_audit_worker via /api/internal/bid/<id>/miles-verify-sms when it
# detects Carfax/AutoCheck odometer > customer-reported by > +2000.
def _maybe_send_miles_verify_sms(bid_id, reason='miles_discrepancy'):
    """Fire one SMS asking the bidder to confirm mileage. Idempotent via
    bids.miles_verify_sms_sent_at. No phone gating, no quiet hours."""
    try:
        _db = get_db()
        _cur = _db.cursor()
        _cur.execute(
            "SELECT id, vin, mileage, miles_carfax, miles_autocheck, "
            "miles_higher_source, miles_discrepancy, phone, bidder_name, "
            "miles_verify_sms_sent_at, year, make, model "
            "FROM bids WHERE id = %s", (bid_id,))
        _row = _cur.fetchone()
        if not _row:
            _db.close(); return False
        if _row.get('miles_verify_sms_sent_at'):
            _db.close(); return False
        _phone = (_row.get('phone') or '').strip()
        if not _phone:
            _db.close(); return False
        _name_raw = (_row.get('bidder_name') or '').strip()
        _first = _name_raw.split()[0] if _name_raw else 'there'
        _customer_miles = int(_row.get('mileage') or 0)
        _higher_source = _row.get('miles_higher_source') or 'history'
        if _higher_source == 'carfax':
            _higher_miles = int(_row.get('miles_carfax') or 0)
        else:
            _higher_miles = int(_row.get('miles_autocheck') or 0)
        _y = _row.get('year') or ''
        _mk = (_row.get('make') or '').title() if _row.get('make') else ''
        _md = _row.get('model') or ''
        _vehicle = (f"{_y} {_mk} {_md}").strip() or "vehicle"

        _body = (
            f"Hey, its the EW Bot. "  # EW_BOT_WORDING_2026_05_15
            f"Quick odometer check on the {_vehicle} — you sent in "
            f"{_customer_miles:,} mi but {_higher_source.title()} shows the "
            f"most recent reading is {_higher_miles:,} mi. Could you confirm "
            f"which is right? Reply with the correct mileage or send a clear "
            f"odometer photo. Thanks!"
        )

        _sent = send_sms(_phone, _body)
        if not _sent:
            _db.close()
            print(f'[miles-verify-sms] bid={bid_id} send_sms returned False',
                  flush=True)
            return False

        _cur.execute(
            "UPDATE bids SET miles_verify_sms_sent_at = NOW() WHERE id = %s",
            (bid_id,))
        _db.commit()
        _db.close()

        try:
            _delta = int(_row.get('miles_discrepancy') or 0)
            _tg_worker_alert(
                f"\U0001f504 EW auto-miles-verify SMS sent\n"
                f"bid <b>#{bid_id}</b> \u00b7 {_first} \u00b7 {_phone}\n"
                f"{_vehicle}\n"
                f"customer: <b>{_customer_miles:,} mi</b>, "
                f"{_higher_source}: <b>{_higher_miles:,} mi</b> "
                f"(\u0394 +{_delta:,})"
            )
        except Exception:
            pass

        print(f'[miles-verify-sms] bid={bid_id} sent to {_phone} '
              f'customer={_customer_miles} higher={_higher_miles} '
              f'source={_higher_source}', flush=True)
        return True
    except Exception as _e:
        print(f'[miles-verify-sms] error bid={bid_id}: '
              f'{type(_e).__name__}: {_e}', flush=True)
        return False


# VIN_VERIFY_SMS_2026_05_15: auto-text bidder when AccuTrade can't find the
# VIN they sent. Strong typo signal — AccuTrade has the biggest VIN database;
# if it can't resolve, it's almost always a 1-character mistype.
def _maybe_send_vin_verify_sms(bid_id, reason='accutrade_vin_not_found'):
    """Fire one SMS asking the bidder to verify their VIN. Idempotent — gated
    on bids.vin_verify_sms_sent_at IS NULL so we never re-send. If a photo on
    this bid OCR'd a DIFFERENT 17-char VIN, include it as a suggestion.
    No phone gating, no quiet hours (operator direction)."""
    try:
        _db = get_db()
        _cur = _db.cursor()
        _cur.execute(
            "SELECT id, vin, phone, bidder_name, vin_verify_sms_sent_at "
            "FROM bids WHERE id = %s", (bid_id,))
        _row = _cur.fetchone()
        if not _row:
            _db.close(); return False
        if _row.get('vin_verify_sms_sent_at'):
            _db.close(); return False
        _phone = (_row.get('phone') or '').strip()
        if not _phone:
            _db.close(); return False
        _vin = (_row.get('vin') or '').strip().upper()
        _name_raw = (_row.get('bidder_name') or '').strip()
        _first = _name_raw.split()[0] if _name_raw else 'there'
        _cur.execute(
            "SELECT DISTINCT vin_extracted FROM bid_photos WHERE bid_id = %s "
            "AND vin_extracted IS NOT NULL AND length(vin_extracted) = 17 "
            "AND upper(vin_extracted) <> %s ORDER BY vin_extracted LIMIT 1",
            (bid_id, _vin))
        _alt_row = _cur.fetchone()
        _alt_vin = (_alt_row.get('vin_extracted') or '').strip().upper() if _alt_row else ''

        # VERIFY_SMS_HASHBID_HINT_2026_05_16: include "#<bid_id>" routing
        # hint so customer replies attach to the right bid even if the
        # 60s/24h stitch window misses.
        _hb_hint = f"\n\nReply with #{bid_id} before the VIN or photo so we attach it to this bid."
        if _alt_vin:
            _body = (
                f"Hey, its the EW Bot. "  # EW_BOT_WORDING_2026_05_15
                f"Quick check on that VIN — {_vin} — our system can't find it "
                f"in the AccuTrade database, which usually means a 1-character typo. "
                f"We also see {_alt_vin} on the photos you sent — is that the right one? "
                f"If not, please re-type the VIN from the dash, door jamb, or "
                f"windshield sticker, or send a clearer photo and we'll pull it from there. "
                f"Thanks!{_hb_hint}")
        else:
            _body = (
                f"Hey, its the EW Bot. "  # EW_BOT_WORDING_2026_05_15
                f"Quick check on that VIN — {_vin} — our system can't find it "
                f"in the AccuTrade database, which usually means a 1-character typo. "
                f"Could you re-type the VIN from the dash, door jamb, or windshield "
                f"sticker, or send a clearer photo and we'll grab it from there? "
                f"Thanks!{_hb_hint}")

        _sent = send_sms(_phone, _body)
        if not _sent:
            _db.close()
            print(f'[vin-verify-sms] bid={bid_id} send_sms returned False '
                  f'(phone={_phone}, vin={_vin}) — not stamping', flush=True)
            return False

        _cur.execute(
            "UPDATE bids SET vin_verify_sms_sent_at = NOW(), "
            "vin_verify_sms_reason = %s WHERE id = %s",
            (reason, bid_id))
        _db.commit()
        _db.close()

        try:
            _alt_line = (f" suggested-alt: <code>{_alt_vin}</code>"
                         if _alt_vin else " no photo alternative")
            _tg_worker_alert(
                f"🔄 EW auto-VIN-verify SMS sent\n"
                f"bid <b>#{bid_id}</b> · {_first} · {_phone}\n"
                f"vin: <code>{_vin}</code> ({reason}){_alt_line}")
        except Exception:
            pass

        print(f'[vin-verify-sms] bid={bid_id} sent to {_phone} '
              f'reason={reason} vin={_vin} alt={_alt_vin or chr(45)}', flush=True)
        return True
    except Exception as _e:
        print(f'[vin-verify-sms] error bid={bid_id}: '
              f'{type(_e).__name__}: {_e}', flush=True)
        return False


@app.route('/api/internal/bid/<int:bid_id>/miles-verify-sms', methods=['POST'])
def api_internal_miles_verify_sms(bid_id):
    """PHASE_D_VERIFY_GATE_2026_05_15: triggered by miles_audit_worker when
    it flags a bid. Auth: X-Auth = EW_VAUTO_REFRESH_SECRET (shared)."""
    import os as _os
    _expected = (_os.environ.get('EW_VAUTO_REFRESH_SECRET') or '').strip()
    _provided = (request.headers.get('X-Auth') or '').strip()
    if not _expected or _provided != _expected:
        return jsonify({'ok': False, 'error': 'unauthorized'}), 401
    _sent = _maybe_send_miles_verify_sms(bid_id, reason='miles_discrepancy')
    return jsonify({'ok': bool(_sent), 'bid_id': bid_id})


@app.route('/api/bid/<int:bid_id>/clear-verification', methods=['POST'])
def api_clear_verification(bid_id):
    """Operator dismiss button. body: {note?, reprocess?, force_ipacket?}.
    If reprocess=true, wipes Phase 1 lookups (preserving recent iPacket
    unless force_ipacket=true). AI re-fires once lookups land."""
    data = request.get_json(silent=True) or {}
    note = (data.get('note') or 'manual_clear')[:200]
    do_reprocess = bool(data.get('reprocess'))
    force_ipacket = bool(data.get('force_ipacket'))
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "UPDATE bids SET needs_verification_cleared_at = NOW(), "
        "needs_verification_cleared_by = %s WHERE id = %s "
        "AND needs_verification_at IS NOT NULL "
        "AND needs_verification_cleared_at IS NULL",
        (f'operator:{note}', bid_id))
    if cur.rowcount == 0:
        db.close()
        return jsonify({'ok': False, 'error': 'no open verification flag'}), 404
    if do_reprocess:
        # iPacket: preserve recent good capture unless explicitly forced.
        if force_ipacket:
            cur.execute('DELETE FROM ipacket_lookups WHERE bid_id=%s', (bid_id,))
        else:
            cur.execute("""DELETE FROM ipacket_lookups
             WHERE bid_id = %s
               AND (looked_up_at IS NULL
                    OR looked_up_at < NOW() - INTERVAL '5 minutes'
                    OR not_available = true)""", (bid_id,))
        cur.execute('DELETE FROM accutrade_lookups WHERE bid_id=%s', (bid_id,))
        cur.execute('DELETE FROM vauto_lookups WHERE bid_id=%s', (bid_id,))
        cur.execute(
            "UPDATE bids SET vauto_claimed_by=NULL, vauto_claimed_at=NULL, "
            "ai_assessed_at=NULL, ai_price=NULL, ai_assessment=NULL, "
            "miles_audit_at=NULL WHERE id=%s", (bid_id,))
        cur.execute(
            "UPDATE worker_jobs SET completed_at=NOW(), "
            "status='released_verify_clear', "
            "duration_ms=EXTRACT(EPOCH FROM (NOW()-claimed_at))::int*1000 "
            "WHERE bid_id=%s AND completed_at IS NULL", (bid_id,))
    db.commit()
    db.close()
    try:
        _tg_worker_alert(
            f"\u2705 EW verify flag cleared (operator)\n"
            f"bid <b>#{bid_id}</b> note: {note}"
            + (' \u00b7 force-reprocess fired' if do_reprocess else '')
            + (' (forced iPacket refetch)' if force_ipacket else ''))
    except Exception:
        pass
    return jsonify({'ok': True, 'bid_id': bid_id,
                    'reprocessed': do_reprocess,
                    'forced_ipacket': force_ipacket})


@app.route('/api/accutrade/submit', methods=['POST'])
def api_accutrade_submit():
    """Accept AccuTrade lookup results from worker."""
    data = request.json
    if not data or not data.get('bid_id'):
        return jsonify({'error': 'missing bid_id'}), 400

    db = get_db()
    cur = db.cursor()
    bid_id = data['bid_id']

    cur.execute("""
        INSERT INTO accutrade_lookups
            (bid_id, vin, guaranteed_offer, trade_in, trade_market, retail,
             market_avg, local_comps, screenshot, raw_json,
             not_available, unavailable_reason, appraisal_url,
             selected_trim_text, trim_select_source, looked_up_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (bid_id) DO UPDATE SET
            vin=EXCLUDED.vin, guaranteed_offer=EXCLUDED.guaranteed_offer,
            trade_in=EXCLUDED.trade_in, trade_market=EXCLUDED.trade_market,
            retail=EXCLUDED.retail, market_avg=EXCLUDED.market_avg,
            local_comps=EXCLUDED.local_comps, screenshot=EXCLUDED.screenshot,
            raw_json=EXCLUDED.raw_json,
            not_available=EXCLUDED.not_available,
            unavailable_reason=EXCLUDED.unavailable_reason,
            appraisal_url=COALESCE(EXCLUDED.appraisal_url, accutrade_lookups.appraisal_url),
            selected_trim_text=COALESCE(EXCLUDED.selected_trim_text, accutrade_lookups.selected_trim_text),
            trim_select_source=COALESCE(EXCLUDED.trim_select_source, accutrade_lookups.trim_select_source),
            looked_up_at=NOW()
    """, (
        bid_id, data.get('vin', ''),
        data.get('guaranteed_offer'), data.get('trade_in'),
        data.get('trade_market'), data.get('retail'),
        data.get('market_avg'),
        json.dumps(data.get('local_comps')) if data.get('local_comps') else None,
        data.get('screenshot'),
        json.dumps(data.get('raw', {})) if data.get('raw') else None,
        bool(data.get('not_available', False)),
        data.get('unavailable_reason'),
        _normalize_accutrade_url(data.get('appraisal_url')),
        normalize_trim_text(data.get('selected_trim_text')),
        data.get('trim_select_source'),
    ))
    db.commit()

    # VIN_VERIFY_SMS_2026_05_15: auto-text bidder if AccuTrade reports no VIN.
    # Detector fires once per bid (idempotent via vin_verify_sms_sent_at).
    try:
        _ua_reason = (data.get('unavailable_reason') or '').lower()
        if bool(data.get('not_available')) and (
            'no appraisal data' in _ua_reason
            or ('vin' in _ua_reason and 'not found' in _ua_reason)
            or ('vin' in _ua_reason and 'no match' in _ua_reason)
        ):
            _maybe_send_vin_verify_sms(bid_id, reason='accutrade_vin_not_found')
            # PHASE_D_VERIFY_GATE_2026_05_15: stamp needs_verification so the
            # AI gate trips. SMS alone tells customer; this also blocks the
            # downstream AI assessment until the VIN is corrected.
            try:
                cur.execute(
                    "UPDATE bids SET "
                    "needs_verification_at = COALESCE(needs_verification_at, NOW()), "
                    "needs_verification_reason = CASE "
                    "  WHEN needs_verification_reason IS NULL "
                    "       THEN 'vin_not_found' "
                    "  WHEN position('vin_not_found' IN needs_verification_reason) > 0 "
                    "       THEN needs_verification_reason "
                    "  ELSE needs_verification_reason || ',vin_not_found' END "
                    "WHERE id = %s AND needs_verification_cleared_at IS NULL",
                    (bid_id,))
                db.commit()
            except Exception as _vvste:
                print(f'[vin-verify-stamp] err bid={bid_id}: {_vvste}',
                      flush=True)
    except Exception as _vvse:
        print(f'[vin-verify-sms] hook error bid={bid_id}: '
              f'{type(_vvse).__name__}: {_vvse}', flush=True)

    # 2026-05-11: canon_trim writeback. When the AccuTrade overseer picked
    # a trim with confidence >= 0.7, propagate it to bids.canon_trim so the
    # rest of the system (bid display, vAuto BFF Trim filter, dealer-match)
    # uses the authoritative VIN-derived trim instead of whatever the seller
    # texted in. Bid 1188 case: seller said "Carrera 4 (Coupe)" but AccuTrade
    # modal only offered GT3 variants → canon_trim should become "GT3".
    try:
        vin = (data.get('vin') or '').upper()
        if vin and len(vin) == 17:
            cur.execute(
                "SELECT clean_trim, selected_text, confidence "
                "FROM accutrade_trim_select_cache WHERE vin=%s", (vin,))
            crow = cur.fetchone()
            if crow:
                cconf = float(crow['confidence'] or 0)
                ctrim = (crow.get('clean_trim')
                         or normalize_trim_text(crow.get('selected_text'))
                         or '').strip()
                if ctrim and cconf >= 0.7:
                    cur.execute("""
                        UPDATE bids
                           SET canon_trim = %s,
                               canon_source = 'accutrade_overseer',
                               canon_confidence = %s
                         WHERE id = %s
                           AND (canon_trim IS NULL OR canon_trim = '')
                    """, (ctrim[:80], cconf, bid_id))
                    if cur.rowcount > 0:
                        print(f'[canon_trim] bid={bid_id} set canon_trim="{ctrim}" '
                              f'conf={cconf:.2f} from accutrade_overseer', flush=True)
                    db.commit()
    except Exception as _canon_err:
        print(f'[canon_trim] writeback err bid={bid_id}: {_canon_err}', flush=True)
        db.rollback()

    db.close()
    _maybe_fire_assessment(bid_id, require_all=True, source='accutrade')
    return jsonify({'ok': True, 'bid_id': bid_id})


@app.route('/api/accutrade/upload_report', methods=['POST'])
def api_accutrade_upload_report():
    """Accept AccuTrade screenshot upload from worker."""
    if 'file' not in request.files:
        return jsonify({'error': 'no file'}), 400
    f = request.files['file']
    filename = f.filename
    save_path = os.path.join(ACCUTRADE_REPORTS_DIR, filename)
    f.save(save_path)
    return jsonify({'ok': True, 'filename': filename})


@app.route('/accutrade_reports/<path:filename>')
def serve_accutrade_report(filename):
    """Serve AccuTrade screenshot images."""
    return send_from_directory(ACCUTRADE_REPORTS_DIR, filename)


@app.route('/api/accutrade/status/<int:bid_id>')
def api_accutrade_status(bid_id):
    """Check if AccuTrade lookup is complete for a bid."""
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("SELECT * FROM accutrade_lookups WHERE bid_id = %s", (bid_id,))
        row = cur.fetchone()
    except Exception:
        row = None
    db.close()
    if row:
        d = dict(row)
        for k, v in d.items():
            if hasattr(v, 'isoformat'):
                d[k] = v.isoformat()
        status = 'not_available' if d.get('not_available') else 'complete'
        return jsonify({'status': status, 'data': d})
    return jsonify({'status': 'pending'})


# ── iPacket worker API ─────────────────────────────────────────────────────

@app.route('/api/ipacket/pending')
def api_ipacket_pending():
    """Return bids that need iPacket sticker lookup."""
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT b.id as bid_id, b.vin, b.mileage, b.year, b.make, b.model
        FROM bids b
        LEFT JOIN ipacket_lookups il ON il.bid_id = b.id
        WHERE b.vin IS NOT NULL AND length(b.vin) = 17
          AND il.id IS NULL
        ORDER BY b.created_at DESC
        LIMIT 20
    """)
    rows = cur.fetchall()
    db.close()
    return jsonify({'pending': [dict(r) for r in rows]})


@app.route('/api/ipacket/submit', methods=['POST'])
def api_ipacket_submit():
    """Accept iPacket sticker lookup results from worker."""
    data = request.json
    if not data or not data.get('bid_id'):
        return jsonify({'error': 'missing bid_id'}), 400

    # 2026-05-09: Reject empty 'success' submissions. Worker should now mark
    # not_available=True on blank captures (see worker_ipacket.py blank-capture
    # branch). This is the safety net — if a worker still tries to submit
    # not_available=False with all-NULL fields, return 422 so it doesn't
    # pollute ipacket_lookups with empty rows that render as blank stickers
    # in the mini-page. Worker can retry / mark not_available explicitly.
    if not data.get('not_available'):
        _has_data = any([
            data.get('total_msrp'),
            data.get('base_price'),
            (data.get('exterior_color') or '').strip(),
            (data.get('interior_color') or '').strip(),
        ])
        _raw = data.get('raw') or {}
        _has_options = bool(_raw.get('options'))
        _text_chars = int(_raw.get('text_chars') or 0)
        if not _has_data and not _has_options and _text_chars < 200:
            # 2026-05-10: Worker should never submit empty fields with
            # not_available=False — worker_main.py was patched to always
            # call ew_submit_ipacket with not_available=True on
            # error/skip paths. This server-side guard is the safety net
            # in case a worker still slips through; coerce to NA rather
            # than reject so the assess-gate doesn't hang.
            print(f'[ipacket-submit] empty submission bid={data.get("bid_id")} from {data.get("worker_id")} — coercing to not_available=True (worker bug — worker_main.py should have submitted NA explicitly)', flush=True)
            data['not_available'] = True
            data['unavailable_reason'] = data.get('unavailable_reason') or 'empty capture (server-coerced safety net — worker should have set not_available=True)' 

    db = get_db()
    cur = db.cursor()
    bid_id = data['bid_id']

    cur.execute("""
        INSERT INTO ipacket_lookups
            (bid_id, vin, total_msrp, base_price, exterior_color,
             interior_color, screenshot, raw_json, not_available,
             unavailable_reason, looked_up_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (bid_id) DO UPDATE SET
            vin=EXCLUDED.vin, total_msrp=EXCLUDED.total_msrp,
            base_price=EXCLUDED.base_price, exterior_color=EXCLUDED.exterior_color,
            interior_color=EXCLUDED.interior_color, screenshot=EXCLUDED.screenshot,
            raw_json=EXCLUDED.raw_json,
            not_available=EXCLUDED.not_available,
            unavailable_reason=EXCLUDED.unavailable_reason,
            looked_up_at=NOW()
    """, (
        bid_id, data.get('vin', ''),
        data.get('total_msrp'), data.get('base_price'),
        data.get('exterior_color'), data.get('interior_color'),
        data.get('screenshot'),
        json.dumps(data.get('raw', {})) if data.get('raw') else None,
        bool(data.get('not_available', False)),
        data.get('unavailable_reason'),
    ))
    db.commit()

    # ── Extract canon_trim NOW (not later at assessment time) ──────────────
    # The iPacket sticker we just saved has authoritative trim/edition info
    # ("SRT Hellcat Jailbreak" etc). Pull it immediately so the bid-detail
    # page can render exact-trim dealer matches without waiting for the full
    # Gemini assessment to fire. Idempotent + cheap; OCR cached in raw_json.
    try:
        from ipacket_trim import extract_and_persist as _ipt_extract
        _ipt_cur = db.cursor()
        _ipt_cur.execute("""
            SELECT make, model, canon_trim FROM bids WHERE id = %s
        """, (bid_id,))
        _ipt_bid = _ipt_cur.fetchone()
        if (_ipt_bid and not _ipt_bid.get('canon_trim')
            and data.get('screenshot')):
            _ipt_trim = _ipt_extract(
                bid_id,
                _ipt_bid.get('make'),
                _ipt_bid.get('model'),
                {
                    'total_msrp': data.get('total_msrp'),
                    'screenshot': data.get('screenshot'),
                    # raw_json was just persisted above; use the same dict
                    'raw_json':   data.get('raw') or {},
                },
                db,
                force_ocr=True,
            )
            if _ipt_trim:
                print(f'[ipacket-submit] Bid {bid_id} canon_trim extracted: '
                      f'{_ipt_trim!r}', flush=True)
    except Exception as _ipt_err:
        print(f'[ipacket-submit] canon_trim extract err bid={bid_id}: '
              f'{_ipt_err}', flush=True)

    db.close()
    # AI assessment still fires in background (saved to ai_assessment column
    # for internal reference) — but the SMS-back no longer waits for it.
    _maybe_fire_assessment(bid_id, require_all=True, source='ipacket')
    # Driver SMS-back fires NOW — iPacket is the last step in the pipeline,
    # success or not_available both qualify. Idempotent (driver_notified_at
    # guard prevents double-text if assessment also tries to notify).
    try:
        _notify_driver_if_pending(bid_id)
    except Exception as e:
        print(f'[ipacket-notify] error bid={bid_id}: {e}', flush=True)
    return jsonify({'ok': True, 'bid_id': bid_id})


@app.route('/api/ipacket/upload_report', methods=['POST'])
def api_ipacket_upload_report():
    """Accept iPacket sticker screenshot upload from worker."""
    if 'file' not in request.files:
        return jsonify({'error': 'no file'}), 400
    f = request.files['file']
    filename = f.filename
    save_path = os.path.join(IPACKET_REPORTS_DIR, filename)
    f.save(save_path)
    return jsonify({'ok': True, 'filename': filename})


@app.route('/ipacket_reports/<path:filename>')
def serve_ipacket_report(filename):
    """Serve iPacket screenshot images."""
    return send_from_directory(IPACKET_REPORTS_DIR, filename)


# ─────────────────────── Comp MSRP queue (Phase 2) ──────────────────────────
# When a bid's rBook competitive set lands, we want MSRP for the top-3 closest-
# in-miles comps so the UI + Gemini prompt can compare retail price vs original
# MSRP. iPacket scraping by VIN is slow (~60s) so we queue per-VIN with a
# CACHED result table — same comp VIN seen across multiple bids only triggers
# ONE iPacket call ever. VM 121 (vm-oscar-worker-2) polls + scrapes.

@app.route('/api/comp_msrp/enqueue', methods=['POST'])
def api_comp_msrp_enqueue():
    """Add a list of VINs to the comp_msrps queue. Idempotent — only inserts
    rows that don't already exist. Body: {vins: [...], trigger_bid_id: 123}"""
    data = request.json or {}
    vins = [v for v in (data.get('vins') or []) if isinstance(v, str) and len(v) == 17]
    trigger = data.get('trigger_bid_id')
    if not vins:
        return jsonify({'enqueued': 0, 'reason': 'no valid vins'})
    db = get_db()
    cur = db.cursor()
    try:
        rows = [(v.upper(), trigger) for v in vins]
        cur.executemany("""
            INSERT INTO comp_msrps (vin, trigger_bid_id, status)
            VALUES (%s, %s, 'pending')
            ON CONFLICT (vin) DO NOTHING
        """, rows)
        db.commit()
        return jsonify({'enqueued': len(vins)})
    except Exception as e:
        db.rollback()
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500
    finally:
        db.close()


@app.route('/api/comp_msrp/jwt')
def api_comp_msrp_jwt():
    """Return the iPacket JWT for distributed comp_msrp workers (e.g.
    VM 121's worker_comp_msrp.py)."""
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("SELECT cookies FROM vauto_session WHERE label='ipacket'")
        row = cur.fetchone()
        if not row:
            return jsonify({'error': 'no ipacket jwt seeded'}), 404
        blob = row['cookies']
        if isinstance(blob, str):
            blob = json.loads(blob)
        jwt = (blob or {}).get('jwt')
        if not jwt:
            return jsonify({'error': 'jwt missing in session'}), 404
        return jsonify({'jwt': jwt})
    finally:
        db.close()


@app.route('/api/comp_msrp/claim', methods=['POST'])
def api_comp_msrp_claim():
    """Worker claims one pending VIN. Body: {worker_id: 'oscar-worker-2'}"""
    data = request.json or {}
    worker_id = (data.get('worker_id') or '').strip()
    if not worker_id:
        return jsonify({'error': 'worker_id required'}), 400
    db = get_db()
    cur = db.cursor()
    try:
        # Atomic claim: SELECT FOR UPDATE SKIP LOCKED so concurrent workers
        # don't pick the same VIN. Lease times out after 600s.
        cur.execute("""
            UPDATE comp_msrps
            SET status='in_progress', claimed_by=%s, claimed_at=NOW(),
                updated_at=NOW()
            WHERE vin = (
                SELECT vin FROM comp_msrps
                WHERE status='pending'
                   OR (status='in_progress'
                       AND claimed_at < NOW() - INTERVAL '10 minutes')
                ORDER BY created_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING vin, trigger_bid_id
        """, (worker_id,))
        row = cur.fetchone()
        db.commit()
        if not row:
            return jsonify({'job': None})
        return jsonify({'job': {'vin': row['vin'], 'trigger_bid_id': row['trigger_bid_id']}})
    except Exception as e:
        db.rollback()
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500
    finally:
        db.close()


@app.route('/api/comp_msrp/submit', methods=['POST'])
def api_comp_msrp_submit():
    """Worker submits result. Body:
       {worker_id, vin, status: 'done'|'failed', msrp, base_price, error, raw}"""
    data = request.json or {}
    vin = (data.get('vin') or '').strip().upper()
    if not vin:
        return jsonify({'error': 'vin required'}), 400
    status = data.get('status', 'done')
    if status not in ('done', 'failed'):
        return jsonify({'error': "status must be 'done' or 'failed'"}), 400
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("""
            UPDATE comp_msrps
            SET status=%s, msrp=%s, base_price=%s,
                error=%s, raw_json=%s::jsonb,
                completed_at=NOW(), updated_at=NOW()
            WHERE vin=%s
            RETURNING vin
        """, (
            status, data.get('msrp'), data.get('base_price'),
            data.get('error'),
            json.dumps(data.get('raw')) if data.get('raw') else None,
            vin,
        ))
        if not cur.fetchone():
            db.rollback()
            return jsonify({'error': f'vin {vin} not in queue'}), 404
        db.commit()
        return jsonify({'ok': True, 'vin': vin, 'status': status})
    except Exception as e:
        db.rollback()
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500
    finally:
        db.close()


@app.route('/api/comp_msrp/status')
def api_comp_msrp_status():
    """Queue stats for monitoring."""
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("""
            SELECT status, COUNT(*) AS n FROM comp_msrps GROUP BY status
        """)
        return jsonify({'queue': {r['status']: r['n'] for r in cur.fetchall()}})
    finally:
        db.close()


# ── Historical purchase retrieval (Phase 4b — per-YMM, replaces XGBoost) ──
# Per the 2026-05-06 decision: with only 146 reconciled samples spread across
# 20+ makes, an XGBoost regressor was too noisy. Replaced with tiered SQL
# retrieval against ai_accuracy. Returns mean/median/stddev of actual paid
# for cars matching subject's year/make/model + mileage band, with broader
# fallbacks when tier 1 is sparse. Honest, explainable, tightens as more
# data accumulates without retraining.

def _retrieve_purchase_history(year, make, model, mileage, exclude_bid_id=None):
    """Tiered query against ai_accuracy. Returns dict with stats + tier used,
    or None if no matches at all. exclude_bid_id prevents self-reference
    when reassessing a bid that's already reconciled.
    Also computes AI-accuracy stats over the same matches so Gemini sees
    its own track record (mean signed error %, median |error| %)."""
    if not (make and model):
        return None
    db = get_db()
    cur = db.cursor()
    try:
        miles_lo = int(mileage * 0.7) if mileage else None
        miles_hi = int(mileage * 1.3) if mileage else None
        excl_clause = ' AND bid_id != %s' if exclude_bid_id else ''
        excl_param  = (exclude_bid_id,) if exclude_bid_id else ()

        # STRICT_YMM_ONLY — exact year+make+model match only. No mileage
        # restriction (mileage variance is fine — same model is same model).
        # No make-only fallback. No year fallback. Empty if no exact match.
        tiers = []
        if year and make and model:
            tiers.append(('YMM (any miles)',
                "year = %s AND UPPER(make) = UPPER(%s) AND UPPER(model) = UPPER(%s)" + excl_clause,
                (year, make, model) + excl_param))

        MIN_TIER_N = 1  # any exact YMM match is meaningful
        for tier_name, where_clause, params in tiers:
            cur.execute(f"""
                SELECT
                    COUNT(*) AS n,
                    AVG(actual_purchase_cost)::int AS mean,
                    percentile_cont(0.5) WITHIN GROUP (ORDER BY actual_purchase_cost) AS median,
                    STDDEV(actual_purchase_cost) AS stddev,
                    MIN(actual_purchase_cost) AS p_min,
                    MAX(actual_purchase_cost) AS p_max,
                    AVG(mileage)::int AS avg_mileage,
                    AVG(year)::numeric(6,1) AS avg_year,
                    AVG(delta_pct)::numeric(7,2) AS ai_mean_signed_pct,
                    percentile_cont(0.5) WITHIN GROUP (ORDER BY abs_delta_pct) AS ai_median_abs_pct,
                    SUM(CASE WHEN in_confidence_range THEN 1 ELSE 0 END) AS ai_in_range_n
                FROM ai_accuracy
                WHERE {where_clause}
                  AND actual_purchase_cost > 0
            """, params)
            r = cur.fetchone()
            n_here = int(r['n']) if (r and r['n']) else 0
            if n_here and n_here < MIN_TIER_N:
                print(f"[purchase_history] tier={tier_name} skipped (n={n_here}<{MIN_TIER_N})", flush=True)
                continue
            if r and r['n'] and r['n'] >= MIN_TIER_N:
                cur.execute(f"""
                    SELECT bid_id, year, make, model, mileage,
                           actual_purchase_cost, ai_recommendation,
                           delta_pct,
                           actual_purchased_at::date AS purchased_date
                    FROM ai_accuracy
                    WHERE {where_clause}
                      AND actual_purchase_cost > 0
                    ORDER BY actual_purchased_at DESC NULLS LAST
                    LIMIT 5
                """, params)
                samples = [dict(s) for s in cur.fetchall()]
                for s in samples:
                    if hasattr(s.get('purchased_date'), 'isoformat'):
                        s['purchased_date'] = s['purchased_date'].isoformat()
                    if s.get('delta_pct') is not None:
                        try: s['delta_pct'] = float(s['delta_pct'])
                        except (TypeError, ValueError): pass
                return {
                    'tier':              tier_name,
                    'n':                 int(r['n']),
                    'mean':              int(r['mean']) if r['mean'] else None,
                    'median':            int(r['median']) if r['median'] else None,
                    'stddev':            int(r['stddev']) if r['stddev'] else None,
                    'min':               int(r['p_min']) if r['p_min'] else None,
                    'max':               int(r['p_max']) if r['p_max'] else None,
                    'avg_mileage':       int(r['avg_mileage']) if r['avg_mileage'] else None,
                    # AI track record for THIS YMM tier — calibration anchor
                    'ai_mean_signed_pct':  float(r['ai_mean_signed_pct']) if r['ai_mean_signed_pct'] is not None else None,
                    'ai_median_abs_pct':   float(r['ai_median_abs_pct']) if r['ai_median_abs_pct'] is not None else None,
                    'ai_in_range_n':       int(r['ai_in_range_n']) if r['ai_in_range_n'] is not None else 0,
                    'samples':           samples,
                }
        return None
    except Exception as e:
        print(f'[retrieval] err: {e}', flush=True)
        return None
    finally:
        db.close()


# ── XGBoost path retired 2026-05-06 — kept stub so callers don't break ────
def _load_ml_predictor():
    import time as _tt
    # Refresh from disk every 10 minutes to pick up retrained models
    if (_ML_MODEL_CACHE['model'] is not None
        and _tt.time() - _ML_MODEL_CACHE['loaded_at'] < 600):
        return _ML_MODEL_CACHE['model'], _ML_MODEL_CACHE['meta']
    try:
        import xgboost as _xgb
        import os as _os
        model_dir = '/opt/expwholesale/ml_models'
        model_path = _os.path.join(model_dir, 'purchase_predictor.json')
        meta_path  = _os.path.join(model_dir, 'purchase_predictor_meta.json')
        if not _os.path.exists(model_path):
            return None, None
        m = _xgb.XGBRegressor()
        m.load_model(model_path)
        with open(meta_path) as f:
            meta = json.load(f)
        _ML_MODEL_CACHE['model'] = m
        _ML_MODEL_CACHE['meta']  = meta
        _ML_MODEL_CACHE['loaded_at'] = _tt.time()
        return m, meta
    except Exception as e:
        print(f'[ml_predictor] load err: {e}', flush=True)
        return None, None


def _ml_purchase_predict(bid: dict, vauto: dict | None,
                         ipacket: dict | None, buyer_intel: dict | None) -> dict | None:
    """Run the XGBoost predictor for one bid. Returns:
       {prediction, confidence_low, confidence_high, mape_pct, n_train}
       or None if model not loaded / features missing."""
    model, meta = _load_ml_predictor()
    if not model or not meta:
        return None
    try:
        import numpy as _np
        feat_names = meta.get('features') or []
        numeric_features = meta.get('numeric_features') or []
        top_makes = meta.get('top_makes') or []

        # Build feature row using SAME ordering as training
        # Pull values from bid + linked tables
        vals = {}
        vals['year']    = bid.get('year')
        vals['mileage'] = bid.get('mileage')

        # Manheim
        mh_summary = ((vauto or {}).get('manheim_transactions') or {}).get('summary') or {}
        if isinstance((vauto or {}).get('manheim_transactions'), str):
            try:
                _t = json.loads(vauto['manheim_transactions'])
                mh_summary = _t.get('summary') or {}
                mh_txns = _t.get('transactions') or []
            except Exception:
                mh_txns = []
        else:
            mh_txns = ((vauto or {}).get('manheim_transactions') or {}).get('transactions') or []
        vals['mmr_adjusted'] = mh_summary.get('adjusted_mmr')
        vals['mmr_base']     = mh_summary.get('base_mmr')
        vals['mmr_n_tx']     = len(mh_txns) if isinstance(mh_txns, list) else 0
        if isinstance(mh_txns, list):
            prices = sorted(t.get('sale_price') for t in mh_txns
                            if isinstance(t, dict)
                            and isinstance(t.get('sale_price'), (int, float))
                            and 1000 < t['sale_price'] < 2_000_000)
            vals['mmr_median'] = prices[len(prices)//2] if prices else None

        # iPacket subject MSRP
        vals['subject_msrp']       = (ipacket or {}).get('total_msrp')
        vals['subject_base_price'] = (ipacket or {}).get('base_price')

        # LSL aggregates
        patterns = (buyer_intel or {}).get('patterns') or {}
        vals['lsl_n_deals']      = patterns.get('total_deals') or 0
        vals['lsl_avg_sale']     = patterns.get('avg_sale_price')
        vals['lsl_avg_gross']    = patterns.get('avg_front_value')  # LSL native field; ML feature name retained for trained model
        if vals['lsl_avg_sale'] and vals['lsl_avg_gross']:
            vals['lsl_avg_purchase'] = int(vals['lsl_avg_sale'] - vals['lsl_avg_gross'])
        else:
            vals['lsl_avg_purchase'] = None

        # rBook stats — currently mostly missing (we only get clean ones from
        # competition_api source). Leave as None when not available.
        rb_obj = (vauto or {}).get('rbook_competitive_set')
        if isinstance(rb_obj, str):
            try: rb_obj = json.loads(rb_obj)
            except Exception: rb_obj = None
        rb_rows = (rb_obj or {}).get('rows') or []
        if rb_rows:
            # 2026-05-11: strict VIN-prefix filter — drops Carrera-vs-GTS bleed
            rb_rows, _drop, _src = filter_rbook_to_strict_peers(
                bid.get('vin'), rb_rows)
            if _drop:
                print(f'[ml] rbook strict-filter bid={bid.get("id")}: '
                      f'dropped {_drop} rows ({_src})', flush=True)
            asks = sorted(v.get('price') for v in rb_rows
                          if isinstance(v, dict)
                          and isinstance(v.get('price'), (int, float))
                          and 1000 < v['price'] < 2_000_000)
            vals['rbook_median'] = asks[len(asks)//2] if asks else None
            vals['rbook_n_clean'] = len(asks)
        if vals.get('rbook_median') and vals.get('mmr_median'):
            vals['retail_mmr_spread'] = vals['rbook_median'] - vals['mmr_median']

        # Build vector in TRAIN order
        row = []
        for c in numeric_features:
            v = vals.get(c)
            try: v = float(v) if v is not None else -1.0
            except (TypeError, ValueError): v = -1.0
            row.append(v)
        make_upper = (bid.get('make') or '').upper()
        for m in top_makes:
            row.append(1.0 if make_upper == m else 0.0)
        row.append(0.0 if make_upper in top_makes else 1.0)  # make_other

        X = _np.array([row], dtype=_np.float32)
        pred = float(model.predict(X)[0])

        # Confidence band from CV MAPE (~27% on first train; tightens over time)
        mape_pct = meta.get('cv_mape_pct') or 25.0
        spread = pred * (mape_pct / 100.0)
        return {
            'prediction':       int(round(pred)),
            'confidence_low':   int(round(pred - spread)),
            'confidence_high':  int(round(pred + spread)),
            'mape_pct':         round(mape_pct, 2),
            'n_train':          meta.get('n_train', 0),
        }
    except Exception as e:
        print(f'[ml_predictor] predict err: {e}', flush=True)
        return None


def _ipacket_lookup_msrp_for_vin(vin):
    """Pull iPacket sticker for ONE VIN and return parsed MSRP/base_price.
    Uses the stored JWT in vauto_session label='ipacket'. Returns dict:
      {ok: True, msrp, base_price, raw}
      {ok: False, error}
    Roughly 5-15s per call. Reused for both subject-vehicle iPacket and
    comp-vehicle MSRP lookups (Phase 2).
    """
    import requests as _rr
    import time as _tt
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT cookies FROM vauto_session WHERE label='ipacket'")
    sess = cur.fetchone()
    db.close()
    if not sess:
        return {'ok': False, 'error': 'no jwt seeded'}
    blob = sess['cookies']
    if isinstance(blob, str):
        blob = json.loads(blob)
    jwt = (blob or {}).get('jwt')
    if not jwt:
        return {'ok': False, 'error': 'jwt missing'}

    H = {
        'Authorization': f'bearer {jwt}',
        'Origin': 'https://dpapp.autoipacket.com',
        'Referer': 'https://dpapp.autoipacket.com/',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36',
    }
    try:
        r = _rr.put(f'https://djapi.autoipacket.com/v2/sticker-puller/pull/{vin}',
                    headers=H, timeout=15)
        if r.status_code == 401:
            return {'ok': False, 'error': 'jwt expired'}
        if r.status_code not in (200, 201):
            return {'ok': False, 'error': f'pull rc={r.status_code}: {r.text[:200]}'}
        job_id = r.json().get('id')
        if not job_id:
            return {'ok': False, 'error': 'no job_id in pull response'}
        viewer = None
        for _ in range(25):
            pr = _rr.get(f'https://djapi.autoipacket.com/v2/sticker-puller/poll/{job_id}',
                         headers=H, timeout=10)
            body = pr.json() if pr.status_code in (200, 201) else {}
            state = body.get('state')
            if state == 'SUCCESS':
                viewer = body.get('pdf') or body.get('ipacket_viewer')
                break
            if state in ('FAILED', 'ERROR'):
                return {'ok': False, 'error': f'pull state={state} {body.get("detail","")}'}
            _tt.sleep(1)
        if not viewer:
            return {'ok': False, 'error': 'pull timed out 25s'}
        # Fetch viewer — content-type can be PDF or HTML depending on
        # the iPacket account. PDFs are text-extractable directly.
        try:
            vr = _rr.get(viewer, headers=H, timeout=30)
            ct = (vr.headers.get('content-type') or '').lower()
            if vr.status_code != 200:
                return {'ok': False, 'error': f'viewer rc={vr.status_code}'}
            text = ''
            ocr_used = False
            if 'pdf' in ct:
                try:
                    import pdfplumber, io as _io
                    with pdfplumber.open(_io.BytesIO(vr.content)) as pdf:
                        text = '\n'.join((p.extract_text() or '') for p in pdf.pages)
                        # OCR fallback for image-only PDFs (Porsche, Cadillac
                        # Escalade dealer-uploaded scans, etc.) where text
                        # layer is empty. Render each page → PNG → Vision.
                        if len(text.strip()) < 200:
                            ocr_chunks = []
                            for page in pdf.pages:
                                try:
                                    pil_img = page.to_image(resolution=200).original
                                    buf = _io.BytesIO()
                                    pil_img.save(buf, format='PNG')
                                    ocr_text = _google_vision_ocr(buf.getvalue())
                                    if ocr_text:
                                        ocr_chunks.append(ocr_text)
                                except Exception as _pe:
                                    pass
                            if ocr_chunks:
                                text = '\n'.join(ocr_chunks)
                                ocr_used = True
                except Exception as _pdf_e:
                    return {'ok': False, 'error': f'pdf parse: {_pdf_e}'}
            else:
                text = vr.text
        except Exception as _vex:
            return {'ok': False, 'error': f'viewer fetch: {_vex}'}
        parsed = _parse_sticker_text(text) if text else {}
        return {'ok': True,
                'msrp':       parsed.get('total_msrp'),
                'base_price': parsed.get('base_price'),
                'viewer_url': viewer,
                'ocr_used':   ocr_used,
                'raw':        {'options':        parsed.get('options'),
                               'exterior_color': parsed.get('exterior_color'),
                               'interior_color': parsed.get('interior_color'),
                               'text_chars':     len(text)}}
    except Exception as e:
        return {'ok': False, 'error': f'{type(e).__name__}: {e}'}


def _comp_msrp_processor_loop():
    """Background daemon: claims one pending comp_msrps row at a time and
    processes it via iPacket. Started by app boot. Sequential processing
    (avoid rate-limiting iPacket API), but sleeps short when work is
    pending and longer when idle."""
    import time as _tt
    import requests as _rr  # noqa: F401
    print('[comp_msrp processor] starting daemon', flush=True)
    consecutive_empty = 0
    while True:
        try:
            db = get_db()
            cur = db.cursor()
            cur.execute("""
                UPDATE comp_msrps
                SET status='in_progress', claimed_by='ew-server-bg',
                    claimed_at=NOW(), updated_at=NOW()
                WHERE vin = (
                    SELECT vin FROM comp_msrps
                    WHERE status='pending'
                       OR (status='in_progress'
                           AND claimed_at < NOW() - INTERVAL '10 minutes')
                    ORDER BY created_at ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING vin
            """)
            row = cur.fetchone()
            db.commit()
            db.close()
            if not row:
                # Idle: ramp up sleep so we're not hammering the DB. First
                # empty poll: 5s, then 10s, then 30s, capped at 60s.
                consecutive_empty += 1
                _tt.sleep(min(60, 5 * (2 ** min(consecutive_empty - 1, 3))))
                continue
            consecutive_empty = 0
            vin = row['vin']
            print(f'[comp_msrp processor] claim {vin}', flush=True)
            res = _ipacket_lookup_msrp_for_vin(vin)
            db = get_db()
            cur = db.cursor()
            try:
                if res.get('ok'):
                    cur.execute("""
                        UPDATE comp_msrps
                        SET status='done', msrp=%s, base_price=%s,
                            raw_json=%s::jsonb,
                            completed_at=NOW(), updated_at=NOW()
                        WHERE vin=%s
                    """, (res.get('msrp'), res.get('base_price'),
                          json.dumps(res.get('raw') or {}), vin))
                    print(f'[comp_msrp processor] {vin} → MSRP=${res.get("msrp") or 0:,} '
                          f'base=${res.get("base_price") or 0:,}', flush=True)
                else:
                    cur.execute("""
                        UPDATE comp_msrps
                        SET status='failed', error=%s,
                            completed_at=NOW(), updated_at=NOW()
                        WHERE vin=%s
                    """, (str(res.get('error'))[:500], vin))
                    print(f'[comp_msrp processor] {vin} FAILED: {res.get("error")}',
                          flush=True)
                db.commit()
            finally:
                db.close()
            # No throttle when more work pending — back-to-back iPacket
            # pulls. Each pull is rate-limited at the iPacket-API level
            # already (PUT/poll has its own ~1-3s overhead).
        except Exception as e:
            print(f'[comp_msrp processor] loop err: {e}', flush=True)
            _tt.sleep(30)


def _start_comp_msrp_processor():
    """Start the daemon thread once per gunicorn worker. Idempotent.
    Gated by env COMP_MSRP_DAEMON=1 — default OFF since VM 121
    (oscar-worker-2) is the canonical comp_msrp processor. Set the env
    var only when VM 121 is offline and you need the server to fall
    back to handling comp_msrp jobs."""
    if os.environ.get('COMP_MSRP_DAEMON', '0') != '1':
        return
    import threading
    global _COMP_MSRP_THREAD_STARTED
    try:
        _COMP_MSRP_THREAD_STARTED
    except NameError:
        _COMP_MSRP_THREAD_STARTED = False
    if _COMP_MSRP_THREAD_STARTED:
        return
    _COMP_MSRP_THREAD_STARTED = True
    t = threading.Thread(target=_comp_msrp_processor_loop,
                         name='comp_msrp_processor', daemon=True)
    t.start()
    print('[comp_msrp processor] DAEMON STARTED (env COMP_MSRP_DAEMON=1)',
          flush=True)


def _enqueue_msrp_vins(bid_id, vins):
    """Generic enqueue: insert any list of VINs into comp_msrps with
    status='pending' (idempotent via ON CONFLICT). Used by the bid view to
    queue dealer_intel + buyer_intel VINs alongside rBook closest_3 — VM
    121's worker scrapes iPacket per-VIN and writes msrp/base_price back.

    Returns count of NEW VINs queued (already-cached ones don't change state).
    """
    if not vins:
        return 0
    clean = list({v.upper() for v in vins
                  if v and isinstance(v, str) and len(v) == 17})
    if not clean:
        return 0
    db = get_db()
    cur = db.cursor()
    try:
        cur.executemany("""
            INSERT INTO comp_msrps (vin, trigger_bid_id, status)
            VALUES (%s, %s, 'pending')
            ON CONFLICT (vin) DO NOTHING
        """, [(v, bid_id) for v in clean])
        db.commit()
        return cur.rowcount
    except Exception as e:
        print(f'[comp_msrp enqueue VINs] bid={bid_id} err: {e}', flush=True)
        return 0
    finally:
        db.close()


def _attach_msrp_to_rows(rows, vin_field, msrps):
    """Mutate rows in place: attach msrp_lookup dict to each row whose
    VIN appears in the msrps cache. Mirrors the rbook closest_3 pattern."""
    if not rows or not msrps:
        return
    for r in rows:
        v = (r.get(vin_field) or '').upper()
        if v and v in msrps:
            r['msrp_lookup'] = {
                'msrp':       msrps[v].get('msrp'),
                'base_price': msrps[v].get('base_price'),
                'status':     msrps[v].get('status'),
            }


def _enqueue_comp_msrps_for_bid(bid_id, market_intel_obj):
    """Helper called from the bid view: take the closest-3 rBook comp VINs
    and INSERT them into comp_msrps (idempotent). Worker on VM 121 picks up.
    No-op if no closest_3 yet."""
    try:
        rb = (market_intel_obj or {}).get('rbook') or {}
        closest = rb.get('closest_3') or []
        vins = [(c.get('vin') or '').upper()
                for c in closest if c.get('vin')]
        vins = [v for v in vins if len(v) == 17]
        if not vins: return
        db = get_db()
        cur = db.cursor()
        try:
            cur.executemany("""
                INSERT INTO comp_msrps (vin, trigger_bid_id, status)
                VALUES (%s, %s, 'pending')
                ON CONFLICT (vin) DO NOTHING
            """, [(v, bid_id) for v in vins])
            db.commit()
        finally:
            db.close()
    except Exception as e:
        print(f'[comp_msrp enqueue] bid={bid_id} err: {e}', flush=True)


def _load_comp_msrps(vins):
    """Lookup MSRPs for a list of VINs. Returns dict {vin: {msrp, status}}."""
    if not vins:
        return {}
    vins = [v.upper() for v in vins if v]
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("""
            SELECT vin, msrp, base_price, status, error
            FROM comp_msrps
            WHERE vin = ANY(%s)
        """, (vins,))
        return {r['vin']: dict(r) for r in cur.fetchall()}
    finally:
        db.close()


@app.route('/api/ipacket/status/<int:bid_id>')
def api_ipacket_status(bid_id):
    """Check if iPacket lookup is complete for a bid."""
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("SELECT * FROM ipacket_lookups WHERE bid_id = %s", (bid_id,))
        row = cur.fetchone()
    except Exception:
        row = None
    db.close()
    if row:
        d = dict(row)
        for k, v in d.items():
            if hasattr(v, 'isoformat'):
                d[k] = v.isoformat()
        status = 'not_available' if d.get('not_available') else 'complete'
        return jsonify({'status': status, 'data': d})
    return jsonify({'status': 'pending'})


@app.route('/api/verify-comps', methods=['POST'])
def api_verify_comps():
    """Verify comp URLs in parallel — check if still active, get live price."""
    data = request.json or {}
    comps = data.get('comps', [])
    if not comps:
        return jsonify({'results': []})

    from concurrent.futures import ThreadPoolExecutor, as_completed
    results = {}

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(verify_comp, c.get('url')): c.get('vin', str(i))
                   for i, c in enumerate(comps)}
        for future in as_completed(futures):
            vin = futures[future]
            try:
                results[vin] = future.result()
            except Exception:
                results[vin] = {'status': 'error'}

    return jsonify({'results': results})


# ---------------------------------------------------------------------------
# Tesla VIN Decoder
# ---------------------------------------------------------------------------
TESLA_MODELS = {'S': 'Model S', 'X': 'Model X', '3': 'Model 3', 'Y': 'Model Y', 'C': 'Cybertruck', 'R': 'Roadster'}

TESLA_BODY = {
    'A': '5-door Hatchback/Sedan LHD',
    'B': '5-door Hatchback/Sedan RHD',
    'C': 'MPV Class E LHD',
    'D': 'MPV Class D LHD',
    'E': '4-door Sedan LHD',
    'F': '4-door Sedan RHD',
    'G': 'MPV Class D',
}

TESLA_BATTERY = {
    'E': 'Standard/Extended Range (NCA/NMC)',
    'F': 'Lithium Iron Phosphate (LFP)',
    'H': 'High Capacity LFP',
    'S': 'Standard Capacity',
    'V': 'Very High Capacity (NCA/NMC)',
}

TESLA_MOTOR = {
    '3': {'A': 'Single Motor (Standard)', 'B': 'Dual Motor AWD', 'C': 'Dual Motor Performance',
           'J': 'Single Motor (Hairpin)', 'K': 'Dual Motor (Hairpin)', 'L': 'Performance (Hairpin)'},
    'Y': {'D': 'Single Motor (Standard)', 'E': 'Dual Motor AWD', 'F': 'Dual Motor Performance',
           'J': 'Single Motor (Hairpin)', 'K': 'Dual Motor (Hairpin)', 'L': 'Performance (Hairpin)'},
    'S': {'1': 'Single Motor (60/70/85)', '2': 'Dual Motor AWD', '3': 'Dual Motor Performance',
           '4': 'Dual Motor Performance (Ludicrous)', '5': 'Dual Motor', '6': 'Tri Motor (Plaid)',
           'A': 'Single Motor', 'B': 'Dual Motor', 'C': 'Dual Motor Performance'},
    'X': {'1': 'Single Motor', '2': 'Dual Motor AWD', '3': 'Dual Motor Performance',
           '4': 'Dual Motor Performance (Ludicrous)', '5': 'Dual Motor', '6': 'Tri Motor (Plaid)',
           'A': 'Single Motor', 'B': 'Dual Motor', 'C': 'Dual Motor Performance'},
    'C': {'D': 'Dual Motor AWD', 'E': 'Tri Motor', 'F': 'Single Motor (Foundation)'},
}

TESLA_YEAR = {
    'A': 2010, 'B': 2011, 'C': 2012, 'D': 2013, 'E': 2014, 'F': 2015,
    'G': 2016, 'H': 2017, 'J': 2018, 'K': 2019, 'L': 2020, 'M': 2021,
    'N': 2022, 'P': 2023, 'R': 2024, 'S': 2025, 'T': 2026, 'V': 2027,
}

TESLA_PLANT = {
    'A': 'Austin, TX (Giga Texas)',
    'B': 'Berlin, Germany (Giga Berlin)',
    'C': 'Shanghai, China (Giga Shanghai)',
    'F': 'Fremont, CA',
    'N': 'Reno, NV (Gigafactory 1)',
}

TESLA_WMI = {
    '5YJ': 'Tesla USA (Fremont)',
    '7SA': 'Tesla USA (Fremont)',
    '7G2': 'Tesla USA (Trucks)',
    'LRW': 'Tesla China (Shanghai)',
    'XP7': 'Tesla Germany (Berlin)',
    'SFZ': 'Tesla UK',
}


# Base MSRP by (model_code, motor_key, year). motor_key derived from trim.
# Sources: Tesla.com historical pricing, press releases, community records.
TESLA_MSRP = {
    # Model 3
    ('3', 'SR', 2018): 35000, ('3', 'SR', 2019): 35000, ('3', 'SR', 2020): 37990,
    ('3', 'SR', 2021): 39490, ('3', 'SR', 2022): 46990, ('3', 'SR', 2023): 40240,
    ('3', 'SR', 2024): 38990, ('3', 'SR', 2025): 38990,
    ('3', 'LR', 2018): 49000, ('3', 'LR', 2019): 48990, ('3', 'LR', 2020): 46990,
    ('3', 'LR', 2021): 48490, ('3', 'LR', 2022): 55990, ('3', 'LR', 2023): 47240,
    ('3', 'LR', 2024): 45990, ('3', 'LR', 2025): 45990,
    ('3', 'P', 2018): 64000, ('3', 'P', 2019): 56990, ('3', 'P', 2020): 54990,
    ('3', 'P', 2021): 56490, ('3', 'P', 2022): 62990, ('3', 'P', 2023): 53240,
    ('3', 'P', 2024): 52990, ('3', 'P', 2025): 52990,
    # Model Y
    ('Y', 'SR', 2020): 39990, ('Y', 'SR', 2021): 41990, ('Y', 'SR', 2022): 47990,
    ('Y', 'SR', 2023): 43990, ('Y', 'SR', 2024): 44990, ('Y', 'SR', 2025): 44990,
    ('Y', 'LR', 2020): 49990, ('Y', 'LR', 2021): 53990, ('Y', 'LR', 2022): 59990,
    ('Y', 'LR', 2023): 50490, ('Y', 'LR', 2024): 48990, ('Y', 'LR', 2025): 48990,
    ('Y', 'P', 2020): 59990, ('Y', 'P', 2021): 60990, ('Y', 'P', 2022): 67990,
    ('Y', 'P', 2023): 54490, ('Y', 'P', 2024): 52490, ('Y', 'P', 2025): 52490,
    # Model S
    ('S', 'SR', 2014): 64000, ('S', 'SR', 2015): 70000,
    ('S', 'SR', 2016): 66000, ('S', 'SR', 2017): 68000, ('S', 'SR', 2018): 74500,
    ('S', 'SR', 2019): 79990, ('S', 'LR', 2016): 75000, ('S', 'LR', 2017): 78000,
    ('S', 'LR', 2018): 82500, ('S', 'LR', 2019): 87490,
    ('S', 'LR', 2020): 74990, ('S', 'LR', 2021): 79990,
    ('S', 'LR', 2022): 94990, ('S', 'LR', 2023): 89990, ('S', 'LR', 2024): 74990,
    ('S', 'LR', 2025): 74990,
    ('S', 'P', 2016): 108000, ('S', 'P', 2017): 115000, ('S', 'P', 2018): 115000,
    ('S', 'P', 2019): 99990, ('S', 'P', 2020): 94990,
    ('S', 'Plaid', 2021): 129990, ('S', 'Plaid', 2022): 135990,
    ('S', 'Plaid', 2023): 108990, ('S', 'Plaid', 2024): 89990, ('S', 'Plaid', 2025): 89990,
    # Model X
    ('X', 'SR', 2016): 80000, ('X', 'SR', 2017): 83000, ('X', 'SR', 2018): 84990,
    ('X', 'SR', 2019): 84990, ('X', 'LR', 2016): 93500, ('X', 'LR', 2017): 96000,
    ('X', 'LR', 2018): 97500, ('X', 'LR', 2019): 94990,
    ('X', 'LR', 2020): 79990, ('X', 'LR', 2021): 89990,
    ('X', 'LR', 2022): 104990, ('X', 'LR', 2023): 98990, ('X', 'LR', 2024): 79990,
    ('X', 'LR', 2025): 79990,
    ('X', 'P', 2016): 115000, ('X', 'P', 2017): 120000, ('X', 'P', 2018): 118000,
    ('X', 'P', 2019): 104990, ('X', 'P', 2020): 99990,
    ('X', 'Plaid', 2021): 119990, ('X', 'Plaid', 2022): 138990,
    ('X', 'Plaid', 2023): 109990, ('X', 'Plaid', 2024): 94990, ('X', 'Plaid', 2025): 94990,
    # Cybertruck
    ('C', 'SR', 2024): 60990, ('C', 'SR', 2025): 60990,
    ('C', 'LR', 2024): 79990, ('C', 'LR', 2025): 79990,
    ('C', 'P', 2024): 99990, ('C', 'P', 2025): 99990,
}


def _tesla_msrp_key(model_code, motor_str):
    """Map motor description to MSRP lookup key."""
    m = motor_str.lower()
    if 'plaid' in m or 'tri motor' in m:
        return 'Plaid'
    if 'performance' in m or 'ludicrous' in m:
        return 'P'
    if 'dual' in m:
        return 'LR'
    if 'single' in m:
        return 'SR'
    return 'SR'


def decode_tesla_vin(vin):
    """Decode a Tesla VIN into human-readable configuration."""
    if not vin or len(vin) != 17:
        return None

    vin = vin.upper()
    wmi = vin[:3]
    if wmi not in TESLA_WMI:
        return None  # not a Tesla

    model_code = vin[3]
    body_code = vin[4]
    battery_code = vin[6]
    motor_code = vin[7]
    year_code = vin[9]
    plant_code = vin[10]
    serial = vin[11:]

    model = TESLA_MODELS.get(model_code, f'Unknown ({model_code})')
    body = TESLA_BODY.get(body_code, f'Unknown ({body_code})')
    battery = TESLA_BATTERY.get(battery_code, f'Unknown ({battery_code})')

    # Motor is model-dependent
    motor_map = TESLA_MOTOR.get(model_code, {})
    motor = motor_map.get(motor_code, f'Unknown ({motor_code})')

    year = TESLA_YEAR.get(year_code, f'Unknown ({year_code})')
    plant = TESLA_PLANT.get(plant_code, f'Unknown ({plant_code})')
    manufacturer = TESLA_WMI.get(wmi, wmi)

    # Derive trim name
    trim = model
    if 'Performance' in motor or 'Plaid' in motor:
        trim += ' Performance' if 'Performance' in motor else ' Plaid'
    elif 'Dual Motor' in motor or 'Tri Motor' in motor:
        trim += ' Long Range AWD' if 'Dual' in motor else ' Tri Motor'
    elif 'Single Motor' in motor:
        trim += ' Standard Range'

    # Drive type
    if 'Dual' in motor or 'Tri' in motor or 'AWD' in motor:
        drive = 'All-Wheel Drive'
    else:
        drive = 'Rear-Wheel Drive'

    # MSRP lookup
    msrp_key = _tesla_msrp_key(model_code, motor)
    msrp = TESLA_MSRP.get((model_code, msrp_key, year))

    return {
        'vin': vin,
        'manufacturer': manufacturer,
        'model': model,
        'trim': trim,
        'body': body,
        'battery': battery,
        'motor': motor,
        'drive': drive,
        'year': year,
        'plant': plant,
        'serial': serial,
        'msrp': msrp,
        'raw': {
            'wmi': wmi, 'model': model_code, 'body': body_code,
            'restraint': vin[5], 'battery': battery_code,
            'motor': motor_code, 'check': vin[8],
            'year': year_code, 'plant': plant_code,
        }
    }


# ── Tesla Fleet API ───────────────────────────────────────────────────────────
TESLA_CLIENT_ID = os.environ.get('TESLA_CLIENT_ID', '434873df-71ed-45f7-9bcf-1f1a4a45b171')
TESLA_CLIENT_SECRET = os.environ.get('TESLA_CLIENT_SECRET', 'ta-secret.CYv2CB&F2_G-2Dnn')
TESLA_TOKEN_URL = 'https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token'
TESLA_FLEET_URL = 'https://fleet-api.prd.na.vn.cloud.tesla.com'

_tesla_token_cache = {'token': None, 'expires': 0}


def _get_tesla_partner_token():
    """Get or refresh Tesla partner token (machine-to-machine)."""
    import time
    if _tesla_token_cache['token'] and time.time() < _tesla_token_cache['expires'] - 60:
        return _tesla_token_cache['token']

    try:
        data = {
            'grant_type': 'client_credentials',
            'client_id': TESLA_CLIENT_ID,
            'client_secret': TESLA_CLIENT_SECRET,
            'scope': 'vehicle_device_data vehicle_specs',
            'audience': TESLA_FLEET_URL,
        }
        resp = requests.post(TESLA_TOKEN_URL, data=data, timeout=15)
        if resp.status_code != 200:
            print(f'Tesla token error: {resp.status_code} {resp.text[:200]}')
            return None
        token_data = resp.json()
        _tesla_token_cache['token'] = token_data['access_token']
        _tesla_token_cache['expires'] = time.time() + token_data.get('expires_in', 3600)
        return _tesla_token_cache['token']
    except Exception as e:
        print(f'Tesla token request failed: {e}')
        return None


def _tesla_fleet_vehicle_specs(vin):
    """Call Tesla Fleet API /vehicles/{vin}/specs for full factory options."""
    token = _get_tesla_partner_token()
    if not token:
        return None

    try:
        resp = requests.get(
            f'{TESLA_FLEET_URL}/api/1/vehicles/{vin}/specs',
            headers={'Authorization': f'Bearer {token}'},
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json()
        print(f'Tesla Fleet API error: {resp.status_code} {resp.text[:200]}')
        return None
    except Exception as e:
        print(f'Tesla Fleet API request failed: {e}')
        return None


# ── Tesla VIN options cache (DB-backed) ───────────────────────────────────────
def _ensure_tesla_table():
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tesla_vin_cache (
                vin VARCHAR(17) PRIMARY KEY,
                options_json JSONB,
                source VARCHAR(50),
                looked_up_at TIMESTAMP DEFAULT NOW()
            )
        """)
        db.commit()
        db.close()
    except Exception:
        pass

_ensure_tesla_table()


@app.route('/tesla-vin')
def tesla_vin_page():
    return render_template('tesla_vin.html')


@app.route('/api/tesla-vin/<vin>')
def api_tesla_vin(vin):
    vin = vin.upper().strip()
    result = decode_tesla_vin(vin)
    if not result:
        return jsonify({'error': 'Not a valid Tesla VIN'}), 400

    # NHTSA data
    nhtsa = decode_vin(vin)
    if nhtsa:
        result['nhtsa'] = nhtsa

    # Check DB cache first
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute("SELECT options_json, source FROM tesla_vin_cache WHERE vin=%s", (vin,))
        row = cur.fetchone()
        db.close()
        if row and row['options_json'] and row['source'] == 'tesla-fleet-api':
            result['fleet_specs'] = row['options_json']
            return jsonify(result)
        if row and row['options_json']:
            result['tesla_options'] = row['options_json']
    except Exception:
        pass

    # Try Tesla Fleet API (authoritative, $0.10/call)
    specs = _tesla_fleet_vehicle_specs(vin)
    if specs and specs.get('response'):
        fleet_data = specs['response']
        result['fleet_specs'] = fleet_data
        # Cache in DB
        try:
            db = get_db()
            cur = db.cursor()
            cur.execute("""
                INSERT INTO tesla_vin_cache (vin, options_json, source, looked_up_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (vin) DO UPDATE SET
                    options_json=EXCLUDED.options_json, source=EXCLUDED.source, looked_up_at=NOW()
            """, (vin, json.dumps(fleet_data), 'tesla-fleet-api'))
            db.commit()
            db.close()
        except Exception:
            pass

    return jsonify(result)


@app.route('/api/tesla-vin/pending')
def api_tesla_vin_pending():
    """Return VINs that need tesla-info lookup (requested but not cached)."""
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT vin FROM tesla_vin_cache
        WHERE options_json IS NULL
        ORDER BY looked_up_at ASC LIMIT 5
    """)
    rows = cur.fetchall()
    db.close()
    return jsonify({'pending': [r['vin'] for r in rows]})


@app.route('/api/tesla-vin/request', methods=['POST'])
def api_tesla_vin_request():
    """Queue a VIN for tesla-info lookup."""
    data = request.json or {}
    vin = (data.get('vin') or '').upper().strip()
    if not vin or len(vin) != 17:
        return jsonify({'error': 'invalid VIN'}), 400
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        INSERT INTO tesla_vin_cache (vin) VALUES (%s)
        ON CONFLICT (vin) DO NOTHING
    """, (vin,))
    db.commit()
    db.close()
    return jsonify({'ok': True, 'vin': vin})


@app.route('/api/tesla-vin/submit', methods=['POST'])
def api_tesla_vin_submit():
    """Accept tesla-info results from Beelink worker."""
    data = request.json or {}
    vin = (data.get('vin') or '').upper().strip()
    options = data.get('options')
    if not vin or not options:
        return jsonify({'error': 'missing vin or options'}), 400
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        INSERT INTO tesla_vin_cache (vin, options_json, source, looked_up_at)
        VALUES (%s, %s, %s, NOW())
        ON CONFLICT (vin) DO UPDATE SET
            options_json=EXCLUDED.options_json,
            source=EXCLUDED.source,
            looked_up_at=NOW()
    """, (vin, json.dumps(options), data.get('source', 'tesla-info')))
    db.commit()
    db.close()
    return jsonify({'ok': True, 'vin': vin})


# ---------------------------------------------------------------------------
# External bid intake (CarHub → EW push)
# ---------------------------------------------------------------------------
@app.route('/api/bid/external', methods=['POST'])
def api_bid_external():
    """Accept a bid from an external system (e.g., CarHub) with all enrichment data.
    Treated as a brand-new bid — EW's own vAuto worker runs a fresh lookup."""
    data = request.json
    if not data:
        return jsonify({'error': 'missing JSON body'}), 400

    vin = (data.get('vin') or '').strip().upper()
    if not vin or len(vin) != 17:
        return jsonify({'error': 'valid 17-char VIN required'}), 400

    db = get_db()
    cur = db.cursor()

    # Use rep name if provided so the bid shows up in their mobile My Bids.
    # Falls back to ext:<source> for anonymous pushes.
    source = data.get('source', 'external')
    rep_name = (data.get('rep_name') or '').strip()
    if rep_name:
        rep_phone = f'field:{rep_name.replace(" ", "_").lower()}'
        contact_name = rep_name
    else:
        rep_phone = f'ext:{source}'
        contact_name = f'{source.title()} Integration'

    cur.execute("""
        INSERT INTO contacts (phone, name)
        VALUES (%s, %s)
        ON CONFLICT (phone) DO UPDATE SET name = EXCLUDED.name
        RETURNING id
    """, (rep_phone, contact_name))
    contact_id = cur.fetchone()['id']

    # Build raw_message summary
    parts = [f'[{source.upper()}]']
    if rep_name:
        parts.append(f'Rep: {rep_name}')
    parts.append(f'VIN: {vin}')
    if data.get('year') and data.get('make') and data.get('model'):
        parts.append(f'{data["year"]} {data["make"]} {data["model"]}')
    if data.get('trim'):
        parts.append(data['trim'])
    if data.get('mileage'):
        try:
            parts.append(f'{int(data["mileage"]):,} mi')
        except (ValueError, TypeError):
            pass
    if data.get('asking_price'):
        try:
            parts.append(f'Asking: ${float(data["asking_price"]):,.0f}')
        except (ValueError, TypeError):
            pass
    raw_message = ' | '.join(parts)

    # Only store manually entered notes — no auto-generated enrichment
    notes_text = None
    if rep_name and data.get('notes'):
        notes_text = f'[Field: {rep_name}] {data["notes"]}'
    elif rep_name:
        notes_text = f'[Field: {rep_name}]'
    elif data.get('notes'):
        notes_text = data['notes']

    cur.execute("""
        INSERT INTO bids (contact_id, phone, vin, year, make, model, trim, mileage, color,
                          raw_message, asking_price, notes, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'new') RETURNING id
    """, (contact_id, rep_phone, vin,
          data.get('year'), data.get('make'), data.get('model'), data.get('trim'),
          data.get('mileage'), data.get('color'),
          raw_message, data.get('asking_price'), notes_text))
    bid_id = cur.fetchone()['id']

    # Direct API kick removed 2026-05-08 (see same removal at intake hooks).

    # Store listing photos (CDN URLs)
    for photo_url in (data.get('photos') or []):
        cur.execute("INSERT INTO bid_photos (bid_id, url) VALUES (%s, %s)",
                    (bid_id, photo_url))

    # Save additional photos (base64 uploads from user's machine)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    for photo in (data.get('additional_photos') or []):
        try:
            img_bytes = base64.b64decode(photo['data'])
            ext = os.path.splitext(photo.get('filename', '.jpg'))[1] or '.jpg'
            fname = f'{uuid.uuid4().hex}{ext}'
            fpath = os.path.join(UPLOAD_DIR, fname)
            with open(fpath, 'wb') as f:
                f.write(img_bytes)
            cur.execute("INSERT INTO bid_photos (bid_id, url) VALUES (%s, %s)",
                        (bid_id, f'/static/uploads/{fname}'))
        except Exception:
            pass  # skip bad photos

    # Always flag for fresh vAuto lookup
    cur.execute("UPDATE bids SET vauto_priority=TRUE WHERE id=%s", (bid_id,))

    db.commit()
    db.close()

    # Owner-portal push fan-out (best-effort, never blocks)
    _fire_owner_new_bid(bid_id)

    # Auto-search Autotrader, Cars.com, CarGurus for this VIN (same as field agent bids)
    trigger_market_check(bid_id, vin)

    return jsonify({'success': True, 'bid_id': bid_id, 'vin': vin})


# ---------------------------------------------------------------------------
# Quick Drop — Carfax screenshot intake
# ---------------------------------------------------------------------------
@app.route('/drop')
def quick_drop_page():
    return render_template('drop.html')


@app.route('/api/bid/quick-drop', methods=['POST'])
def api_bid_quick_drop():
    """Accept Carfax/AutoCheck screenshots OR a manual VIN (no photos required),
    extract vehicle info, create bid."""

    # Collect uploaded images (optional — VIN-only path is allowed)
    files_list = []
    saved_photos = []
    os.makedirs(UPLOAD_DIR, exist_ok=True)

    i = 0
    while True:
        f = request.files.get(f'photo_{i}')
        if not f:
            break
        file_bytes = f.read()
        media_type = f.content_type or 'image/jpeg'
        files_list.append((file_bytes, media_type))

        # Save to disk
        ext = os.path.splitext(f.filename)[1] or '.jpg'
        fname = f'{uuid.uuid4().hex}{ext}'
        fpath = os.path.join(UPLOAD_DIR, fname)
        with open(fpath, 'wb') as out:
            out.write(file_bytes)
        saved_photos.append(f'/static/uploads/{fname}')
        i += 1

    # Manual VIN + mileage from form — both optional, VIN is the VIN-only-path trigger
    manual_vin = (request.form.get('manual_vin') or '').strip().upper()
    manual_vin_valid = bool(manual_vin and len(manual_vin) == 17 and VIN_RE.match(manual_vin))

    manual_mileage_raw = (request.form.get('mileage') or '').strip()
    manual_mileage = None
    if manual_mileage_raw:
        try:
            manual_mileage = int(manual_mileage_raw.replace(',', '').replace(' ', '').replace('mi', ''))
            if manual_mileage < 0 or manual_mileage > 2_000_000:
                manual_mileage = None
        except (ValueError, TypeError):
            manual_mileage = None

    # Require at least one image OR a valid manual VIN
    if not files_list and not manual_vin_valid:
        return jsonify({'error': 'Need at least one image or a valid 17-character VIN'}), 400

    # Extract from images via Gemini (skip if VIN-only path)
    extracted = extract_carfax_multi(files_list) if files_list else {}

    vin = (extracted.get('vin') or '').strip().upper()

    # Manual VIN always wins if valid
    if manual_vin_valid:
        vin = manual_vin

    # Get precise VIN decode (VDS table → auto.dev → NHTSA cascade)
    nhtsa = {}
    _trim_confidence = 'low'
    if vin and len(vin) == 17 and VIN_RE.match(vin):
        nhtsa = decode_vin_precise_wrapper(vin) or {}
        _trim_confidence = nhtsa.get('trim_confidence', 'low')

    year = nhtsa.get('year') or extracted.get('year')
    make = nhtsa.get('make') or extracted.get('make')
    model = nhtsa.get('model') or extracted.get('model')
    trim = nhtsa.get('trim') or extracted.get('trim')
    # Manual mileage wins over Carfax extraction when both provided
    mileage = manual_mileage if manual_mileage is not None else extracted.get('mileage')
    color = extracted.get('color')

    # Form fields
    rep_name = (request.form.get('rep_name') or '').strip()
    notes = (request.form.get('notes') or '').strip()
    asking_price = request.form.get('asking_price')
    if asking_price:
        try:
            asking_price = float(asking_price.replace(',', '').replace('$', ''))
        except (ValueError, TypeError):
            asking_price = None

    # Build extra notes from Carfax extraction
    carfax_notes = []
    if extracted.get('title_status'):
        carfax_notes.append(f'Title: {extracted["title_status"]}')
    if extracted.get('accidents') is not None:
        carfax_notes.append(f'Accidents: {extracted["accidents"]}')
    if extracted.get('owners') is not None:
        carfax_notes.append(f'Owners: {extracted["owners"]}')

    full_notes_parts = []
    if rep_name:
        full_notes_parts.append(f'[Quick Drop: {rep_name}]')
    else:
        full_notes_parts.append('[Quick Drop]')
    if carfax_notes:
        full_notes_parts.append(' | '.join(carfax_notes))
    if notes:
        full_notes_parts.append(notes)
    full_notes = ' — '.join(full_notes_parts)

    # Create bid
    db = get_db()
    cur = db.cursor()

    if rep_name:
        rep_phone = f'field:{rep_name.replace(" ", "_").lower()}'
        contact_name = rep_name
    else:
        rep_phone = 'drop:dashboard'
        contact_name = 'Quick Drop'

    cur.execute("""
        INSERT INTO contacts (phone, name)
        VALUES (%s, %s)
        ON CONFLICT (phone) DO UPDATE SET name = EXCLUDED.name
        RETURNING id
    """, (rep_phone, contact_name))
    contact_id = cur.fetchone()['id']

    # Raw message summary
    parts = ['[QUICK DROP]']
    if vin:
        parts.append(f'VIN: {vin}')
    if year and make and model:
        parts.append(f'{year} {make} {model}')
    if trim:
        parts.append(trim)
    if mileage:
        parts.append(f'{mileage:,} mi')
    raw_message = ' | '.join(parts)

    # 2026-05-11: capture client IP + source for partner-push filtering.
    # Quick-drop bids from the operator's home IP (108.64.163.112) are test
    # bids and must NOT be pushed to subscribed partners (Nuccio etc.).
    _client_ip = (request.headers.get('X-Forwarded-For', '').split(',')[0].strip()
                  or request.remote_addr or '')

    cur.execute("""
        INSERT INTO bids (contact_id, phone, vin, year, make, model, trim, mileage, color,
                          raw_message, asking_price, notes, status, trim_confidence,
                          creation_ip, creation_source)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'new', %s,
                %s, 'quick_drop') RETURNING id
    """, (contact_id, rep_phone, vin if vin and len(vin) == 17 else None,
          year, make, model, trim, mileage, color,
          raw_message, asking_price, full_notes, _trim_confidence,
          _client_ip))
    bid_id = cur.fetchone()['id']

    # Direct API kick removed 2026-05-08 (see same removal at intake hooks).

    # Save photos
    for photo_url in saved_photos:
        cur.execute("INSERT INTO bid_photos (bid_id, url) VALUES (%s, %s)",
                    (bid_id, photo_url))

    # Flag for vAuto
    cur.execute("UPDATE bids SET vauto_priority=TRUE WHERE id=%s", (bid_id,))

    db.commit()
    db.close()

    # Owner-portal push fan-out (best-effort, never blocks)
    _fire_owner_new_bid(bid_id)

    # Trigger market check if we have a VIN
    if vin and len(vin) == 17:
        trigger_market_check(bid_id, vin)

    # Check VIN validity for UI warning
    vin_final = vin if vin and len(vin) == 17 else None
    vin_valid = vin_check_digit_valid(vin_final) if vin_final else None

    return jsonify({
        'success': True,
        'bid_id': bid_id,
        'extracted': {
            'vin': vin_final,
            'vin_check_digit_valid': vin_valid,
            'vin_warning': None if (vin_valid is None or vin_valid) else 'VIN check digit failed — please verify each character',
            'year': year,
            'make': make,
            'model': model,
            'trim': trim,
            'mileage': mileage,
            'color': color,
            'title_status': extracted.get('title_status'),
            'accidents': extracted.get('accidents'),
            'owners': extracted.get('owners'),
        }
    })


# ---------------------------------------------------------------------------
# Share — public vehicle page + clipboard copy
# ---------------------------------------------------------------------------
def _ensure_share_columns():
    """One-time migration: add share columns if missing."""
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute("ALTER TABLE bids ADD COLUMN IF NOT EXISTS share_token VARCHAR(36)")
        cur.execute("ALTER TABLE bids ADD COLUMN IF NOT EXISTS share_notes TEXT")
        cur.execute("ALTER TABLE bids ADD COLUMN IF NOT EXISTS share_asking NUMERIC(10,2)")
        # Driver mini-page columns: driver_token is the unguessable URL slug
        # for /m/<token>; driver_phone is the SMS sender we auto-reply to
        # when AI assessment finishes. Set together at SMS-intake time.
        cur.execute("ALTER TABLE bids ADD COLUMN IF NOT EXISTS driver_token VARCHAR(16)")
        cur.execute("ALTER TABLE bids ADD COLUMN IF NOT EXISTS driver_phone VARCHAR(20)")
        cur.execute("ALTER TABLE bids ADD COLUMN IF NOT EXISTS driver_notified_at TIMESTAMP")
        cur.execute("ALTER TABLE bids ADD COLUMN IF NOT EXISTS int_color VARCHAR(64)")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_bids_driver_token ON bids(driver_token) WHERE driver_token IS NOT NULL")
        db.commit()
        db.close()
    except Exception:
        pass

_ensure_share_columns()


@app.route('/reports')
def reports_page():
    return render_template('reports.html')


@app.route('/api/reports')
def api_reports():
    """Return bid stats for the selected period."""
    period = request.args.get('period', 'this_week')
    db = get_db()
    cur = db.cursor()

    # Build date range based on period
    if period == 'today':
        where = "created_at::date = CURRENT_DATE"
        group_by = "date_trunc('hour', created_at)"
        fmt = 'hour'
    elif period == 'yesterday':
        where = "created_at::date = CURRENT_DATE - 1"
        group_by = "date_trunc('hour', created_at)"
        fmt = 'hour'
    elif period == 'this_week':
        where = "created_at >= date_trunc('week', CURRENT_DATE)"
        group_by = "created_at::date"
        fmt = 'day'
    elif period == 'last_week':
        where = "created_at >= date_trunc('week', CURRENT_DATE) - INTERVAL '7 days' AND created_at < date_trunc('week', CURRENT_DATE)"
        group_by = "created_at::date"
        fmt = 'day'
    elif period == 'this_month':
        where = "created_at >= date_trunc('month', CURRENT_DATE)"
        group_by = "created_at::date"
        fmt = 'day'
    elif period == 'last_month':
        where = "created_at >= date_trunc('month', CURRENT_DATE) - INTERVAL '1 month' AND created_at < date_trunc('month', CURRENT_DATE)"
        group_by = "created_at::date"
        fmt = 'day'
    elif period == 'last_90':
        where = "created_at >= NOW() - INTERVAL '90 days'"
        group_by = "date_trunc('week', created_at)"
        fmt = 'week'
    elif period == 'this_year':
        where = "created_at >= date_trunc('year', CURRENT_DATE)"
        group_by = "date_trunc('month', created_at)"
        fmt = 'month'
    else:  # last_30
        where = "created_at >= NOW() - INTERVAL '30 days'"
        group_by = "created_at::date"
        fmt = 'day'

    cur.execute(f"""
        SELECT {group_by} as period, status, COUNT(*) as cnt
        FROM bids WHERE {where}
        GROUP BY period, status ORDER BY period
    """)
    rows = cur.fetchall()

    from collections import OrderedDict
    data = OrderedDict()
    for r in rows:
        if fmt == 'hour':
            d = r['period'].strftime('%H:%M') if hasattr(r['period'], 'strftime') else str(r['period'])
        elif fmt == 'month':
            d = r['period'].strftime('%Y-%m') if hasattr(r['period'], 'strftime') else str(r['period'])
        else:
            d = r['period'].strftime('%Y-%m-%d') if hasattr(r['period'], 'strftime') else str(r['period'])
        if d not in data:
            data[d] = {'new': 0, 'reviewing': 0, 'bid_sent': 0, 'bought': 0, 'passed': 0, 'total': 0}
        data[d][r['status']] = int(r['cnt'])
        data[d]['total'] += int(r['cnt'])

    cur.execute(f"SELECT status, COUNT(*) as cnt FROM bids WHERE {where} GROUP BY status")
    totals = {'new': 0, 'reviewing': 0, 'bid_sent': 0, 'bought': 0, 'passed': 0, 'total': 0}
    for r in cur.fetchall():
        totals[r['status']] = int(r['cnt'])
        totals['total'] += int(r['cnt'])

    if totals['total'] > 0:
        totals['buy_rate'] = round(totals['bought'] / totals['total'] * 100, 1)
        totals['pass_rate'] = round(totals['passed'] / totals['total'] * 100, 1)
    else:
        totals['buy_rate'] = 0
        totals['pass_rate'] = 0

    # Date range label
    cur.execute(f"SELECT MIN(created_at)::date as start_date, MAX(created_at)::date as end_date FROM bids WHERE {where}")
    range_row = cur.fetchone()
    range_label = ''
    if range_row and range_row['start_date']:
        s = range_row['start_date'].strftime('%b %d, %Y')
        e = range_row['end_date'].strftime('%b %d, %Y')
        range_label = f'{s} — {e}' if s != e else s

    cur.execute(f"""
        SELECT c.name, COUNT(*) as cnt,
               COUNT(*) FILTER (WHERE b.status='bought') as bought,
               COUNT(*) FILTER (WHERE b.status='passed') as passed,
               COUNT(*) FILTER (WHERE b.status='bid_sent') as sent
        FROM bids b LEFT JOIN contacts c ON b.contact_id = c.id
        WHERE {where.replace('created_at', 'b.created_at')} AND b.phone LIKE 'field:%%'
        GROUP BY c.name ORDER BY cnt DESC LIMIT 10
    """)
    reps = [dict(r) for r in cur.fetchall()]

    db.close()
    return jsonify({
        'labels': list(data.keys()),
        'series': data,
        'totals': totals,
        'reps': reps,
        'period': period,
        'fmt': fmt,
        'range_label': range_label,
    })


@app.route('/api/share-contacts', methods=['GET'])
def api_share_contacts_list():
    """Return saved share contacts."""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id, name, phone FROM share_contacts ORDER BY name")
    contacts = cur.fetchall()
    db.close()
    return jsonify([{'id': c['id'], 'name': c['name'], 'phone': c['phone']} for c in contacts])


@app.route('/api/share-contacts', methods=['POST'])
def api_share_contacts_save():
    """Save a new share contact."""
    data = request.json or {}
    name = (data.get('name') or '').strip()
    phone = (data.get('phone') or '').strip()
    if not name or not phone:
        return jsonify({'error': 'Name and phone required'}), 400
    digits = re.sub(r'\D', '', phone)
    if len(digits) == 10:
        digits = '1' + digits
    if len(digits) != 11 or not digits.startswith('1'):
        return jsonify({'error': 'Invalid phone number'}), 400
    formatted = f'+{digits}'
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        INSERT INTO share_contacts (name, phone)
        VALUES (%s, %s)
        ON CONFLICT (phone) DO UPDATE SET name = EXCLUDED.name
        RETURNING id
    """, (name, formatted))
    contact_id = cur.fetchone()['id']
    db.commit()
    db.close()
    return jsonify({'success': True, 'id': contact_id, 'name': name, 'phone': formatted})


@app.route('/api/share-contacts/<int:contact_id>', methods=['DELETE'])
def api_share_contacts_delete(contact_id):
    """Delete a share contact."""
    db = get_db()
    cur = db.cursor()
    cur.execute("DELETE FROM share_contacts WHERE id=%s", (contact_id,))
    db.commit()
    db.close()
    return jsonify({'success': True})


@app.route('/api/bid/<int:bid_id>/share-media', methods=['GET'])
def api_bid_share_media(bid_id):
    """Return available media (reports + photos) for the share modal picker."""
    base_url = 'https://experience-wholesale.net'
    db = get_db()
    cur = db.cursor()

    reports = []
    cur.execute("SELECT carfax_screenshot, autocheck_screenshot FROM vauto_lookups WHERE bid_id=%s", (bid_id,))
    vauto_row = cur.fetchone()
    if vauto_row:
        if vauto_row.get('carfax_screenshot'):
            url = vauto_row['carfax_screenshot']
            reports.append({'key': 'carfax', 'label': 'Carfax', 'url': url if url.startswith('http') else base_url + url})
        if vauto_row.get('autocheck_screenshot'):
            url = vauto_row['autocheck_screenshot']
            reports.append({'key': 'autocheck', 'label': 'AutoCheck', 'url': url if url.startswith('http') else base_url + url})

    try:
        cur.execute("SELECT screenshot FROM accutrade_lookups WHERE bid_id=%s", (bid_id,))
        acc_row = cur.fetchone()
        if acc_row and acc_row.get('screenshot'):
            url = acc_row['screenshot']
            reports.append({'key': 'accutrade', 'label': 'AccuTrade', 'url': url if url.startswith('http') else base_url + url})
    except Exception:
        pass

    try:
        cur.execute("SELECT screenshot FROM ipacket_lookups WHERE bid_id=%s", (bid_id,))
        ip_row = cur.fetchone()
        if ip_row and ip_row.get('screenshot'):
            url = ip_row['screenshot']
            reports.append({'key': 'ipacket', 'label': 'iPacket Sticker', 'url': url if url.startswith('http') else base_url + url})
    except Exception:
        pass

    photos = []
    cur.execute("SELECT id, url FROM bid_photos WHERE bid_id=%s ORDER BY id", (bid_id,))
    for row in cur.fetchall():
        url = row['url']
        thumb = base_url + '/thumb?url=' + url + '&size=mobile' if url.startswith('/static/uploads/') else base_url + '/thumb?url=' + url + '&size=mobile' if not url.startswith('http') else base_url + '/thumb?url=' + url + '&size=mobile'
        photos.append({'id': row['id'], 'url': url if url.startswith('http') else base_url + url, 'thumb': thumb})

    # share_cards_response_marker_20260514 — enhance share-media payload with
    # per-bid card selection state + what data is actually available so the
    # modal can show pre-checked enabled checkboxes (and grey out cards that
    # have no data for this VIN yet).
    cur.execute("SELECT share_cards FROM bids WHERE id=%s", (bid_id,))
    _sc_row = cur.fetchone()
    _share_cards = (_sc_row or {}).get('share_cards') or {}
    if isinstance(_share_cards, str):
        try:
            _share_cards = json.loads(_share_cards)
        except Exception:
            _share_cards = {}
    # Defaults for any missing key (older bids predating the column)
    for _k in ('carfax', 'autocheck', 'ipacket', 'manheim', 'rbook'):
        if _k not in _share_cards:
            _share_cards[_k] = True

    # cards_available: which cards have actual data for this VIN
    _cards_available = {
        'carfax':    bool(vauto_row and vauto_row.get('carfax_screenshot')),
        'autocheck': bool(vauto_row and vauto_row.get('autocheck_screenshot')),
        'ipacket':   False,
        'manheim':   False,
        'rbook':     False,
        'accutrade': False,
    }
    try:
        cur.execute("SELECT screenshot, not_available FROM ipacket_lookups WHERE bid_id=%s", (bid_id,))
        _ip_r = cur.fetchone()
        # Only mark iPacket available if screenshot exists AND not flagged not_available
        _cards_available['ipacket'] = bool(_ip_r and _ip_r.get('screenshot') and not _ip_r.get('not_available'))
    except Exception:
        pass
    try:
        cur.execute("""SELECT guaranteed_offer, trade_in, trade_market, retail,
                              market_avg, screenshot, not_available
                         FROM accutrade_lookups WHERE bid_id=%s""", (bid_id,))
        _at_r = cur.fetchone()
        _cards_available['accutrade'] = bool(_at_r and not _at_r.get('not_available') and (
            _at_r.get('guaranteed_offer') or _at_r.get('trade_in') or
            _at_r.get('trade_market') or _at_r.get('retail') or
            _at_r.get('market_avg') or _at_r.get('screenshot')
        ))
    except Exception:
        pass
    try:
        cur.execute("SELECT market_intel_cached, manheim_transactions FROM vauto_lookups WHERE bid_id=%s", (bid_id,))
        _va_r = cur.fetchone() or {}
        _mi_raw = _va_r.get('market_intel_cached')
        if _mi_raw:
            _mi = _mi_raw if isinstance(_mi_raw, dict) else (json.loads(_mi_raw) if isinstance(_mi_raw, str) else {})
            _cards_available['rbook'] = bool((_mi.get('rbook') or {}).get('retail_median'))
        _mh_raw = _va_r.get('manheim_transactions')
        if _mh_raw:
            _mh = _mh_raw if isinstance(_mh_raw, dict) else (json.loads(_mh_raw) if isinstance(_mh_raw, str) else {})
            _cards_available['manheim'] = bool(_mh.get('transactions'))
    except Exception:
        pass

    db.close()
    return jsonify({
        'reports': reports,
        'photos': photos,
        'share_cards': _share_cards,
        'cards_available': _cards_available,
    })


@app.route('/api/bid/<int:bid_id>/share', methods=['POST'])
def api_bid_share(bid_id):
    """Generate share token, save custom notes/asking, return clipboard text."""
    data = request.json or {}

    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT b.*, c.name as contact_name
        FROM bids b LEFT JOIN contacts c ON b.contact_id = c.id
        WHERE b.id = %s
    """, (bid_id,))
    bid = cur.fetchone()
    if not bid:
        db.close()
        return jsonify({'error': 'Not found'}), 404

    # Generate token if not exists
    token = bid.get('share_token')
    if not token:
        token = uuid.uuid4().hex[:12]

    # Save share-specific notes and asking price
    share_notes = (data.get('share_notes') or '').strip() or None
    share_asking = None
    if data.get('share_asking'):
        try:
            share_asking = float(str(data['share_asking']).replace(',', '').replace('$', ''))
        except (ValueError, TypeError):
            pass

    # share_cards: optional JSONB toggle map ({carfax,autocheck,ipacket,manheim,rbook}: bool)
    _sc_in = data.get('share_cards')
    _sc_to_save = None
    if isinstance(_sc_in, dict):
        # Whitelist keys + force booleans
        _allowed = {'carfax', 'autocheck', 'ipacket', 'manheim', 'rbook', 'accutrade'}
        _sc_to_save = {k: bool(_sc_in.get(k)) for k in _allowed if k in _sc_in}
    if _sc_to_save is not None:
        cur.execute("""
            UPDATE bids SET share_token=%s, share_notes=%s, share_asking=%s,
                            share_cards=%s::jsonb
             WHERE id=%s
        """, (token, share_notes, share_asking, json.dumps(_sc_to_save), bid_id))
    else:
        cur.execute("""
            UPDATE bids SET share_token=%s, share_notes=%s, share_asking=%s WHERE id=%s
        """, (token, share_notes, share_asking, bid_id))
    db.commit()
    db.close()

    share_url = f'https://experience-wholesale.net/share/{token}'

    # Build clipboard text — NO internal prices
    lines = []
    ymm = ''
    if bid.get('year') or bid.get('make'):
        ymm = f"{bid.get('year', '')} {bid.get('make', '')} {bid.get('model', '')}".strip()
        if bid.get('trim'):
            ymm += f" {bid['trim']}"
        lines.append(ymm)
    if bid.get('vin'):
        lines.append(f"VIN: {bid['vin']}")

    details = []
    if bid.get('mileage'):
        details.append(f"{bid['mileage']:,} mi")
    if bid.get('color'):
        details.append(bid['color'])
    if details:
        lines.append(' | '.join(details))

    if share_asking:
        lines.append(f"Asking: ${share_asking:,.0f}")

    if share_notes:
        lines.append(share_notes)

    lines.append('')
    lines.append(share_url)

    clipboard_text = '\n'.join(lines)

    result = {
        'success': True,
        'share_url': share_url,
        'clipboard_text': clipboard_text,
    }

    # Send MMS if phone number provided (via A2P messaging service)
    share_phone = (data.get('share_phone') or '').strip()
    if share_phone:
        digits = re.sub(r'\D', '', share_phone)
        if len(digits) == 10:
            digits = '1' + digits
        if len(digits) == 11 and digits.startswith('1'):
            to_phone = f'+{digits}'

            try:
                twilio_client = TwilioClient(TWILIO_SID, TWILIO_TOKEN)
                twilio_client.messages.create(
                    messaging_service_sid='MGf87b5c3822c46a1e0c28f14d2d6006ce',
                    to=to_phone,
                    body=clipboard_text
                )
                result['sms_sent'] = True
                # Track this share so inbound replies route back to this bid
                try:
                    sdb = get_db()
                    scur = sdb.cursor()
                    scur.execute("""
                        INSERT INTO share_sent (bid_id, phone) VALUES (%s, %s)
                        ON CONFLICT DO NOTHING
                    """, (bid_id, to_phone))
                    sdb.commit()
                    sdb.close()
                except Exception:
                    pass
            except Exception as e:
                print(f'Share SMS error: {e}')
                result['sms_sent'] = False
                result['sms_error'] = str(e)[:80]
        else:
            result['sms_sent'] = False
            result['sms_error'] = 'Invalid phone number'

    return jsonify(result)


@app.route('/api/bid-photo/<int:photo_id>/set-share', methods=['POST'])
def set_photo_share(photo_id):
    """Admin sets whether a specific bid_photo is included in the public
    share page. Body: {include: true|false}. Explicit-value API — safer
    than toggle because repeated clicks can't drift the UI and DB apart."""
    data = request.get_json(silent=True) or {}
    want = bool(data.get('include'))
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        UPDATE bid_photos SET include_in_share = %s
        WHERE id = %s
        RETURNING id, include_in_share
    """, (want, photo_id))
    row = cur.fetchone()
    db.commit()
    db.close()
    if not row:
        return jsonify({'error': 'not_found'}), 404
    return jsonify({'ok': True, 'id': row['id'], 'include_in_share': row['include_in_share']})


@app.route('/api/bid-photo/<int:photo_id>/toggle-share', methods=['POST'])
def toggle_photo_share(photo_id):
    """Legacy toggle endpoint — kept for any older UI code still calling it."""
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        UPDATE bid_photos SET include_in_share = NOT include_in_share
        WHERE id = %s RETURNING id, include_in_share
    """, (photo_id,))
    row = cur.fetchone()
    db.commit()
    db.close()
    if not row:
        return jsonify({'error': 'not_found'}), 404
    return jsonify({'ok': True, 'id': row['id'], 'include_in_share': row['include_in_share']})


@app.route('/api/bid/<int:bid_id>/dealer_gallery', methods=['GET'])
def api_bid_dealer_gallery(bid_id):
    """Surface VIN-matched photos from dealer_inventory so admin can pull
    them into the bid without re-uploading. Strictly read-only."""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT vin FROM bids WHERE id = %s", (bid_id,))
    row = cur.fetchone()
    if not row or not row.get("vin"):
        db.close()
        return jsonify({"groups": []})
    vin = row["vin"].strip()
    if len(vin) != 17:
        db.close()
        return jsonify({"groups": []})
    cur.execute("SELECT url FROM bid_photos WHERE bid_id = %s", (bid_id,))
    existing = {r["url"] for r in cur.fetchall()}
    _JUNK = ("logo", "badge", "icon", "placeholder", "/cms/", "/ui/",
             "facebook", "instagram", "twitter", "youtube")
    cur.execute("""
        SELECT di.id, di.dealer_id, d.name AS dealer_name, di.photos, di.url,
               di.last_seen_at, di.status
        FROM dealer_inventory di
        LEFT JOIN dealers d ON d.id = di.dealer_id
        WHERE di.vin = %s
        ORDER BY di.last_seen_at DESC NULLS LAST
    """, (vin,))
    dealer_rows = cur.fetchall()
    groups = []
    for r in dealer_rows:
        raw_photos = r.get("photos") or []
        if not isinstance(raw_photos, list):
            raw_photos = []
        clean = []
        for p in raw_photos:
            if not isinstance(p, str):
                continue
            u = p.strip().split(" ", 1)[0].replace("&amp;", "&")
            if not u.startswith("http"):
                continue
            if any(j in u.lower() for j in _JUNK):
                continue
            if u in existing:
                continue
            clean.append(u)
        if clean:
            groups.append({
                "dealer_inventory_id": r["id"],
                "dealer_name": r.get("dealer_name") or ("dealer_" + str(r.get("dealer_id"))),
                "listing_url": r.get("url"),
                "status": r.get("status"),
                "last_seen_at": r["last_seen_at"].isoformat() if r.get("last_seen_at") else None,
                "photos": clean,
            })
    db.close()
    return jsonify({"groups": groups})


@app.route('/api/bid/<int:bid_id>/add_dealer_photos', methods=['POST'])
def api_bid_add_dealer_photos(bid_id):
    """Copy selected URLs from dealer_inventory.photos into bid_photos.
    Idempotent: skips URLs already present on this bid."""
    data = request.get_json(silent=True) or {}
    urls = data.get("urls") or []
    if not isinstance(urls, list):
        return jsonify({"error": "urls must be a list"}), 400
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM bids WHERE id = %s", (bid_id,))
    if not cur.fetchone():
        db.close()
        return jsonify({"error": "bid not found"}), 404
    cur.execute("SELECT url FROM bid_photos WHERE bid_id = %s", (bid_id,))
    existing = {r["url"] for r in cur.fetchall()}
    _JUNK = ("logo", "badge", "icon", "placeholder", "/cms/", "/ui/",
             "facebook", "instagram", "twitter", "youtube")
    added = []
    for raw in urls:
        if not isinstance(raw, str):
            continue
        u = raw.strip().split(" ", 1)[0].replace("&amp;", "&")
        if not u.startswith("http"):
            continue
        if any(j in u.lower() for j in _JUNK):
            continue
        if u in existing:
            continue
        cur.execute("INSERT INTO bid_photos (bid_id, url) VALUES (%s, %s) RETURNING id",
                    (bid_id, u))
        new_id = cur.fetchone()["id"]
        added.append({"id": new_id, "url": u})
        existing.add(u)
    db.commit()
    db.close()
    return jsonify({"added": added, "count": len(added)})


@app.route('/api/bid/<int:bid_id>/upload_photos', methods=['POST'])
def api_bid_upload_photos(bid_id):
    """Drag-drop or file-input upload of photos directly into a bid.
    Accepts multipart with field 'photos' (multi). Saves to
    static/uploads/bid/<bid_id>/<uuid>.<ext>."""
    import os as _os, uuid as _uuid
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM bids WHERE id = %s", (bid_id,))
    if not cur.fetchone():
        db.close()
        return jsonify({"error": "bid not found"}), 404
    files = request.files.getlist("photos")
    if not files:
        single = request.files.get("photo")
        if single:
            files = [single]
    if not files:
        db.close()
        return jsonify({"error": "no files in upload"}), 400
    upload_dir = _os.path.join("/opt/expwholesale/static/uploads/bid", str(bid_id))
    _os.makedirs(upload_dir, exist_ok=True)
    _ALLOWED_EXT = {"jpg", "jpeg", "png", "gif", "webp", "heic", "heif"}
    _MAX_BYTES = 30 * 1024 * 1024
    added = []
    skipped = []
    for f in files:
        try:
            filename = (f.filename or "").lower()
            ext = filename.rsplit(".", 1)[-1] if "." in filename else ""
            if ext not in _ALLOWED_EXT:
                ct = (f.content_type or "").lower()
                if "png" in ct: ext = "png"
                elif "jpeg" in ct or "jpg" in ct: ext = "jpg"
                elif "gif" in ct: ext = "gif"
                elif "webp" in ct: ext = "webp"
                elif "heic" in ct or "heif" in ct: ext = "heic"
                else:
                    skipped.append({"name": filename or "(unnamed)", "reason": "unsupported type"})
                    continue
            f.stream.seek(0, 2)
            size = f.stream.tell()
            f.stream.seek(0)
            if size > _MAX_BYTES:
                skipped.append({"name": filename, "reason": "too large"})
                continue
            if size <= 0:
                skipped.append({"name": filename, "reason": "empty"})
                continue
            pid = _uuid.uuid4().hex[:16]
            target = _os.path.join(upload_dir, pid + "." + ext)
            f.save(target)
            url_path = "/static/uploads/bid/" + str(bid_id) + "/" + pid + "." + ext
            cur.execute("INSERT INTO bid_photos (bid_id, url, local_path) VALUES (%s, %s, %s) RETURNING id",
                        (bid_id, url_path, target))
            new_id = cur.fetchone()["id"]
            added.append({"id": new_id, "url": url_path})
        except Exception as e:
            skipped.append({"name": f.filename or "(unnamed)", "reason": "error: " + str(e)})
    db.commit()
    db.close()
    return jsonify({"added": added, "skipped": skipped, "count": len(added)})


@app.route('/share/<token>')
def share_page(token):
    """Public share page — no login required."""
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT b.*, c.name as contact_name
        FROM bids b LEFT JOIN contacts c ON b.contact_id = c.id
        WHERE b.share_token = %s
    """, (token,))
    bid = cur.fetchone()
    if not bid:
        db.close()
        return 'Not found', 404

    # Share page shows only photos the admin has marked for inclusion
    # (toggle lives on the admin bid-detail page). include_in_share
    # defaults to TRUE so existing bids aren't accidentally emptied.
    cur.execute("""
        SELECT id, url FROM bid_photos
        WHERE bid_id = %s AND include_in_share = TRUE
        ORDER BY id
    """, (bid['id'],))
    photos = cur.fetchall()

    cur.execute("SELECT * FROM vauto_lookups WHERE bid_id = %s", (bid['id'],))
    vauto = cur.fetchone()

    accutrade = None
    try:
        cur.execute("SELECT * FROM accutrade_lookups WHERE bid_id = %s", (bid['id'],))
        accutrade = cur.fetchone()
    except Exception:
        pass

    ipacket = None
    try:
        cur.execute("SELECT * FROM ipacket_lookups WHERE bid_id = %s", (bid['id'],))
        ipacket = cur.fetchone()
    except Exception:
        pass

    db.close()

    # ── mh + rb prep (mirrors driver_full_page logic 2026-05-14) ──
    def _j_share(v):
        if v is None: return None
        if isinstance(v, str):
            try: return json.loads(v)
            except Exception: return None
        return v

    mh = None
    if vauto and vauto.get('manheim_transactions'):
        _mh_data = _j_share(vauto['manheim_transactions']) or {}
        _txns = _mh_data.get('transactions') or []
        if _txns:
            _prices = sorted(int(t.get('sale_price') or 0) for t in _txns if t.get('sale_price'))
            mh = {
                'count': len(_txns),
                'median': _prices[len(_prices)//2] if _prices else None,
                'lo': _prices[0] if _prices else None,
                'hi': _prices[-1] if _prices else None,
            }
            if bid.get('mileage'):
                try:
                    _bm = int(bid['mileage'])
                    _twm = [t for t in _txns if t.get('odometer')]
                    if _twm:
                        _closest = min(_twm, key=lambda t: abs(int(t.get('odometer') or 0) - _bm))
                        mh['closest'] = {
                            'mileage': _closest.get('odometer'),
                            'price': _closest.get('sale_price'),
                            'date': _closest.get('date_sold') or _closest.get('sold_at'),
                            'condition': _closest.get('condition'),
                            'region': _closest.get('region') or _closest.get('auction_region'),
                        }
                except Exception:
                    pass

    rb = None
    if vauto and vauto.get('market_intel_cached'):
        _cached = _j_share(vauto['market_intel_cached']) or {}
        if _cached.get('rbook'):
            rb = dict(_cached['rbook'])
            rb['count'] = rb.get('n_rows') or len(rb.get('all_rows') or []) or rb.get('n_visible')
            rb['median'] = rb.get('retail_median')
            rb['lo'] = rb.get('retail_min')
            rb['hi'] = rb.get('retail_max')
            rb['median_dol'] = rb.get('median_days_on_lot')

    # Share-card toggle state (defaults all-on for older bids w/o the column)
    _sc = bid.get('share_cards') or {}
    if isinstance(_sc, str):
        try: _sc = json.loads(_sc)
        except Exception: _sc = {}
    # accutrade_full_marker_20260514 — add accutrade to share_cards picker
    share_cards = {
        'carfax':    _sc.get('carfax',    True),
        'autocheck': _sc.get('autocheck', True),
        'ipacket':   _sc.get('ipacket',   True),
        'manheim':   _sc.get('manheim',   True),
        'rbook':     _sc.get('rbook',     True),
        'accutrade': _sc.get('accutrade', True),
    }

    return render_template('share.html', bid=bid, photos=photos, vauto=vauto,
                           accutrade=accutrade, ipacket=ipacket,
                           mh=mh, rb=rb, share_cards=share_cards)


# ---------------------------------------------------------------------------
# /m/<token> — driver mini-page (owner-view, prices visible, action buttons)
# Triggered by inbound SMS forward → auto-replied link when AI assess finishes.
# Token is unguessable; no login. Reply endpoint at /m/<token>/reply.
# ---------------------------------------------------------------------------
@app.route('/m/<token>')
def driver_mini_page(token):
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT b.*, c.name as contact_name
        FROM bids b LEFT JOIN contacts c ON b.contact_id = c.id
        WHERE b.driver_token = %s
    """, (token,))
    bid = cur.fetchone()
    if not bid:
        db.close()
        return 'Not found', 404

    cur.execute("""
        SELECT id, url FROM bid_photos
        WHERE bid_id = %s ORDER BY id LIMIT 12
    """, (bid['id'],))
    photos = cur.fetchall()

    cur.execute("SELECT * FROM vauto_lookups WHERE bid_id = %s", (bid['id'],))
    vauto = cur.fetchone()

    accutrade = None
    try:
        cur.execute("SELECT * FROM accutrade_lookups WHERE bid_id = %s", (bid['id'],))
        accutrade = cur.fetchone()
    except Exception:
        pass

    ipacket = None
    try:
        cur.execute("SELECT * FROM ipacket_lookups WHERE bid_id = %s", (bid['id'],))
        ipacket = cur.fetchone()
    except Exception:
        pass

    # 2026-05-09: Fallback to a better iPacket for same VIN. Prefer rows with
    # parsed data; else pick the LARGEST screenshot file (PDF stickers often
    # render visually but yield no selectable text → parsed fields stay NULL,
    # but the screenshot itself is still useful). Threshold: <80KB = blank
    # submit-form capture; >100KB likely a real sticker.
    if ipacket and bid.get('vin'):
        _cur_path = (ipacket.get('screenshot') or '').lstrip('/')
        _cur_size = 0
        try:
            if _cur_path:
                _abs = os.path.join('/opt/expwholesale', _cur_path)
                _cur_size = os.path.getsize(_abs) if os.path.exists(_abs) else 0
        except Exception: _cur_size = 0
        _cur_has_data = bool(ipacket.get('total_msrp') or ipacket.get('base_price') or ipacket.get('exterior_color'))
        # Trigger fallback if: current row has no parsed data AND screenshot is small/missing
        if not _cur_has_data and _cur_size < 80_000:
            try:
                cur.execute("""SELECT * FROM ipacket_lookups
                                WHERE vin=%s AND bid_id != %s
                                ORDER BY looked_up_at DESC LIMIT 10""",
                            (bid['vin'], bid['id']))
                candidates = cur.fetchall()
                best = None; best_score = 0
                for c in candidates:
                    cp = (c.get('screenshot') or '').lstrip('/')
                    cs = 0
                    try:
                        if cp:
                            ap = os.path.join('/opt/expwholesale', cp)
                            cs = os.path.getsize(ap) if os.path.exists(ap) else 0
                    except Exception: pass
                    has_data = bool(c.get('total_msrp') or c.get('base_price') or c.get('exterior_color'))
                    # Score: parsed data dominates; else screenshot size
                    score = (1_000_000 if has_data else 0) + cs
                    if score > best_score:
                        best_score = score; best = c
                if best and best_score > _cur_size:
                    print(f'[m-page] bid={bid["id"]} vin={bid["vin"]} fell back to ipacket bid_id={best["bid_id"]} (score {best_score} vs {_cur_size})', flush=True)
                    ipacket = best
            except Exception as _e:
                print(f'[m-page] ipacket fallback error bid={bid["id"]}: {_e}', flush=True)

    db.close()
    return render_template('m.html', bid=bid, photos=photos,
                           vauto=vauto, accutrade=accutrade, ipacket=ipacket,
                           token=token)


@app.route('/m/<token>/full')
def driver_full_page(token):
    """Phase 2 detailed bid view — collapsible cards with manheim,
    rbook closest_3, comp_msrps, LSL deals, dealer match, AI reasoning,
    book values. Sent via second SMS once _run_assessment completes.
    """
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT b.*, c.name as contact_name
        FROM bids b LEFT JOIN contacts c ON b.contact_id = c.id
        WHERE b.driver_token = %s
    """, (token,))
    bid = cur.fetchone()
    if not bid:
        db.close()
        return 'Not found', 404

    cur.execute("SELECT * FROM vauto_lookups WHERE bid_id = %s", (bid['id'],))
    vauto = cur.fetchone()
    cur.execute("SELECT * FROM accutrade_lookups WHERE bid_id = %s", (bid['id'],))
    accutrade = cur.fetchone()
    cur.execute("SELECT * FROM ipacket_lookups WHERE bid_id = %s", (bid['id'],))
    ipacket = cur.fetchone()
    # Same same-VIN fallback as /m/<token>: file-size-aware
    if ipacket and bid.get('vin'):
        _cur_path = (ipacket.get('screenshot') or '').lstrip('/')
        _cur_size = 0
        try:
            if _cur_path:
                _abs = os.path.join('/opt/expwholesale', _cur_path)
                _cur_size = os.path.getsize(_abs) if os.path.exists(_abs) else 0
        except Exception: _cur_size = 0
        _cur_has_data = bool(ipacket.get('total_msrp') or ipacket.get('base_price') or ipacket.get('exterior_color'))
        if not _cur_has_data and _cur_size < 80_000:
            try:
                cur.execute("""SELECT * FROM ipacket_lookups
                                WHERE vin=%s AND bid_id != %s
                                ORDER BY looked_up_at DESC LIMIT 10""",
                            (bid['vin'], bid['id']))
                candidates = cur.fetchall()
                best = None; best_score = 0
                for c in candidates:
                    cp = (c.get('screenshot') or '').lstrip('/')
                    cs = 0
                    try:
                        if cp:
                            ap = os.path.join('/opt/expwholesale', cp)
                            cs = os.path.getsize(ap) if os.path.exists(ap) else 0
                    except Exception: pass
                    has_data = bool(c.get('total_msrp') or c.get('base_price') or c.get('exterior_color'))
                    score = (1_000_000 if has_data else 0) + cs
                    if score > best_score:
                        best_score = score; best = c
                if best and best_score > _cur_size:
                    print(f'[m-full] bid={bid["id"]} vin={bid["vin"]} fell back to ipacket bid_id={best["bid_id"]} (score {best_score} vs {_cur_size})', flush=True)
                    ipacket = best
            except Exception as _e:
                print(f'[m-full] ipacket fallback error bid={bid["id"]}: {_e}', flush=True)

    cur.execute("""
        SELECT confidence_low, confidence_high, llm_reasoning, flags_v2,
               buyer_intel, dealer_intel, market_intel
          FROM ai_assessment_log
         WHERE bid_id = %s
         ORDER BY created_at DESC LIMIT 1
    """, (bid['id'],))
    ass = cur.fetchone() or {}

    # Helper: parse JSONB into dict (psycopg2 may return either str or dict)
    def _j(v):
        if v is None:
            return None
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                return None
        return v

    buyer = _j(ass.get('buyer_intel') if ass else None)
    dealer = _j(ass.get('dealer_intel') if ass else None)
    market_intel = _j(ass.get('market_intel') if ass else None) or {}
    flags_v2 = _j(ass.get('flags_v2') if ass else None) or []

    # ── Build manheim block from vauto_lookups.manheim_transactions
    mh = None
    if vauto and vauto.get('manheim_transactions'):
        mh_data = _j(vauto['manheim_transactions']) or {}
        txns = mh_data.get('transactions') or []
        if txns:
            prices = sorted(int(t.get('sale_price') or 0) for t in txns if t.get('sale_price'))
            mh = {
                'count': len(txns),
                'median': prices[len(prices)//2] if prices else None,
                'lo': prices[0] if prices else None,
                'hi': prices[-1] if prices else None,
            }
            # Closest-mile sale
            if bid.get('mileage'):
                bid_miles = int(bid['mileage'])
                txns_with_miles = [t for t in txns if t.get('odometer')]
                if txns_with_miles:
                    closest = min(txns_with_miles,
                                  key=lambda t: abs(int(t.get('odometer') or 0) - bid_miles))
                    mh['closest'] = {
                        'mileage': closest.get('odometer'),
                        'price': closest.get('sale_price'),
                        'date': closest.get('date_sold') or closest.get('sold_at'),
                        'condition': closest.get('condition'),
                        'region': closest.get('region') or closest.get('auction_region'),
                    }

    # ── Build rbook block. Prefer market_intel_cached.rbook (already
    #    computed) else fall back to live computation from rbook_competitive_set.
    rb = None
    if vauto and vauto.get('market_intel_cached'):
        cached = _j(vauto['market_intel_cached']) or {}
        if cached.get('rbook'):
            rb = dict(cached['rbook'])
            # rename fields for template clarity
            rb['count'] = rb.get('n_rows') or len(rb.get('all_rows') or [])
            rb['median'] = rb.get('retail_median')
            rb['p25'] = rb.get('retail_p25')
            rb['p75'] = rb.get('retail_p75')
            rb['median_dol'] = rb.get('median_days_on_lot')
            rb['implied_gross'] = cached.get('implied_buyer_gross')
            # Pass-through extras: full retail range + avg mileage + stocking
            rb['min'] = rb.get('retail_min')
            rb['max'] = rb.get('retail_max')
            # avg_mileage and stocking_report already in dict from dict(cached.rbook)

    # Always compute closest_3 from rbook_competitive_set.rows if rb is set
    # but lacks closest_3 (market_intel_cached.rbook doesn't carry closest_3).
    if rb and not rb.get('closest_3') and bid.get('mileage') and vauto and vauto.get('rbook_competitive_set'):
        rcs = _j(vauto['rbook_competitive_set']) or {}
        rows = rcs.get('rows') or []
        # Strict VIN-prefix filter: keep only true trim peers (drops
        # Carrera-vs-GTS / Tahoe-vs-Suburban bleed).
        rows_strict, _drop, _src = filter_rbook_to_strict_peers(bid.get('vin'), rows)
        if _drop:
            print(f'[m_full] rbook strict-filter bid={bid["id"]} '
                  f'closest_3: dropped {_drop} of {len(rows)} ({_src})', flush=True)
        rows = rows_strict
        if rows:
            bid_miles = int(bid['mileage'])
            by_dist = sorted(rows,
                             key=lambda r: abs(int(r.get('mileage') or 0) - bid_miles))
            rb['closest_3'] = by_dist[:3]
            rb['_strict_filter'] = {'dropped': _drop, 'kept': len(rows), 'source': _src}

    if not rb and vauto and vauto.get('rbook_competitive_set'):
        rcs = _j(vauto['rbook_competitive_set']) or {}
        rows = rcs.get('rows') or []
        rows_strict, _drop, _src = filter_rbook_to_strict_peers(bid.get('vin'), rows)
        if _drop:
            print(f'[m_full] rbook strict-filter bid={bid["id"]} '
                  f'full: dropped {_drop} of {len(rows)} ({_src})', flush=True)
        rows = rows_strict
        if rows:
            asks = sorted(int(r.get('price') or 0) for r in rows if r.get('price'))
            rb = {
                'count': len(rows),
                'median': asks[len(asks)//2] if asks else None,
                'p25': asks[len(asks)//4] if len(asks) >= 4 else None,
                'p75': asks[(3*len(asks))//4] if len(asks) >= 4 else None,
                '_strict_filter': {'dropped': _drop, 'kept': len(rows), 'source': _src},
            }
            if bid.get('mileage'):
                bid_miles = int(bid['mileage'])
                by_dist = sorted(rows,
                                 key=lambda r: abs(int(r.get('mileage') or 0) - bid_miles))
                rb['closest_3'] = by_dist[:3]

    # Merge comp_msrps onto closest_3 if present
    if rb and rb.get('closest_3'):
        vins = [c.get('vin') for c in rb['closest_3'] if c.get('vin')]
        if vins:
            cur.execute(
                "SELECT vin, msrp, base_price, status FROM comp_msrps WHERE vin = ANY(%s)",
                (vins,),
            )
            cmap = {r['vin']: {'msrp': r['msrp'], 'base_price': r['base_price'],
                               'status': r['status']} for r in cur.fetchall()}
            for c in rb['closest_3']:
                v = c.get('vin')
                if v and v in cmap:
                    c['msrp_lookup'] = cmap[v]

    db.close()

    return render_template(
        'm_full.html',
        bid=bid, vauto=vauto, accutrade=accutrade, ipacket=ipacket,
        confidence_low=ass.get('confidence_low') if ass else None,
        confidence_high=ass.get('confidence_high') if ass else None,
        reasoning=(ass.get('llm_reasoning') if ass else None) or bid.get('ai_assessment'),
        flags=flags_v2,
        manheim=mh, rbook=rb,
        buyer=buyer, dealer=dealer,
        token=token,
    )


def _notify_driver_phase2(bid_id):
    """Send the second SMS with link to /m/<token>/full once Phase 2 is done.
    Idempotent via bids.phase2_notified_at — runs at most once per bid.
    Called from _run_assessment success path (which gates on rbook + manheim
    completion). Returns True if SMS sent, False otherwise.
    """
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute("""
            SELECT id, driver_token, driver_phone, phase2_notified_at,
                   year, make, model, ai_price
              FROM bids WHERE id = %s
        """, (bid_id,))
        bid = cur.fetchone()
        if not bid or not bid['driver_phone'] or not bid['driver_token']:
            db.close()
            return False
        if bid['phase2_notified_at'] is not None:
            db.close()
            return False  # already sent

        # Phase 2 phone whitelist gate. PHASE2_PHONE_GATE accepts a comma- or
        # whitespace-separated list of phones; only sellers whose driver_phone
        # normalizes to one of those gets Phase 2 SMS. Empty = no gating
        # (every bid gets phase 2). Single value still works (back-compat).
        # Digit-equivalence: +14074309675 == 4074309675 == 14074309675.
        gate = (os.environ.get('PHASE2_PHONE_GATE') or '').strip()
        if gate:
            def _digits(p):
                d = ''.join(c for c in (p or '') if c.isdigit())
                if len(d) == 11 and d[0] == '1':
                    d = d[1:]
                return d
            allowed = {_digits(tok) for tok in gate.replace(',', ' ').split()
                       if len(_digits(tok)) == 10}
            if _digits(bid['driver_phone']) not in allowed:
                print(f'[phase2-notify] gated — bid={bid_id} '
                      f'driver={bid["driver_phone"]} not in allowlist '
                      f'(size={len(allowed)})', flush=True)
                db.close()
                return False

        ymm_parts = [str(bid['year']) if bid['year'] else '',
                     bid['make'] or '', bid['model'] or '']
        ymm = ' '.join(p for p in ymm_parts if p).strip() or 'Vehicle'
        base = os.environ.get('PUBLIC_BASE_URL', 'https://experience-wholesale.net')
        link = f"{base}/m/{bid['driver_token']}/full"
        price_str = f" ${'{:,}'.format(int(bid['ai_price']))}" if bid['ai_price'] else ''
        body = f"Bid #{bid['id']} {ymm}{price_str}\nFull report:\n{link}"

        sent = send_sms(bid['driver_phone'], body)
        if sent:
            cur.execute(
                "UPDATE bids SET phase2_notified_at = NOW() WHERE id = %s",
                (bid_id,),
            )
            db.commit()
            print(f'[phase2-notify] bid={bid_id} → {bid["driver_phone"]}', flush=True)
        else:
            print(f'[phase2-notify] SMS failed bid={bid_id}', flush=True)
        db.close()
        return sent
    except Exception as e:
        print(f'[phase2-notify] error bid={bid_id}: {e}', flush=True)
        return False


@app.route('/m/<token>/reply', methods=['POST'])
def driver_mini_reply(token):
    """Token-authenticated bid action. Mirrors /api/bid/<id>/reply but scoped
    by driver_token so the partner doesn't need a login."""
    data = request.json or {}
    action = data.get('action', 'bid')   # 'bid' | 'pass' | 'counter'
    bid_amount = data.get('bid_amount')
    message = (data.get('message') or '').strip()

    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id, phone, year, make, model FROM bids WHERE driver_token=%s", (token,))
    bid = cur.fetchone()
    if not bid:
        db.close()
        return jsonify({'error': 'Not found'}), 404

    bid_id = bid['id']
    if action == 'pass':
        new_status = 'passed'
        if not message:
            message = 'Pass'
    elif action == 'counter':
        new_status = 'reviewing'
        if not message and bid_amount:
            message = f'Counter: ${int(float(bid_amount)):,}'
    else:
        new_status = 'bid_sent'
        if not message and bid_amount:
            message = f'Bid: ${int(float(bid_amount)):,}'

    if not message:
        db.close()
        return jsonify({'error': 'Message or bid amount required'}), 400

    cur.execute("""
        UPDATE bids SET status=%s, bid_amount=%s, bid_response=%s,
        bid_sent_at=NOW(), updated_at=NOW(), has_unread=TRUE WHERE id=%s
    """, (new_status, bid_amount, message, bid_id))

    cur.execute("""
        INSERT INTO bid_messages (bid_id, direction, message, from_phone)
        VALUES (%s, 'inbound', %s, %s)
    """, (bid_id, f'[driver] {message}', bid['phone']))
    db.commit()
    db.close()

    return jsonify({'success': True, 'status': new_status})




# ─── Proxmox VM Management (added 2026-05-01) ───────────────────────────────
# Internal admin dashboard for managing the Proxmox host that runs Playwright
# bid-worker VMs. Routes:
#   GET  /admin/vms                              — page
#   GET  /api/proxmox/vms                        — list VMs
#   GET  /api/proxmox/host                       — host stats
#   POST /api/proxmox/vm/<vmid>/start            — start
#   POST /api/proxmox/vm/<vmid>/stop             — graceful shutdown
#   POST /api/proxmox/vm/<vmid>/forcestop        — force stop
#   POST /api/proxmox/vm/<vmid>/reboot           — graceful reboot
#   POST /api/proxmox/vm/<vmid>/reset            — force reset
#   GET  /api/proxmox/vm/<vmid>/snapshots        — list snapshots
#   POST /api/proxmox/vm/<vmid>/snapshot         — create snapshot
#   POST /api/proxmox/vm/<vmid>/snapshot/<name>/restore — rollback
#   DELETE /api/proxmox/vm/<vmid>/snapshot/<name>       — delete snapshot
#
# Connectivity: Contabo 1 cannot reach Oscar's home LAN (192.168.1.209)
# directly. PROXMOX_API_BASE must point at a Cloudflare-tunnel hostname that
# forwards to the Proxmox API (e.g. https://pve.experience-wholesale.net:8006).
# If unreachable, endpoints return HTTP 502 with a clear "cannot reach
# Proxmox" message and the UI shows a dedicated error state.

PROXMOX_API_BASE = os.environ.get('PROXMOX_API_BASE', '').rstrip('/')
PROXMOX_API_TOKEN = os.environ.get('PROXMOX_API_TOKEN', '')
PROXMOX_NODE = os.environ.get('PROXMOX_NODE', 'pve')
# Per-vmid node lookup (cluster has multiple nodes; vmids 110-114 on pve115, etc.)
_NODE_BY_VMID = {}
_NODE_BY_VMID_TS = 0

def _node_for_vmid(vmid):
    """Return the cluster node hosting this vmid. 30s cache. Falls back to PROXMOX_NODE."""
    import time as _t
    global _NODE_BY_VMID_TS
    now = _t.time()
    if now - _NODE_BY_VMID_TS > 30 or int(vmid) not in _NODE_BY_VMID:
        try:
            data, err = _proxmox_get_fast('/cluster/resources?type=vm')
            if not err and data:
                _NODE_BY_VMID.clear()
                for v in (data.get('data') or []):
                    if v.get('vmid') is not None:
                        _NODE_BY_VMID[int(v['vmid'])] = v.get('node') or PROXMOX_NODE
                _NODE_BY_VMID_TS = now
        except Exception: pass
    return _NODE_BY_VMID.get(int(vmid), PROXMOX_NODE)

PROXMOX_TIMEOUT = int(os.environ.get('PROXMOX_TIMEOUT', '8'))


def _proxmox_log(action, vmid=None, snapshot=None, success=False, response_excerpt=None):
    """Audit-log a Proxmox action to proxmox_action_log."""
    try:
        username = session.get('username') or 'admin'
    except Exception:
        username = 'system'
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute("""
            INSERT INTO proxmox_action_log
                (action, vmid, snapshot, username, success, response_excerpt)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (action, vmid, snapshot, username, bool(success),
              (response_excerpt or '')[:1000]))
        db.commit()
        db.close()
    except Exception as e:
        try:
            print(f'[proxmox_log] failed: {e}')
        except Exception:
            pass


def _proxmox_request(method, path, **kwargs):
    """Wrapper around requests.* that injects the Proxmox API token, base URL,
    and standard timeout/verify settings. Returns (status_code, json_or_text,
    error_string_or_None)."""
    if not PROXMOX_API_BASE or not PROXMOX_API_TOKEN:
        return 0, None, 'Proxmox API not configured (PROXMOX_API_BASE / PROXMOX_API_TOKEN missing)'
    url = f'{PROXMOX_API_BASE}/api2/json{path}'
    headers = kwargs.pop('headers', {}) or {}
    headers['Authorization'] = f'PVEAPIToken={PROXMOX_API_TOKEN}'
    try:
        resp = requests.request(
            method, url,
            headers=headers,
            verify=False,
            timeout=PROXMOX_TIMEOUT,
            **kwargs
        )
    except requests.exceptions.SSLError as e:
        return 0, None, f'SSL error: {e}'
    except requests.exceptions.ConnectTimeout:
        return 0, None, 'Cannot reach Proxmox (connection timeout)'
    except requests.exceptions.ConnectionError as e:
        return 0, None, f'Cannot reach Proxmox (connection error): {str(e)[:200]}'
    except requests.exceptions.Timeout:
        return 0, None, 'Proxmox request timeout'
    except Exception as e:
        return 0, None, f'Request failed: {str(e)[:200]}'
    body = None
    try:
        body = resp.json()
    except Exception:
        body = resp.text
    if resp.status_code >= 400:
        excerpt = (resp.text or '')[:300]
        return resp.status_code, body, f'HTTP {resp.status_code}: {excerpt}'
    return resp.status_code, body, None


def _proxmox_unreachable_response(err):
    """Standard JSON error envelope for Proxmox connectivity / config issues.
    Returns HTTP 200 (not 502) so Cloudflare doesn't replace the body with its
    own error page — the UI distinguishes failures via the `ok:false` field."""
    return jsonify({
        'ok': False,
        'reachable': False,
        'error': err or 'Cannot reach Proxmox',
        'hint': ('Set up a Cloudflare tunnel hostname (e.g. '
                 'pve.experience-wholesale.net -> https://192.168.1.209:8006) '
                 'and set PROXMOX_API_BASE / PROXMOX_API_TOKEN env vars on '
                 'the EW service.'),
    }), 200


@app.route('/admin/vms')
def admin_vms_page():
    """Render the Proxmox VM management dashboard."""
    return render_template(
        'admin_vms.html',
        proxmox_configured=bool(PROXMOX_API_BASE and PROXMOX_API_TOKEN),
        proxmox_base=PROXMOX_API_BASE or '(not configured)',
        proxmox_node=PROXMOX_NODE,
    )


@app.route('/api/proxmox/vms', methods=['GET'])
def api_proxmox_vms():
    """List VMs across the cluster (currently single-node)."""
    status, data, err = _proxmox_request('GET', '/cluster/resources?type=vm')
    if err:
        return _proxmox_unreachable_response(err)
    vms = []
    for v in (data or {}).get('data', []) or []:
        if v.get('type') != 'qemu':
            continue
        maxmem = v.get('maxmem') or 0
        mem = v.get('mem') or 0
        maxdisk = v.get('maxdisk') or 0
        disk = v.get('disk') or 0
        cpu = v.get('cpu') or 0
        vms.append({
            'vmid': v.get('vmid'),
            'name': v.get('name') or f"vm-{v.get('vmid')}",
            'status': v.get('status') or 'unknown',
            'node': v.get('node') or PROXMOX_NODE,
            'cpu_pct': round(float(cpu) * 100.0, 2),
            'maxcpu': v.get('maxcpu') or 0,
            'mem_bytes': mem,
            'maxmem_bytes': maxmem,
            'mem_pct': round((mem / maxmem) * 100.0, 1) if maxmem else 0.0,
            'disk_bytes': disk,
            'maxdisk_bytes': maxdisk,
            'uptime_s': v.get('uptime') or 0,
            'template': bool(v.get('template')),
        })
    vms.sort(key=lambda x: x['vmid'])
    return jsonify({'ok': True, 'reachable': True, 'vms': vms})


@app.route('/api/proxmox/host', methods=['GET'])
def api_proxmox_host():
    """Host (node) stats."""
    status, data, err = _proxmox_request('GET', f'/nodes/{PROXMOX_NODE}/status')
    if err:
        return _proxmox_unreachable_response(err)
    s, vms_data, vms_err = _proxmox_request('GET', '/cluster/resources?type=vm')
    total = running = stopped = 0
    if not vms_err:
        for v in (vms_data or {}).get('data', []) or []:
            if v.get('type') != 'qemu':
                continue
            total += 1
            if v.get('status') == 'running':
                running += 1
            else:
                stopped += 1
    d = (data or {}).get('data', {}) or {}
    cpu = float(d.get('cpu') or 0) * 100.0
    cpuinfo = d.get('cpuinfo', {}) or {}
    mem = d.get('memory', {}) or {}
    rootfs = d.get('rootfs', {}) or {}
    loadavg = d.get('loadavg', [0, 0, 0]) or [0, 0, 0]
    return jsonify({
        'ok': True,
        'reachable': True,
        'node': PROXMOX_NODE,
        'cpu_pct': round(cpu, 2),
        'cpu_cores': cpuinfo.get('cpus') or cpuinfo.get('cores') or 0,
        'cpu_model': cpuinfo.get('model') or '',
        'mem_total': mem.get('total') or 0,
        'mem_used': mem.get('used') or 0,
        'mem_pct': round((mem.get('used', 0) / mem.get('total', 1)) * 100.0, 1) if mem.get('total') else 0,
        'disk_total': rootfs.get('total') or 0,
        'disk_used': rootfs.get('used') or 0,
        'disk_pct': round((rootfs.get('used', 0) / rootfs.get('total', 1)) * 100.0, 1) if rootfs.get('total') else 0,
        'loadavg': loadavg,
        'uptime_s': d.get('uptime') or 0,
        'pve_version': d.get('pveversion') or '',
        'vm_total': total,
        'vm_running': running,
        'vm_stopped': stopped,
    })


def _proxmox_vm_action(vmid, action_path, action_label):
    """Helper: POST /nodes/<node>/qemu/<vmid>/status/<action_path> and audit-log."""
    status, data, err = _proxmox_request(
        'POST',
        f'/nodes/{_node_for_vmid(vmid)}/qemu/{vmid}/status/{action_path}'
    )
    success = (err is None)
    excerpt = err if err else (str(data)[:300] if data else 'ok')
    _proxmox_log(action_label, vmid=vmid, success=success, response_excerpt=excerpt)
    if err:
        return jsonify({'ok': False, 'error': err}), 200
    upid = (data or {}).get('data') if isinstance(data, dict) else None
    return jsonify({'ok': True, 'upid': upid, 'action': action_label})


@app.route('/api/proxmox/vm/<int:vmid>/start', methods=['POST'])
def api_proxmox_vm_start(vmid):
    return _proxmox_vm_action(vmid, 'start', 'start')


@app.route('/api/proxmox/vm/<int:vmid>/stop', methods=['POST'])
def api_proxmox_vm_stop(vmid):
    # 'shutdown' in Proxmox = graceful via guest agent / ACPI
    return _proxmox_vm_action(vmid, 'shutdown', 'shutdown')


@app.route('/api/proxmox/vm/<int:vmid>/forcestop', methods=['POST'])
def api_proxmox_vm_forcestop(vmid):
    return _proxmox_vm_action(vmid, 'stop', 'forcestop')


@app.route('/api/proxmox/vm/<int:vmid>/reboot', methods=['POST'])
def api_proxmox_vm_reboot(vmid):
    return _proxmox_vm_action(vmid, 'reboot', 'reboot')


@app.route('/api/proxmox/vm/<int:vmid>/reset', methods=['POST'])
def api_proxmox_vm_reset(vmid):
    return _proxmox_vm_action(vmid, 'reset', 'reset')


@app.route('/api/proxmox/vm/<int:vmid>/snapshots', methods=['GET'])
def api_proxmox_vm_snapshots(vmid):
    status, data, err = _proxmox_request(
        'GET', f'/nodes/{_node_for_vmid(vmid)}/qemu/{vmid}/snapshot'
    )
    if err:
        return _proxmox_unreachable_response(err)
    snaps = []
    for s in (data or {}).get('data', []) or []:
        # Proxmox always includes a synthetic 'current' entry — keep it last
        # but mark it so UI can render distinctly.
        snaps.append({
            'name': s.get('name'),
            'description': (s.get('description') or '').strip(),
            'snaptime': s.get('snaptime') or 0,
            'parent': s.get('parent'),
            'vmstate': bool(s.get('vmstate')),
            'is_current': s.get('name') == 'current',
        })
    return jsonify({'ok': True, 'reachable': True, 'snapshots': snaps})


@app.route('/api/proxmox/vm/<int:vmid>/snapshot', methods=['POST'])
def api_proxmox_vm_snapshot_create(vmid):
    body = request.get_json(silent=True) or {}
    name = (body.get('name') or '').strip()
    description = (body.get('description') or '').strip()
    if not name:
        return jsonify({'ok': False, 'error': 'snapshot name required'}), 400
    # Proxmox snapname rules: starts with letter, alnum/_/- only, <=40 chars
    safe = re.sub(r'[^A-Za-z0-9_-]', '_', name)[:40]
    if not safe or not safe[0].isalpha():
        safe = 'snap_' + safe
    payload = {'snapname': safe}
    if description:
        payload['description'] = description[:500]
    status, data, err = _proxmox_request(
        'POST',
        f'/nodes/{_node_for_vmid(vmid)}/qemu/{vmid}/snapshot',
        data=payload,
    )
    success = (err is None)
    excerpt = err if err else (str(data)[:300] if data else 'ok')
    _proxmox_log('snapshot_create', vmid=vmid, snapshot=safe,
                 success=success, response_excerpt=excerpt)
    if err:
        return jsonify({'ok': False, 'error': err}), 200
    upid = (data or {}).get('data') if isinstance(data, dict) else None
    return jsonify({'ok': True, 'upid': upid, 'snapshot': safe})


@app.route('/api/proxmox/vm/<int:vmid>/snapshot/<name>/restore', methods=['POST'])
def api_proxmox_vm_snapshot_restore(vmid, name):
    status, data, err = _proxmox_request(
        'POST',
        f'/nodes/{_node_for_vmid(vmid)}/qemu/{vmid}/snapshot/{name}/rollback'
    )
    success = (err is None)
    excerpt = err if err else (str(data)[:300] if data else 'ok')
    _proxmox_log('snapshot_restore', vmid=vmid, snapshot=name,
                 success=success, response_excerpt=excerpt)
    if err:
        return jsonify({'ok': False, 'error': err}), 200
    upid = (data or {}).get('data') if isinstance(data, dict) else None
    return jsonify({'ok': True, 'upid': upid})


@app.route('/api/proxmox/vm/<int:vmid>/snapshot/<name>', methods=['DELETE'])
def api_proxmox_vm_snapshot_delete(vmid, name):
    status, data, err = _proxmox_request(
        'DELETE',
        f'/nodes/{_node_for_vmid(vmid)}/qemu/{vmid}/snapshot/{name}'
    )
    success = (err is None)
    excerpt = err if err else (str(data)[:300] if data else 'ok')
    _proxmox_log('snapshot_delete', vmid=vmid, snapshot=name,
                 success=success, response_excerpt=excerpt)
    if err:
        return jsonify({'ok': False, 'error': err}), 200
    upid = (data or {}).get('data') if isinstance(data, dict) else None
    return jsonify({'ok': True, 'upid': upid})


@app.route('/api/proxmox/console-url/<int:vmid>', methods=['GET'])
def api_proxmox_console_url(vmid):
    """Return the Proxmox web-console URL for a VM. The user has their own
    Proxmox login still — this just opens the right deep link in a new tab."""
    if not PROXMOX_API_BASE:
        return jsonify({'ok': False, 'error': 'Proxmox API not configured'}), 200
    base = PROXMOX_API_BASE
    return jsonify({
        'ok': True,
        'url': f'{base}/?console=kvm&vmid={vmid}&node={_node_for_vmid(vmid)}&resize=scale',
    })

# ─── End Proxmox VM Management ──────────────────────────────────────────────


# ─── Sourcing-bot dashboard API ──────────────────────────────────────────
@app.route('/api/sourcing/active')
def api_sourcing_active_count():
    """Lightweight count of non-archived sourcing requests for banner refresh."""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM sourcing_requests WHERE status <> 'archived'")
    r = cur.fetchone()
    return jsonify({'count': int(r['c'])})


@app.route('/api/sourcing/<int:req_id>/close', methods=['POST'])
def api_sourcing_close(req_id):
    """Manually archive a sourcing request from the dashboard banner."""
    reason = (request.json or {}).get('reason', 'closed_by_staff') if request.is_json else 'closed_by_staff'
    db = get_db()
    cur = db.cursor()
    cur.execute("""UPDATE sourcing_requests
                      SET status='archived',
                          archived_at=NOW(),
                          archive_reason=%s
                    WHERE id=%s
                    RETURNING id""", (reason, req_id))
    row = cur.fetchone()
    if not row:
        return jsonify({'error': 'not found'}), 404
    db.commit()
    return jsonify({'ok': True, 'id': req_id})


@app.route('/sourcing/<int:req_id>')
def sourcing_thread(req_id):
    """Dashboard thread-detail view: full conversation + spec + actions.
    Side effects: marks the request 'seen' (sets seen_at=NOW()) so the
    Sourcing Alerts banner stops pulsing for this row. Also acknowledges
    any pending wishlist<->bid match alerts for this request — by opening
    the thread, staff has 'seen' the matches too."""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM sourcing_requests WHERE id=%s", (req_id,))
    req = cur.fetchone()
    if not req:
        return ('not found', 404)
    try:
        cur.execute("UPDATE sourcing_requests SET seen_at=NOW() WHERE id=%s", (req_id,))
        cur.execute("""UPDATE sourcing_bid_matches
                          SET acknowledged_at = NOW()
                        WHERE sourcing_request_id = %s
                          AND acknowledged_at IS NULL""", (req_id,))
        # Also fetch matching bids for sidebar context on the thread page.
        cur.execute("""SELECT b.id, b.vin, b.year, b.make, b.model, b.trim,
                              b.color, b.mileage, b.status,
                              m.match_strength, m.match_reasons, m.detected_at
                         FROM sourcing_bid_matches m
                         JOIN bids b ON b.id = m.bid_id
                        WHERE m.sourcing_request_id = %s
                        ORDER BY m.detected_at DESC LIMIT 10""", (req_id,))
        matched_bids = [dict(r) for r in cur.fetchall()]
        db.commit()
    except Exception as _e:
        print(f'[sourcing] seen mark err id={req_id}: {_e}', flush=True)
        try: db.rollback()
        except Exception: pass
        matched_bids = []
    return render_template('sourcing_thread.html', req=req, matched_bids=matched_bids)


@app.route('/api/sourcing/<int:req_id>/seen', methods=['POST'])
def api_sourcing_seen(req_id):
    """Mark a sourcing request as seen by staff (clears the alerts pulse).
    Used for dismiss-without-opening; /sourcing/<id> render also marks seen."""
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("UPDATE sourcing_requests SET seen_at=NOW() WHERE id=%s", (req_id,))
        db.commit()
        db.close()
        return jsonify({'ok': True, 'id': req_id})
    except Exception as e:
        try: db.rollback()
        except Exception: pass
        db.close()
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/buyer-match/<int:match_id>/ack', methods=['POST'])
def api_buyer_match_ack(match_id):
    """Manually acknowledge a single wishlist<->bid match without opening
    the thread. Opening the thread (/sourcing/<id>) bulk-acks all matches
    for that request already; this endpoint is for dismiss-without-opening
    patterns (e.g. a future 'mark all seen' button on the dashboard)."""
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("""UPDATE sourcing_bid_matches
                          SET acknowledged_at = NOW()
                        WHERE id = %s AND acknowledged_at IS NULL""", (match_id,))
        db.commit()
        db.close()
        return jsonify({'ok': True, 'id': match_id})
    except Exception as e:
        try: db.rollback()
        except Exception: pass
        db.close()
        return jsonify({'ok': False, 'error': str(e)}), 500


# ─── Persistent EW alerts: context processor ─────────────────────────────
# Injects two counts and one list into every template render so the
# _persistent_alerts.html partial can render the pulsing Buyer Inbox tab
# and the Buyer Match yellow banner regardless of which page is being
# served. The query is intentionally cheap — a single COUNT + a small
# JOIN limited to 5 rows.

@app.context_processor
def inject_ew_alerts():
    """Counts and rows for the persistent EW alert surfaces. Designed for
    SIGNAL not noise — only 'exact' wishlist<->bid matches count toward
    the Buyer Match banner. Partial matches still get inserted into
    sourcing_bid_matches (visible on the thread page for staff review),
    but they don't pulse on every page until adjudicated."""
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute("""
            SELECT COUNT(*)::int AS n
              FROM sourcing_requests
             WHERE status <> 'archived'
               AND (seen_at IS NULL OR last_msg_at > seen_at)
        """)
        unseen = (cur.fetchone() or {}).get('n', 0) or 0
        cur.execute("""
            SELECT COUNT(*)::int AS n
              FROM sourcing_bid_matches
             WHERE acknowledged_at IS NULL
               AND match_strength = 'exact'
        """)
        match_count = (cur.fetchone() or {}).get('n', 0) or 0
        matches = []
        if match_count:
            cur.execute("""
                SELECT m.id AS match_id, m.sourcing_request_id, m.bid_id,
                       m.match_strength, m.detected_at,
                       sr.customer_name, sr.phone, sr.narrative_brief,
                       b.year, b.make, b.model, b.trim
                  FROM sourcing_bid_matches m
                  JOIN sourcing_requests sr ON sr.id = m.sourcing_request_id
                  JOIN bids b ON b.id = m.bid_id
                 WHERE m.acknowledged_at IS NULL
                   AND m.match_strength = 'exact'
                 ORDER BY m.detected_at DESC
                 LIMIT 5
            """)
            for r in cur.fetchall():
                r = dict(r)
                r['year_label'] = str(r['year']) if r.get('year') else ''
                matches.append(r)
        db.close()
        return dict(
            ew_alert_unseen_count=unseen,
            ew_alert_match_count=match_count,
            ew_alert_matches=matches,
        )
    except Exception as _e:
        print(f'[ew-alerts ctx err] {_e}', flush=True)
        try: db.close()
        except Exception: pass
        return dict(
            ew_alert_unseen_count=0,
            ew_alert_match_count=0,
            ew_alert_matches=[],
        )



# ── Network push (2026-05-12) — bidder-triggered scored broadcast ─────
@app.route('/api/bid/<int:bid_id>/network-push', methods=['POST'])
def api_bid_network_push(bid_id):
    """Bidder hits 'Push & Text' on bid.html. Stamps target ask, scores
    every subscribed dealer with VIN-on-lot exclusion, texts surviving
    salespeople. Returns sent/skipped/errors JSON for the result UI."""
    try:
        ask = float(request.form.get('network_ask') or 0)
    except (TypeError, ValueError):
        return {'error': 'invalid network_ask'}, 400
    if ask <= 0:
        return {'error': 'network_ask must be > 0'}, 400
    note = (request.form.get('note') or '').strip() or None
    pushed_by = session.get('username') or 'bidder'

    # Per-dealer selection (CSV of dealer IDs from the checkbox UI). When
    # absent, push to ALL scored matches (legacy behavior).
    raw_ids = (request.form.get('dealer_ids') or '').strip()
    only_ids = None
    if raw_ids:
        try:
            only_ids = [int(x) for x in raw_ids.split(',') if x.strip().isdigit()]
        except Exception:
            only_ids = None
    from partner_portal import push_bid_with_network_ask
    result = push_bid_with_network_ask(bid_id, ask, note, pushed_by=pushed_by,
                                       only_dealer_ids=only_ids)
    return result


@app.route('/api/bid/<int:bid_id>/network-push/preview', methods=['GET'])
def api_bid_network_push_preview(bid_id):
    """Dry-run: returns who WOULD be texted without sending. No DB writes."""
    try:
        ask = float(request.args.get('ask') or 0)
    except (TypeError, ValueError):
        ask = 0
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT id, year, make, model, mileage, vin,
               COALESCE(asking_price, ai_price, bid_amount) AS asking_price
          FROM bids WHERE id = %s
    """, (bid_id,))
    bid = cur.fetchone()
    if not bid:
        db.close()
        return {'error': 'bid not found'}, 404
    cur.execute("""
        SELECT id, name, salesperson, salesperson_phone, buy_profile
          FROM dealers
         WHERE receive_inbound_pushes = TRUE AND active = TRUE
    """)
    subscribers = cur.fetchall()
    db.close()
    from partner_portal import _score_bid_for_dealer, INBOUND_PUSH_MIN_SCORE
    sent, skipped = [], []
    for d in subscribers:
        score, reason = _score_bid_for_dealer(dict(bid), d.get('buy_profile'),
                                              dealer_id=d['id'])
        target = {
            'dealer_id': d['id'], 'name': d['name'],
            'score': score, 'reason': reason,
            'sms_to': d.get('salesperson_phone'),
            'salesperson': d.get('salesperson'),
        }
        if score is not None and score >= INBOUND_PUSH_MIN_SCORE:
            sent.append(target)
        else:
            skipped.append(target)
    return {'preview': True, 'ask': ask, 'sent': sent, 'skipped': skipped, 'errors': []}


# ============================================================
# 2026-05-12 Buy Profile admin routes
# Buy Profile admin routes — added 2026-05-12
# ============================================================

import math as _bp_math

def _bp_score(bid, dealer_id, profile):
    """Score one bid against one dealer's buy_profile JSONB."""
    if not profile or not profile.get('makes'):
        return None, "no profile"
    make = (bid.get('make') or '').upper().strip()
    if not make:
        return None, "bid missing make"
    makes = profile.get('makes') or {}
    if make not in makes:
        return None, f"never stocks {make.title()}"

    m = makes[make]
    bands = profile.get('bands') or {}
    overrides = profile.get('overrides') or {}

    # Hard NEVER overrides
    for rule in overrides.get('never', []) or []:
        if 'price_lt' in rule and bid.get('price') and bid['price'] < rule['price_lt']:
            return None, f"override never (price<{rule['price_lt']})"
        if 'miles_gt' in rule and bid.get('miles') and bid['miles'] > rule['miles_gt']:
            return None, f"override never (miles>{rule['miles_gt']})"

    # Hard ALWAYS overrides — boost to 100
    for rule in overrides.get('always', []) or []:
        if rule.get('make') == make:
            return 100, "override always"

    score = 50
    share = m.get('share') or 0
    if share >= 20:   score += 15
    elif share >= 10: score += 10
    elif share >= 5:  score += 5

    avg_y = m.get('avg_year')
    if avg_y and bid.get('year'):
        yd = abs(bid['year'] - avg_y)
        if yd <= 2:    score += 20
        elif yd <= 5:  score += 10
        elif yd <= 10: score += 0
        else:          score -= 20

    ymin = bands.get('year_min'); ymax = bands.get('year_max')
    if ymin and ymax and bid.get('year'):
        if bid['year'] < ymin - 2 or bid['year'] > ymax + 2:
            score -= 25

    sold_n = m.get('sold_n') or 0
    days = m.get('avg_days_on_lot') or 0
    if sold_n >= 3 and 0 < days < 15:
        score += 15
    elif sold_n >= 1 and 0 < days < 30:
        score += 5

    p10 = bands.get('price_p10'); p90 = bands.get('price_p90')
    if bid.get('price') and p10 and p90:
        if p10 <= bid['price'] <= p90:
            score += 10
        elif bid['price'] < p10 * 0.6 or bid['price'] > p90 * 1.5:
            score -= 15

    mp90 = bands.get('miles_p90')
    if bid.get('miles') is not None and mp90:
        if bid['miles'] <= mp90:
            score += 5
        elif bid['miles'] > mp90 * 1.5:
            score -= 5

    return score, "ok"


def _bp_tier(score):
    if score is None: return "skip"
    if score >= 80:   return "T1"
    if score >= 60:   return "T2"
    return "skip"


@app.route('/admin/buy-profiles')
def admin_buy_profiles():
    """Index — all 14 partner buy profiles side-by-side."""
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT id, name, portal_slug, buy_profile, buy_profile_built_at,
               receive_inbound_pushes
          FROM dealers
         WHERE portal_slug IS NOT NULL
         ORDER BY id
    """)
    dealers = []
    for r in cur.fetchall():
        r = dict(r)
        p = r.get('buy_profile') or {}
        sample = p.get('sample') or {}
        bands = p.get('bands') or {}
        makes = p.get('makes') or {}
        top_makes = sorted(
            [(k, v.get('share') or 0) for k, v in makes.items()],
            key=lambda x: -x[1]
        )[:5]
        r['_summary'] = {
            'active': sample.get('active_n'),
            'sold_180d': sample.get('sold_n_180d'),
            'days_scanned': sample.get('days_scanned'),
            'year_min': bands.get('year_min'),
            'year_max': bands.get('year_max'),
            'price_p10': bands.get('price_p10'),
            'price_p90': bands.get('price_p90'),
            'top_makes': top_makes,
            'makes_count': len(makes),
        }
        dealers.append(r)
    db.close()
    return render_template('admin_buy_profiles.html', dealers=dealers)


@app.route('/admin/dealer/<int:dealer_id>/buy-profile')
def admin_dealer_buy_profile(dealer_id):
    """Detail — single dealer profile with full makes table + overrides."""
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT id, name, portal_slug, buy_profile, buy_profile_built_at,
               receive_inbound_pushes, salesperson
          FROM dealers WHERE id = %s
    """, (dealer_id,))
    row = cur.fetchone()
    db.close()
    if not row:
        return "Not found", 404
    d = dict(row)
    p = d.get('buy_profile') or {}
    makes = p.get('makes') or {}
    makes_sorted = sorted(
        [(k, v) for k, v in makes.items()],
        key=lambda x: -(x[1].get('share') or 0)
    )
    return render_template(
        'admin_dealer_buy_profile.html',
        d=d, profile=p, makes=makes_sorted,
        sample=p.get('sample') or {}, bands=p.get('bands') or {},
        behavioral=p.get('behavioral') or {}, overrides=p.get('overrides') or {}
    )


@app.route('/admin/buy-profiles/preview', methods=['GET', 'POST'])
def admin_buy_profile_preview():
    """Match-routing preview — enter a hypothetical bid, see which dealers fire."""
    bid = {'year': None, 'make': '', 'model': '', 'miles': None, 'price': None}
    routing = None
    if request.method == 'POST':
        try:
            bid['year'] = int(request.form.get('year') or 0) or None
            bid['make'] = (request.form.get('make') or '').strip()
            bid['model'] = (request.form.get('model') or '').strip()
            bid['miles'] = int(request.form.get('miles') or 0) or None
            bid['price'] = int(request.form.get('price') or 0) or None
        except (TypeError, ValueError):
            pass
        if bid['make']:
            db = get_db()
            cur = db.cursor()
            cur.execute("""
                SELECT id, name, buy_profile
                  FROM dealers
                 WHERE portal_slug IS NOT NULL AND buy_profile IS NOT NULL
                 ORDER BY id
            """)
            routing = []
            for r in cur.fetchall():
                r = dict(r)
                s, why = _bp_score(bid, r['id'], r['buy_profile'] or {})
                routing.append({
                    'id': r['id'], 'name': r['name'],
                    'score': s, 'tier': _bp_tier(s), 'why': why,
                })
            db.close()
            routing.sort(key=lambda x: (x['score'] if x['score'] is not None else -9999), reverse=True)
    return render_template('admin_buy_profile_preview.html', bid=bid, routing=routing)


@app.route('/api/admin/buy-profile/rebuild', methods=['POST'])
def api_admin_buy_profile_rebuild():
    """Manual rebuild trigger — handy during the meeting."""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT rebuild_all_buy_profiles()")
    n = list(cur.fetchone().values())[0]
    db.commit()
    db.close()
    return {'rebuilt': n}



@app.route('/api/bid/<int:bid_id>/confirm-sold', methods=['POST'])
def api_bid_confirm_sold(bid_id):
    """Marks winning bid_pushes row sold + flips bid.status='bought' +
    broadcasts a polite 'just sold' SMS to every OTHER dealer who received
    the original network push. Winner is excluded from the broadcast (they
    already got 'It's yours' on their YES reply)."""
    db = get_db()
    cur = db.cursor()

    # 1. Mark winner sold
    cur.execute("""
        UPDATE bid_pushes
           SET sold_confirmed_at = NOW(),
               sold_confirmed_by = %s
         WHERE bid_id = %s
           AND claimed_at IS NOT NULL
           AND claim_late IS NOT TRUE
         RETURNING dealer_id
    """, (session.get('username') or 'admin', bid_id))
    w = cur.fetchone()
    if not w:
        db.close()
        return {'ok': False, 'error': 'no active claim to confirm'}
    winner_id = w['dealer_id']

    # 2. Flip bid to bought
    cur.execute("UPDATE bids SET status='bought', updated_at=NOW() WHERE id=%s",
                (bid_id,))

    # 3. Pull bid context for the broadcast message
    cur.execute("SELECT year, make, model FROM bids WHERE id=%s", (bid_id,))
    b = cur.fetchone()
    ymm = (b and f"{b['year'] or ''} {b['make'] or ''} {b['model'] or ''}".strip()) or 'that vehicle'

    # 4. Find losers — everyone else who got the SMS for this bid
    cur.execute("""
        SELECT bp.id AS push_id, bp.dealer_id, bp.sms_to,
               d.name AS dealer_name, d.salesperson_phone
          FROM bid_pushes bp
          JOIN dealers d ON d.id = bp.dealer_id
         WHERE bp.bid_id = %s
           AND bp.sms_sent_at IS NOT NULL
           AND bp.dealer_id != %s
    """, (bid_id, winner_id))
    losers = cur.fetchall()

    # 5. Broadcast polite 'just sold' SMS to each loser
    from partner_portal import _send_network_sms
    broadcast_sent = 0
    broadcast_failed = 0
    sold_body = (f"Update: the {ymm} you saw earlier just sold to another buyer. "
                 f"Thanks for taking a look — we'll text you when the next one hits.")
    for L in losers:
        target = L.get('sms_to') or L.get('salesperson_phone')
        if not target:
            broadcast_failed += 1
            continue
        if _send_network_sms(target, sold_body):
            broadcast_sent += 1
            cur.execute("""
                UPDATE bid_pushes SET sold_confirmed_at = NOW()
                 WHERE id = %s
            """, (L['push_id'],))
        else:
            broadcast_failed += 1

    db.commit()
    db.close()

    print(f'[confirm-sold] bid={bid_id} winner={winner_id} '
          f'broadcast_sent={broadcast_sent} failed={broadcast_failed}', flush=True)

    return {
        'ok': True,
        'dealer_id': winner_id,
        'broadcast_sent': broadcast_sent,
        'broadcast_failed': broadcast_failed,
    }



# ── /opportunities — daily AI scout dashboard ─────────────────────────────
@app.route('/opportunities')
def opportunities_page():
    """Salesperson-facing buy-opportunity dashboard. Auth via global require_login."""
    return render_template('opportunities.html')


@app.route('/api/opportunities/snapshot')
def api_opportunities_snapshot():
    """Today's scored opportunities + summary + last-run audit.

    Anyone who can see the main dashboard can hit this — relies on the
    global require_login gate.
    """
    db = get_db()
    cur = db.cursor()

    # Most-recent run for this calendar day
    cur.execute("""
        SELECT id, started_at, finished_at, mmr_attempted, mmr_ok,
               mmr_no_data, mmr_errors, candidates_5pct,
               rbook_attempted, rbook_ok, rbook_errors,
               opportunities_written, orphans_removed, auth_failed
          FROM opportunity_runs
         WHERE started_at::date = CURRENT_DATE
         ORDER BY started_at DESC
         LIMIT 1
    """)
    run = cur.fetchone()
    run_dict = dict(run) if run else {}
    if run_dict.get('started_at'):
        run_dict['started_at_iso'] = run_dict['started_at'].strftime('%Y-%m-%d %H:%M:%S')

    # Today's opportunities — most recent snapshot_date that has any rows
    cur.execute("""
        SELECT MAX(snapshot_date) AS d FROM dealer_opportunities
    """)
    latest_day = (cur.fetchone() or {}).get('d')
    if not latest_day:
        return jsonify({'opportunities': [], 'summary': {}, 'run': run_dict, 'dealers': []})

    cur.execute("""
        SELECT o.id, o.snapshot_date, o.vin, o.dealer_id, o.inventory_id,
               o.year, o.make, o.model, o.trim, o.mileage, o.ext_color,
               o.photo_url, o.detail_url,
               o.asking_price, o.mmr_wholesale_avg, o.mmr_wholesale_above,
               o.mmr_wholesale_below, o.mmr_grade,
               o.mmr_retail_avg, o.mmr_retail_above, o.mmr_retail_below,
               o.dollars_under_mmr, o.pct_under_mmr,
               o.dollars_under_retail, o.pct_under_retail, o.opportunity_type,
               o.rbook_comp_count, o.rbook_p25, o.rbook_p50, o.rbook_p75,
               o.rbook_avg_dol, o.retail_headroom,
               o.dealer_dol, o.recent_price_drop_amount,
               o.recent_price_drop_days_ago,
               o.lsl_deal_count, o.lsl_avg_gross,
               o.score, o.score_breakdown, o.signals,
               o.gemini_pitch,
               o.status, o.assigned_to, o.notes,
               o.created_at, o.updated_at,
               d.name AS dealer_name, d.phone AS dealer_phone
          FROM dealer_opportunities o
          JOIN dealers d ON d.id = o.dealer_id
         WHERE o.snapshot_date = %s
         ORDER BY o.score DESC NULLS LAST, o.pct_under_mmr DESC NULLS LAST
    """, (latest_day,))
    rows = [dict(r) for r in cur.fetchall()]

    # Stringify timestamps + ensure clean JSON
    for r in rows:
        for k in ('created_at', 'updated_at'):
            if r.get(k):
                r[k] = r[k].isoformat()
        if r.get('snapshot_date'):
            r['snapshot_date'] = r['snapshot_date'].isoformat()
        if r.get('rbook_avg_dol') is not None:
            r['rbook_avg_dol'] = float(r['rbook_avg_dol'])
        if r.get('pct_under_mmr') is not None:
            r['pct_under_mmr'] = float(r['pct_under_mmr'])
        # signals + score_breakdown stored as JSONB — already dicts when
        # using RealDictCursor; leave alone

    # Summary
    cur.execute("""
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE status='new')      AS "new",
            COUNT(*) FILTER (WHERE status='pursuing') AS pursuing,
            COUNT(*) FILTER (WHERE status='acquired') AS acquired,
            COUNT(*) FILTER (WHERE status='passed')   AS passed,
            MAX(score) AS top_score,
            SUM(dollars_under_mmr) FILTER (WHERE status NOT IN ('passed','acquired')) AS total_under_mmr
          FROM dealer_opportunities
         WHERE snapshot_date = %s
    """, (latest_day,))
    summary = dict(cur.fetchone() or {})

    # Dealer breakdown chips
    cur.execute("""
        SELECT d.id, d.name, COUNT(*) AS n
          FROM dealer_opportunities o
          JOIN dealers d ON d.id = o.dealer_id
         WHERE o.snapshot_date = %s
         GROUP BY d.id, d.name
         ORDER BY n DESC, d.name
    """, (latest_day,))
    dealers = [dict(r) for r in cur.fetchall()]

    db.close()
    return jsonify({
        'opportunities': rows,
        'summary': summary,
        'run': run_dict,
        'dealers': dealers,
    })


@app.route('/api/opportunities/<int:opp_id>/status', methods=['POST'])
def api_opportunity_status(opp_id):
    """Update workflow status. Body: {status, notes?, assigned_to?}."""
    data = request.get_json(silent=True) or {}
    new_status = (data.get('status') or '').strip()
    if new_status not in ('new', 'called', 'pursuing', 'passed', 'acquired', 'snoozed'):
        return jsonify({'ok': False, 'error': 'bad status'}), 400

    actor = (session.get('user_email') if 'session' in globals() else None) or 'unknown'

    db = get_db()
    cur = db.cursor()
    cur.execute("""
        UPDATE dealer_opportunities
           SET status = %s,
               status_updated_at = NOW(),
               status_updated_by = %s,
               notes = COALESCE(%s, notes),
               assigned_to = COALESCE(%s, assigned_to),
               updated_at = NOW()
         WHERE id = %s
        RETURNING id, status
    """, (new_status, actor, data.get('notes'), data.get('assigned_to'), opp_id))
    row = cur.fetchone()
    db.commit()
    db.close()
    if not row:
        return jsonify({'ok': False, 'error': 'not found'}), 404
    return jsonify({'ok': True, 'id': row['id'], 'status': row['status']})



# ── /api/opportunities/<id>/pitch — lazy Gemini Pro narrative ─────────────
OPPORTUNITY_PITCH_PROMPT = """You are advising Experience Wholesale's buying team on whether to chase a single car off a partner dealer's lot for wholesale acquisition.

This isn't a customer bid — EW would BUY this car wholesale from the dealer and resell to another dealer or retail it. Give a sharp, salesperson-actionable read.

═══ VEHICLE ═══
{ymm}{trim_clause}
VIN: {vin}
Mileage: {mileage:,}
Dealer: {dealer_name}{dealer_phone_clause}
Asking price: ${asking:,}

═══ SIGNALS ═══
MMR Wholesale Avg: ${mmr_avg:,}  ({pct_under:.1f}% under MMR, ${dollars_under:,} delta)
MMR Above (clean): ${mmr_above_s}
MMR Below (rough): ${mmr_below_s}
MMR Grade: {grade}

rBook competitive set: {comp_count_s} cars
  Retail P25: ${rbook_p25_s}
  Retail P50: ${rbook_p50_s}
  Retail P75: ${rbook_p75_s}
  Retail headroom (P50 - asking): ${headroom_s}
  Avg DOL on comps: {comp_dol_s}

Dealer-side:
  Days listed on dealer lot: {dealer_dol_s}
  Recent price drop: {drop_str}

LSL track record (our historical EW deals on this YMM):
  Deals: {lsl_n}{lsl_gross_clause}

═══ TASK ═══
Write 3-4 SHORT sentences. Cover:
  1. Why this is (or isn't) a wholesale buy — point to the strongest signal
  2. Suggested target buy price + walk-away ceiling
  3. ONE risk or thing to verify before chasing it

Do NOT restate the data. Make a recommendation. Plain text, no markdown."""


@app.route('/api/opportunities/<int:opp_id>/pitch', methods=['POST', 'GET'])
def api_opportunity_pitch(opp_id):
    """Lazy-generate or return cached Gemini Pro narrative for one opportunity.

    GET returns cached or 404. POST regenerates (or generates first time).
    """
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT o.id, o.vin, o.year, o.make, o.model, o.trim, o.mileage,
               o.asking_price, o.mmr_wholesale_avg, o.mmr_wholesale_above,
               o.mmr_wholesale_below, o.mmr_grade,
               o.dollars_under_mmr, o.pct_under_mmr,
               o.rbook_comp_count, o.rbook_p25, o.rbook_p50, o.rbook_p75,
               o.rbook_avg_dol, o.retail_headroom,
               o.dealer_dol, o.recent_price_drop_amount,
               o.recent_price_drop_days_ago,
               o.lsl_deal_count, o.lsl_avg_gross,
               o.gemini_pitch, o.gemini_pitch_at,
               d.name AS dealer_name, d.phone AS dealer_phone
          FROM dealer_opportunities o
          JOIN dealers d ON d.id = o.dealer_id
         WHERE o.id = %s
    """, (opp_id,))
    o = cur.fetchone()
    if not o:
        db.close()
        return jsonify({'ok': False, 'error': 'not found'}), 404

    method = request.method
    if method == 'GET' and o.get('gemini_pitch'):
        db.close()
        return jsonify({
            'ok': True, 'cached': True,
            'pitch': o['gemini_pitch'],
            'generated_at': o['gemini_pitch_at'].isoformat() if o.get('gemini_pitch_at') else None,
        })

    if method == 'GET' and not o.get('gemini_pitch'):
        db.close()
        return jsonify({'ok': False, 'error': 'no pitch — POST to generate'}), 404

    # POST: regenerate
    def _s(v, default='—'):
        return f'{v:,}' if isinstance(v, (int, float)) and v else default

    drop_amt = o.get('recent_price_drop_amount') or 0
    drop_days = o.get('recent_price_drop_days_ago')
    if drop_amt and drop_days is not None:
        drop_str = f'${drop_amt:,} cut {drop_days} days ago'
    else:
        drop_str = 'none in last 30 days'

    lsl_n = o.get('lsl_deal_count') or 0
    lsl_g = o.get('lsl_avg_gross') or 0
    lsl_gross_clause = f', avg gross ${lsl_g:,}' if lsl_n and lsl_g else ''

    trim_clause = f' {o["trim"]}' if o.get('trim') else ''
    dealer_phone_clause = f' ({o["dealer_phone"]})' if o.get('dealer_phone') else ''

    prompt = OPPORTUNITY_PITCH_PROMPT.format(
        ymm=f'{o["year"]} {o["make"]} {o["model"]}',
        trim_clause=trim_clause,
        vin=o['vin'],
        mileage=o.get('mileage') or 0,
        dealer_name=o.get('dealer_name') or 'unknown',
        dealer_phone_clause=dealer_phone_clause,
        asking=o['asking_price'] or 0,
        mmr_avg=o['mmr_wholesale_avg'] or 0,
        pct_under=float(o['pct_under_mmr'] or 0),
        dollars_under=o['dollars_under_mmr'] or 0,
        mmr_above_s=_s(o.get('mmr_wholesale_above')),
        mmr_below_s=_s(o.get('mmr_wholesale_below')),
        grade=o.get('mmr_grade') or 'n/a',
        comp_count_s=_s(o.get('rbook_comp_count')),
        rbook_p25_s=_s(o.get('rbook_p25')),
        rbook_p50_s=_s(o.get('rbook_p50')),
        rbook_p75_s=_s(o.get('rbook_p75')),
        headroom_s=_s(o.get('retail_headroom')),
        comp_dol_s=f'{float(o["rbook_avg_dol"]):.1f}d' if o.get('rbook_avg_dol') else '—',
        dealer_dol_s=f'{o["dealer_dol"]}d' if o.get('dealer_dol') else 'unknown',
        drop_str=drop_str,
        lsl_n=lsl_n,
        lsl_gross_clause=lsl_gross_clause,
    )

    try:
        pitch = gemini_call(prompt, model='gemini-2.5-flash',
                            max_tokens=1500, temperature=0.4)
    except Exception as e:
        db.close()
        return jsonify({'ok': False, 'error': f'gemini_call failed: {e}'}), 500

    if not pitch:
        db.close()
        return jsonify({'ok': False, 'error': 'empty gemini response'}), 500

    cur.execute("""
        UPDATE dealer_opportunities
           SET gemini_pitch = %s,
               gemini_pitch_at = NOW(),
               updated_at = NOW()
         WHERE id = %s
    """, (pitch, opp_id))
    db.commit()
    db.close()
    return jsonify({'ok': True, 'cached': False, 'pitch': pitch})



# ── /api/bid/<id>/estimate ─ operator value estimate (training signal) ──
# Independent of the AI assessment — Gemini does NOT read this value.
# Consumed by reconcile_ai_accuracy.py (every 30 min) + train_per_make.py
# (nightly 04:30). Treated as peer ground-truth to actual_purchase_cost so
# we can learn from bids we estimated but didn't acquire.
@app.route('/api/bid/<int:bid_id>/estimate', methods=['POST'])
def api_bid_estimate(bid_id):
    """Set the operator's value estimate on a bid.

    Body: {"estimate": <integer-dollars>, "actor": "<optional name>"}
    Returns: {ok, bid_id, client_estimate, client_estimate_at,
              ai_assessed_at, entered_before_ai}
    """
    data = request.get_json(silent=True) or {}
    try:
        est = int(data.get('estimate'))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'estimate (int dollars) required'}), 400
    if est <= 0 or est > 5_000_000:
        return jsonify({'ok': False, 'error': 'estimate out of range (1..5M)'}), 400

    actor = (data.get('actor') or '').strip()[:64] or None
    if not actor:
        # Pull from session if available, else from logged-in email
        try:
            actor = (session.get('user_email') or '')[:64] or None
        except Exception:
            actor = None

    db = get_db()
    cur = db.cursor()
    cur.execute("""
        UPDATE bids
           SET client_estimate    = %s,
               client_estimate_at = NOW(),
               client_estimate_by = %s,
               updated_at         = NOW()
         WHERE id = %s
        RETURNING id, client_estimate, client_estimate_at, client_estimate_by,
                  ai_assessed_at
    """, (est, actor, bid_id))
    row = cur.fetchone()
    db.commit()
    db.close()
    if not row:
        return jsonify({'ok': False, 'error': 'bid not found'}), 404

    cea = row['client_estimate_at']
    aaa = row.get('ai_assessed_at')
    entered_before_ai = bool(aaa is None or (cea and aaa and cea.replace(tzinfo=None) < aaa.replace(tzinfo=None)))
    return jsonify({
        'ok': True,
        'bid_id': row['id'],
        'client_estimate': row['client_estimate'],
        'client_estimate_at': cea.isoformat() if cea else None,
        'client_estimate_by': row['client_estimate_by'],
        'ai_assessed_at': aaa.isoformat() if aaa else None,
        'entered_before_ai': entered_before_ai,
    })


@app.route('/api/bid/<int:bid_id>/estimate', methods=['DELETE'])
def api_bid_estimate_clear(bid_id):
    """Clear an erroneous estimate. Sets columns back to NULL."""
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        UPDATE bids
           SET client_estimate    = NULL,
               client_estimate_at = NULL,
               client_estimate_by = NULL,
               updated_at         = NOW()
         WHERE id = %s
        RETURNING id
    """, (bid_id,))
    row = cur.fetchone()
    db.commit()
    db.close()
    if not row:
        return jsonify({'ok': False, 'error': 'bid not found'}), 404
    return jsonify({'ok': True, 'bid_id': row['id'], 'cleared': True})



# ── /admin/bias-segments — live source mix view ─────────────────────────
@app.route('/admin/bias-segments')
def admin_bias_segments():
    """Live view of per-segment bias correction sources.
    Shows where operator estimates have started influencing live AI output.
    """
    return render_template('admin_bias_segments.html')


@app.route('/api/admin/bias-segments/data')
def api_admin_bias_segments_data():
    """Snapshot of bias_segments split by source (actual vs estimate)."""
    window = request.args.get('window', 30, type=int)
    if window not in (30, 90):
        window = 30

    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT make, model, year_band, mileage_band,
               n, n_actual, n_estimate,
               bias_pct, bias_pct_actual, bias_pct_estimate,
               abs_pct, stddev_pct, refreshed_at
          FROM bias_segments
         WHERE window_days = %s
         ORDER BY n_estimate DESC NULLS LAST, n DESC, abs(bias_pct) DESC
    """, (window,))
    rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        if r.get('refreshed_at'):
            r['refreshed_at'] = r['refreshed_at'].isoformat()
        for k in ('bias_pct', 'bias_pct_actual', 'bias_pct_estimate',
                  'abs_pct', 'stddev_pct'):
            if r.get(k) is not None:
                r[k] = float(r[k])

    # Summary stats
    cur.execute("""
        SELECT
            count(*) AS segments,
            sum(n) AS total_n,
            sum(n_actual) AS total_actual,
            sum(n_estimate) AS total_estimate,
            count(*) FILTER (WHERE n_estimate > 0) AS segments_with_estimates,
            count(*) FILTER (WHERE n_estimate >= n_actual AND n_estimate > 0)
              AS segments_estimate_majority,
            max(refreshed_at) AS last_refresh
          FROM bias_segments
         WHERE window_days = %s
    """, (window,))
    s = dict(cur.fetchone() or {})
    if s.get('last_refresh'):
        s['last_refresh'] = s['last_refresh'].isoformat()

    db.close()
    return jsonify({'rows': rows, 'summary': s, 'window': window})



# ── /api/thalist/make_sweep ─ defense-in-depth backfill for NULL makes ──
# Catches thalist bids where the make_id wasn\'t in the static map AND the
# title-parse fallback wasn\'t in place at intake time. Backfills make from
# the title, then re-fires AI assessment if the bid was stuck on analyzing
# (ai_assessed_at stamped, ai_price NULL).
def _thalist_parse_make_from_title(title: str) -> str | None:
    """Same regex as api_thalist_post intake (single source of truth).
    Extracts make from \"<year> <make> <rest>\" titles."""
    if not title:
        return None
    import re as _re
    m = _re.match(
        r'^\s*(?:19|20)\d{2}\s+'
        r'(Mercedes[-\s]Benz|Aston[-\s]Martin|Land[-\s]Rover|'
        r'Rolls[-\s]Royce|Alfa[-\s]Romeo|\S+)',
        title.strip(), _re.IGNORECASE,
    )
    return m.group(1).strip() if m else None


@app.route('/api/thalist/make_sweep', methods=['POST'])
def api_thalist_make_sweep():
    """Sweep thalist bids with make=NULL, backfill from title + re-assess.

    Optional body: {"limit": N} to cap how many bids to process this call.
    Default cap: 50.
    """
    data = request.get_json(silent=True) or {}
    limit = int(data.get('limit') or 50)

    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT b.id, b.year, b.vin, tp.title, tp.make_id
          FROM bids b
          JOIN thalist_posts tp ON tp.bid_id = b.id
         WHERE b.creation_source = 'thalist'
           AND (b.make IS NULL OR b.make = '' OR b.make = 'Unknown')
           AND tp.title IS NOT NULL
         ORDER BY b.id DESC
         LIMIT %s
    """, (limit,))
    candidates = cur.fetchall()

    fixed = []
    reassessed = []
    skipped = []
    new_make_ids = {}  # harvested ID → parsed name (for ops to add to map later)

    for row in candidates:
        bid_id = row['id']
        title = row['title']
        make_id = row['make_id']
        # Try static map first
        parsed = _thalist_resolve_make(make_id)
        if not parsed:
            parsed = _thalist_parse_make_from_title(title)
            if parsed and make_id and make_id not in _THALIST_MAKE_ID_TO_NAME:
                new_make_ids[make_id] = parsed
        if not parsed:
            skipped.append({'bid_id': bid_id, 'title': title, 'make_id': make_id,
                            'reason': 'unparseable'})
            continue
        cur.execute("""
            UPDATE bids SET make = %s, updated_at = NOW() WHERE id = %s
        """, (parsed, bid_id))
        fixed.append({'bid_id': bid_id, 'make': parsed, 'make_id': make_id,
                      'from_static_map': bool(_thalist_resolve_make(make_id))})
    db.commit()

    # Also catch any thalist bid stuck on "analyzing" regardless of whether
    # we just backfilled make. Covers the silent-Gemini-abort case
    # (bid 1222: make set, Gemini failed mid-flight, ai_price stayed NULL).
    cur.execute("""
        SELECT id FROM bids
         WHERE creation_source = 'thalist'
           AND ai_assessed_at IS NOT NULL
           AND ai_price IS NULL
           AND make IS NOT NULL
           AND created_at > NOW() - INTERVAL '7 days'
         ORDER BY id DESC LIMIT %s
    """, (limit,))
    extra_stuck = [r['id'] for r in cur.fetchall()]

    # For each fixed bid that\'s stuck on "analyzing" (ai_assessed_at set,
    # ai_price NULL), re-fire the assessment in a daemon thread so the
    # request returns fast.
    if fixed or extra_stuck:
        cur.execute("""
            SELECT id FROM bids
             WHERE id = ANY(%s)
               AND ai_assessed_at IS NOT NULL
               AND ai_price IS NULL
        """, ([f['bid_id'] for f in fixed],))
        stuck_from_fixed = [r['id'] for r in cur.fetchall()]
        stuck_ids = list(set(stuck_from_fixed) | set(extra_stuck))
        # Clear ai_assessed_at so _maybe_fire_assessment will re-fire
        if stuck_ids:
            cur.execute("""
                UPDATE bids SET ai_assessed_at = NULL WHERE id = ANY(%s)
            """, (stuck_ids,))
            db.commit()
            for bid_id in stuck_ids:
                try:
                    threading.Thread(
                        target=_run_assessment,
                        args=(bid_id,),
                        kwargs={},
                        daemon=True,
                        name=f'reassess-thalist-{bid_id}',
                    ).start()
                    reassessed.append(bid_id)
                except Exception as e:
                    print(f'[thalist-sweep] re-assess thread failed bid={bid_id}: {e}',
                          flush=True)

    db.close()
    return jsonify({
        'ok': True,
        'fixed_count': len(fixed),
        'fixed': fixed,
        'reassessed_count': len(reassessed),
        'reassessed': reassessed,
        'skipped_count': len(skipped),
        'skipped': skipped,
        'new_make_ids_to_add': new_make_ids,
    })


# ── Phone-gate admin (added 2026-05-14) ─────────────────────────────────
# UI for managing the gated_phones table. Reads env baselines + DB rows.
# gate_helpers.bust_gate_cache() fires after every write so the next SMS
# check sees the change immediately (no service restart).

@app.route('/admin/phone-gates')
def admin_phone_gates():
    db = get_db()
    cur = db.cursor()
    cur.execute("""SELECT id, phone_digits, gate_type, label, added_by,
                          added_at, disabled_at, disabled_by
                     FROM gated_phones
                    ORDER BY disabled_at IS NULL DESC, added_at DESC""")
    rows = [dict(r) for r in cur.fetchall()]
    db.close()
    for r in rows:
        d = r['phone_digits']
        r['phone_pretty'] = f'({d[0:3]}) {d[3:6]}-{d[6:10]}' if len(d) == 10 else d
        if r.get('added_at') and hasattr(r['added_at'], 'isoformat'):
            r['added_at_iso'] = r['added_at'].isoformat()
        if r.get('disabled_at') and hasattr(r['disabled_at'], 'isoformat'):
            r['disabled_at_iso'] = r['disabled_at'].isoformat()
    # Env baselines (read-only; edit /etc/systemd/system/expwholesale.service)
    env_full_broker = sorted(gate_helpers._env_digits('PHASE2_PHONE_GATE'))
    env_sourcing = sorted(gate_helpers._env_digits('SOURCING_PHONE_GATE')
                          or gate_helpers._env_digits('PHASE2_PHONE_GATE'))
    def _pretty(d):
        return f'({d[0:3]}) {d[3:6]}-{d[6:10]}' if len(d) == 10 else d
    env_full_broker = [{'digits': d, 'pretty': _pretty(d)} for d in env_full_broker]
    env_sourcing = [{'digits': d, 'pretty': _pretty(d)} for d in env_sourcing]
    return render_template('admin_phone_gates.html',
                           rows=rows,
                           env_full_broker=env_full_broker,
                           env_sourcing=env_sourcing)


@app.route('/admin/phone-gates/add', methods=['POST'])
def admin_phone_gates_add():
    phone_raw = (request.form.get('phone') or '').strip()
    gate_type = (request.form.get('gate_type') or '').strip()
    label = (request.form.get('label') or '').strip() or None
    digits = gate_helpers.phone_digits(phone_raw)
    if len(digits) != 10:
        return jsonify({'ok': False, 'error': f'invalid phone (need 10 digits, got {len(digits)})'}), 400
    if gate_type not in ('full_broker', 'sourcing'):
        return jsonify({'ok': False, 'error': 'gate_type must be full_broker or sourcing'}), 400
    db = get_db()
    cur = db.cursor()
    # If a previously-disabled row exists, re-enable it (preserves history).
    cur.execute("""SELECT id FROM gated_phones
                    WHERE phone_digits=%s AND gate_type=%s AND disabled_at IS NOT NULL
                    ORDER BY disabled_at DESC LIMIT 1""", (digits, gate_type))
    prior = cur.fetchone()
    if prior:
        cur.execute("""UPDATE gated_phones SET disabled_at=NULL, disabled_by=NULL,
                                                  label=COALESCE(%s, label),
                                                  added_by=%s, added_at=NOW()
                          WHERE id=%s""",
                    (label, 'admin', prior['id']))
        action = 're-enabled'
        row_id = prior['id']
    else:
        try:
            cur.execute("""INSERT INTO gated_phones (phone_digits, gate_type, label, added_by)
                              VALUES (%s, %s, %s, %s) RETURNING id""",
                        (digits, gate_type, label, 'admin'))
            row_id = cur.fetchone()['id']
            action = 'added'
        except psycopg2.errors.UniqueViolation:
            db.rollback()
            db.close()
            return jsonify({'ok': False, 'error': 'already active for this gate'}), 409
    db.commit()
    db.close()
    gate_helpers.bust_gate_cache()
    return jsonify({'ok': True, 'action': action, 'id': row_id,
                    'phone_digits': digits, 'gate_type': gate_type, 'label': label})


@app.route('/admin/phone-gates/<int:row_id>/remove', methods=['POST'])
def admin_phone_gates_remove(row_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("""UPDATE gated_phones
                      SET disabled_at=NOW(), disabled_by=%s
                    WHERE id=%s AND disabled_at IS NULL
                RETURNING id, phone_digits, gate_type""", ('admin', row_id))
    row = cur.fetchone()
    db.commit()
    db.close()
    if not row:
        return jsonify({'ok': False, 'error': 'not found or already disabled'}), 404
    gate_helpers.bust_gate_cache()
    return jsonify({'ok': True, 'id': row['id'],
                    'phone_digits': row['phone_digits'], 'gate_type': row['gate_type']})


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=9000)
