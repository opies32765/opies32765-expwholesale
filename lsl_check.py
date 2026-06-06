# -*- coding: utf-8 -*-
# READ-ONLY: confirm whether the two test records still exist in LSL.
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

print('=== CUSTOMER "TESTPUSH" ===')
r = s.post(API+'/customer/list', json={'page':0,'pageSize':20,'query':'TESTPUSH','dealerId':10010,
    'sortBy':'firstName.asc','requiresStatistics':False,'showRejectedCustomers':True,'includeActiveCustomers':True}, timeout=60)
print('http', r.status_code)
try:
    res = r.json().get('results') or []
    print('matches:', len(res))
    for c in res:
        print('  id=%s name=%s status=%s' % (c.get('id'), c.get('fullName') or c.get('name'), c.get('status')))
except Exception as e:
    print('err', e, r.text[:200])

print('\n=== INVENTORY VIN WMW53DH01R2U95381 (the MINI test) ===')
body = {'showUnGrouped':True,'collapseGroups':False,'dealerId':10010,'showInStockOnly':False,
        'query':'WMW53DH01R2U95381','typeId':[-1],'saleType':[-1],'tabIndex':0,'pageSize':20,
        'sortBy':'stockNo.desc','page':0,'makeIds':[-1],'startDate':None,'endDate':None,'agingIndex':0,'status':[-1]}
r = s.post(API+'/inventory/list', json=body, timeout=60)
print('http', r.status_code)
try:
    res = r.json().get('results') or []
    print('matches:', len(res))
    for it in res:
        print('  stock=%s id=%s status=%s archived=%s vin=%s' % (it.get('stockNo'), it.get('id'), it.get('status'), it.get('archiveRequested'), it.get('vinNo')))
except Exception as e:
    print('err', e, r.text[:200])

print('\n=== direct id check (inventory 1367373) ===')
try:
    r = s.post(API+'/inventory/get', json={'id':1367373}, timeout=60)
    print('inventory/get http', r.status_code, '-', r.text[:160])
except Exception as e:
    print('err', e)
