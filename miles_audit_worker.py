#!/usr/bin/env python3
"""miles_audit_worker — extract odometer readings from Carfax + AutoCheck
screenshots via Google Vision OCR, compare to bids.mileage, flag for
operator verification when customer under-reported by > +2,000 miles.

Runs as a standalone systemd unit on C1 — does NOT touch gunicorn. Idempotent
via bids.miles_audit_at column. Asymmetric: only flags when higher-of-two >
customer + 2000 (under-reporting).
"""
import os
import re
import sys
import time
import traceback

import psycopg2
import psycopg2.extras

DB_URL = os.environ.get(
    'DATABASE_URL',
    'postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale')

DELTA_OVER = int(os.environ.get('MILES_AUDIT_DELTA_OVER', '2000'))
POLL_INTERVAL_S = int(os.environ.get('MILES_AUDIT_POLL_S', '30'))
# Only audit bids newer than this many days. Keeps the backfill window
# bounded so we don't suddenly flag thousands of historical bids the
# operator has already moved past. New bids get audited within ~60s of
# Phase 1 completing regardless of this cap.
MAX_AGE_DAYS = int(os.environ.get('MILES_AUDIT_MAX_AGE_DAYS', '7'))

GOOGLE_VISION_KEY_PATH = os.environ.get(
    'GOOGLE_VISION_KEY_PATH',
    '/opt/expwholesale/google_vision_key.json')

# Internal endpoint for miles-discrepancy SMS (Rule 3).
EW_INTERNAL_BASE = os.environ.get(
    'EW_INTERNAL_BASE', 'https://experience-wholesale.net').rstrip('/')
EW_INTERNAL_SECRET = os.environ.get(
    'EW_VAUTO_REFRESH_SECRET',
    '72bb9c82c4fb8d72220cdff8292afb7d1e8cc73bd073c67a5d4c3b4e1ed0a420')


def _resolve_screenshot_path(p):
    if not p:
        return None
    if p.startswith('/opt/'):
        return p
    if p.startswith('/'):
        return '/opt/expwholesale' + p
    return '/opt/expwholesale/' + p


def _google_vision_ocr(file_bytes):
    if not os.path.exists(GOOGLE_VISION_KEY_PATH):
        return None
    try:
        from google.cloud import vision
        client = vision.ImageAnnotatorClient.from_service_account_json(
            GOOGLE_VISION_KEY_PATH)
        image = vision.Image(content=file_bytes)
        response = client.text_detection(image=image)
        if response.error.message:
            print(f'[gv-ocr] error: {response.error.message}', flush=True)
            return None
        if not response.text_annotations:
            return None
        return response.text_annotations[0].description
    except Exception as e:
        print(f'[gv-ocr] call failed: {e}', flush=True)
        return None


# Comma-format mileage with mi/miles suffix. Requires comma to skip service
# interval markers ("50000 mi service", "100,000-mile maintenance" — the
# bare "50000" is service-interval; "50,000-mile" usually is too but we need
# at least the comma signal that this is a number-with-thousands not an ID).
# Actual odometer readings are non-round numbers like "53,573" or "23,599".
_MI_SUFFIX_RE = re.compile(
    r'(\d{1,3}(?:,\d{3})+)\s*(?:mi|miles)\b', re.IGNORECASE)

# Reject mi-suffix matches whose preceding context shows these phrases —
# they indicate warranty-remaining, service-interval, marketing-average, or
# projection text, not actual odometer readings.
_MI_REJECT_CONTEXT = (
    'remaining', 'to go', 'left', 'until', 'next service', 'due in',
    'due at', 'warranty', 'estimated to', 'projected', 'expected',
    'every',  # "every 5,000 miles"
    'each',
    # Carfax marketing text:
    #   "than the industry average of 15,000"
    #   "assumes an average of 15,000 miles driven per year"
    'average of', 'industry average', 'per year', 'driven per',
    'annual', 'yearly', 'a year',
    # Service-interval narratives ("recommended service at 30,000 miles")
    'recommended', 'service at', 'service due',
)

# AutoCheck's prominent labeled line. Most authoritative when present.
_AUTOCHECK_LAST_REPORTED_RE = re.compile(
    r'Last\s+[Rr]eported\s+[Oo]dometer\s*:?\s*(\d{1,3}(?:,\d{3})+|\d{3,7})',
    re.IGNORECASE,
)

