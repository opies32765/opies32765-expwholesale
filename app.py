import os
import re
import base64
import uuid
import psycopg2
import psycopg2.extras
import requests
from datetime import datetime
from flask import Flask, render_template, request, jsonify, redirect, url_for
from twilio.rest import Client as TwilioClient

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'expwholesale2026!')

DB_URL = os.environ.get('DATABASE_URL', 'postgresql://expuser:ExpWholesale2026!@localhost/expwholesale')
TWILIO_SID = os.environ.get('TWILIO_ACCOUNT_SID', '')
TWILIO_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN', '')
TWILIO_PHONE = os.environ.get('TWILIO_PHONE', '')
ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
UPLOAD_DIR = os.environ.get('UPLOAD_DIR', '/opt/expwholesale/static/uploads')

VIN_RE = re.compile(r'\b[A-HJ-NPR-Z0-9]{17}\b')

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


# ── VIN extraction ───────────────────────────────────────────────────────────

def extract_vin_from_text(text):
    match = VIN_RE.search(text.upper())
    return match.group(0) if match else None


def extract_vin_from_photo(image_url):
    """Use Claude Vision to read a VIN from a photo (vehicle sticker, window, etc.)"""
    try:
        import anthropic
        resp = requests.get(image_url, auth=(TWILIO_SID, TWILIO_TOKEN), timeout=15)
        img_b64 = base64.standard_b64encode(resp.content).decode()
        media_type = resp.headers.get('Content-Type', 'image/jpeg').split(';')[0]

        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        msg = client.messages.create(
            model='claude-opus-4-6',
            max_tokens=100,
            messages=[{
                'role': 'user',
                'content': [
                    {'type': 'image', 'source': {'type': 'base64', 'media_type': media_type, 'data': img_b64}},
                    {'type': 'text', 'text': VIN_PROMPT}
                ]
            }]
        )
        result = msg.content[0].text.strip().upper()
        if VIN_RE.match(result):
            return result
    except Exception as e:
        print(f'VIN photo extract error: {e}')
    return None


def extract_vin_from_file(file_bytes, media_type='image/jpeg'):
    """Use Claude Vision to read a VIN from raw file bytes (mobile upload)."""
    try:
        import anthropic
        img_b64 = base64.standard_b64encode(file_bytes).decode()
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        msg = client.messages.create(
            model='claude-opus-4-6',
            max_tokens=100,
            messages=[{
                'role': 'user',
                'content': [
                    {'type': 'image', 'source': {'type': 'base64', 'media_type': media_type, 'data': img_b64}},
                    {'type': 'text', 'text': VIN_PROMPT}
                ]
            }]
        )
        result = msg.content[0].text.strip().upper()
        if VIN_RE.match(result):
            return result
    except Exception as e:
        print(f'VIN file extract error: {e}')
    return None


def extract_mileage_from_file(file_bytes, media_type='image/jpeg'):
    """Use Claude Vision to read odometer mileage from raw file bytes."""
    try:
        import anthropic
        img_b64 = base64.standard_b64encode(file_bytes).decode()
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        msg = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=50,
            messages=[{
                'role': 'user',
                'content': [
                    {'type': 'image', 'source': {'type': 'base64', 'media_type': media_type, 'data': img_b64}},
                    {'type': 'text', 'text': ODO_PROMPT}
                ]
            }]
        )
        result = msg.content[0].text.strip().upper()
        if result != 'NONE':
            digits = re.sub(r'[^\d]', '', result)
            if digits:
                return int(digits)
    except Exception as e:
        print(f'Mileage extract error: {e}')
    return None


# ── Helpers ──────────────────────────────────────────────────────────────────

