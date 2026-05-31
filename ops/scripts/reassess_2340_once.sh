#!/bin/bash
cd /opt/expwholesale
DBURL=$(tr "\0" "\n" < /proc/$(pgrep -f "wsgi:app"|head -1)/environ | grep ^DATABASE_URL= | cut -d= -f2-)
OUT=/tmp/reassess_2340.out
echo "=== $(date) reassess 2340 (one-shot, post rate-limit) ===" >> $OUT
echo "BEFORE:" >> $OUT
psql "$DBURL" -c "SELECT total_msrp, not_available, left(unavailable_reason,40) reason FROM ipacket_lookups WHERE bid_id=2340;" >> $OUT 2>&1
/opt/expwholesale/venv/bin/python -c "import sys;sys.path.insert(0,'/opt/expwholesale');import app;r=app._run_assessment(2340);print('result success=',r.get('success'),'buy=',r.get('buy_price'))" >> $OUT 2>&1
echo "AFTER:" >> $OUT
psql "$DBURL" -c "SELECT total_msrp, base_price, not_available, left(screenshot,45) ss FROM ipacket_lookups WHERE bid_id=2340;" >> $OUT 2>&1
echo "=== done $(date) ===" >> $OUT