# Generic odometer-labeled context
_ODOMETER_LABEL_RE = re.compile(
    r'\b[Oo]dometer\s+[Rr]eading\s*:?\s*(\d{1,3}(?:,\d{3})+|\d{3,7})',
    re.IGNORECASE,
)


def _extract_highest_odometer_text(text):
    """Find the largest mileage-like number in Carfax/AutoCheck OCR text.

    Priority:
      1. AutoCheck "Last Reported Odometer: NN,NNN" — most authoritative
      2. Generic "Odometer Reading: NN,NNN" patterns
      3. Largest number with comma-formatted thousands + "mi"/"miles" suffix
         (comma requirement filters out service-interval markers like 50000,
          15000 that appear in "50,000-mile maintenance" service records)

    Returns the largest of all matches across all three patterns, or None.
    """
    if not text:
        return None

    # 1. AutoCheck "Last Reported Odometer: N,NNN" — MOST AUTHORITATIVE.
    # If present, use it directly. Don't pool with weaker signals — that's
    # how warranty text "5 years/60,000 miles" pollutes max() comparisons.
    # (bid 1487 case: real reading 25,336 lost to 60,000 mi warranty text.)
    auth = []
    for m in _AUTOCHECK_LAST_REPORTED_RE.finditer(text):
        s = m.group(1).replace(',', '')
        if s.isdigit():
            n = int(s)
            if 100 <= n <= 500000:
                auth.append(n)
    if auth:
        return max(auth)

    # 2. Generic "Odometer Reading: N,NNN" labeled — second-strongest signal.
    label_hits = []
    for m in _ODOMETER_LABEL_RE.finditer(text):
        s = m.group(1).replace(',', '')
        if s.isdigit():
            n = int(s)
            if 100 <= n <= 500000:
                label_hits.append(n)
    if label_hits:
        return max(label_hits)

    candidates = []

    # 3. mi/miles suffix with comma-formatted thousands (skip round service
    # intervals like 50000, 15000 which appear in service-record narratives).
    # Reject matches with context words indicating warranty-remaining /
    # projected-not-actual readings (e.g. "warranty estimated to have 30
    # months or 35,990 miles remaining").
    for m in _MI_SUFFIX_RE.finditer(text):
        before = text[max(0, m.start() - 80):m.start()].lower()
        if any(bad in before for bad in _MI_REJECT_CONTEXT):
            continue
        s = m.group(1).replace(',', '')
        if s.isdigit():
            n = int(s)
            if 100 <= n <= 500000:
                candidates.append(n)

    if not candidates:
        return None
    return max(candidates)


def _trigger_miles_verify_sms(bid_id):
    """Rule 3: fire the miles-discrepancy SMS via the internal endpoint
    so Twilio creds + bid-context stay in app.py."""
    try:
        import urllib.request as _ureq
        import json as _json
        _data = _json.dumps({}).encode('utf-8')
        _req = _ureq.Request(
            f'{EW_INTERNAL_BASE}/api/internal/bid/{bid_id}/miles-verify-sms',
            data=_data,
            headers={
                'Content-Type': 'application/json',
                'X-Auth': EW_INTERNAL_SECRET,
                'User-Agent': 'EW-MilesAuditWorker/1.0',
            },
            method='POST',
        )
        with _ureq.urlopen(_req, timeout=10) as _resp:
            print(f'[miles-discrepancy-sms] fired bid={bid_id} '
                  f'status={_resp.status}', flush=True)
            return _resp.status == 200
    except Exception as _e:
        print(f'[miles-discrepancy-sms] error bid={bid_id}: '
              f'{type(_e).__name__}: {_e}', flush=True)
        return False


# Rule 4: damage cross-check parser. Patterns shared by Carfax + AutoCheck.
_CLEAN_RE = re.compile(
    r'NO\s+ACCIDENTS(?:\s+OR\s+DAMAGE)?\s+REPORTED', re.IGNORECASE)
# Damage signals (use only when NOT in "No Accidents..." vicinity).
_DAMAGE_COUNT_RE = re.compile(
    r'\b([1-9]\d*)\s+ACCIDENT(?:S)?\s+REPORTED', re.IGNORECASE)