def send_sms(to, body):
    client = TwilioClient(TWILIO_SID, TWILIO_TOKEN)
    client.messages.create(to=to, from_=TWILIO_PHONE, body=body)


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
    stats = {'new': 0, 'reviewing': 0, 'bid_sent': 0, 'passed': 0, 'total': 0}
    for r in cur.fetchall():
        stats[r['status']] = int(r['cnt'])
        stats['total'] += int(r['cnt'])

    cur.execute("SELECT COUNT(*) as cnt FROM bids WHERE created_at::date = CURRENT_DATE")
    stats['today'] = int(cur.fetchone()['cnt'])

    cur.execute("SELECT COUNT(*) as cnt FROM bids WHERE phone LIKE 'field:%'")
    stats['field'] = int(cur.fetchone()['cnt'])

    status_filter = request.args.get('status', 'all')
    q = """
        SELECT b.*, c.name as contact_name, c.company as contact_company
        FROM bids b LEFT JOIN contacts c ON b.contact_id = c.id
        {where}
        ORDER BY b.created_at DESC LIMIT 200
    """
    if status_filter == 'field':
        cur.execute(q.format(where="WHERE b.phone LIKE 'field:%'"))
    elif status_filter != 'all':
        cur.execute(q.format(where='WHERE b.status = %s'), (status_filter,))
    else:
        cur.execute(q.format(where=''))

    bids = cur.fetchall()

    cur.execute("SELECT bid_id, COUNT(*) as cnt FROM bid_photos GROUP BY bid_id")
    photo_counts = {r['bid_id']: int(r['cnt']) for r in cur.fetchall()}

    db.close()
    return render_template('index.html', bids=bids, stats=stats,
                           status_filter=status_filter, photo_counts=photo_counts,
                           time_ago=time_ago)


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
        cur.execute("UPDATE bids SET status='reviewing', updated_at=NOW() WHERE id=%s", (bid_id,))
        db.commit()

    db.close()
    return render_template('bid.html', bid=bid, photos=photos,
                           messages=messages, valuations=valuations, time_ago=time_ago)


@app.route('/webhook/twilio', methods=['POST'])
def twilio_webhook():
    from_phone = request.form.get('From', '')
    body = request.form.get('Body', '').strip()
    num_media = int(request.form.get('NumMedia', 0))

    db = get_db()
    cur = db.cursor()

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

    db.commit()
    db.close()

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

    cur.execute("SELECT phone FROM bids WHERE id=%s", (bid_id,))
    bid = cur.fetchone()
    if not bid:
        db.close()
        return jsonify({'error': 'Not found'}), 404

    send_sms(bid['phone'], message)

    new_status = 'passed' if action == 'pass' else 'bid_sent'
    bid_amount = data.get('bid_amount')

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
    return jsonify({'success': True, 'status': new_status})


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
    db = get_db()
    cur = db.cursor()

    cur.execute("SELECT status, COUNT(*) as cnt FROM bids GROUP BY status")
    stats = {'new': 0, 'reviewing': 0, 'bid_sent': 0, 'passed': 0, 'total': 0}
    for r in cur.fetchall():
        stats[r['status']] = int(r['cnt'])
        stats['total'] += int(r['cnt'])
    cur.execute("SELECT COUNT(*) as cnt FROM bids WHERE created_at::date = CURRENT_DATE")
    stats['today'] = int(cur.fetchone()['cnt'])
    cur.execute("SELECT COUNT(*) as cnt FROM bids WHERE phone LIKE 'field:%'")
    stats['field'] = int(cur.fetchone()['cnt'])

    q = """
        SELECT b.id, b.phone, b.vin, b.year, b.make, b.model, b.mileage,
               b.raw_message, b.status, b.created_at,
               c.name as contact_name, c.company as contact_company
        FROM bids b LEFT JOIN contacts c ON b.contact_id = c.id
        {where}
        ORDER BY b.created_at DESC LIMIT 200
    """
    if status_filter == 'field':
        cur.execute(q.format(where="WHERE b.phone LIKE 'field:%'"))
    elif status_filter != 'all':
        cur.execute(q.format(where='WHERE b.status = %s'), (status_filter,))
    else:
        cur.execute(q.format(where=''))

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
            'is_field': r['phone'].startswith('field:'),
            'is_new': r['id'] > since_id
        })

    cur.execute("SELECT bid_id, COUNT(*) as cnt FROM bid_photos GROUP BY bid_id")
    photo_counts = {r['bid_id']: int(r['cnt']) for r in cur.fetchall()}

    db.close()
    return jsonify({'bids': bids, 'stats': stats, 'photo_counts': photo_counts})


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


