"""recon_home_auto.py — RECON_AUTO_HOME_2026_08_18

Joe's rule: once a car is booked "bought" in LSL, if ANY recon value is entered
on the deal, that is an automatic flag the vehicle is coming home. Today Joe /
Todd / Gregg do the resulting steps by hand in the Home Base wizard; this makes
them automatic off the */5 LSL cost sync.

On a recon value transition (0/NULL -> > 0) for a unit still in a STAGING lane:
  1. flag path='to_home'
  2. auto-move New -> Dealer to Home / Individual to Home (by purchased_from_type)
  3. text Denes  ("on the way home, needs recon")
  4. route the pickup by distance from Pompano:
         <= COBRA_MAX_MI  -> text Jordan to send Cobra
         >  COBRA_MAX_MI  -> email Austin to arrange it
         location unclear -> text Jordan "distance unknown - check"
  5. write recon_notes for every message so the board shows what was sent

Distance rule set by the operator 2026-08-18: "naples and up to vero beach north
for cobra and everything else austin". Naples measures 104 mi and Vero Beach 98
mi from Pompano, and the next places out are Fort Myers 112 / Melbourne 131 --
so 105 straight-line miles separates them cleanly.

DELIBERATE REUSE: the SMS/email bodies come from recon_routes' own composers
(api_home_transport_sms / api_home_transport_body) and the Denes wording mirrors
api_notify_denes, so the automatic message is byte-identical to what the wizard
sends by hand. Do not fork the wording here.

Everything is best-effort and one-shot per unit (recon_auto_home_log), so a
failed send can never break the cost sync and nothing double-texts.
"""
import argparse
import math
import os
import re
import sqlite3
import sys

sys.path.insert(0, '/opt/expwholesale')

# Cron does NOT inherit the systemd unit's Environment=, and send_sms() returns
# False *silently* without these (TWILIO_CREDS_2026_08_17). Load them from the
# unit file rather than hardcoding -- literals get the file rejected by GitHub
# secret scanning.
_TWILIO_KEYS = ('TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_PHONE')
_UNIT_FILE = '/etc/systemd/system/expwholesale.service'