_DAMAGE_PHRASE_RE = re.compile(
    r'\b(?:DAMAGE|ACCIDENT|FRAME\s+DAMAGE|STRUCTURAL\s+DAMAGE|'
    r'AIRBAG\s+DEPLOYED|SALVAGE\s+TITLE|TOTAL\s+LOSS|FLOOD\s+DAMAGE|'
    r'HAIL\s+DAMAGE|LEMON\s+LAW)\s+REPORTED', re.IGNORECASE)


def _parse_damage_signal(text):
    """Returns True (damage detected), False (explicit clean), None (unknown).
    Conservative: only flags damage on STRONG explicit signals (numeric
    accident count >= 1, OR "X Issue(s) Reported" with positive count).
    Otherwise defers to explicit-clean markers."""
    if not text:
        return None
    up = text.upper()

    # Strong damage signal #1: numeric accident count >= 1
    # "1 Accident Reported" / "2 Accidents Reported"
    for m in _DAMAGE_COUNT_RE.finditer(up):
        try:
            n = int(m.group(1))
            if n >= 1:
                return True
        except ValueError:
            pass

    # Strong damage signal #2: AutoCheck "X Event(s) Reported" with count >= 1
    # in the Accident/Damage section context.
    for m in re.finditer(
            r'\b([1-9]\d*)\s+(?:EVENT|RECORD|INCIDENT)S?\s+(?:OF\s+)?'
            r'(?:ACCIDENT|DAMAGE|COLLISION)',
            up, re.IGNORECASE):
        return True

    # Strong damage signal #3: explicit "Branded Title" / "Salvage Title"
    # statements (not just the legend listing).
    if re.search(r'\bSALVAGE\s+TITLE\s+ISSUED\b', up) or \
       re.search(r'\bTITLE\s+BRAND(?:ED|ING)?:\s*(?!NONE|CLEAN)', up):
        return True

    # Explicit-clean wins when no positive damage signal found.
    if _CLEAN_RE.search(up):
        return False

    return None


def _extract_damage_signal(image_path):
    try:
        with open(image_path, 'rb') as f:
            file_bytes = f.read()
    except Exception:
        return None
    if len(file_bytes) < 1000:
        return None
    text = _google_vision_ocr(file_bytes)
    if not text:
        return None
    return _parse_damage_signal(text)


def _extract_highest_odometer(image_path):
    try:
        with open(image_path, 'rb') as f:
            file_bytes = f.read()
    except Exception as e:
        print(f'[ocr] read failed {image_path}: {e}', flush=True)
        return None
    if len(file_bytes) < 1000:
        return None
    text = _google_vision_ocr(file_bytes)
    if not text:
        return None
    return _extract_highest_odometer_text(text)


