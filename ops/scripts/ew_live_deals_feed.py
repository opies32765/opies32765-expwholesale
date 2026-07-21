#!/usr/bin/env python3
# Ticker feed: BOUGHT (LSL deals) + BIDDING (today's live bids) + live deals-closed count
import json, sqlite3, re
LSL="/opt/livesaleslog/crm.db"; OUT="/opt/expwholesale/static/uploads/live-deals.json"
MAKES={'MCLAREN':'McLaren','BMW':'BMW','GMC':'GMC','RAM':'Ram','KIA':'Kia','MINI':'Mini','LAND ROVER':'Land Rover','ROLLS-ROYCE':'Rolls-Royce','ALFA ROMEO':'Alfa Romeo','INFINITI':'Infiniti'}
SUFFIX=re.compile(r'\s+(Sport Utility Vehicle|Pickup Truck|Passenger Van|Cargo Van|Station Wagon|Sport Utility|Minivan|Sedan|Coupe|Convertible|Hatchback|Wagon|Van|SUV)\s*$', re.I)
ACRON={'Gmc':'GMC','Bmw':'BMW','Amg':'AMG','Srt':'SRT','Gti':'GTI','Sti':'STI','Trd':'TRD','Zr2':'ZR2','Ev':'EV'}
def cap(x): return MAKES.get((x or '').upper(), ' '.join(w.capitalize() for w in (x or '').split()))
def cm(x): return ' '.join(w.capitalize() for w in x.split()) if (x or '').isupper() else (x or '')
def clean(v):
    v=(v or '').strip()
    for _ in range(3):
        v2=SUFFIX.sub('',v).strip()
        if v2==v: break
        v=v2
    v=re.sub(r'\s+',' ',v).strip(); v=re.sub(r'\s+\d{1,2}$','',v)
    return re.sub(r"[A-Za-z0-9]+", lambda m: ACRON.get(m.group(0),m.group(0)), v)
def bought():
    c=sqlite3.connect(LSL); out=[]; seen=set()
    for (vi,) in c.execute("SELECT vehicle_info FROM deals WHERE vehicle_info IS NOT NULL AND length(vehicle_info)>6 AND COALESCE(purchase_cost,0)>0 ORDER BY created_at DESC LIMIT 40"):
        v=clean(vi)
        if v and v.lower() not in seen: seen.add(v.lower()); out.append(v)
        if len(out)>=14: break
    return out
def closed(): return sqlite3.connect(LSL).execute("SELECT count(*) FROM deals").fetchone()[0]
def bidding():
    try:
        import psycopg2
        p=psycopg2.connect(host="localhost",port=5433,dbname="expwholesale",user="expuser",password="ExpWholesale2026!")
        cur=p.cursor()
        base="SELECT canon_year,canon_make,canon_model FROM bids WHERE canon_make IS NOT NULL AND canon_model IS NOT NULL AND canon_year IS NOT NULL AND canon_year::int BETWEEN 2005 AND 2027 AND %s ORDER BY id DESC LIMIT 30"
        cur.execute(base % "created_at::date=CURRENT_DATE"); rows=cur.fetchall()
        if len(rows)<6:
            cur.execute(base % "created_at > now()-interval '2 days'"); rows=cur.fetchall()
        out=[]; seen=set()
        for y,mk,md in rows:
            v=(str(int(y))+" "+cap(mk)+" "+cm(md)).strip()
            if v.lower() not in seen: seen.add(v.lower()); out.append(v)
            if len(out)>=12: break
        return out
    except Exception: return []
try:
    from itertools import zip_longest
    B=bought(); D=bidding(); deals=[]
    for a,b in zip_longest(D,B):
        if a: deals.append({"v":a,"s":"bidding"})
        if b: deals.append({"v":b,"s":"bought"})
    json.dump({"deals":deals[:28],"closed":closed()}, open(OUT,"w"))
    print("bought=%d bidding=%d total=%d closed=%d" % (len(B),len(D),len(deals[:28]),closed()))
except Exception as e:
    print("ERR",e)