def resize_for_claude(file_bytes, max_bytes=4_000_000, max_dim=1600):
    """Resize image so it fits under Claude's 5MB limit."""
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
    """Check whether an uploaded photo looks like a vehicle or vehicle interior."""
    f = request.files.get('photo')
    if not f or not ANTHROPIC_KEY:
        return jsonify({'ok': True})  # fail open if no key

    file_bytes = f.read()
    media_type = f.mimetype or 'image/jpeg'

    # Resize before sending to Claude (5MB hard limit)
    try:
        file_bytes, media_type = resize_for_claude(file_bytes)
    except Exception as e:
        print(f'resize error: {e}')

    try:
        import anthropic
        img_b64 = base64.standard_b64encode(file_bytes).decode()
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        msg = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=60,
            messages=[{
                'role': 'user',
                'content': [
                    {'type': 'image', 'source': {'type': 'base64', 'media_type': media_type, 'data': img_b64}},
                    {'type': 'text', 'text': (
                        'In one sentence, describe the main subject of this photo. '
                        'Then on a new line write VEHICLE or NOT_VEHICLE.\n\n'
                        'Write VEHICLE only if the photo shows the physical body, paint, '
                        'interior cabin, seats, dashboard, engine bay, tires, or wheels '
                        'of a real automobile (car, truck, SUV, or van).\n\n'
                        'Write NOT_VEHICLE for everything else, including: '
                        'people, selfies, computer screens, websites, computer hardware, '
                        'electronics, food, buildings, documents, or any non-automotive object.'
                    )}
                ]
            }]
        )
        raw = msg.content[0].text.strip().upper()
        ok = 'VEHICLE' in raw and 'NOT_VEHICLE' not in raw
        print(f'verify-photo: "{msg.content[0].text.strip()}" → ok={ok}')
        # Extract a human-readable reason for the rejection
        lines = msg.content[0].text.strip().split('\n')
        description = lines[0] if lines else 'Unknown'
        return jsonify({'ok': ok, 'description': description})
    except Exception as e:
        print(f'verify-photo error: {e}')
        return jsonify({'ok': True})  # fail open on error


@app.route('/api/quick-extract', methods=['POST'])
def quick_extract():
    """Extract VIN or mileage from a single uploaded photo immediately."""
    extract_type = request.form.get('type', 'vin')  # 'vin' or 'odo'
    f = request.files.get('photo')
    if not f:
        return jsonify({'error': 'No photo'}), 400
    if not ANTHROPIC_KEY:
        return jsonify({'error': 'No API key'}), 500

    file_bytes = f.read()
    media_type = f.mimetype or 'image/jpeg'
    try:
        file_bytes, media_type = resize_for_claude(file_bytes)
    except Exception as e:
        print(f'resize error (extract): {e}')

    try:
        import anthropic
        img_b64 = base64.standard_b64encode(file_bytes).decode()
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

        if extract_type == 'vin':
            prompt = VIN_PROMPT
        else:
            prompt = ODO_PROMPT

        model = 'claude-opus-4-6' if extract_type == 'vin' else 'claude-sonnet-4-6'
        msg = client.messages.create(
            model=model,
            max_tokens=100,
            messages=[{
                'role': 'user',
                'content': [
                    {'type': 'image', 'source': {'type': 'base64', 'media_type': media_type, 'data': img_b64}},
                    {'type': 'text', 'text': prompt}
                ]
            }]
        )
        result = msg.content[0].text.strip().upper()

        if extract_type == 'vin':
            if VIN_RE.match(result):
                return jsonify({'success': True, 'value': result})
            return jsonify({'success': False, 'raw': result})
        else:
            if result != 'NONE':
                digits = re.sub(r'[^\d]', '', result)
                if digits and 100 <= int(digits) <= 999999:
                    return jsonify({'success': True, 'value': int(digits)})
            return jsonify({'success': False, 'raw': result})

    except Exception as e:
        print(f'quick-extract error: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/privacy')
def privacy():
    return render_template('privacy.html')

@app.route('/terms')
def terms():
    return render_template('terms.html')

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
        INSERT INTO bids (contact_id, phone, vin, mileage, raw_message,
                          bid_amount, notes, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'new') RETURNING id
    """, (contact_id, rep_phone, vin, mileage, raw_message, asking_price,
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

    db.commit()
    db.close()

    return jsonify({
        'success': True,
        'bid_id': bid_id,
        'vin': vin,
        'mileage': mileage,
        'asking_price': asking_price,
        'photos': len(all_photos)
    })


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=9000)
