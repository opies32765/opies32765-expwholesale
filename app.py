import json
import os
import re
import base64
import uuid
import threading
import psycopg2
import psycopg2.extras
import requests
from datetime import datetime
from flask import Flask, render_template, request, jsonify, redirect, url_for, send_from_directory, session
from twilio.rest import Client as TwilioClient

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'expwholesale2026!')
app.permanent_session_lifetime = 86400 * 30  # 30 days

# Dealer DB blueprint (partner inventory scanning + UI)
try:
    from dealer_db import bp as _dealer_bp
    app.register_blueprint(_dealer_bp)
except Exception as _e:
    print(f'[dealer_db] blueprint not loaded: {_e}', flush=True)

# ── Dashboard login ───────────────────────────────────────────────────────────
EW_USERNAME = os.environ.get('EW_USERNAME', 'admin')
EW_PASSWORD = os.environ.get('EW_PASSWORD', 'Sedecrem3')

# Paths that don't require login
_PUBLIC_PREFIXES = (
    '/login', '/mobile', '/webhook/', '/static/', '/thumb',
    '/vauto_reports/', '/service-worker', '/privacy', '/terms',
    '/api/mobile-submit', '/api/rep-bids', '/api/register-rep',
    '/api/vauto/', '/api/accutrade/', '/accutrade_reports/',
    '/api/ipacket/', '/ipacket_reports/',
    '/api/bid/external', '/api/push-subscribe',
    '/api/push-unsubscribe', '/api/vapid-public-key',
    '/.well-known/', '/api/tesla-vin/', '/share/',
    '/api/quick-extract',
)