def _load_env_from_unit():
    try:
        with open(_UNIT_FILE, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line.startswith('Environment='):
                    continue
                kv = line[len('Environment='):].strip().strip('"')
                if '=' not in kv:
                    continue
                k, v = kv.split('=', 1)
                os.environ.setdefault(k, v.strip().strip('"'))
    except Exception as exc:                                   # pragma: no cover
        print('[recon-auto] WARN reading %s: %s' % (_UNIT_FILE, exc), flush=True)
    missing = [k for k in _TWILIO_KEYS if not os.environ.get(k)]
    if missing:
        sys.exit('[recon-auto] FATAL missing %s -- send_sms() would fail silently'
                 % ', '.join(missing))


_load_env_from_unit()
os.environ.setdefault(
    'DATABASE_URL',
    'postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale')

import recon_routes as R                                        # noqa: E402

SENTINEL = '/opt/expwholesale/RECON_ENABLED'
COBRA_MAX_MI = float(os.environ.get('RECON_COBRA_MAX_MI', '105'))
POMPANO = (26.2379, -80.1248)
# Only these lanes auto-move. Operator 2026-08-18: a car already at the dealer
# that gets a late recon entry is flagged + Denes told, but NOT yanked home.
STAGING = ('all', 'dealer_to_dealer', 'dealer_to_home',
           'indiv_to_dealer', 'indiv_to_home', 'dealer_to_buyer')

CITY = {
    ('naples', 'fl'): (26.1420, -81.7948), ('delray beach', 'fl'): (26.4615, -80.0728),
    ('coconut creek', 'fl'): (26.2517, -80.1789), ('fort lauderdale', 'fl'): (26.1224, -80.1373),
    ('ft lauderdale', 'fl'): (26.1224, -80.1373), ('pompano beach', 'fl'): (26.2379, -80.1248),
    ('miami', 'fl'): (25.7617, -80.1918), ('hialeah', 'fl'): (25.8576, -80.2781),
    ('west palm beach', 'fl'): (26.7153, -80.0534), ('boca raton', 'fl'): (26.3683, -80.1289),
    ('davie', 'fl'): (26.0765, -80.2521), ('plantation', 'fl'): (26.1276, -80.2331),
    ('hollywood', 'fl'): (26.0112, -80.1495), ('orlando', 'fl'): (28.5383, -81.3792),
    ('tampa', 'fl'): (27.9506, -82.4572), ('jacksonville', 'fl'): (30.3322, -81.6557),
    ('sarasota', 'fl'): (27.3364, -82.5307), ('fort myers', 'fl'): (26.6406, -81.8723),
    ('stuart', 'fl'): (27.1976, -80.2528), ('vero beach', 'fl'): (27.6386, -80.3973),
    ('port saint lucie', 'fl'): (27.2730, -80.3582), ('port st lucie', 'fl'): (27.2730, -80.3582),
    ('melbourne', 'fl'): (28.0836, -80.6081), ('doral', 'fl'): (25.8195, -80.3553),
    ('pembroke pines', 'fl'): (26.0031, -80.2239), ('margate', 'fl'): (26.2445, -80.2064),
    ('deerfield beach', 'fl'): (26.3184, -80.0998), ('boynton beach', 'fl'): (26.5318, -80.0905),
    ('lake worth', 'fl'): (26.6168, -80.0684), ('jupiter', 'fl'): (26.9342, -80.0942),
    ('wellington', 'fl'): (26.6618, -80.2684), ('sunrise', 'fl'): (26.1669, -80.2564),
    ('tamarac', 'fl'): (26.2129, -80.2498), ('coral springs', 'fl'): (26.2712, -80.2706),
    ('tequesta', 'fl'): (26.9645, -80.1281), ('aventura', 'fl'): (25.9565, -80.1392),
    ('homestead', 'fl'): (25.4687, -80.4776), ('marco island', 'fl'): (25.9412, -81.7184),
    ('naples', 'florida'): (26.1420, -81.7948),
}

_STOP = re.compile(r"\b(inc|llc|corp|corporation|co|company|ltd|the|of)\b")


def _norm(s):
    s = (s or '').lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = _STOP.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def _hav(a, b):
    r = 3958.8
    dlat, dlon = math.radians(b[0] - a[0]), math.radians(b[1] - a[1])
    x = (math.sin(dlat / 2) ** 2 + math.cos(math.radians(a[0]))
         * math.cos(math.radians(b[0])) * math.sin(dlon / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(x))


def resolve_distance(bought_from):
    """-> (miles|None, 'City, ST'|None, confidence). Confidence is 'exact' only
    on a whole-name supplier match. A fuzzy hit is NEVER trusted to pick a
    carrier -- an early pass of this analysis matched 'Sunpaz' to West Chicago,
    Illinois. Unsure => the caller tells Jordan to check, it does not guess."""
    if not bought_from:
        return None, None, 'none'
    n = _norm(bought_from)
    try:
        c = sqlite3.connect('file:%s?mode=ro' % R.LSL_DB, uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        rows = [dict(r) for r in c.execute(
            'SELECT name, city, state FROM suppliers')]
        crows = [dict(r) for r in c.execute(
            'SELECT company_name AS name, full_address FROM customers '
            'WHERE company_name IS NOT NULL AND company_name <> ""')]
        c.close()
    except Exception as exc:
        print('[recon-auto] LSL read failed: %s' % exc, flush=True)
        return None, None, 'none'

    exact = {}
    for s in rows:
        exact.setdefault(_norm(s['name']), s)
    hit, conf = exact.get(n), 'exact'
    if not hit:
        for cr in crows:
            if _norm(cr['name']) == n and cr.get('full_address'):
                parts = [p.strip() for p in cr['full_address'].split(',')]
                if len(parts) >= 2:
                    hit, conf = {'city': parts[1], 'state': 'FL'}, 'exact'
                break
    if not hit:
        cands = [v for k, v in exact.items() if k and (k in n or n in k)]
        if len(cands) == 1:
            hit, conf = cands[0], 'fuzzy'
    if not hit:
        return None, None, 'none'

    city = (hit.get('city') or '').strip()
    state = (hit.get('state') or '').strip()
    key = (city.lower(), state.lower()[:2])
    if key not in CITY:
        key = (city.lower(), 'fl')
    pt = CITY.get(key)
    label = '%s, %s' % (city or '?', (state or '?')[:2].upper())
    if not pt:
        return None, label, conf
    return _hav(POMPANO, pt), label, conf


def resolve_source_type(u):
    """Dealer vs Individual decides which lane the car is staged into
    (dealer_to_home vs indiv_to_home), so getting it wrong mis-stages the car.

    recon_units.buying_from_type is normally set by ew_recon_lsl_sync (627
    Dealer / 36 Individual / 2 empty at wire-in), but when it is empty we go
    back to LSL for `inventory.purchased_from_type` rather than silently
    defaulting to Dealer. Mirrors the sync's own mapping exactly:
        Individual iff purchased_from_type == 'individual', else Dealer.

    Returns (type, source) where source is 'unit' | 'lsl' | 'assumed'.
    'assumed' means we genuinely could not tell -- the caller flags it for a
    human instead of pretending it knew.
    """
    bft = (u.get('buying_from_type') or '').strip()
    if bft:
        return ('Individual' if bft.lower() == 'individual' else 'Dealer'), 'unit'
    try:
        c = sqlite3.connect('file:%s?mode=ro' % R.LSL_DB, uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        row = None
        if u.get('stock_no'):
            row = c.execute('SELECT purchased_from_type FROM inventory '
                            'WHERE stock_no=? ORDER BY created_at DESC LIMIT 1',
                            (u['stock_no'],)).fetchone()
        if not row and u.get('vin'):
            row = c.execute('SELECT purchased_from_type FROM inventory '
                            'WHERE vin_no=? ORDER BY created_at DESC LIMIT 1',
                            (u['vin'],)).fetchone()
        c.close()
        if row and (row['purchased_from_type'] or '').strip():
            pft = row['purchased_from_type'].strip()
            return ('Individual' if pft.lower() == 'individual' else 'Dealer'), 'lsl'
    except Exception as exc:
        print('[recon-auto] source-type LSL lookup failed: %s' % exc, flush=True)
    return 'Dealer', 'assumed'


def _move_to(cur, u, to_code, actor):
    """Mirror of api_move's bookkeeping so the board history stays truthful:
    close the open step event, swap the active step, write a new event + audit.
    Never raw-UPDATE current_step_id on its own or the day-counters go blind."""
    steps = R._steps(cur)
    by_code = {s['code']: s for s in steps}
    target = by_code.get(to_code)
    if not target:
        raise RuntimeError('unknown step %r' % to_code)
    now = R._utcnow()
    cur.execute("""UPDATE recon_step_events SET exited_at=%s,
                     duration_sec=EXTRACT(EPOCH FROM (%s - entered_at))::bigint
                   WHERE unit_id=%s AND exited_at IS NULL""", (now, now, u['id']))
    cur.execute("DELETE FROM recon_unit_active_steps WHERE unit_id=%s", (u['id'],))
    cur.execute("""UPDATE recon_units
                      SET current_step_id=%s, current_step_entered_at=%s,
                          path='to_home', buying_from_type=%s, updated_at=now()
                    WHERE id=%s""",
                (target['id'], now,
                 'Individual' if to_code == 'indiv_to_home' else 'Dealer', u['id']))
    cur.execute("""INSERT INTO recon_step_events
                     (unit_id, step_id, entered_at, moved_by, move_reason, from_step_id)
                   VALUES (%s,%s,%s,%s,'auto_recon_home',%s) RETURNING id""",
                (u['id'], target['id'], now, actor, u.get('current_step_id')))
    ev = cur.fetchone()['id']
    cur.execute("""INSERT INTO recon_unit_active_steps (unit_id, step_id, event_id, entered_at)
                   VALUES (%s,%s,%s,%s) ON CONFLICT (unit_id, step_id) DO NOTHING""",
                (u['id'], target['id'], ev, now))
    R._audit(cur, u['id'], 'step', target['id'], 'auto_recon_home', actor,
             {'to': to_code, 'trigger': 'lsl_recon_value'})
    return target


def _note(cur, unit_id, step_id, actor, body, category='general'):
    cur.execute("INSERT INTO recon_notes (unit_id, step_id, author, body, category) "
                "VALUES (%s,%s,%s,%s,%s)", (unit_id, step_id, actor, body, category))


def process_unit(cur, u, recon_amount, staged, actor='auto:recon-home'):
    """Do the whole flow for one unit. Returns a dict describing what was sent."""
    out = {'unit_id': u['id'], 'staged': staged, 'moved': None,
           'denes': None, 'transport': None, 'miles': None, 'where': None}

    steps = R._steps(cur)
    by_id = {s['id']: s for s in steps}
    cur_code = (by_id.get(u.get('current_step_id')) or {}).get('code', 'all')

    src_type, src_from = resolve_source_type(u)
    u['buying_from_type'] = src_type
    out['source_type'] = src_type
    out['source_type_from'] = src_from
    to_code = 'indiv_to_home' if src_type == 'Individual' else 'dealer_to_home'
    if src_from == 'assumed':
        print('[recon-auto] unit=%s source type UNKNOWN, assuming Dealer'
              % u['id'], flush=True)

    if cur_code in STAGING and cur_code != to_code:
        tgt = _move_to(cur, u, to_code, actor)
        out['moved'] = '%s -> %s' % (cur_code, tgt['code'])
        u['current_step_id'] = tgt['id']
    else:
        out['moved'] = 'skipped (%s not a staging lane)' % cur_code

    ymm = ' '.join(str(x) for x in (u.get('year'), u.get('make'), u.get('model')) if x)
    money = '${:,.0f}'.format(float(recon_amount or 0))
    note_txt = 'recon %s entered on the LSL deal' % money

    _note(cur, u['id'], u.get('current_step_id'), actor,
          '🏠 Auto: %s → staged as **%s** (source: %s). %s'
          % (out['moved'], src_type,
             {'unit': 'LSL sync field', 'lsl': 'read live from the LSL deal',
              'assumed': '⚠ COULD NOT DETERMINE — assumed Dealer, please verify'}[src_from],
             note_txt))

    # --- 1. Denes (wording mirrors api_notify_denes) ---
    dbody = '%s%s%s is on the way home, needs recon: %s' % (
        '[TEST] ' if staged else '', ymm or 'A vehicle',
        (' (VIN %s)' % u['vin']) if u.get('vin') else '', note_txt)
    dto = R.TRANSPORT_TEST_PHONE if staged else R.DENES_SMS_TO
    dok = R._send_sms([dto], dbody)
    out['denes'] = {'to': dto, 'sent': bool(dok), 'body': dbody}
    _note(cur, u['id'], u.get('current_step_id'), actor,
          '🔧 Denes texted automatically — %s%s:\n%s'
          % (note_txt, ' [TEST — sent to the operator]' if staged else '', dbody),
          category='recon')

    # --- 2. transport routing ---
    miles, where, conf = resolve_distance(u.get('bought_from'))
    out['miles'], out['where'] = (round(miles) if miles is not None else None), where
    use_cobra = (miles is not None and conf == 'exact' and miles <= COBRA_MAX_MI)
    unknown = (miles is None or conf != 'exact')

    if unknown:
        body = ('%sCobra pickup? distance UNKNOWN — check. %s from %s. '
                'Recon %s.' % ('[TEST] ' if staged else '', ymm,
                               u.get('bought_from') or 'seller', money))
        to = R.TRANSPORT_TEST_PHONE if staged else R.COBRA_SMS_TO
        ok = R._send_sms([to], body)
        out['transport'] = {'via': 'jordan_unknown', 'to': to, 'sent': bool(ok), 'body': body}
        _note(cur, u['id'], u.get('current_step_id'), actor,
              '🚚 Jordan texted — pickup distance unknown, needs a human check%s:\n%s'
              % (' [TEST — sent to the operator]' if staged else '', body))
    elif use_cobra:
        body = R.api_home_transport_sms(u, note_txt, staged)
        to = R.TRANSPORT_TEST_PHONE if staged else R.COBRA_SMS_TO
        ok = R._send_sms([to], body)
        out['transport'] = {'via': 'cobra', 'to': to, 'sent': bool(ok), 'body': body}
        cur.execute("""UPDATE recon_units
                          SET transport_company=COALESCE(transport_company,%s),
                              shipping_arranged_at=now(), shipping_arranged_via='cobra',
                              shipping_arranged_who=%s, shipping_arranged_note=%s,
                              updated_at=now()
                        WHERE id=%s""",
                    (R.COBRA_LABEL, R.COBRA_LABEL + ' (Jordan)', note_txt, u['id']))
        _note(cur, u['id'], u.get('current_step_id'), actor,
              '🚚 Cobra texted automatically to collect (%s, ~%d mi)%s:\n%s'
              % (where, round(miles), ' [TEST — sent to the operator]' if staged else '', body))
    else:
        subj, body = R.api_home_transport_body(u, note_txt, staged)
        to = R.TRANSPORT_TEST_EMAIL if staged else R.EMAIL_RECIP['austin']
        R._recon_email(cur, u['id'], 'austin_auto_home', to, subj, body, dedupe=False)
        out['transport'] = {'via': 'austin', 'to': to, 'sent': True,
                            'subject': subj, 'body': body}
        cur.execute("""UPDATE recon_units
                          SET austin_emailed_at=now(), shipping_arranged_at=now(),
                              shipping_arranged_via='austin', shipping_arranged_who='Austin',
                              shipping_arranged_note=%s, updated_at=now()
                        WHERE id=%s""", (note_txt, u['id']))
        _note(cur, u['id'], u.get('current_step_id'), actor,
              '📧 Austin emailed automatically — out of Cobra range (%s, ~%d mi)%s.'
              % (where, round(miles), ' [TEST — sent to the operator]' if staged else ''))

    R._audit(cur, u['id'], 'recon', None, 'auto_home', actor,
             {'recon': float(recon_amount or 0), 'miles': out['miles'],
              'where': where, 'staged': staged, 'transport': out['transport']['via']})
    return out


# ── ledger / sweeper ───────────────────────────────────────────────────────
# ON switch. Absent = the sweeper does nothing. `rm` this file to stop the
# automation instantly without touching cron or code.
AUTO_ON = '/opt/expwholesale/RECON_AUTO_HOME_ON'
# Operator watchdog copy — every automatic fire is mirrored here so a human sees
# what went out to Denes / Jordan / Austin.
WATCHDOG_TO = os.environ.get('RECON_AUTO_WATCHDOG_SMS', '+14074309675')


def ensure_ledger(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS recon_auto_home_log (
            unit_id      BIGINT PRIMARY KEY,
            fired_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            recon_amount NUMERIC(12,2),
            miles        INTEGER,
            where_from   TEXT,
            routed_via   TEXT,
            staged       BOOLEAN,
            preexisting  BOOLEAN NOT NULL DEFAULT FALSE,
            note         TEXT
        )""")


def backfill(cur):
    """CRITICAL, run ONCE at wire-in. Operator 2026-08-18: "start from scratch
    any new vehicles going forward" — so claim EVERY unit that already exists,
    not just the 85 already carrying lsl_recon_cost > 0. Two reasons:

      * without it the first sweep would treat all 85 as fresh transitions and
        blast Denes / Jordan / Austin in one burst;
      * a car already on the board that gets a recon value tomorrow is still an
        OLD car, and the operator wants only genuinely new arrivals automated.

    Net effect: the automation can only ever fire for units created after this
    ran. Deliberately claims every row regardless of status or lane."""
    cur.execute("""
        INSERT INTO recon_auto_home_log
              (unit_id, recon_amount, routed_via, preexisting, note)
        SELECT id, lsl_recon_cost, 'none', TRUE,
               'claimed by wire-in backfill 2026-08-18 (start from scratch: '
               'only vehicles added after this point may fire)'
          FROM recon_units
        ON CONFLICT (unit_id) DO NOTHING""")
    return cur.rowcount


def sweep(staged, limit=10, dry=False):
    """One pass: find units whose recon value just appeared, and run the flow."""
    if not os.path.exists(SENTINEL):
        print('[recon-auto] RECON_ENABLED absent — dark', flush=True)
        return 0
    if not os.path.exists(AUTO_ON):
        print('[recon-auto] %s absent — automation OFF' % AUTO_ON, flush=True)
        return 0
    db = R._db()
    cur = db.cursor()
    try:
        cur.execute('SELECT pg_is_in_recovery() AS rec')
        if cur.fetchone()['rec']:
            print('[recon-auto] standby — no-op', flush=True)
            return 0
        ensure_ledger(cur)
        db.commit()
        cur.execute("""
            SELECT u.*, d.code AS step_code
              FROM recon_units u
              JOIN recon_step_defs d ON d.id = u.current_step_id
             WHERE u.status IN ('in_transit_stage0','in_recon','on_hold')
               AND COALESCE(u.lsl_recon_cost,0) > 0
               AND d.code = ANY(%s)
               AND NOT EXISTS (SELECT 1 FROM recon_auto_home_log l
                                WHERE l.unit_id = u.id)
             ORDER BY u.updated_at
             LIMIT %s""", (list(STAGING), limit))
        rows = [dict(r) for r in cur.fetchall()]
        if not rows:
            return 0
        n = 0
        for u in rows:
            amt = float(u.get('lsl_recon_cost') or 0)
            if dry:
                print('[recon-auto] DRY would fire unit=%s %s recon=%.2f'
                      % (u['id'], u.get('stock_no'), amt), flush=True)
                continue
            try:
                res = process_unit(cur, u, amt, staged=staged)
                cur.execute("""INSERT INTO recon_auto_home_log
                                 (unit_id, recon_amount, miles, where_from,
                                  routed_via, staged)
                               VALUES (%s,%s,%s,%s,%s,%s)
                               ON CONFLICT (unit_id) DO NOTHING""",
                            (u['id'], amt, res['miles'], res['where'],
                             res['transport']['via'], staged))
                db.commit()
                n += 1
                ymm = ' '.join(str(x) for x in (u.get('year'), u.get('make'),
                                                u.get('model')) if x)
                line = ('AUTO recon->home: %s %s | recon $%s | %s (%s mi) | '
                        'moved %s | %s'
                        % (u.get('stock_no') or u.get('vin'), ymm,
                           '{:,.0f}'.format(amt), res['where'], res['miles'],
                           res['moved'], res['transport']['via']))
                print('[recon-auto] ' + line, flush=True)
                # watchdog copy to the operator, always, live or staged
                try:
                    R._send_sms([WATCHDOG_TO], (
                        '\U0001f3e0 %s\n%s %s\nRecon $%s\n%s (~%s mi)\n%s\nsent to: %s'
                        % ('AUTO: car flagged coming home' if not staged
                           else 'AUTO [STAGED]: car flagged coming home',
                           u.get('stock_no') or '', ymm,
                           '{:,.0f}'.format(amt), res['where'], res['miles'],
                           'Denes texted + ' + res['transport']['via'],
                           res['transport']['to'])))
                except Exception as exc:
                    print('[recon-auto] watchdog sms failed: %s' % exc, flush=True)
            except Exception:
                db.rollback()
                import traceback
                print('[recon-auto] unit=%s FAILED\n%s'
                      % (u['id'], traceback.format_exc()), flush=True)
        return n
    finally:
        db.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--unit', type=int)
    ap.add_argument('--recon', type=float)
    ap.add_argument('--sweep', action='store_true')
    ap.add_argument('--backfill', action='store_true')
    ap.add_argument('--dry', action='store_true')
    ap.add_argument('--live', action='store_true',
                    help='send to the REAL Denes/Jordan/Austin. Default is staged '
                         'to the operator.')
    a = ap.parse_args()

    if a.backfill:
        db = R._db(); cur = db.cursor()
        ensure_ledger(cur)
        n = backfill(cur)
        db.commit(); db.close()
        print('[recon-auto] backfill claimed %d pre-existing unit(s)' % n)
        return

    if a.sweep:
        n = sweep(staged=not a.live, dry=a.dry)
        if n:
            print('[recon-auto] fired %d' % n)
        return

    if not a.unit or a.recon is None:
        sys.exit('need --unit and --recon, or --sweep / --backfill')
    if not os.path.exists(SENTINEL):
        sys.exit('[recon-auto] RECON_ENABLED absent — dark')
    db = R._db()
    cur = db.cursor()
    try:
        cur.execute('SELECT * FROM recon_units WHERE id=%s', (a.unit,))
        row = cur.fetchone()
        if not row:
            sys.exit('no unit %s' % a.unit)
        res = process_unit(cur, dict(row), a.recon, staged=not a.live)
        db.commit()
    finally:
        db.close()
    print()
    print('=' * 74)
    print('moved      : %s' % res['moved'])
    print('location   : %s  (%s mi)' % (res['where'], res['miles']))
    print('routed via : %s' % res['transport']['via'])
    print('=' * 74)
    for label, m in (('TEXT 1 — Denes', res['denes']),
                     ('TEXT/EMAIL 2 — transport', res['transport'])):
        print()
        print('--- %s  ->  %s  (sent=%s) ---' % (label, m['to'], m.get('sent')))
        if m.get('subject'):
            print('Subject: %s' % m['subject'])
        print(m['body'])


if __name__ == '__main__':
    main()
