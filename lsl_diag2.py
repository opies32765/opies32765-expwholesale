# -*- coding: utf-8 -*-
# READ-ONLY: find the out-of-sequence LL121xxx / TST / test records to delete.
import json, requests
API = 'https://api.livesaleslog.com'
def ses():
    s = requests.Session()
    s.headers.update({'content-type':'application/json','accept':'application/json, text/plain, */*',
        'origin':'https://app.livesaleslog.com','referer':'https://app.livesaleslog.com/',
        'x-requested-with':'XMLHttpRequest','x-app-version':'6.2.9','user-agent':'Mozilla/5.0'})
    for c in json.load(open('/opt/livesaleslog/session.json')):
        s.cookies.set(c['name'], c['value'], domain=c.get('domain'))
    return s
s = ses()
def deals(q):
    rb={'dealerId':10010,'query':q,'tabIndex':0,'pageSize':40,'sortBy':'soldAt.desc','page':0,'showUnGrouped':True,
        'collapseGroups':False,'statusId':[-1],'startDate':None,'endDate':None,'showBackupDealsOnly':False,
        'saleType':'-1','vehicleType':'-1','salesmanId':0,'fiId':0,'appointmentStatusId':[-1],'typeId':'-1',
        'makeIds':[-1],'showAllocatedDealsOnly':False,'showWithTradeInIncomplete':False,'showWithTradeIn':False,
        'showBuyersAgreementUploadedOnly':False,'showMissingBuyersAgreement':False,'showPreDealsOnly':False,'showCancelledDeals':True}
    return s.post(API+'/deal/list', json=rb, timeout=60)
def invq(q):
    body={'showUnGrouped':True,'collapseGroups':False,'dealerId':10010,'showInStockOnly':False,'query':q,
          'typeId':[-1],'saleType':[-1],'tabIndex':0,'pageSize':40,'sortBy':'stockNo.desc','page':0,
          'makeIds':[-1],'startDate':None,'endDate':None,'agingIndex':0,'status':[-1]}
    return s.post(API+'/inventory/list', json=body, timeout=60)
for q in ['LL121','121458','TST','TESTPUSH']:
    print('=== DEALS query %r ===' % q)
    r=deals(q)
    try:
        for it in (r.json().get('results') or []):
            print('  DEAL code=%s stock=%s id=%s inventoryId=%s vin=%s created=%s' % (it.get('code'),it.get('stockNo'),it.get('id'),it.get('inventoryId'),it.get('vinNo'),it.get('createdAt')))
    except Exception as e: print('  err',r.status_code,r.text[:200])
    print('=== INVENTORY query %r ===' % q)
    r=invq(q)
    try:
        for it in (r.json().get('results') or []):
            print('  INV stock=%s id=%s vin=%s created=%s' % (it.get('stockNo'),it.get('id'),it.get('vinNo'),it.get('createdAt')))
    except Exception as e: print('  err',r.status_code,r.text[:200])