@app.route('/.well-known/appspecific/<path:filename>')
def well_known(filename):
    return send_from_directory(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', '.well-known', 'appspecific'),
        filename,
        mimetype='application/x-pem-file'
    )
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
    return redirect('/login')


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        if (request.form.get('username') == EW_USERNAME and
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
DIA_DB_URL = 'postgresql://scraper@62.146.226.100/dealer_intelligence'
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
    """Call NHTSA vPIC API and return dict with year/make/model/trim or empty dict."""
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
        want = {'ModelYear': 'year', 'Model Year': 'year', 'Make': 'make', 'Model': 'model', 'Trim': 'trim'}
        out = {}
        for item in items:
            key = want.get(item.get('Variable'))
            val = (item.get('Value') or '').strip()
            if key and val and val != 'null':
                if key == 'year':
                    try:
                        out[key] = int(val)
                    except ValueError:
                        pass
                else:
                    # NHTSA returns ambiguous "Trim A / Trim B" — take the first option
                    if ' / ' in val:
                        val = val.split(' / ')[0].strip()
                    out[key] = val
        return out
    except Exception:
        return {}

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


def gemini_call(prompt, image_bytes=None, mime='image/jpeg', model='gemini-2.5-flash',
                max_tokens=1024, temperature=0.4):
    """One-shot Gemini call. Returns text response or None on failure.
    Pass image_bytes for vision tasks. Defaults to Flash (cheap).
    Use model='gemini-2.5-pro' for high-quality reasoning (assessments)."""
    client = _gemini()
    if not client:
        return None
    try:
        from google.genai import types
        if image_bytes:
            contents = [
                types.Part.from_bytes(data=image_bytes, mime_type=mime),
                prompt,
            ]
        else:
            contents = prompt
        resp = client.models.generate_content(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(
                max_output_tokens=max_tokens,
                temperature=temperature,
            ),
        )
        return resp.text.strip() if resp.text else None
    except Exception as e:
        print(f'Gemini call failed ({model}): {e}', flush=True)
        return None


# ── VIN extraction ───────────────────────────────────────────────────────────

def extract_vin_from_text(text):
    match = VIN_RE.search(text.upper())
    return match.group(0) if match else None


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


def extract_vin_from_file(file_bytes, media_type='image/jpeg'):
    """Extract VIN from image. Google Vision first (cheap), Claude fallback."""
    # Try Google Vision first — ~$0.0015/call vs Claude ~$0.05/call
    text = _google_vision_ocr(file_bytes)
    if text:
        up = text.upper()
        # VIN regex: 17 chars, no I/O/Q
        match = re.search(r'\b[A-HJ-NPR-Z0-9]{17}\b', up)
        if match:
            print(f'[OCR] VIN via Google Vision: {match.group(0)}', flush=True)
            return match.group(0)
        # Fallback: 17-char sequences with O/I/Q (likely OCR misreads) —
        # try substituting O→0, I→1, Q→0 to recover
        for m in re.finditer(r'\b[A-Z0-9]{17}\b', up):
            candidate = m.group(0).replace('O', '0').replace('I', '1').replace('Q', '0')
            if VIN_RE.match(candidate):
                print(f'[OCR] VIN via Google Vision (O→0 recovered): {candidate}', flush=True)
                return candidate
    print('[OCR] Google Vision missed, falling back to Gemini Flash', flush=True)

    # Fallback 1: Gemini Flash (printed text, still cheap)
    result = gemini_call(VIN_PROMPT, image_bytes=file_bytes, mime=media_type,
                         model='gemini-2.5-flash', max_tokens=100)
    if result:
        result = result.strip().upper()
        if VIN_RE.match(result):
            print(f'[OCR] VIN via Gemini Flash: {result}', flush=True)
            return result

    # Fallback 2: Gemini Pro (handles handwriting + ambiguous text)
    print('[OCR] Gemini Flash missed, trying Gemini Pro (handwriting)', flush=True)
    hw_prompt = (
        'Read the VIN from this image. The image may contain a handwritten note, '
        'a VIN sticker, or any vehicle identifier. Apply strict VIN rules:\n'
        '- Exactly 17 characters (A-Z, 0-9)\n'
        '- Letters I, O, Q are NEVER valid — substitute 1, 0, 0\n'
        '- Handwriting: resolve 1/7, 0/O/Q, 5/S/G, 2/Z, 4/Y/A confusion\n'
        '- The 9th character is a math check digit. Common values: 0-9 or X.\n'
        '- Common prefixes: 1G, 1F, 1C, 1H, 2H, 5J, 5Y, 7S, WP, WB, WD, YV\n\n'
        'Reply with ONLY the 17-char VIN. No other text.'
    )
    # Collect candidates — may run twice if first fails check digit
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
            print(f'[OCR] VIN via Gemini Pro (check digit OK): {vin}', flush=True)
            return vin

    # If no check-digit-valid VIN found, return the first candidate with a warning
    if candidates:
        print(f'[OCR] VIN via Gemini Pro (check digit FAILED, manual review needed): {candidates[0]}', flush=True)
        return candidates[0]
    return None


def extract_mileage_from_file(file_bytes, media_type='image/jpeg'):
    """Extract odometer mileage. Google Vision first, Claude fallback."""
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

        # First try: numbers explicitly labeled "mi" / "miles" / "km"
        labeled = re.findall(r'(\d{1,3}(?:,\d{3})+|\d{3,7})\s*(?:MI|MILES|KM)\b', up)
        if labeled:
            for c in labeled:
                n = int(c.replace(',', ''))
                if 100 <= n <= 999999:
                    print(f'[OCR] Mileage via Google Vision (labeled): {n}', flush=True)
                    return n

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
    print('[OCR] Google Vision missed mileage, falling back to Claude', flush=True)

    # Fallback to Gemini Flash
    result = gemini_call(ODO_PROMPT, image_bytes=file_bytes, mime=media_type,
                         model='gemini-2.5-flash', max_tokens=50)
    if result:
        result = result.strip().upper()
        if result != 'NONE':
            digits = re.sub(r'[^\d]', '', result)
            if digits:
                n = int(digits)
                if 100 <= n <= 999999:
                    print(f'[OCR] Mileage via Gemini Flash: {n}', flush=True)
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
    """Run Carfax extraction on multiple images, merge results (first non-null wins)."""
    merged = {}
    fields = ['vin', 'year', 'make', 'model', 'trim', 'mileage',
              'title_status', 'accidents', 'owners', 'color', 'asking_price']
    for file_bytes, media_type in files_list:
        info = extract_carfax_info(file_bytes, media_type)
        for f in fields:
            if not merged.get(f) and info.get(f) is not None:
                merged[f] = info[f]
        # If we have VIN + mileage, good enough to stop early
        if merged.get('vin') and merged.get('mileage'):
            break
    return merged


# ── Helpers ──────────────────────────────────────────────────────────────────

def send_sms(to, body):
    """Send SMS via Twilio. Returns True on success, False on failure. Never raises."""
    if not to or to.startswith('field:') or not TWILIO_SID or not TWILIO_TOKEN or not TWILIO_PHONE:
        return False
    try:
        client = TwilioClient(TWILIO_SID, TWILIO_TOKEN)
        client.messages.create(to=to, from_=TWILIO_PHONE, body=body)
        return True
    except Exception as e:
        print(f'SMS send error: {e}')
        return False


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

    cur.execute("SELECT status, COUNT(*) as cnt FROM bids GROUP BY status")
    stats = {'new': 0, 'reviewing': 0, 'bid_sent': 0, 'passed': 0, 'bought': 0, 'total': 0}
    for r in cur.fetchall():
        stats[r['status']] = int(r['cnt'])
        stats['total'] += int(r['cnt'])

    cur.execute("SELECT COUNT(*) as cnt FROM bids WHERE created_at::date = CURRENT_DATE")
    stats['today'] = int(cur.fetchone()['cnt'])

    cur.execute("SELECT COUNT(*) as cnt FROM bids WHERE phone LIKE 'field:%'")
    stats['field'] = int(cur.fetchone()['cnt'])

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
        SELECT b.*, c.name as contact_name, c.company as contact_company
        FROM bids b LEFT JOIN contacts c ON b.contact_id = c.id
        {where}
        ORDER BY b.created_at DESC LIMIT 200
    """
    cur.execute(q.format(where=where), params)
    bids = cur.fetchall()

    cur.execute("SELECT bid_id, COUNT(*) as cnt FROM bid_photos GROUP BY bid_id")
    photo_counts = {r['bid_id']: int(r['cnt']) for r in cur.fetchall()}

    cur.execute("SELECT bid_id FROM vauto_lookups")
    vauto_done = {r['bid_id'] for r in cur.fetchall()}

    db.close()
    return render_template('index.html', bids=bids, stats=stats,
                           status_filter=status_filter, rep_filter=rep_filter,
                           reps=reps, photo_counts=photo_counts,
                           vauto_done=vauto_done, time_ago=time_ago)


@app.route('/bid/<int:bid_id>')
def bid_detail(bid_id):
    db = get_db()
    cur = db.cursor()

    cur.execute("""
        SELECT b.*, c.name as contact_name, c.company as contact_company
        FROM bids b LEFT JOIN contacts c ON b.contact_id = c.id
        WHERE b.id = %s
    """, (bid_id,))
    bid = cur.fetchone()
    if not bid:
        db.close()
        return 'Not found', 404

    cur.execute("SELECT * FROM bid_photos WHERE bid_id = %s ORDER BY id", (bid_id,))
    photos = cur.fetchall()

    cur.execute("SELECT * FROM bid_messages WHERE bid_id = %s ORDER BY created_at", (bid_id,))
    messages = cur.fetchall()

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
            fields = ', '.join(f'{k}=%s' for k in decoded)
            cur.execute(f"UPDATE bids SET {fields} WHERE id=%s", list(decoded.values()) + [bid_id])
            db.commit()
            bid = dict(bid)
            bid.update(decoded)

    # vAuto lookup data
    cur.execute("SELECT * FROM vauto_lookups WHERE bid_id = %s", (bid_id,))
    vauto_data = cur.fetchone()

    # AccuTrade lookup data
    accutrade_data = None
    try:
        cur.execute("SELECT * FROM accutrade_lookups WHERE bid_id = %s", (bid_id,))
        accutrade_data = cur.fetchone()
    except Exception:
        pass

    # iPacket sticker data
    ipacket_data = None
    try:
        cur.execute("SELECT * FROM ipacket_lookups WHERE bid_id = %s", (bid_id,))
        ipacket_data = cur.fetchone()
    except Exception:
        pass

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

    db.close()
    return render_template('bid.html', bid=bid, photos=photos,
                           messages=messages, valuations=valuations,
                           vauto_data=vauto_data,
                           accutrade_data=accutrade_data,
                           ipacket_data=ipacket_data,
                           tesla_data=tesla_data,
                           ai_assessment=bid.get('ai_assessment'),
                           time_ago=time_ago)


@app.route('/webhook/twilio', methods=['POST'])
def twilio_webhook():
    from_phone = request.form.get('From', '')
    body = request.form.get('Body', '').strip()
    num_media = int(request.form.get('NumMedia', 0))

    db = get_db()
    cur = db.cursor()

    # ── Check if this is a reply to a shared bid ──
    # Find the most recent bid shared to this phone number
    cur.execute("""
        SELECT bid_id FROM share_sent
        WHERE phone = %s
        ORDER BY sent_at DESC LIMIT 1
    """, (from_phone,))
    share_row = cur.fetchone()

    if share_row:
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
            if media_url:
                cur.execute("INSERT INTO bid_photos (bid_id, url) VALUES (%s, %s)",
                            (shared_bid_id, media_url))

        cur.execute("UPDATE bids SET updated_at=NOW(), has_unread=TRUE WHERE id=%s", (shared_bid_id,))
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

    # Extract VIN from text
    vin = extract_vin_from_text(body) if body else None

    # Create bid record
    cur.execute("""
        INSERT INTO bids (contact_id, phone, vin, raw_message, status)
        VALUES (%s, %s, %s, %s, 'new') RETURNING id
    """, (contact_id, from_phone, vin, body))
    bid_id = cur.fetchone()['id']

    # Store inbound message
    if body:
        cur.execute("""
            INSERT INTO bid_messages (bid_id, direction, message, from_phone)
            VALUES (%s, 'inbound', %s, %s)
        """, (bid_id, body, from_phone))

    # Handle photos
    for i in range(num_media):
        media_url = request.form.get(f'MediaUrl{i}')
        media_type = request.form.get(f'MediaContentType{i}', '')
        if not media_url:
            continue

        photo_vin = None
        if 'image' in media_type and ANTHROPIC_KEY:
            photo_vin = extract_vin_from_photo(media_url)
            if photo_vin and not vin:
                vin = photo_vin
                cur.execute("UPDATE bids SET vin=%s WHERE id=%s", (vin, bid_id))

        cur.execute("""
            INSERT INTO bid_photos (bid_id, url, vin_extracted)
            VALUES (%s, %s, %s)
        """, (bid_id, media_url, photo_vin))

    # Decode VIN → populate vehicle fields
    if vin:
        decoded = decode_vin(vin)
        if decoded:
            fields = ', '.join(f'{k}=%s' for k in decoded)
            cur.execute(f"UPDATE bids SET {fields} WHERE id=%s",
                        list(decoded.values()) + [bid_id])
        cur.execute("UPDATE bids SET vauto_priority=TRUE WHERE id=%s", (bid_id,))

    db.commit()
    db.close()

    # Background market check
    if vin:
        trigger_market_check(bid_id, vin)

    return ('<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
            200, {'Content-Type': 'text/xml'})


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

    db.close()
    return jsonify({'success': True})


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
    """Launch background thread to run Playwright market check."""
    if not vin or len(vin) != 17:
        return
    import threading
    t = threading.Thread(target=_run_market_check_playwright, args=(bid_id, vin), daemon=True)
    t.start()


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

    cur.execute("SELECT url FROM bid_photos WHERE bid_id = %s ORDER BY id LIMIT 8", (bid_id,))
    photos = cur.fetchall()

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

    # ── Build vehicle context ─────────────────────────────────────────────────
    vparts = [str(bid['year'] or ''), bid['make'] or '', bid['model'] or '', bid['trim'] or '']
    vehicle_str = ' '.join(p for p in vparts if p).strip() or 'Unknown vehicle'
    mileage_str = '{:,} miles'.format(bid['mileage']) if bid['mileage'] else 'unknown mileage'
    asking_str = '${:,.0f}'.format(bid['asking_price']) if bid.get('asking_price') else 'not stated'

    ctx = f"VEHICLE: {vehicle_str}\nVIN: {bid['vin'] or 'N/A'}\nMileage: {mileage_str}\n"
    ctx += f"Color: {bid['color'] or 'N/A'}\nAsking price: {asking_str}\n"
    ctx += f"Notes / condition: {bid['notes'] or 'None'}\n"

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

    # Market check
    mc = bid.get('market_check') or {}
    if mc:
        ctx += "\nCURRENT MARKET LISTINGS:\n"
        for key, label in [('autotrader','Autotrader'),('cars_com','Cars.com'),('cargurus','CarGurus')]:
            if key in mc:
                found = mc[key].get('found')
                ctx += f"  {label}: {'found listed' if found else 'not listed'}\n"

    # DIA data — exact VIN history
    dia = dia_vin_lookup(bid['vin']) if bid['vin'] else None
    if dia and dia['dealers']:
        ctx += f"\nTHIS VEHICLE — DEALER HISTORY ({len(dia['dealers'])} records):\n"
        for d in dia['dealers'][:6]:
            price = '${:,.0f}'.format(d['price']) if d['price'] else 'N/A'
            ctx += f"  {d['dealer_name']} ({d['city']}, {d['state']}) — {d['status']} — {price} — last seen {d['last_seen'] or '?'}\n"
    if dia and dia['auctions']:
        ctx += f"\nTHIS VEHICLE — AUCTION HISTORY ({len(dia['auctions'])} records):\n"
        for a in dia['auctions'][:6]:
            price = '${:,.0f}'.format(a['hammer_price']) if a['hammer_price'] else 'N/A'
            sold = 'Sold' if a['sold'] else 'Not sold'
            ctx += f"  {a['house_name']} — {sold} at {price} — {a['sale_date'] or '?'}\n"

    # DIA data — comparable vehicles (same make/model, ±1 year, active with prices)
    if bid['make'] and bid['model']:
        try:
            dia_conn = psycopg2.connect(DIA_DB_URL,
                                        cursor_factory=psycopg2.extras.RealDictCursor,
                                        connect_timeout=5)
            dia_cur = dia_conn.cursor()
            bid_year = bid['year'] or 2023
            bid_miles = bid['mileage'] or 50000
            mile_low = max(0, bid_miles - 25000)
            mile_high = bid_miles + 25000

            # Dealer comps — VIN prefix match (same model/body/trim), same year, must have URL
            vin_prefix = (bid['vin'] or '')[:8]
            dia_cur.execute("""
                SELECT i.vin, i.price, i.mileage, i.year, i.trim, i.url,
                       d.city, d.state, d.name as dealer_name
                FROM inventory i
                JOIN dealers d ON i.dealer_id = d.dealer_id
                WHERE LEFT(i.vin, 8) = %s
                  AND i.year::int = %s
                  AND i.status = 'active' AND i.price > 0
                  AND i.url IS NOT NULL AND i.url != ''
                ORDER BY ABS(i.mileage - %s) ASC
                LIMIT 10
            """, (vin_prefix, bid_year, bid_miles))
            comps = dia_cur.fetchall()

            # Fall back to make/model if VIN prefix found < 3
            if len(comps) < 3:
                dia_cur.execute("""
                    SELECT i.vin, i.price, i.mileage, i.year, i.trim, i.url,
                           d.city, d.state, d.name as dealer_name
                    FROM inventory i
                    JOIN dealers d ON i.dealer_id = d.dealer_id
                    WHERE i.make ILIKE %s AND i.model ILIKE %s
                      AND i.year::int = %s
                      AND i.status = 'active' AND i.price > 0
                      AND i.url IS NOT NULL AND i.url != ''
                    ORDER BY ABS(i.mileage - %s) ASC
                    LIMIT 10
                """, (bid['make'], bid['model'], bid_year, bid_miles))
                comps = dia_cur.fetchall()

            # Auction comps — VIN prefix, same year
            dia_cur.execute("""
                SELECT al.hammer_price::numeric as hammer_price, al.sale_date,
                       al.year, al.mileage, ah.name as house_name
                FROM auction_lots al
                JOIN auction_houses ah ON al.house_id = ah.id
                WHERE LEFT(al.vin, 8) = %s
                  AND al.year ~ '^\d+$' AND al.year::int = %s
                  AND al.sold = 'true' AND al.hammer_price ~ '^\d+\.?\d*$'
                  AND al.hammer_price::numeric > 0
                ORDER BY al.sale_date DESC NULLS LAST
                LIMIT 10
            """, (vin_prefix, bid_year))
            auction_comps = dia_cur.fetchall()

            dia_conn.close()

            if comps:
                prices = [float(c['price']) for c in comps]
                avg_price = sum(prices) / len(prices)
                ctx += f"\nRETAIL COMPS — {len(comps)} similar {bid['make']} {bid['model']} (±1 year, ±25K miles):\n"
                ctx += f"  Price range: ${min(prices):,.0f} — ${max(prices):,.0f} (avg ${avg_price:,.0f})\n"
                for c in comps:
                    trim = f" {c['trim']}" if c.get('trim') else ''
                    miles = f"{int(c['mileage']):,} mi" if c.get('mileage') else '? mi'
                    ctx += f"  VIN: {c.get('vin','?')} | {c['year']}{trim} | {c.get('city','')}, {c.get('state','')} | {miles} | ${float(c['price']):,.0f}\n"

            if auction_comps:
                auc_prices = [float(a['hammer_price']) for a in auction_comps]
                avg_auc = sum(auc_prices) / len(auc_prices)
                ctx += f"\nAUCTION COMPS — {len(auction_comps)} similar, recently sold:\n"
                ctx += f"  Hammer range: ${min(auc_prices):,.0f} — ${max(auc_prices):,.0f} (avg ${avg_auc:,.0f})\n"
                for a in auction_comps:
                    miles_str = f"{int(float(a['mileage'])):,} mi" if a.get('mileage') else '? mi'
                    ctx += f"  {a.get('year','')} | {a['house_name']} | ${float(a['hammer_price']):,.0f} | {a.get('sale_date','?')} | {miles_str}\n"

        except Exception as e:
            print(f'DIA comps error: {e}')

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
    report_count = 0
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
                        report_count += 1
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

    # ── Load AccuTrade screenshot ───────────────────────────────────────────
    accutrade_report = 0
    if accutrade and accutrade.get('screenshot'):
        report_path = accutrade['screenshot']
        try:
            if report_path.startswith('/accutrade_reports/'):
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
                content.append({'type': 'text', 'text': '\n--- ACCUTRADE APPRAISAL (screenshot) ---'})
                content.append({'type': 'image', 'source': {
                    'type': 'base64', 'media_type': media_type,
                    'data': base64.standard_b64encode(img_bytes).decode()
                }})
                accutrade_report = 1
                ctx += "\nACCUTRADE APPRAISAL: screenshot attached below\n"
        except Exception as e:
            print(f'assess AccuTrade screenshot error: {e}')

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

    prompt = f"""{ctx}

{img_line}

Based on all the data above — book values, photos, Carfax, AutoCheck, AccuTrade, iPacket OEM sticker, history, and market listings — what should we pay for this vehicle at wholesale?

IMPORTANT — read Carfax/AutoCheck carefully and be FACTUALLY ACCURATE:
- If the report says "sideswipe" or "left/right side impact", that is a SIDE collision — do NOT call it a front-end or rear-end accident
- If it says "minor damage", do not describe it as major
- Quote the exact damage description from the report when possible
- Do not invent facts that are not in the reports

Keep it under 200 words. End with:
Max wholesale buy price: **$X,XXX**"""

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
            config=_gtypes.GenerateContentConfig(max_output_tokens=3000, temperature=0.4),
        )
        assessment = (resp.text or '').strip()
        if not assessment:
            raise RuntimeError('Empty Gemini response')
        print(f'[ASSESS] Bid {bid_id} via Gemini 2.5 Pro ({len(assessment)} chars)', flush=True)

        # Extract buy price for dashboard column (look for $X,XXX pattern near "Max wholesale")
        import re as _re
        price_match = _re.search(r'Max wholesale buy price[^\$]*\$([0-9,]+)', assessment, _re.IGNORECASE)
        buy_price = int(price_match.group(1).replace(',', '')) if price_match else None

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
            else:
                print(f'Auto-assess failed for bid {bid_id}: {result.get("error")}')
    except Exception as e:
        print(f'Auto-assess error for bid {bid_id}: {e}')


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
               b.has_unread,
               c.name as contact_name, c.company as contact_company
        FROM bids b LEFT JOIN contacts c ON b.contact_id = c.id
        {where}
        ORDER BY b.created_at DESC LIMIT 200
    """
    cur.execute(q.format(where=where), params)

    bids = []
    for r in cur.fetchall():
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
            'asking_price': float(r['asking_price']) if r['asking_price'] else None,
            'ai_price': float(r['ai_price']) if r['ai_price'] else None,
            'bid_amount': float(r['bid_amount']) if r['bid_amount'] else None,
            'is_field': r['phone'].startswith('field:'),
            'is_new': r['id'] > since_id,
            'has_unread': bool(r.get('has_unread'))
        })

    cur.execute("SELECT bid_id, COUNT(*) as cnt FROM bid_photos GROUP BY bid_id")
    photo_counts = {r['bid_id']: int(r['cnt']) for r in cur.fetchall()}

    # vAuto lookup status per bid
    cur.execute("SELECT bid_id FROM vauto_lookups")
    vauto_done = {r['bid_id'] for r in cur.fetchall()}

    db.close()
    return jsonify({'bids': bids, 'stats': stats, 'photo_counts': photo_counts,
                    'vauto_done': list(vauto_done)})


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
    return render_template('mobile.html', rep_name='', force_setup=force_setup)