def claim_one(conn):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT b.id, b.vin, b.mileage,
                   v.carfax_screenshot, v.autocheck_screenshot
              FROM bids b
              JOIN vauto_lookups v ON v.bid_id = b.id
             WHERE b.miles_audit_at IS NULL
               AND b.mileage IS NOT NULL
               AND b.mileage > 0
               AND b.created_at > NOW() - INTERVAL '%s days'
               AND (v.carfax_screenshot IS NOT NULL
                    OR v.autocheck_screenshot IS NOT NULL)
             ORDER BY b.id DESC
             LIMIT 1
        """, (MAX_AGE_DAYS,))
        return cur.fetchone()


def process_one(conn, row):
    bid_id = row['id']
    customer_miles = row['mileage']
    carfax_path = _resolve_screenshot_path(row.get('carfax_screenshot'))
    autocheck_path = _resolve_screenshot_path(row.get('autocheck_screenshot'))

    carfax_miles = None
    autocheck_miles = None

    if carfax_path and os.path.exists(carfax_path):
        carfax_miles = _extract_highest_odometer(carfax_path)
    if autocheck_path and os.path.exists(autocheck_path):
        autocheck_miles = _extract_highest_odometer(autocheck_path)

    higher = max(carfax_miles or 0, autocheck_miles or 0)
    if higher == 0:
        higher_source = None
        discrepancy = None
        flag = False
    else:
        if (carfax_miles or 0) >= (autocheck_miles or 0):
            higher_source = 'carfax'
        else:
            higher_source = 'autocheck'
        discrepancy = higher - customer_miles
        flag = discrepancy > DELTA_OVER

    with conn.cursor() as cur:
        cur.execute("""
            UPDATE bids
               SET miles_carfax = %s,
                   miles_autocheck = %s,
                   miles_higher_source = %s,
                   miles_discrepancy = %s,
                   miles_discrepancy_at = CASE WHEN %s THEN NOW() ELSE NULL END,
                   miles_audit_at = NOW()
             WHERE id = %s
        """, (carfax_miles, autocheck_miles, higher_source, discrepancy,
              flag, bid_id))

        if flag:
            cur.execute("""
                UPDATE bids
                   SET needs_verification_at = COALESCE(needs_verification_at, NOW()),
                       needs_verification_reason = CASE
                          WHEN needs_verification_reason IS NULL
                               THEN 'miles_discrepancy'
                          WHEN position('miles_discrepancy'
                                        IN needs_verification_reason) > 0
                               THEN needs_verification_reason
                          ELSE needs_verification_reason || ',miles_discrepancy'
                       END
                 WHERE id = %s
                   AND needs_verification_cleared_at IS NULL
            """, (bid_id,))

    conn.commit()

    # Rule 3: fire miles-discrepancy SMS when flag just landed. Idempotent
    # via bids.miles_verify_sms_sent_at (the endpoint checks). Outside the
    # transaction so HTTP failure doesn't roll back the audit stamp.
    if flag:
        _trigger_miles_verify_sms(bid_id)
    print(
        f'[miles-audit] bid={bid_id} customer={customer_miles} '
        f'carfax={carfax_miles} autocheck={autocheck_miles} '
        f'higher={higher_source}/{higher} disc={discrepancy} '
        f'flagged={flag}',
        flush=True,
    )


# Twilio creds for the missing-miles SMS path. Same env names as app.py.
TWILIO_SID = os.environ.get('TWILIO_ACCOUNT_SID', '')
TWILIO_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN', '')
TWILIO_PHONE = os.environ.get('TWILIO_PHONE', '')


def _twilio_send(to_phone, body):
    """Direct Twilio send. Returns True on success."""
    if not (TWILIO_SID and TWILIO_TOKEN and TWILIO_PHONE and to_phone and body):
        return False
    try:
        from twilio.rest import Client
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        client.messages.create(to=to_phone, from_=TWILIO_PHONE, body=body)
        return True
    except Exception as e:
        print(f'[twilio] send failed: {e}', flush=True)
        return False


def claim_missing_miles(conn):
    """Find next bid that has a VIN+phone but NO mileage, >30s old <12h old.
    The 30-sec buffer gives photo-OCR a chance to land miles before we ask
    (most MMS+OCR completes <30s; tighter than the original 3-min so the
    customer feedback loop is fast)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT id, vin, phone, bidder_name, year, make, model
              FROM bids
             WHERE mileage IS NULL
               AND vin IS NOT NULL AND length(vin) = 17
               AND phone IS NOT NULL AND phone <> ''
               AND phone NOT LIKE 'field:%%'
               AND phone NOT LIKE 'sys:%%'
               AND phone NOT LIKE 'thalist:%%'
               AND phone NOT LIKE 'dealerclub:%%'
               AND miles_request_sms_sent_at IS NULL
               AND created_at BETWEEN NOW() - INTERVAL '12 hours'
                                  AND NOW() - INTERVAL '30 seconds'
             ORDER BY id DESC
             LIMIT 1
        """)
        return cur.fetchone()


def claim_missing_vin(conn):
    """Bid created without a VIN at all (customer texted miles or notes but
    didn't include a 17-char VIN). Different from vin_invalid (had a VIN
    that failed structural check) — here we have nothing to validate."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT id, vin, phone, bidder_name, year, make, model, mileage
              FROM bids
             WHERE (vin IS NULL OR vin = '' OR length(vin) <> 17)
               AND vin_invalid_reason IS NULL
               AND vin_verify_sms_sent_at IS NULL
               AND phone IS NOT NULL AND phone <> ''
               AND phone NOT LIKE 'field:%%'
               AND phone NOT LIKE 'sys:%%'
               AND phone NOT LIKE 'thalist:%%'
               AND phone NOT LIKE 'dealerclub:%%'
               AND created_at BETWEEN NOW() - INTERVAL '12 hours'
                                  AND NOW() - INTERVAL '30 seconds'
             ORDER BY id DESC
             LIMIT 1
        """)
        return cur.fetchone()


def process_missing_vin(conn, row):
    bid_id = row['id']
    phone = (row.get('phone') or '').strip()
    name_raw = (row.get('bidder_name') or '').strip()
    first = name_raw.split()[0] if name_raw else 'there'
    miles_ctx = ''
    if row.get('mileage'):
        miles_ctx = f' (we have {int(row["mileage"]):,} miles noted)'
    body = (
        f"Hey, its the EW Bot. Got your message{miles_ctx} but I didn't "
        f"catch a VIN — can you text me the 17-character VIN from the dash, "
        f"door jamb, or windshield sticker? Or send a clear photo and we'll "
        f"pull it from there. Thanks!"
        # VERIFY_SMS_HASHBID_HINT_2026_05_16
        f"\n\nReply with #{bid_id} before the VIN or photo so we attach it to this bid."
    )
    sent = _twilio_send(phone, body)
    if not sent:
        print(f'[missing-vin-sms] bid={bid_id} send failed', flush=True)
        return False
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE bids
               SET vin_verify_sms_sent_at = NOW(),
                   vin_verify_sms_reason = 'missing_vin_at_intake',
                   needs_verification_at = COALESCE(needs_verification_at, NOW()),
                   needs_verification_reason = CASE
                      WHEN needs_verification_reason IS NULL
                           THEN 'missing_vin'
                      WHEN position('missing_vin' IN needs_verification_reason) > 0
                           THEN needs_verification_reason
                      ELSE needs_verification_reason || ',missing_vin'
                   END
             WHERE id = %s
               AND needs_verification_cleared_at IS NULL
        """, (bid_id,))
    conn.commit()
    print(f'[missing-vin-sms] bid={bid_id} sent to {phone}', flush=True)
    return True


