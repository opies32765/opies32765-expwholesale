
# ============================================================================
# LSL_WIZARD_2026_06_05 - guided "Book to LSL" wizard backend (ADDITIVE).
# Consumed by /static/lsl_wizard.js. Leaves LSL_BOOK_TO_LSL_2026_05_21 intact.
# STAGE-ONLY (no live LSL POST yet). Corrected model:
#   bought-from (purchasedFrom*/source) vs sold-to (customer*==supplier*=buyer);
#   buyerName=buy-rep, salesPersonName=sell-rep;
#   fees -> supplementaryCostsDescription + totalSupplementaryCosts;
#   payoff = separate PurchasePayoff payment (payee Bank), NOT deal fields.
# Payoff -> notify Evelyn by TEXT + MMS (DL photo) from EW's Twilio number,
#   with full seller info incl SSN last-4. NO email (operator opted out of Resend).
#   SSN/DL live EW-side only (LSL has no field).
# ============================================================================
import os as _os_lslw
EVELYN_PAYOFF_CELL = _os_lslw.environ.get('EVELYN_PAYOFF_CELL', '4074309675')  # test-safe; set to +19546758854 (Evelyn) to go live
_LSLW_DL_DIR = '/opt/expwholesale/static/uploads/dl'
_LSLW_PUBLIC_BASE = _os_lslw.environ.get('PORTAL_BASE', 'https://experience-wholesale.net')

def _lslw_num(v, default=0):
    try:
        if v is None or v == '':
            return default
        return int(round(float(str(v).replace(',', '').replace('$', '').strip())))
    except Exception:
        return default

def _lslw_vehicle(bid):
    yr = bid.get('canon_year') or bid.get('year') or ''
    mk = (bid.get('canon_make') or bid.get('make') or '').strip()
    md = (bid.get('canon_model') or bid.get('model') or '').strip()
    tr = (bid.get('canon_trim') or bid.get('trim') or '').strip()
    info = ' '.join(str(x) for x in [yr, mk, md, tr] if x)
    return yr, mk, md, tr, info

def _lslw_send_mms(to_addr, body, media_url):
    """MMS via EW's existing Twilio number (same creds as send_sms)."""
    try:
        from twilio.rest import Client as _TwC
        _TwC(TWILIO_SID, TWILIO_TOKEN).messages.create(to=to_addr, from_=TWILIO_PHONE, body=body, media_url=[media_url])
        return True
    except Exception as _e:
        print('[lslw mms FAIL] %s: %s' % (type(_e).__name__, _e), flush=True)
        return False