@app.route('/api/mobile-submit', methods=['POST'])
def mobile_submit():
    os.makedirs(UPLOAD_DIR, exist_ok=True)

    rep_name = request.form.get('rep_name', '').strip()
    notes = request.form.get('notes', '').strip()
    asking_price_raw = request.form.get('asking_price', '').strip()
    manual_vin = request.form.get('manual_vin', '').strip().upper()
    manual_mileage_raw = request.form.get('manual_mileage', '').strip()

    asking_price = None
    if asking_price_raw:
        try:
            asking_price = float(asking_price_raw)
        except ValueError:
            pass

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

    # --- Detect color from first car photo ---
    detected_color = None
    if ANTHROPIC_KEY and car_photo_urls:
        try:
            first_photo_path = os.path.join(UPLOAD_DIR, os.path.basename(car_photo_urls[0]))
            with open(first_photo_path, 'rb') as fp:
                detected_color = extract_color_from_file(fp.read())
        except Exception:
            pass

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

    # --- DB insert ---
    db = get_db()
    cur = db.cursor()

    # Use a placeholder phone for field reps (no phone number)
    rep_phone = f'field:{rep_name.replace(" ", "_").lower() or "rep"}'

    cur.execute("""
        INSERT INTO contacts (phone, name)
        VALUES (%s, %s)
        ON CONFLICT (phone) DO UPDATE SET name = EXCLUDED.name
        RETURNING id
    """, (rep_phone, rep_name or None))
    contact_id = cur.fetchone()['id']

    cur.execute("""
        INSERT INTO bids (contact_id, phone, vin, mileage, year, make, model, trim, color,
                          raw_message, asking_price, notes, status, has_unread)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'new', TRUE) RETURNING id
    """, (contact_id, rep_phone, vin, mileage,
          decoded_vin.get('year'), decoded_vin.get('make'),
          decoded_vin.get('model'), decoded_vin.get('trim'),
          detected_color,
          raw_message, asking_price,
          f'[Field: {rep_name}] {notes}'.strip() if notes else f'[Field: {rep_name}]'))

    bid_id = cur.fetchone()['id']

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

    # Background market check
    if vin:
        trigger_market_check(bid_id, vin)

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
    cur.execute("""
        SELECT b.id, b.vin, b.year, b.make, b.model, b.mileage, b.status,
               b.created_at, b.bid_amount, b.bid_response,
               (SELECT url FROM bid_photos WHERE bid_id = b.id ORDER BY id LIMIT 1) AS first_photo
        FROM bids b
        WHERE b.phone = %s
        ORDER BY b.created_at DESC LIMIT 30
    """, (rep_phone,))
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
    """Return dashboard visitors and field reps seen in the last 2 minutes."""
    db = get_db()
    cur = db.cursor()
    # Dashboard viewers (by IP)
    cur.execute("""
        SELECT ip, last_seen FROM dashboard_visitors
        WHERE last_seen > NOW() - INTERVAL '2 minutes'
        ORDER BY last_seen DESC
    """)
    viewers = [{'ip': r['ip'],
                'last_seen': r['last_seen'].isoformat() if r['last_seen'] else None}
               for r in cur.fetchall()]
    # Field reps online
    cur.execute("""
        SELECT name, last_ip, last_seen FROM contacts
        WHERE phone LIKE 'field:%%' AND last_seen > NOW() - INTERVAL '2 minutes'
        ORDER BY name
    """)
    reps = [{'name': r['name'], 'ip': r.get('last_ip', ''),
             'last_seen': r['last_seen'].isoformat() if r['last_seen'] else None}
            for r in cur.fetchall()]
    db.close()
    return jsonify({'viewers': viewers, 'reps': reps})


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


