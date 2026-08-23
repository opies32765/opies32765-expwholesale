"""dealer_intake.py — DEALER_INTAKE_2026_08_23

A dedicated, dealer-facing upload page that lives on its own URL and exposes
NOTHING else on the site. An EW employee generates a link for one specific
dealer from the dashboard; the dealer opens it, drops in whatever list they
have (spreadsheet, PDF, screenshot, phone photo, pasted email), confirms the
VINs and miles we read, and hits send. Bids are created immediately on the
existing bulk-upload path.

Why a token per dealer and not one open URL:
  - the dealer types nothing — we already know who they are from the link
  - every submission is attributed to that dealer automatically
  - a link that ends up somewhere it shouldn't is revoked on its own,
    without disturbing anyone else's link
  - an unauthenticated endpoint that spins the enrichment fleet needs a
    gate; the token IS that gate

This module owns no parsing and no bid-insert logic of its own. It calls
bulk_upload.parse_any() and the shared commit core, so the dealer path and
the operator's /admin/bulk_upload path produce byte-identical bids.

Wired up from app.py:
    import dealer_intake
    dealer_intake.init(app, get_db=get_db, vision_fn=..., commit_rows=...,
                       send_sms=send_sms, alert_phone='+1...')
"""
from __future__ import annotations

import re
import secrets
import time

from flask import (Blueprint, jsonify, render_template, request, session,
                   abort)

bp = Blueprint('dealer_intake', __name__)

# ── Caps. A public endpoint gets exactly this much and no more. ───────────
MAX_FILE_BYTES = 15 * 1024 * 1024   # 15 MB per file
MAX_FILES = 10                       # per submission
MAX_ROWS = 300                       # vehicles per submission
RATE_WINDOW_SEC = 3600
RATE_MAX_SUBMITS = 6                 # submissions per token per hour
RATE_MAX_PARSES = 40                 # parse calls per token per hour

# Enrichment stagger between bids. Same knob the operator's bulk upload uses;
# 5s keeps the vAuto/AccuTrade fleet from being hit all at once.
DEFAULT_DELAY_SECONDS = 5

_DEPS: dict = {}
_rate: dict = {}   # token -> {'parse': [ts...], 'submit': [ts...]}