def _build_lsl_stage_payload(bid, data, supplier, pending_customer, buyer_dealer, buy_rep, sell_rep):
    now_iso = datetime.utcnow().replace(microsecond=0).isoformat()
    yr, mk, md, tr, vinfo = _lslw_vehicle(bid)
    source_kind = (data.get('source_kind') or 'wholesaler').strip().lower()
    if source_kind == 'individual':
        pc = pending_customer or {}
        bought_name = (pc.get('full_name') or (str(pc.get('first_name') or '') + ' ' + str(pc.get('last_name') or '')).strip())
        pft = 'Individual'; pfid = pc.get('lsl_id')
    elif source_kind == 'factory':
        bought_name = (data.get('factory_name') or '').strip(); pft = 'Factory'; pfid = None
    else:
        bought_name = (supplier or {}).get('name') or ''; pft = 'Wholesaler'; pfid = (supplier or {}).get('id')
    bd = buyer_dealer or {}
    not_sold = bool(data.get('not_sold'))
    sell_kind = (data.get('sell_kind') or 'wholesale').strip().lower()
    fees = data.get('fees') or {}
    pack = _lslw_num(fees.get('pack'), 100); transport = _lslw_num(fees.get('transport'), 0)
    referral = _lslw_num(fees.get('referral'), 0); recon = _lslw_num(fees.get('recon'), 0)
    mcd = _lslw_num(fees.get('mcd'), 0)
    referral_name = (fees.get('referral_name') or '').strip()
    supp_total = pack + transport + referral + recon + mcd
    ref_label = ('Referral Fee-%s' % referral_name) if referral_name else 'Referral Fee'
    supp_desc = ('Inventory Pack - $%d.00|Transport - $%d.00|%s - $%d.00|Recon - $%d.00|MCD Live Fee - $%d.00|'
                 % (pack, transport, ref_label, referral, recon, mcd))
    purchase_cost = _lslw_num(data.get('purchase_cost'), 0)
    sale_price = 0 if not_sold else _lslw_num(data.get('sale_price'), 0)
    title_status = (data.get('title_status') or 'Yes').strip()
    inventory = {
        'vinNo': bid.get('vin') or '', 'stockNo': '', 'makeId': _lsl_make_id(mk), 'makeName': mk,
        'vehicleMakeName': mk, 'groupModelName': md, 'groupModelTrim': tr,
        'groupModelTrimYear': yr or None, 'name': vinfo, 'usage': _lslw_num(bid.get('mileage'), 0),
        'type': 'Purchased', 'purchasedFromType': pft, 'purchasedFromId': pfid, 'source': bought_name,
        'purchaseCost': purchase_cost, 'purchaseCostTotal': purchase_cost + supp_total,
        'titleStatus': title_status, 'originalTitleReceived': (title_status == 'Yes'),
        'dispositionIntention': (data.get('disposition_intention') or 'WholesaleImmediately'),
        'retailDays': _lslw_num(data.get('retail_days'), 0),
        'possessionStatus': (data.get('possession_status') or 'Arrived'),
        'saleStatus': ('Not Sold' if not_sold else 'Sold'), 'sold': (not not_sold),
        'dealerId': LSL_DEALER_ID, 'subscriberId': LSL_SUBSCRIBER_ID,
    }
    deal = {
        'vinNo': bid.get('vin') or '', 'makeId': _lsl_make_id(mk), 'makeName': mk,
        'vehicleInfo': vinfo, 'vehicleSaleType': 'Used',
        'supplierId': bd.get('supplier_id') or bd.get('id'), 'supplierName': bd.get('name') or '',
        'customerId': bd.get('customer_id'), 'customerName': bd.get('name') or '',
        'customerType': ('Individual' if sell_kind == 'retail' else (bd.get('customer_type') or '')),
        'purchasedFromType': pft, 'purchasedFromId': pfid, 'source': bought_name,
        'purchaseCost': purchase_cost, 'salePrice': sale_price,
        'frontValue': sale_price - purchase_cost - supp_total,
        'totalSupplementaryCosts': supp_total, 'supplementaryCostsDescription': supp_desc,
        'buyerName': (buy_rep or {}).get('full_name') or '',
        'salesPersonName': (sell_rep or {}).get('full_name') or (buy_rep or {}).get('full_name') or '',
        'salesManagerName': (sell_rep or {}).get('full_name') or '', 'bookedBy': (sell_rep or {}).get('full_name') or '',
        'saleType': ('Retail' if sell_kind == 'retail' else 'Wholesale'),
        'type': ('AwaitingArrival' if not_sold else 'Booked'), 'status': 'Active',
        'dealerId': LSL_DEALER_ID, 'subscriberId': LSL_SUBSCRIBER_ID, 'dealerName': 'Experience Wholesale',
        'soldAt': (None if not_sold else now_iso),
    }
    out = {'inventory': inventory, 'deal': deal,
           'fees': {'pack': pack, 'transport': transport, 'referral': referral, 'recon': recon, 'mcd': mcd, 'referral_name': referral_name, 'total': supp_total},
           '_ew_origin': {'bid_id': bid.get('id'), 'staged_at': now_iso, 'source_kind': source_kind}}
    if source_kind == 'individual' and pending_customer is not None:
        out['customer'] = {k: pending_customer.get(k) for k in ('id', 'full_name', 'first_name', 'last_name', 'mobile', 'email', 'full_address', 'drivers_license') if k in pending_customer}
    if title_status == 'PayOff':
        po = data.get('payoff') or {}
        out['payoff_payment'] = {
            'sourceType': 'PurchasePayoff', 'payeeType': 'Bank', 'isPayOff': True,
            'vendorName': (po.get('lien_company') or '').strip(), 'payoffAmount': _lslw_num(po.get('amount'), 0),
            'goodUntilDate': (po.get('good_until') or '').strip(), 'cdkAccountNo': (po.get('account_no') or '').strip(),
            'paymentReqType': 'Check',
        }
    return out


