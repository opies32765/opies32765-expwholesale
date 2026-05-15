"""Phase D — verification gate + miles-discrepancy SMS + auto-clear on edit
+ manual override endpoint + accutrade-vin-not-found needs_verification stamp
+ iPacket 5-min preservation on reprocess paths.

Single idempotent patcher for app.py. Marker: PHASE_D_VERIFY_GATE_2026_05_15.

Sections:
  1. _maybe_send_miles_verify_sms helper.
  2. AI assessment gate inside _maybe_fire_assessment.
  3. /api/internal/bid/<id>/miles-verify-sms — endpoint for miles_audit_worker.
  4. /api/bid/<id>/clear-verification — operator dismiss button endpoint
     (with iPacket 5-min preservation).
  5. Auto-clear in /api/bid/<id>/update (with iPacket 5-min preservation).
  6. /api/accutrade/submit — also stamp needs_verification_at='vin_not_found'.
  7. /api/admin/bid/<id>/force-reprocess — iPacket 5-min preservation
     (so a quick reprocess doesn't blow away a recently-captured sticker that
     iPacket would now rate-limit refetching).
"""
import sys, shutil

PATH = "/opt/expwholesale/app.py"
with open(PATH, "r", encoding="utf-8") as f:
    src = f.read()

if "PHASE_D_VERIFY_GATE_2026_05_15" in src:
    print("already patched")
    sys.exit(0)


# ─────────────────────────────────────────────────────────────────────────────
# Reused SQL fragment — preserve iPacket rows that are <5 min old AND were
# successfully captured. This avoids clobbering a good sticker during a
# rapid reprocess, since iPacket rate-limits same-VIN resubmits for ~1h.
IPACKET_PRESERVE_SQL = """DELETE FROM ipacket_lookups
             WHERE bid_id = %s
               AND (looked_up_at IS NULL
                    OR looked_up_at < NOW() - INTERVAL '5 minutes'
                    OR not_available = true)"""

# ─────────────────────────────────────────────────────────────────────────────
# 1) _maybe_send_miles_verify_sms helper — inserted BEFORE _maybe_send_vin_verify_sms.
ANCHOR_VIN_VERIFY = "# VIN_VERIFY_SMS_2026_05_15: auto-text bidder when AccuTrade can't find the"

MILES_VERIFY_HELPER = '''# PHASE_D_VERIFY_GATE_2026_05_15: miles-discrepancy SMS helper. Triggered by
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
            f"Hi {_first}, it's Oscar from Experience Wholesale. "
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
                f"\\U0001f504 EW auto-miles-verify SMS sent\\n"
                f"bid <b>#{bid_id}</b> \\u00b7 {_first} \\u00b7 {_phone}\\n"
                f"{_vehicle}\\n"
                f"customer: <b>{_customer_miles:,} mi</b>, "
                f"{_higher_source}: <b>{_higher_miles:,} mi</b> "
                f"(\\u0394 +{_delta:,})"
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


'''

if ANCHOR_VIN_VERIFY not in src:
    sys.stderr.write("ANCHOR_VIN_VERIFY not found\\n")
    sys.exit(2)
src = src.replace(ANCHOR_VIN_VERIFY, MILES_VERIFY_HELPER + ANCHOR_VIN_VERIFY, 1)


# ─────────────────────────────────────────────────────────────────────────────
# 2) AI assessment gate inside _maybe_fire_assessment.
ASSESS_OLD = """        cur.execute("SELECT ai_assessed_at, ai_assessment FROM bids WHERE id=%s", (bid_id,))
        row = cur.fetchone()
        if not row:
            db.close()
            return False
        # If already claimed or assessed, bail
        if row['ai_assessed_at'] is not None:
            db.close()
            return False"""

ASSESS_NEW = """        cur.execute("SELECT ai_assessed_at, ai_assessment, "
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
            return False"""

if ASSESS_OLD not in src:
    sys.stderr.write("ASSESS_OLD anchor not found\\n")
    sys.exit(3)
src = src.replace(ASSESS_OLD, ASSESS_NEW, 1)


# ─────────────────────────────────────────────────────────────────────────────
# 3+4) New endpoints — inserted before api_accutrade_submit.
NEW_ENDPOINTS_ANCHOR = "@app.route('/api/accutrade/submit', methods=['POST'])\ndef api_accutrade_submit():"

NEW_ENDPOINTS = '''@app.route('/api/internal/bid/<int:bid_id>/miles-verify-sms', methods=['POST'])
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
            cur.execute(IPACKET_PRESERVE_SQL_PLACEHOLDER, (bid_id,))
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
            f"\\u2705 EW verify flag cleared (operator)\\n"
            f"bid <b>#{bid_id}</b> note: {note}"
            + (' \\u00b7 force-reprocess fired' if do_reprocess else '')
            + (' (forced iPacket refetch)' if force_ipacket else ''))
    except Exception:
        pass
    return jsonify({'ok': True, 'bid_id': bid_id,
                    'reprocessed': do_reprocess,
                    'forced_ipacket': force_ipacket})


'''.replace("IPACKET_PRESERVE_SQL_PLACEHOLDER",
            '"""' + IPACKET_PRESERVE_SQL + '"""')

if NEW_ENDPOINTS_ANCHOR not in src:
    sys.stderr.write("NEW_ENDPOINTS_ANCHOR not found\\n")
    sys.exit(4)
src = src.replace(NEW_ENDPOINTS_ANCHOR, NEW_ENDPOINTS + NEW_ENDPOINTS_ANCHOR, 1)