SCRAPFLY_KEY = os.environ.get('SCRAPFLY_API_KEY', 'scp-live-e6c86de6355844f79af1d49495f0bdef')


def verify_comp(url):
    """Check comp URL via ScrapFly (JS rendering) for price + mileage, with plain fallback."""
    if not url:
        return {'status': 'no_url'}
    try:
        # Try ScrapFly first for JS-rendered content (gets mileage)
        try:
            sf = requests.get('https://api.scrapfly.io/scrape', params={
                'key': SCRAPFLY_KEY,
                'url': url,
                'render_js': 'true',
                'rendering_wait': 8000,
                'auto_scroll': 'true',
            }, timeout=45)
            sf_data = sf.json().get('result', {})
            if sf_data.get('status_code') == 200:
                r_text = sf_data.get('content', '')
                r_status = 200
                r_url = sf_data.get('url', url)
                r_history = []
            else:
                raise Exception(f"ScrapFly status {sf_data.get('status_code')}")
        except Exception:
            # Fallback to plain requests
            r = requests.get(url, timeout=10, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }, allow_redirects=True)
            r_text = r.text
            r_status = r.status_code
            r_url = r.url
            r_history = r.history

        if r_status == 404:
            return {'status': 'gone'}
        if r_status == 429 or r_status == 403:
            return {'status': 'active', 'live_price': None, 'live_mileage': None}
        if r_status != 200:
            return {'status': 'error', 'code': r_status}

        text = r_text.lower()

        # Check if redirected to homepage (car removed)
        if len(r_history) > 0 and ('inventory' not in r_url.lower() and 'vehicle' not in r_url.lower() and 'vin' not in r_url.lower()):
            return {'status': 'gone'}

        # Check for sold indicators
        sold_phrases = ['this vehicle has been sold', 'this vehicle has sold',
                        'no longer available', 'vehicle sold', 'has sold',
                        'this listing has ended', 'sorry, this vehicle', 'has been removed',
                        'vehicle is no longer', 'already been sold', 'is no longer available',
                        'vehicle not found', 'listing not found']
        for phrase in sold_phrases:
            if phrase in text:
                return {'status': 'sold'}

        import re as _re
        from collections import Counter

        # Extract price — most frequently occurring reasonable dollar amount
        all_prices = _re.findall(r'\$\s?([\d,]{5,10})', r_text)
        all_prices += _re.findall(r'"price"\s*:\s*"?([\d,]{5,10})', r_text, _re.IGNORECASE)
        all_prices += _re.findall(r'data-price[=:"\']+\s*([\d,]{5,10})', r_text, _re.IGNORECASE)
        parsed_prices = []
        for p in all_prices:
            try:
                val = int(p.replace(',', ''))
                if 2000 < val < 500000:
                    parsed_prices.append(val)
            except ValueError:
                pass
        price = Counter(parsed_prices).most_common(1)[0][0] if parsed_prices else None

        # Extract mileage — multiple patterns
        mileage = None
        mile_patterns = [
            _re.compile(r'[Mm]ileage[:\s]+(\d[\d,]*)', _re.IGNORECASE),       # Mileage: 35103
            _re.compile(r'[Oo]dometer[:\s]+(\d[\d,]*)', _re.IGNORECASE),       # Odometer: 35103
            _re.compile(r'([\d,]+)\s*(?:mi\b|miles)', _re.IGNORECASE),         # 35,103 mi
            _re.compile(r'"mileage"[:\s]*"?(\d[\d,]*)', _re.IGNORECASE),       # "mileage":"35103"
            _re.compile(r'data-mileage[="\s]+(\d[\d,]*)', _re.IGNORECASE),     # data-mileage="35103"
        ]
        # Search both raw HTML and stripped visible text
        import re as _re2
        visible = _re2.sub(r'<[^>]+>', ' ', r_text)
        for search_text in [r_text, visible]:
            for pat in mile_patterns:
                match = pat.search(search_text)
                if match:
                    try:
                        m = int(match.group(1).replace(',', ''))
                        if 100 < m < 500000:
                            mileage = m
                            break
                    except ValueError:
                        pass
            if mileage:
                break

        return {'status': 'active', 'live_price': price, 'live_mileage': mileage}
    except requests.Timeout:
        return {'status': 'timeout'}
    except Exception:
        return {'status': 'error'}
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
        db.commit()
        db.close()
    except Exception:
        pass