def claim_invalid_vin(conn):
    """Find next bid with vin_invalid_reason set (ISO-3779 check digit fail
    OR length != 17) where we haven't yet asked the customer to verify.
    Phase 1 workers already skip these — we just need to alert the bidder."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT id, vin, phone, bidder_name, year, make, model,
                   vin_invalid_reason
              FROM bids
             WHERE vin_invalid_reason IS NOT NULL
               AND vin_verify_sms_sent_at IS NULL
               AND phone IS NOT NULL AND phone <> ''
               AND phone NOT LIKE 'field:%%'
               AND phone NOT LIKE 'sys:%%'
               AND phone NOT LIKE 'thalist:%%'
               AND phone NOT LIKE 'dealerclub:%%'
               AND created_at > NOW() - INTERVAL '24 hours'
             ORDER BY id DESC
             LIMIT 1
        """)
        return cur.fetchone()


def process_invalid_vin(conn, row):
    bid_id = row['id']
    phone = (row.get('phone') or '').strip()
    name_raw = (row.get('bidder_name') or '').strip()
    first = name_raw.split()[0] if name_raw else 'there'
    vin = (row.get('vin') or '').strip().upper()
    body = (
        f"Hey, it's the EW Bot. The VIN we have "
        f"on file ({vin}) doesn't look right — it should be exactly 17 "
        f"characters and pass the standard VIN check. Could you re-type the "
        f"VIN from the dash, door jamb, or windshield sticker, or send a "
        f"clearer photo? Thanks!"
        # VERIFY_SMS_HASHBID_HINT_2026_05_16
        f"\n\nReply with #{bid_id} before the VIN or photo so we attach it to this bid."
    )
    sent = _twilio_send(phone, body)
    if not sent:
        print(f'[vin-verify-sms] bid={bid_id} send failed (invalid_vin)',
              flush=True)
        return False
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE bids
               SET vin_verify_sms_sent_at = NOW(),
                   vin_verify_sms_reason = 'vin_invalid_structural',
                   needs_verification_at = COALESCE(needs_verification_at, NOW()),
                   needs_verification_reason = CASE
                      WHEN needs_verification_reason IS NULL
                           THEN 'vin_invalid'
                      WHEN position('vin_invalid' IN needs_verification_reason) > 0
                           THEN needs_verification_reason
                      ELSE needs_verification_reason || ',vin_invalid'
                   END
             WHERE id = %s
               AND needs_verification_cleared_at IS NULL
        """, (bid_id,))
    conn.commit()
    print(f'[vin-verify-sms] bid={bid_id} sent to {phone} '
          f'reason=vin_invalid vin={vin}', flush=True)
    return True


def process_missing_miles(conn, row):
    bid_id = row['id']
    phone = (row.get('phone') or '').strip()
    name_raw = (row.get('bidder_name') or '').strip()
    first = name_raw.split()[0] if name_raw else 'there'
    y = row.get('year') or ''
    mk = (row.get('make') or '').title() if row.get('make') else ''
    md = row.get('model') or ''
    vehicle = (f"{y} {mk} {md}").strip() or "vehicle"

    body = (
        f"Hey, it's the EW Bot. Got the VIN — "
        f"looks like a {vehicle}. To finalize the value I need the odometer "
        f"reading too. Reply with the mileage or send a clear photo of the "
        f"dash. Thanks!"
        # VERIFY_SMS_HASHBID_HINT_2026_05_16
        f"\n\nReply with #{bid_id} before the mileage or photo so we attach it to this bid."
    )

    sent = _twilio_send(phone, body)
    if not sent:
        print(f'[miles-request-sms] bid={bid_id} send failed', flush=True)
        return False

    with conn.cursor() as cur:
        cur.execute("""
            UPDATE bids
               SET miles_request_sms_sent_at = NOW(),
                   miles_request_sms_reason = 'missing_at_intake',
                   needs_verification_at = COALESCE(needs_verification_at, NOW()),
                   needs_verification_reason = CASE
                      WHEN needs_verification_reason IS NULL
                           THEN 'missing_miles'
                      WHEN position('missing_miles' IN needs_verification_reason) > 0
                           THEN needs_verification_reason
                      ELSE needs_verification_reason || ',missing_miles'
                   END
             WHERE id = %s
               AND needs_verification_cleared_at IS NULL
        """, (bid_id,))
    conn.commit()
    print(f'[miles-request-sms] bid={bid_id} sent to {phone} '
          f'vehicle="{vehicle}"', flush=True)
    return True


def claim_damage_audit(conn):
    """Rule 4: bids with Carfax + AutoCheck screenshots ready but damage
    cross-check not yet done."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT b.id, v.carfax_screenshot, v.autocheck_screenshot
              FROM bids b
              JOIN vauto_lookups v ON v.bid_id = b.id
             WHERE b.damage_audit_at IS NULL
               AND (v.carfax_screenshot IS NOT NULL
                    OR v.autocheck_screenshot IS NOT NULL)
               AND b.created_at > NOW() - INTERVAL '%s days'
             ORDER BY b.id DESC
             LIMIT 1
        """, (MAX_AGE_DAYS,))
        return cur.fetchone()


def process_damage_audit(conn, row):
    bid_id = row['id']
    cf_path = _resolve_screenshot_path(row.get('carfax_screenshot'))
    ac_path = _resolve_screenshot_path(row.get('autocheck_screenshot'))

    cf_damage = None
    ac_damage = None
    if cf_path and os.path.exists(cf_path):
        cf_damage = _extract_damage_signal(cf_path)
    if ac_path and os.path.exists(ac_path):
        ac_damage = _extract_damage_signal(ac_path)

    # Truth table:
    #   both clean -> 'both_clean' (don't surface)
    #   both damage -> 'both_damaged' (surface yellow)
    #   one says damage -> 'disagreement' (surface red — strongest signal)
    #   unknown either -> 'unknown' (don't surface; data wasn't conclusive)
    if cf_damage is True and ac_damage is True:
        signal = 'both_damaged'
    elif cf_damage is False and ac_damage is False:
        signal = 'both_clean'
    elif (cf_damage is True and ac_damage is False) or \
         (cf_damage is False and ac_damage is True):
        signal = 'disagreement'
    else:
        signal = 'unknown'

    with conn.cursor() as cur:
        cur.execute("""
            UPDATE bids
               SET carfax_damage = %s,
                   autocheck_damage = %s,
                   damage_signal = %s,
                   damage_audit_at = NOW()
             WHERE id = %s
        """, (cf_damage, ac_damage, signal, bid_id))
    conn.commit()
    print(
        f'[damage-audit] bid={bid_id} carfax={cf_damage} '
        f'autocheck={ac_damage} signal={signal}',
        flush=True,
    )


def claim_ack_due(conn):
    """Find bids where a verification SMS fired earlier, customer replied,
    and AI assessment has now completed — time to acknowledge them."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT id, vin, phone, bidder_name, year, make, model, ai_price
              FROM bids
             WHERE ai_assessed_at IS NOT NULL
               AND acknowledgement_sms_sent_at IS NULL
               AND (miles_request_sms_sent_at IS NOT NULL
                    OR vin_verify_sms_sent_at IS NOT NULL
                    OR miles_verify_sms_sent_at IS NOT NULL)
               AND phone IS NOT NULL AND phone <> ''
               AND phone NOT LIKE 'field:%%'
               AND phone NOT LIKE 'sys:%%'
               AND phone NOT LIKE 'thalist:%%'
               AND phone NOT LIKE 'dealerclub:%%'
               AND needs_verification_cleared_at IS NOT NULL
               AND created_at > NOW() - INTERVAL '24 hours'
             ORDER BY id DESC
             LIMIT 1
        """)
        return cur.fetchone()


def process_ack(conn, row):
    bid_id = row['id']
    phone = (row.get('phone') or '').strip()
    name_raw = (row.get('bidder_name') or '').strip()
    first = name_raw.split()[0] if name_raw else 'there'
    y = row.get('year') or ''
    mk = (row.get('make') or '').title() if row.get('make') else ''
    md = row.get('model') or ''
    vehicle = (f"{y} {mk} {md}").strip() or "vehicle"

    body = (
        f"Thanks {first} — got everything we need on the {vehicle}. "
        f"Someone from EW will reach out to you shortly with our offer."
    )

    sent = _twilio_send(phone, body)
    if not sent:
        print(f'[ack-sms] bid={bid_id} send failed', flush=True)
        return False
    with conn.cursor() as cur:
        cur.execute("UPDATE bids SET acknowledgement_sms_sent_at = NOW() "
                    "WHERE id = %s", (bid_id,))
    conn.commit()
    print(f'[ack-sms] bid={bid_id} sent to {phone} vehicle="{vehicle}"',
          flush=True)
    return True


def main():
    print(f'[miles-audit-worker] starting — delta_over={DELTA_OVER} '
          f'poll={POLL_INTERVAL_S}s '
          f'twilio={"on" if TWILIO_SID and TWILIO_PHONE else "off"}',
          flush=True)
    while True:
        try:
            conn = psycopg2.connect(DB_URL)
            try:
                # Customer-facing first — ack SMS for completed verification
                # cycles. Idempotent (acknowledgement_sms_sent_at gate) so
                # safe to run every cycle without worrying about repeat work.
                ack = claim_ack_due(conn)
                if ack:
                    process_ack(conn, ack)
                    continue

                # Then time-sensitive: missing miles SMS (within 30s of intake)
                missing = claim_missing_miles(conn)
                if missing:
                    process_missing_miles(conn, missing)
                    continue

                # Invalid-VIN SMS
                bad_vin = claim_invalid_vin(conn)
                if bad_vin:
                    process_invalid_vin(conn, bad_vin)
                    continue

                # Missing-VIN SMS (no VIN at all, not just malformed)
                no_vin = claim_missing_vin(conn)
                if no_vin:
                    process_missing_vin(conn, no_vin)
                    continue

                # Background backlog scans (slower, can wait):
                row = claim_one(conn)
                if row:
                    process_one(conn, row)
                    continue

                dmg = claim_damage_audit(conn)
                if dmg:
                    process_damage_audit(conn, dmg)
                    continue

                # Nothing to do — sleep
                conn.close()
                time.sleep(POLL_INTERVAL_S)
                continue
            finally:
                try: conn.close()
                except Exception: pass
        except KeyboardInterrupt:
            print('[miles-audit-worker] interrupt', flush=True)
            sys.exit(0)
        except Exception:
            traceback.print_exc()
            time.sleep(POLL_INTERVAL_S)


if __name__ == '__main__':
    main()