# ─────────────────────────────────────────────────────────────────────────────
# 5) Auto-clear in /api/bid/<id>/update.
UPDATE_OLD = """    # Market check fires after DB close so worker thread doesn't share cur.
    if _vin_just_added:
        try:
            trigger_market_check(bid_id, _new_vin_in)
        except Exception as _e:
            print(f'[update_bid] market_check error bid={bid_id}: {_e}', flush=True)

    return jsonify({'success': True, 'vin_pipeline_triggered': _vin_just_added})"""

UPDATE_NEW = ("""    # PHASE_D_VERIFY_GATE_2026_05_15: VIN or miles edit clears open verify
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
                _vcur.execute(\"\"\"""" + IPACKET_PRESERVE_SQL + """\"\"\", (bid_id,))
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
                        f\"\\u2705 EW verify flag auto-cleared (VIN/miles edit)\\n\"
                        f\"bid <b>#{bid_id}</b> \\u00b7 force-reprocess fired \"
                        f\"(iPacket preserved if <5min)\")
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
                    'verification_cleared': _verif_cleared})""")

if UPDATE_OLD not in src:
    sys.stderr.write("UPDATE_OLD anchor not found\\n")
    sys.exit(5)
src = src.replace(UPDATE_OLD, UPDATE_NEW, 1)


# ─────────────────────────────────────────────────────────────────────────────
# 6) Extend /api/accutrade/submit hook — stamp needs_verification_at too.
ACCUTRADE_HOOK_OLD = """            _maybe_send_vin_verify_sms(bid_id, reason='accutrade_vin_not_found')
    except Exception as _vvse:
        print(f'[vin-verify-sms] hook error bid={bid_id}: '
              f'{type(_vvse).__name__}: {_vvse}', flush=True)"""

ACCUTRADE_HOOK_NEW = """            _maybe_send_vin_verify_sms(bid_id, reason='accutrade_vin_not_found')
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
              f'{type(_vvse).__name__}: {_vvse}', flush=True)"""

if ACCUTRADE_HOOK_OLD not in src:
    sys.stderr.write("ACCUTRADE_HOOK_OLD anchor not found\\n")
    sys.exit(6)
src = src.replace(ACCUTRADE_HOOK_OLD, ACCUTRADE_HOOK_NEW, 1)


# ─────────────────────────────────────────────────────────────────────────────
# 7) Force-reprocess endpoint: preserve recent iPacket unless force_ipacket=true.
FORCE_REPROCESS_OLD = """    # Wipe phase-1 lookups
    cur.execute('DELETE FROM ipacket_lookups WHERE bid_id=%s', (bid_id,))
    n_ipacket = cur.rowcount
    cur.execute('DELETE FROM accutrade_lookups WHERE bid_id=%s', (bid_id,))
    n_accu = cur.rowcount
    cur.execute('DELETE FROM vauto_lookups WHERE bid_id=%s', (bid_id,))
    n_vauto = cur.rowcount"""

FORCE_REPROCESS_NEW = """    # Wipe phase-1 lookups. PHASE_D_VERIFY_GATE_2026_05_15: iPacket has a
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
        cur.execute(\"\"\"""" + IPACKET_PRESERVE_SQL + """\"\"\", (bid_id,))
    n_ipacket = cur.rowcount
    cur.execute('DELETE FROM accutrade_lookups WHERE bid_id=%s', (bid_id,))
    n_accu = cur.rowcount
    cur.execute('DELETE FROM vauto_lookups WHERE bid_id=%s', (bid_id,))
    n_vauto = cur.rowcount"""

if FORCE_REPROCESS_OLD not in src:
    sys.stderr.write("FORCE_REPROCESS_OLD anchor not found\\n")
    sys.exit(7)
src = src.replace(FORCE_REPROCESS_OLD, FORCE_REPROCESS_NEW, 1)


# ─────────────────────────────────────────────────────────────────────────────
# 8) TESLA_DASH_FIX_2026_05_15 — fix extract_mileage_from_file so the
# Tesla-dash "234 mi range" status-bar reading doesn't beat the real
# "72,785 mi" odometer. Strategy:
#   - Collect ALL "(N) mi/miles/km" matches (not just the first).
#   - Filter Tesla-specific noise contexts (range/trip/remaining/battery/etc).
#   - Return the LARGEST plausible survivor.
# Odometers don't legally go down, so MAX is the correct rule. Original
# code returned the FIRST match — top-of-image numbers always won, which
# is exactly where Tesla shows the range indicator.
MILEAGE_OCR_OLD = """        # Then: numbers explicitly labeled "mi" / "miles" / "km" — label AFTER number
        labeled = re.findall(r'(\\d{1,3}(?:,\\d{3})+|\\d{3,7})\\s*(?:MI|MILES|KM)\\b', up)
        if labeled:
            for c in labeled:
                n = int(c.replace(',', ''))
                if 100 <= n <= 999999:
                    print(f'[OCR] Mileage via Google Vision (labeled): {n}', flush=True)
                    return n"""

MILEAGE_OCR_NEW = """        # Then: numbers explicitly labeled "mi" / "miles" / "km" — label AFTER number.
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
                r'(\\d{1,3}(?:,\\d{3})+|\\d{3,7})\\s*(?:MI|MILES|KM)\\b', up):
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
            return _result"""

if MILEAGE_OCR_OLD not in src:
    sys.stderr.write("MILEAGE_OCR_OLD anchor not found\n")
    sys.exit(8)
src = src.replace(MILEAGE_OCR_OLD, MILEAGE_OCR_NEW, 1)


# Write
bak = PATH + ".bak.20260515-phase-d-verify-gate"
shutil.copy(PATH, bak)
with open(PATH, "w", encoding="utf-8") as f:
    f.write(src)

import os as _check_os
new_size = _check_os.path.getsize(PATH)
print(f"patched: app.py written ({new_size} bytes)")
print(f"backup: {bak}")
print("NOTE: code is staged. NOT loaded into running gunicorn. HUP to activate.")