_ensure_accutrade_table()


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
    """Return bids that need vAuto lookup. Priority bids first."""
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT b.id as bid_id, b.vin, b.mileage, b.year, b.make, b.model, b.trim,
               b.vauto_priority
        FROM bids b
        LEFT JOIN vauto_lookups vl ON vl.bid_id = b.id
        WHERE b.vin IS NOT NULL AND length(b.vin) = 17
          AND vl.id IS NULL
        ORDER BY b.vauto_priority DESC, b.created_at DESC
        LIMIT 20
    """)
    rows = cur.fetchall()
    # Don't clear priority here — clear it when worker submits results
    db.close()
    return jsonify({'pending': [dict(r) for r in rows]})


@app.route('/api/vauto/submit', methods=['POST'])
def api_vauto_submit():
    """Accept vAuto lookup results from worker."""
    data = request.json
    if not data or not data.get('bid_id'):
        return jsonify({'error': 'missing bid_id'}), 400

    db = get_db()
    cur = db.cursor()
    bid_id = data['bid_id']
    vin = data.get('vin', '')

    cur.execute("""
        INSERT INTO vauto_lookups
            (bid_id, vin, rbook, black_book, mmr, kbb, kbb_com, jd_power,
             title_status, price_rank, adj_pct_market,
             carfax_screenshot, autocheck_screenshot, raw_json, looked_up_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (bid_id) DO UPDATE SET
            vin=EXCLUDED.vin, rbook=EXCLUDED.rbook, black_book=EXCLUDED.black_book,
            mmr=EXCLUDED.mmr, kbb=EXCLUDED.kbb, kbb_com=EXCLUDED.kbb_com,
            jd_power=EXCLUDED.jd_power, title_status=EXCLUDED.title_status,
            price_rank=EXCLUDED.price_rank, adj_pct_market=EXCLUDED.adj_pct_market,
            carfax_screenshot=EXCLUDED.carfax_screenshot,
            autocheck_screenshot=EXCLUDED.autocheck_screenshot,
            raw_json=EXCLUDED.raw_json, looked_up_at=NOW()
    """, (
        bid_id, vin,
        data.get('rbook'), data.get('wholesale_avg'), data.get('mmr_val'),
        data.get('kbb_val'), data.get('kbb_com_val'), data.get('jd_power_val'),
        data.get('title_status'), data.get('price_rank'), data.get('adj_pct_market'),
        data.get('carfax_screenshot'), data.get('autocheck_screenshot'),
        json.dumps(data.get('raw', {})) if data.get('raw') else None,
    ))
    # Clear priority flag now that we have the data
    cur.execute("UPDATE bids SET vauto_priority=FALSE WHERE id=%s", (bid_id,))
    db.commit()
    db.close()

    # Auto-trigger AI assessment in background
    # NOTE: _live_scan_comps disabled for now — comps feature paused
    threading.Thread(target=_auto_assess, args=(bid_id,), daemon=True).start()

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
                # External CDN URL
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
             market_avg, local_comps, screenshot, raw_json, looked_up_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (bid_id) DO UPDATE SET
            vin=EXCLUDED.vin, guaranteed_offer=EXCLUDED.guaranteed_offer,
            trade_in=EXCLUDED.trade_in, trade_market=EXCLUDED.trade_market,
            retail=EXCLUDED.retail, market_avg=EXCLUDED.market_avg,
            local_comps=EXCLUDED.local_comps, screenshot=EXCLUDED.screenshot,
            raw_json=EXCLUDED.raw_json, looked_up_at=NOW()
    """, (
        bid_id, data.get('vin', ''),
        data.get('guaranteed_offer'), data.get('trade_in'),
        data.get('trade_market'), data.get('retail'),
        data.get('market_avg'),
        json.dumps(data.get('local_comps')) if data.get('local_comps') else None,
        data.get('screenshot'),
        json.dumps(data.get('raw', {})) if data.get('raw') else None,
    ))
    db.commit()
    db.close()
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
        return jsonify({'status': 'complete', 'data': d})
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
    db.close()
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

    # Get NHTSA decode if we have a VIN (supplements/overrides Carfax data)
    nhtsa = {}
    if vin and len(vin) == 17 and VIN_RE.match(vin):
        nhtsa = decode_vin(vin)

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

    cur.execute("""
        INSERT INTO bids (contact_id, phone, vin, year, make, model, trim, mileage, color,
                          raw_message, asking_price, notes, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'new') RETURNING id
    """, (contact_id, rep_phone, vin if vin and len(vin) == 17 else None,
          year, make, model, trim, mileage, color,
          raw_message, asking_price, full_notes))
    bid_id = cur.fetchone()['id']

    # Save photos
    for photo_url in saved_photos:
        cur.execute("INSERT INTO bid_photos (bid_id, url) VALUES (%s, %s)",
                    (bid_id, photo_url))

    # Flag for vAuto
    cur.execute("UPDATE bids SET vauto_priority=TRUE WHERE id=%s", (bid_id,))

    db.commit()
    db.close()

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

    db.close()
    return jsonify({'reports': reports, 'photos': photos})


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

    cur.execute("SELECT url FROM bid_photos WHERE bid_id = %s ORDER BY id", (bid['id'],))
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

    return render_template('share.html', bid=bid, photos=photos, vauto=vauto, accutrade=accutrade, ipacket=ipacket)


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=9000)