# ── Schema ────────────────────────────────────────────────────────────────
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS intake_links (
    id               SERIAL PRIMARY KEY,
    token            TEXT UNIQUE NOT NULL,
    dealer_id        INTEGER,
    dealer_name      TEXT NOT NULL,
    contact_name     TEXT,
    contact_phone    TEXT,
    contact_email    TEXT,
    note             TEXT,
    created_by       TEXT,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    revoked          BOOLEAN DEFAULT FALSE,
    revoked_at       TIMESTAMPTZ,
    last_used_at     TIMESTAMPTZ,
    submission_count INTEGER DEFAULT 0,
    vehicle_count    INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS intake_links_token_idx ON intake_links (token);

CREATE TABLE IF NOT EXISTS intake_submissions (
    id             SERIAL PRIMARY KEY,
    link_id        INTEGER,
    dealer_name    TEXT,
    file_names     TEXT,
    row_count      INTEGER,
    created_count  INTEGER,
    bulk_upload_id INTEGER,
    client_ip      TEXT,
    user_agent     TEXT,
    created_at     TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS intake_submissions_link_idx
    ON intake_submissions (link_id, created_at DESC);
"""


def _ensure_schema():
    try:
        db = _DEPS['get_db']()
        cur = db.cursor()
        cur.execute(SCHEMA_SQL)
        db.commit()
        db.close()
    except Exception as e:
        print(f'[dealer_intake] schema ensure failed: {e}', flush=True)


# ── Helpers ───────────────────────────────────────────────────────────────
def _client_ip() -> str:
    return (request.headers.get('X-Forwarded-For', '').split(',')[0].strip()
            or request.remote_addr or '')


def _rate_ok(token: str, kind: str, limit: int) -> bool:
    """In-process sliding window. Deliberately simple: this stops a runaway
    loop or a bored dealer re-uploading 200 times, not a determined attacker
    — the token already handles that."""
    now = time.time()
    bucket = _rate.setdefault(token, {}).setdefault(kind, [])
    cutoff = now - RATE_WINDOW_SEC
    bucket[:] = [t for t in bucket if t > cutoff]
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    return True


def _load_link(token: str):
    """Return the intake_links row for a live token, or None. A revoked or
    unknown token is indistinguishable from the outside — both 404."""
    if not token or not re.fullmatch(r'[A-Za-z0-9_-]{8,64}', token):
        return None
    try:
        db = _DEPS['get_db']()
        cur = db.cursor()
        cur.execute("""
            SELECT id, token, dealer_id, dealer_name, contact_name,
                   contact_phone, contact_email, revoked, submission_count
              FROM intake_links
             WHERE token = %s
        """, (token,))
        row = cur.fetchone()
        db.close()
    except Exception as e:
        print(f'[dealer_intake] link lookup failed: {e}', flush=True)
        return None
    if not row or row['revoked']:
        return None
    return dict(row)


def _vin_ok(vin: str) -> bool:
    return bool(re.fullmatch(r'[A-HJ-NPR-Z0-9]{17}', (vin or '').upper()))


# ── Public: the dealer's page ─────────────────────────────────────────────
@bp.route('/dealer-intake/<token>')
def intake_page(token):
    link = _load_link(token)
    if not link:
        # No hint about whether the token ever existed.
        return render_template('dealer_intake_invalid.html'), 404
    return render_template('dealer_intake.html', link=link, token=token)


@bp.route('/api/dealer-intake/<token>/parse', methods=['POST'])
def intake_parse(token):
    """Read one or more uploaded files (and/or pasted text) into candidate
    rows. Inserts NOTHING — this is the 'here's what we read, check it'
    step."""
    link = _load_link(token)
    if not link:
        return jsonify({'error': 'This link is no longer active.'}), 404
    if not _rate_ok(token, 'parse', RATE_MAX_PARSES):
        return jsonify({'error': 'Too many uploads in a row. Give it a '
                                 'few minutes, or call us.'}), 429

    import bulk_upload as BU

    files = [f for f in request.files.getlist('files') if f and f.filename]
    paste_text = (request.form.get('paste_text') or '').strip()
    if not files and not paste_text:
        return jsonify({'error': 'Add a file or paste your list.'}), 400
    if len(files) > MAX_FILES:
        return jsonify({'error': f'Up to {MAX_FILES} files at a time, '
                                 f'please.'}), 400

    rows: list[dict] = []
    read_names: list[str] = []
    problems: list[str] = []

    for f in files:
        name = f.filename
        ext = (name.rsplit('.', 1)[-1].lower() if '.' in name else '')
        if ext not in BU.ALLOWED_EXTS:
            problems.append(f"{name}: we can't open a .{ext} file")
            continue
        data = f.read()
        if len(data) > MAX_FILE_BYTES:
            problems.append(f'{name}: too big '
                            f'({len(data) // (1024 * 1024)} MB, limit '
                            f'{MAX_FILE_BYTES // (1024 * 1024)} MB)')
            continue
        try:
            got = BU.parse_any(name, data, f.mimetype or '',
                               vision_fn=_DEPS.get('vision_fn'))
        except Exception as e:
            print(f'[dealer_intake] parse_any failed {name}: '
                  f'{type(e).__name__}: {e}', flush=True)
            problems.append(f"{name}: we couldn't read this one")
            continue
        if not got:
            problems.append(f'{name}: no vehicles found in this file')
            continue
        rows.extend(got)
        read_names.append(name)

    if paste_text:
        try:
            rows.extend(BU.parse_pasted_text(paste_text,
                                             _DEPS.get('text_fn')))
            read_names.append('pasted list')
        except Exception as e:
            print(f'[dealer_intake] paste parse failed: {e}', flush=True)
            problems.append("pasted text: we couldn't read that")

    rows = BU._dedupe_rows(rows)

    # Drop footer/summary noise. A "TOTAL 12 ... 331,790" or "10 units" line
    # parses into a row with no VIN; on the operator's own page that is
    # harmless clutter, but the dealer is being asked to CHECK this table and
    # a phantom vehicle is a real distraction.
    #
    # The test is simply "did we read any VIN at all". A row with a PARTIAL or
    # garbled VIN is kept so the dealer can correct it; a row with none cannot
    # be submitted anyway. Deliberately not a denylist of words like
    # total/units/subtotal — a guard built from the phrasings we happened to
    # see dies the moment a dealer words their footer differently.
    _before = len(rows)
    rows = [r for r in rows if (r.get('vin') or '').strip()]
    _dropped = _before - len(rows)
    if _dropped:
        # Say it out loud rather than quietly shrinking their list.
        problems.append(
            f'{_dropped} line{"" if _dropped == 1 else "s"} had no VIN on '
            f'{"it" if _dropped == 1 else "them"} (totals, headings) and '
            f'{"was" if _dropped == 1 else "were"} skipped.')

    if not rows:
        return jsonify({'error': 'We opened it but found no vehicles. '
                                 'Make sure the list has VINs on it.',
                        'problems': problems}), 400
    if len(rows) > MAX_ROWS:
        rows = rows[:MAX_ROWS]
        problems.append(f'Only the first {MAX_ROWS} vehicles are shown — '
                        f'send the rest as a second list.')

    # Flag duplicates against open bids so the dealer sees "you already sent
    # us this one" instead of us silently creating a second bid.
    vins = [r['vin'] for r in rows if r.get('vin')]
    dupes = {}
    if vins:
        try:
            db = _DEPS['get_db']()
            cur = db.cursor()
            cur.execute("""
                SELECT DISTINCT ON (vin) vin, id
                  FROM bids
                 WHERE vin = ANY(%s)
                   AND COALESCE(status,'') NOT IN ('cancelled','rejected')
                 ORDER BY vin, id DESC
            """, (vins,))
            dupes = {r['vin']: r['id'] for r in cur.fetchall()}
            db.close()
        except Exception as e:
            print(f'[dealer_intake] dupe check failed: {e}', flush=True)

    for r in rows:
        r['vin_valid'] = _vin_ok(r.get('vin') or '')
        r['already_have'] = bool(r.get('vin') and r['vin'] in dupes)

    return jsonify({
        'rows': rows,
        'files': read_names,
        'problems': problems,
        'dealer_name': link['dealer_name'],
    })


@bp.route('/api/dealer-intake/<token>/submit', methods=['POST'])
def intake_submit(token):
    """Create the bids. Runs the SAME commit core the operator's bulk upload
    uses, so enrichment, staggering and bid shape are identical."""
    link = _load_link(token)
    if not link:
        return jsonify({'error': 'This link is no longer active.'}), 404
    if not _rate_ok(token, 'submit', RATE_MAX_SUBMITS):
        return jsonify({'error': 'That is a lot of lists in one hour. '
                                 'Give us a call and we will take the '
                                 'rest by hand.'}), 429

    data = request.get_json(silent=True) or {}
    rows = data.get('rows') or []
    if not isinstance(rows, list) or not rows:
        return jsonify({'error': 'Nothing to send.'}), 400
    if len(rows) > MAX_ROWS:
        rows = rows[:MAX_ROWS]

    # Only rows the dealer left checked, with a real VIN. Everything the
    # dealer typed in the review table wins over what we parsed — they are
    # looking at the car, we are looking at a picture of a spreadsheet.
    keep = []
    deselected = 0   # the dealer unticked these on purpose — not a problem
    invalid = 0      # we could not read a complete VIN — worth telling them
    for r in rows:
        if r.get('skip'):
            deselected += 1
            continue
        vin = (r.get('vin') or '').strip().upper()
        if not _vin_ok(vin):
            invalid += 1
            continue
        keep.append({**r, 'vin': vin})
    if not keep:
        return jsonify({'error': 'None of these have a complete 17-character '
                                 'VIN. Fix the ones in red and try again.'}), 400

    source_name = link['dealer_name']
    if link.get('contact_name'):
        source_name = f"{link['contact_name']} @ {link['dealer_name']}"

    try:
        result = _DEPS['commit_rows'](
            rows=keep,
            source_name=source_name,
            delay_seconds=DEFAULT_DELAY_SECONDS,
            client_ip=_client_ip(),
            uploaded_by=f'dealer-link:{token}',
            contact_phone=(link.get('contact_phone') or None),
            contact_name=(link.get('contact_name')
                          or link['dealer_name']),
            creation_source='dealer_intake',
        )
    except Exception as e:
        print(f'[dealer_intake] commit failed token={token}: '
              f'{type(e).__name__}: {e}', flush=True)
        return jsonify({'error': 'Something went wrong on our end. Your '
                                 'list did not go through — please call '
                                 'us.'}), 500

    created = result.get('created') or []

    # Bookkeeping + operator alert. Neither may block the dealer's success
    # response, so both are best-effort.
    try:
        db = _DEPS['get_db']()
        cur = db.cursor()
        cur.execute("""
            UPDATE intake_links
               SET submission_count = COALESCE(submission_count,0) + 1,
                   vehicle_count    = COALESCE(vehicle_count,0) + %s,
                   last_used_at     = NOW()
             WHERE id = %s
        """, (len(created), link['id']))
        cur.execute("""
            INSERT INTO intake_submissions
                (link_id, dealer_name, file_names, row_count, created_count,
                 bulk_upload_id, client_ip, user_agent)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        """, (link['id'], link['dealer_name'],
              (data.get('files') or '')[:500] if isinstance(
                  data.get('files'), str) else ', '.join(
                      data.get('files') or [])[:500],
              len(rows), len(created), result.get('bulk_upload_id'),
              _client_ip(), (request.headers.get('User-Agent') or '')[:300]))
        db.commit()
        db.close()
    except Exception as e:
        print(f'[dealer_intake] bookkeeping failed: {e}', flush=True)

    try:
        sms = _DEPS.get('send_sms')
        phone = _DEPS.get('alert_phone')
        if sms and phone:
            sms(phone,
                f"EW: {source_name} sent {len(created)} vehicle"
                f"{'' if len(created) == 1 else 's'} through their upload "
                f"link. Bids are enriching now.")
    except Exception as e:
        print(f'[dealer_intake] alert failed: {e}', flush=True)

    # RECEIPT ONLY — no valuations, no numbers, no /m/ link. The enrichment
    # deny-by-default rule covers what a submitter may be shown, and this
    # page is a submitter surface.
    return jsonify({
        'ok': True,
        'created': len(created),
        'invalid': invalid,          # left out: no complete VIN
        'deselected': deselected,    # left out: the dealer unticked them
        'dealer_name': link['dealer_name'],
    })


# ── Admin: the tab EW employees use to make a link ────────────────────────
@bp.route('/admin/intake-links')
def admin_intake_links_page():
    return render_template('admin_intake_links.html')


@bp.route('/api/admin/intake-links', methods=['GET'])
def admin_intake_links_list():
    db = _DEPS['get_db']()
    cur = db.cursor()
    cur.execute("""
        SELECT id, token, dealer_id, dealer_name, contact_name, contact_phone,
               contact_email, note, created_by, created_at, revoked,
               last_used_at, submission_count, vehicle_count
          FROM intake_links
         ORDER BY revoked ASC, COALESCE(last_used_at, created_at) DESC
         LIMIT 500
    """)
    out = [dict(r) for r in cur.fetchall()]
    db.close()
    for r in out:
        for k in ('created_at', 'last_used_at'):
            if r.get(k):
                r[k] = r[k].isoformat()
    return jsonify({'links': out})


@bp.route('/api/admin/intake-links', methods=['POST'])
def admin_intake_links_create():
    d = request.get_json(silent=True) or {}
    dealer_name = (d.get('dealer_name') or '').strip()[:200]
    if not dealer_name:
        return jsonify({'error': 'dealer_name required'}), 400
    dealer_id = d.get('dealer_id')
    try:
        dealer_id = int(dealer_id) if dealer_id else None
    except (TypeError, ValueError):
        dealer_id = None

    token = secrets.token_urlsafe(9)   # ~12 chars, short enough to text
    db = _DEPS['get_db']()
    cur = db.cursor()
    cur.execute("""
        INSERT INTO intake_links
            (token, dealer_id, dealer_name, contact_name, contact_phone,
             contact_email, note, created_by)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id, token
    """, (token, dealer_id, dealer_name,
          (d.get('contact_name') or '').strip()[:120] or None,
          (d.get('contact_phone') or '').strip()[:40] or None,
          (d.get('contact_email') or '').strip()[:160] or None,
          (d.get('note') or '').strip()[:400] or None,
          session.get('username') or 'admin'))
    row = cur.fetchone()
    db.commit()
    db.close()
    return jsonify({'ok': True, 'id': row['id'], 'token': row['token'],
                    'url': f"{request.url_root.rstrip('/')}"
                           f"/dealer-intake/{row['token']}"})


@bp.route('/api/admin/intake-links/<int:link_id>/revoke', methods=['POST'])
def admin_intake_links_revoke(link_id):
    d = request.get_json(silent=True) or {}
    revoke = bool(d.get('revoke', True))
    db = _DEPS['get_db']()
    cur = db.cursor()
    cur.execute("""
        UPDATE intake_links
           SET revoked = %s,
               revoked_at = CASE WHEN %s THEN NOW() ELSE NULL END
         WHERE id = %s
    """, (revoke, revoke, link_id))
    db.commit()
    db.close()
    return jsonify({'ok': True, 'revoked': revoke})


@bp.route('/api/admin/intake-links/<int:link_id>', methods=['DELETE'])
def admin_intake_links_delete(link_id):
    """Remove a link permanently.

    Only a REVOKED link can be deleted. That makes it a deliberate two-step —
    revoke, then delete — so a live link a dealer is using can never vanish on
    a mis-click, and there is no modal to confirm through.

    Bids are NOT affected: nothing in `bids` references intake_links. The
    submission history rows go with it, since they are only meaningful in the
    context of the link.
    """
    db = _DEPS['get_db']()
    cur = db.cursor()
    cur.execute('SELECT revoked, dealer_name FROM intake_links WHERE id = %s',
                (link_id,))
    row = cur.fetchone()
    if not row:
        db.close()
        return jsonify({'error': 'no such link'}), 404
    if not row['revoked']:
        db.close()
        return jsonify({'error': 'Revoke the link first, then delete it.'}), 400
    cur.execute('DELETE FROM intake_submissions WHERE link_id = %s', (link_id,))
    cur.execute('DELETE FROM intake_links WHERE id = %s', (link_id,))
    db.commit()
    db.close()
    return jsonify({'ok': True, 'deleted': row['dealer_name']})


@bp.route('/api/admin/intake-links/dealer-search')
def admin_intake_dealer_search():
    """Typeahead over the existing dealers table so a link can be attached to
    a real dealer row instead of a free-typed name."""
    q = (request.args.get('q') or '').strip()
    if len(q) < 2:
        return jsonify({'dealers': []})
    db = _DEPS['get_db']()
    cur = db.cursor()
    cur.execute("""
        SELECT id, name, city, state, phone
          FROM dealers
         WHERE name ILIKE %s
         ORDER BY active DESC, name ASC
         LIMIT 12
    """, (f'%{q}%',))
    out = [dict(r) for r in cur.fetchall()]
    db.close()
    return jsonify({'dealers': out})


def init(app, *, get_db, vision_fn, text_fn, commit_rows, send_sms=None,
         alert_phone=None):
    """Register the blueprint and wire the callables this module needs.
    Kept as explicit dependencies (not imports) so dealer_intake.py never
    imports app.py — the same contract bulk_upload.py already uses."""
    _DEPS.update({
        'get_db': get_db,
        'vision_fn': vision_fn,
        'text_fn': text_fn,
        'commit_rows': commit_rows,
        'send_sms': send_sms,
        'alert_phone': alert_phone,
    })
    app.register_blueprint(bp)
    _ensure_schema()