@app.route('/api/bid/<int:bid_id>/dl-upload', methods=['POST'])
def api_bid_dl_upload(bid_id):
    """Quickdrop: store a driver's-license photo under public uploads with an
    unguessable name so it can be MMS'd to Evelyn. Returns its URL path."""
    f = request.files.get('file') or request.files.get('dl')
    if not f or not f.filename:
        return jsonify({'ok': False, 'error': 'no file'}), 400
    ext = (f.filename.rsplit('.', 1)[-1] if '.' in f.filename else 'jpg').lower()[:5]
    if ext not in ('jpg', 'jpeg', 'png', 'gif', 'webp', 'heic'):
        ext = 'jpg'
    d = _os_lslw.path.join(_LSLW_DL_DIR, str(bid_id))
    _os_lslw.makedirs(d, exist_ok=True)
    fn = 'dl_%s_%s.%s' % (datetime.utcnow().strftime('%Y%m%d%H%M%S'), _os_lslw.urandom(5).hex(), ext)
    f.save(_os_lslw.path.join(d, fn))
    url = '/static/uploads/dl/%d/%s' % (bid_id, fn)
    return jsonify({'ok': True, 'url': url})


@app.route('/api/bid/<int:bid_id>/book-context')
def api_bid_book_context(bid_id):
    db = get_db(); cur = db.cursor()
    try:
        cur.execute("SELECT * FROM bids WHERE id = %s", (bid_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({'ok': False, 'error': 'bid not found'}), 404
        bid = dict(row)
        yr, mk, md, tr, vinfo = _lslw_vehicle(bid)
        cur.execute("SELECT id, name, customer_type, supplier_id, customer_id FROM lsl_customers WHERE is_blocked = FALSE ORDER BY deals_12mo DESC NULLS LAST, name LIMIT 6")
        buyers = [dict(r) for r in cur.fetchall()]
        sa = bid.get('lsl_staged_at'); pa = bid.get('lsl_pushed_at')
        return jsonify({'ok': True, 'bid_id': bid_id,
            'vehicle': {'year': yr, 'make': mk, 'model': md, 'trim': tr, 'vin': bid.get('vin') or '', 'mileage': bid.get('mileage') or 0, 'label': vinfo},
            'prefill': {'purchase_cost': _lslw_num(bid.get('bid_amount'), 0),
                        'sale_hint': _lslw_num(bid.get('asking_price') or bid.get('ai_price'), 0),
                        'source': (bid.get('canon_source') or bid.get('source') or bid.get('lsl_source') or ''),
                        'fees': {'pack': 100, 'transport': 0, 'referral': 0, 'recon': 0, 'mcd': 0}},
            'buyer_candidates': buyers, 'evelyn_cell': EVELYN_PAYOFF_CELL,
            'state': {'staged_at': (sa.isoformat() if sa else None), 'pushed_at': (pa.isoformat() if pa else None)}})
    finally:
        db.close()


@app.route('/api/bid/<int:bid_id>/lsl-stage', methods=['POST'])
def api_bid_lsl_stage(bid_id):
    data = request.get_json(silent=True) or {}
    source_kind = (data.get('source_kind') or 'wholesaler').strip().lower()
    not_sold = bool(data.get('not_sold'))
    title_status = (data.get('title_status') or 'Yes').strip()
    if title_status not in ('Yes', 'Pending', 'PayOff', 'Lost'):
        return jsonify({'ok': False, 'error': 'invalid title_status: %s' % title_status}), 400
    if source_kind not in ('wholesaler', 'individual', 'factory'):
        return jsonify({'ok': False, 'error': 'invalid source_kind: %s' % source_kind}), 400
    db = get_db(); cur = db.cursor()
    try:
        cur.execute("SELECT * FROM bids WHERE id = %s", (bid_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({'ok': False, 'error': 'bid not found'}), 404
        bid = dict(row)
        supplier = None; pending_customer = None
        if source_kind == 'wholesaler':
            sid = data.get('supplier_id')
            if not sid:
                return jsonify({'ok': False, 'error': 'supplier_id required'}), 400
            cur.execute("SELECT id, name, primary_contact FROM lsl_suppliers WHERE id = %s", (sid,))
            supplier = cur.fetchone()
            if not supplier:
                return jsonify({'ok': False, 'error': 'supplier %s not found' % sid}), 404
            supplier = dict(supplier)
        elif source_kind == 'individual':
            s = data.get('seller') or {}; pcid = data.get('pending_customer_id')
            if pcid:
                cur.execute("SELECT * FROM lsl_pending_customers WHERE id = %s", (pcid,))
                r2 = cur.fetchone(); pending_customer = dict(r2) if r2 else None
            if pending_customer is None:
                fn = (s.get('first_name') or '').strip(); ln = (s.get('last_name') or '').strip()
                if not (fn or ln or s.get('company_name')):
                    return jsonify({'ok': False, 'error': 'seller name required'}), 400
                full = (s.get('full_name') or (fn + ' ' + ln)).strip()
                addr_parts = [x for x in [s.get('address_street'), s.get('address_city'), s.get('address_state'), s.get('address_postal_code')] if x]
                cur.execute("INSERT INTO lsl_pending_customers (type, first_name, last_name, full_name, mobile, email, address_street, address_city, address_state, address_postal_code, full_address, drivers_license, ssn_last4, dl_photo_path, created_by_bid_id) VALUES ('Individual',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                    (fn or None, ln or None, full, (s.get('mobile') or None), (s.get('email') or None), (s.get('address_street') or None), (s.get('address_city') or None), (s.get('address_state') or None), (s.get('address_postal_code') or None), (', '.join(addr_parts) or None), (s.get('drivers_license') or None), (s.get('ssn_last4') or None), (s.get('dl_photo_url') or None), bid_id))
                pending_customer = dict(cur.fetchone())
        buyer_dealer = None; bcid = data.get('buyer_customer_id')
        if not not_sold and bcid:
            cur.execute("SELECT id, name, customer_type, supplier_id, customer_id FROM lsl_customers WHERE id = %s", (bcid,))
            r3 = cur.fetchone(); buyer_dealer = dict(r3) if r3 else None
        def _rep(rid):
            if not rid:
                return None
            cur.execute("SELECT id, full_name FROM lsl_sales_reps WHERE id = %s", (rid,))
            rr = cur.fetchone(); return dict(rr) if rr else None
        buy_rep = _rep(data.get('buy_rep_id')); sell_rep = _rep(data.get('sell_rep_id'))
        payload = _build_lsl_stage_payload(bid, data, supplier, pending_customer, buyer_dealer, buy_rep, sell_rep)
        fees = payload['fees']; po = data.get('payoff') or {}
        cur.execute("UPDATE bids SET lsl_seller_kind=%s, lsl_supplier_id=%s, lsl_supplier_name=%s, lsl_pending_customer_id=%s, lsl_buyer_customer_id=%s, lsl_buyer_dealer_name=%s, lsl_purchase_cost=%s, lsl_sale_price=%s, lsl_sell_type=%s, lsl_disposition_intention=%s, lsl_possession_status=%s, lsl_not_sold=%s, lsl_source=%s, lsl_title_status=%s, lsl_payoff_amount=%s, lsl_lienholder_name=%s, lsl_good_until=%s, lsl_lien_account_no=%s, lsl_text_evelyn=%s, lsl_fee_pack=%s, lsl_fee_transport=%s, lsl_fee_referral=%s, lsl_fee_recon=%s, lsl_fee_mcd=%s, lsl_total_supp_costs=%s, lsl_sales_person_id=%s, lsl_buyer_id=%s, lsl_staged_at=NOW(), lsl_book_payload=%s::jsonb WHERE id=%s",
            (source_kind, (supplier or {}).get('id'), (supplier or {}).get('name'), (pending_customer or {}).get('id'),
             (buyer_dealer or {}).get('id'), (buyer_dealer or {}).get('name'),
             _lslw_num(data.get('purchase_cost'), 0), _lslw_num(data.get('sale_price'), 0),
             payload['deal']['saleType'], payload['inventory']['dispositionIntention'], payload['inventory']['possessionStatus'],
             not_sold, payload['inventory']['source'], title_status, _lslw_num(po.get('amount'), 0), (po.get('lien_company') or None),
             (po.get('good_until') or None), (po.get('account_no') or None), bool(po.get('text_evelyn', True)),
             fees['pack'], fees['transport'], fees['referral'], fees['recon'], fees['mcd'], fees['total'],
             data.get('sell_rep_id'), data.get('buy_rep_id'), json.dumps(payload), bid_id))
        db.commit()

        # On EVERY payoff: TEXT Evelyn full info + MMS the DL photo (EW Twilio #). No email.
        notified = {'sms': False, 'mms': False}
        if title_status == 'PayOff' and po.get('text_evelyn', True):
            yr, mk, md, tr, vinfo = _lslw_vehicle(bid)
            pc = pending_customer or {}
            lines = [
                'EW Bid #%s just BOOKED - has a PAYOFF' % bid_id,
                '%s  VIN %s' % (vinfo, bid.get('vin') or '-'),
                'SSN last-4: %s' % (pc.get('ssn_last4') or '-'),
                'Lien: %s' % (po.get('lien_company') or '-'),
                'Payoff: $%s (good until %s)' % (_lslw_num(po.get('amount'), 0), po.get('good_until') or '-'),
                'Acct #: %s' % (po.get('account_no') or '-'),
                'Seller: %s' % (pc.get('full_name') or '-'),
                'Phone: %s  Email: %s' % (pc.get('mobile') or '-', pc.get('email') or '-'),
                'Address: %s' % (pc.get('full_address') or '-'),
                'DL #: %s' % (pc.get('drivers_license') or '-'),
            ]
            dlu = pc.get('dl_photo_path')
            sms_body = '\n'.join(lines) + ('\n(DL photo sent)' if dlu else '')
            try:
                send_sms(EVELYN_PAYOFF_CELL, sms_body); notified['sms'] = True
            except Exception as _e:
                app.logger.warning('Evelyn payoff SMS failed: %s' % _e)
            if dlu:
                media = dlu if str(dlu).startswith('http') else (_LSLW_PUBLIC_BASE + dlu)
                try:
                    notified['mms'] = _lslw_send_mms(EVELYN_PAYOFF_CELL, 'DL - %s' % vinfo, media)
                except Exception as _e:
                    app.logger.warning('Evelyn DL MMS failed: %s' % _e)

        return jsonify({'ok': True, 'staged': True, 'pushed': False,
                        'evelyn_texted': notified['sms'], 'evelyn_dl_mms': notified['mms'],
                        'note': 'Staged locally - no live LSL write yet.', 'payload': payload})
    finally:
        db.close()
# ============================ /LSL_WIZARD_2026_06_05 =========================
