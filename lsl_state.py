# -*- coding: utf-8 -*-
# READ-ONLY: current sequence health + status of the test MINI + the 3 real deals.
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
def deals(q='', sort='soldAt.desc'):
    rb={'dealerId':10010,'query':q,'tabIndex':0,'pageSize':12,'sortBy':sort,'page':0,'showUnGrouped':True,
        'collapseGroups':False,'statusId':[-1],'startDate':None,'endDate':None,'showBackupDealsOnly':False,
        'saleType':'-1','vehicleType':'-1','salesmanId':0,'fiId':0,'appointmentStatusId':[-1],'typeId':'-1',
        'makeIds':[-1],'showAllocatedDealsOnly':False,'showWithTradeInIncomplete':False,'showWithTradeIn':False,
        'showBuyersAgreementUploadedOnly':False,'showMissingBuyersAgreement':False,'showPreDealsOnly':False,'showCancelledDeals':True}
    return s.post(API+'/deal/list', json=rb, timeout=60)
print('=== newest deals (are new stock #s back to LL367xx?) ===')
r = deals()
for it in (r.json().get('results') or [])[:12]:
    print('  stock=%-10s code=%s vin=%s created=%s' % (it.get('stockNo'), it.get('code'), it.get('vinNo'), it.get('createdAt')))
print('\n=== the 3 real deals — current stock # (renumbered yet?) ===')
for vin in ['5N1DR3AA0RC263063','5N1BT3BA4RC752315','1GYS9HK90TR146756']:
    r = deals(q=vin)
    res = r.json().get('results') or []
    for it in res:
        if it.get('vinNo')==vin:
            print('  vin=%s stock=%s code=%s' % (vin, it.get('stockNo'), it.get('code')))
            break
    else:
        print('  vin=%s -> not found (gone?)' % vin)
print('\n=== test MINI (VIN WMW53DH01R2U95381) still present? ===')
body={'showUnGrouped':True,'collapseGroups':False,'dealerId':10010,'showInStockOnly':False,'query':'WMW53DH01R2U95381',
      'typeId':[-1],'saleType':[-1],'tabIndex':0,'pageSize':10,'sortBy':'stockNo.desc','page':0,'makeIds':[-1],
      'startDate':None,'endDate':None,'agingIndex':0,'status':[-1]}
r = s.post(API+'/inventory/list', json=body, timeout=60)
res = r.json().get('results') or []
print('  matches:', len(res))
for it in res:
    print('  stock=%s id=%s status=%s archived=%s' % (it.get('stockNo'), it.get('id'), it.get('status'), it.get('archiveRequested')))
