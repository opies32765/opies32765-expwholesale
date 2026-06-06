# -*- coding: utf-8 -*-
# READ-ONLY diagnostic of live LSL inventory/deals to find bad/test records + the stock sequence.
import json, requests
API = 'https://api.livesaleslog.com'
def load_session():
    s = requests.Session()
    s.headers.update({'content-type':'application/json','accept':'application/json, text/plain, */*',
        'origin':'https://app.livesaleslog.com','referer':'https://app.livesaleslog.com/',
        'x-requested-with':'XMLHttpRequest','x-app-version':'6.2.9','user-agent':'Mozilla/5.0'})
    for c in json.load(open('/opt/livesaleslog/session.json')):
        s.cookies.set(c['name'], c['value'], domain=c.get('domain'))
    return s
s = load_session()
def inv(sortBy, query=''):
    body = {'showUnGrouped':True,'collapseGroups':False,'dealerId':10010,'showInStockOnly':False,
            'query':query,'typeId':[-1],'saleType':[-1],'tabIndex':0,'pageSize':25,'sortBy':sortBy,
            'page':0,'makeIds':[-1],'startDate':None,'endDate':None,'agingIndex':0,'status':[-1]}
    r = s.post(API+'/inventory/list', json=body, timeout=60)
    return r
print('=== inventory by stockNo DESC (top of sequence) ===')
r = inv('stockNo.desc')
print('http', r.status_code)
try:
    d=r.json(); print('totalAvailable', d.get('totalAvailable'))
    for it in (d.get('results') or [])[:20]:
        print('  stock=%-14s id=%-9s vin=%-18s created=%s name=%s' % (it.get('stockNo'), it.get('id'), it.get('vinNo'), it.get('createdAt'), (it.get('name') or '')[:24]))
except Exception as e:
    print('parse err', e, r.text[:400])
print('\n=== inventory matching TST (my test) ===')
r = inv('stockNo.desc','TST')
try:
    for it in (r.json().get('results') or [])[:10]:
        print('  stock=%s id=%s created=%s' % (it.get('stockNo'), it.get('id'), it.get('createdAt')))
except Exception as e:
    print('err', r.status_code, r.text[:300])
print('\n=== recent deals ===')
rb={'dealerId':10010,'query':'','tabIndex':0,'pageSize':15,'sortBy':'soldAt.desc','page':0,'showUnGrouped':True,
    'collapseGroups':False,'statusId':[-1],'startDate':None,'endDate':None,'showBackupDealsOnly':False,
    'saleType':'-1','vehicleType':'-1','salesmanId':0,'fiId':0,'appointmentStatusId':[-1],'typeId':'-1',
    'makeIds':[-1],'showAllocatedDealsOnly':False,'showWithTradeInIncomplete':False,'showWithTradeIn':False,
    'showBuyersAgreementUploadedOnly':False,'showMissingBuyersAgreement':False,'showPreDealsOnly':False,'showCancelledDeals':False}
r = s.post(API+'/deal/list', json=rb, timeout=60)
print('http', r.status_code)
try:
    for it in (r.json().get('results') or [])[:15]:
        print('  code=%s stock=%s id=%s vin=%s created=%s' % (it.get('code'), it.get('stockNo'), it.get('id'), it.get('vinNo'), it.get('createdAt')))
except Exception as e:
    print('err', e, r.text[:300])
