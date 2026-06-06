# -*- coding: utf-8 -*-
# One-shot TAGGED test push of a staged EW booking into live LSL.
# Loads LSL session (read-side pattern), POSTs customer->inventory->deal->payoff,
# stops at first non-2xx, prints LSL's exact response. Everything tagged for deletion.
import json, sys, time, subprocess, requests

BID = sys.argv[1] if len(sys.argv) > 1 else '2546'
EXISTING_CUST = sys.argv[2] if len(sys.argv) > 2 else None
API = 'https://api.livesaleslog.com'
TAG = 'EW TEST PUSH - DELETE'

def staged_payload(bid):
    r = subprocess.run(['sudo', '-u', 'postgres', 'psql', '-p', '5433', '-d', 'expwholesale',
                        '-tAc', "SELECT lsl_book_payload FROM bids WHERE id=%s" % bid],
                       capture_output=True, text=True)
    return json.loads(r.stdout.strip())

def load_session():
    s = requests.Session()
    s.headers.update({
        'content-type': 'application/json',
        'accept': 'application/json, text/plain, */*',
        'origin': 'https://app.livesaleslog.com',
        'referer': 'https://app.livesaleslog.com/',
        'x-requested-with': 'XMLHttpRequest',
        'x-app-version': '6.2.9',
        'user-agent': 'Mozilla/5.0',
    })
    for c in json.load(open('/opt/livesaleslog/session.json')):
        s.cookies.set(c['name'], c['value'], domain=c.get('domain'))
    return s

def grab_id(res, *keys):
    if not isinstance(res, dict):
        return None
    for k in keys:
        if res.get(k) is not None:
            return res.get(k)
    for wrap in ('customer', 'inventory', 'deal', 'payment', 'result', 'data'):
        sub = res.get(wrap)
        if isinstance(sub, dict):
            for k in keys:
                if sub.get(k) is not None:
                    return sub.get(k)
    return None

def call(s, path, body, label):
    try:
        r = s.post(API + path, json=body, timeout=60)
    except Exception as e:
        print('  [%s] %s -> EXCEPTION %s' % (label, path, e)); return None, None
    print('  [%s] POST %s -> HTTP %s' % (label, path, r.status_code))
    print('  resp: %s' % (r.text[:1100],))
    if r.status_code // 100 == 2:
        try: return r.json(), r.status_code
        except Exception: return {}, r.status_code
    return None, r.status_code

def main():
    p = staged_payload(BID)
    inv = dict(p.get('inventory') or {})
    deal = dict(p.get('deal') or {})
    cust = p.get('customer')
    payoff = p.get('payoff_payment')
    print('=== TAGGED TEST PUSH bid %s | customer=%s payoff=%s ===' % (BID, bool(cust), bool(payoff)))
    s = load_session()

    # liveness check first (read) — confirms session valid before any write
    chk, code = call(s, '/user/list', {'page': 0, 'pageSize': 1, 'dealerId': 10010, 'query': '', 'sortBy': 'firstName.asc'}, 'SESSION-CHECK')
    if chk is None:
        print('SESSION INVALID (HTTP %s) — aborting before any write.' % code); return

    created = {}

    # 1) SELLER customer (override to clearly-fake TESTPUSH)
    if EXISTING_CUST:
        created['customer'] = EXISTING_CUST; inv['purchasedFromId'] = int(EXISTING_CUST)
        print('  [CUSTOMER] reusing existing %s (skip create)' % EXISTING_CUST)
    elif cust:
        ts = time.strftime('%m%d-%H%M%S')
        cpay = dict(cust)
        cpay.update({
            'firstName': 'TESTPUSH', 'lastName': ts, 'fullName': 'TESTPUSH ' + ts,
            'type': 'Individual', 'companyName': '',
            'email': 'opies32765@gmail.com', 'mobile': '4074309675',
            'fullAddress': cpay.get('full_address') or '', 'leadStatus': '', 'note': TAG,
            'defaultPaymentMethod': 'Check', 'subscriberId': 1, 'dealerId': 10010,
            'status': 'Active', 'verified': False, 'isBlocked': False,
            'isNewEntity': True, 'isExistingEntity': False,
        })
        res, code = call(s, '/customer/save', cpay, 'CUSTOMER')
        if res is None:
            print('STOP at customer/save (HTTP %s).' % code); return
        cid = grab_id(res, 'id', 'customerId')
        created['customer'] = cid; print('  -> customer id: %s' % cid)
        if cid:
            inv['purchasedFromId'] = cid

    # 2) INVENTORY
    inv['note'] = TAG; inv['isNewEntity'] = True; inv['isExistingEntity'] = False
    inv['stockNo'] = 'TST' + time.strftime('%H%M%S')
    inv['isCustomMakeModel'] = True
    inv['customVehicleMakeVersion'] = ((inv.get('makeName') or '') + ' ' + (inv.get('groupModelName') or '')).strip() or (inv.get('name') or 'Test Vehicle')
    res, code = call(s, '/inventory/save', inv, 'INVENTORY')
    if res is None:
        print('STOP at inventory/save (HTTP %s). created=%s' % (code, created)); return
    inv_id = grab_id(res, 'id', 'inventoryId'); stock = grab_id(res, 'stockNo')
    created['inventory'] = inv_id; created['stockNo'] = stock
    print('  -> inventory id: %s  stock: %s' % (inv_id, stock))

    # 3) DEAL
    if inv_id:
        deal['inventoryId'] = inv_id
    deal['isNewEntity'] = True; deal['isExistingEntity'] = False; deal['lastSaleNote'] = TAG
    res, code = call(s, '/deal/save', deal, 'DEAL')
    if res is None:
        print('STOP at deal/save (HTTP %s). created=%s' % (code, created)); return
    deal_id = grab_id(res, 'id', 'dealId'); dcode = grab_id(res, 'code', 'dealCode')
    created['deal'] = deal_id; created['dealCode'] = dcode
    print('  -> deal id: %s  code: %s' % (deal_id, dcode))

    # 4) PAYOFF payment
    if payoff:
        pp = dict(payoff)
        pp['note'] = TAG
        if inv_id: pp['inventoryId'] = inv_id
        if deal_id: pp['dealId'] = deal_id
        pp['dealerId'] = 10010; pp['subscriberId'] = 1
        res, code = call(s, '/payment/save', pp, 'PAYOFF')
        created['payoff_payment'] = grab_id(res, 'id') if res else ('FAILED HTTP %s' % code)

    print('=== DONE. CREATED (delete these in LSL): %s ===' % json.dumps(created))

main()
