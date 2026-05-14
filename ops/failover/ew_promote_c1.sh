#!/bin/bash
# Called by ew-failover-watchdog (DO droplet) when C2 is determined dead.
# Promotes C1 postgres → primary, re-enables EW crons + thalist, starts
# expwholesale.service.
set -uo pipefail

LOG=/var/log/ew_promote_c1.log
TG_BOT='8639130743:AAHobws_MAaShpjxaHC0kXMuHZwbebtuYFM'
TG_CHAT='7985611488'

log() { echo "$(date -Iseconds) $*" | tee -a "$LOG"; }
tg() {
    curl -fsS -X POST "https://api.telegram.org/bot${TG_BOT}/sendMessage"         --data-urlencode "chat_id=${TG_CHAT}"         --data-urlencode "text=$1" --max-time 10 > /dev/null 2>&1 || true
}

if [[ ! -f /var/lib/postgresql/16/ewreplica/standby.signal ]]; then
    log 'standby.signal already gone — already promoted? exiting'
    exit 0
fi

log '=== PROMOTING C1 to primary ==='
tg '🚨 EW FAILOVER STARTED: promoting C1 to primary...'

# 1. Promote postgres
log 'pg_promote()...'
sudo -u postgres psql -p 5433 -tc 'SELECT pg_promote(true, 60);' 2>&1 | tee -a "$LOG"
sleep 3
ROLE=$(sudo -u postgres psql -p 5433 -tc "SELECT CASE WHEN pg_is_in_recovery() THEN 'standby' ELSE 'primary' END" | tr -d ' ')
log "postgres role: $ROLE"
if [[ "$ROLE" != 'primary' ]]; then
    log 'ERROR: pg_promote did not flip role'
    tg '🔥 EW FAILOVER: pg_promote() did NOT flip C1 role. MANUAL INTERVENTION required.'
    exit 1
fi

# 2. Re-enable EW crons (un-comment the DISABLED lines)
log 're-enabling EW crons'
crontab -l | sed -E 's|^# DISABLED 2026-05-13 failover-state \(C2 owns this now\): ||' | crontab -

# 3. Re-enable thalist timer
log 'enabling thalist-scrape.timer'
systemctl enable --now thalist-scrape.timer

# Install + enable cookie-bridge timer. Source-of-truth lives in
# ops/services/; copy into /etc/systemd/system/ then enable. Idempotent.
# Added 2026-05-14 — without this, failover loses the cookie pool safety net.
log 'installing + enabling ew-cookie-bridge.timer'
cp -f /opt/expwholesale/ops/services/ew-cookie-bridge.service /etc/systemd/system/ 2>/dev/null || log '  ew-cookie-bridge.service: skipping (not in rsync yet?)'
cp -f /opt/expwholesale/ops/services/ew-cookie-bridge.timer   /etc/systemd/system/ 2>/dev/null || log '  ew-cookie-bridge.timer: skipping (not in rsync yet?)'
systemctl daemon-reload
systemctl enable --now ew-cookie-bridge.timer 2>/dev/null || log '  ew-cookie-bridge.timer: enable failed (units missing?)'


# 4a. Remove the failover-state db_url.conf dropin (C1 was pointing at C2
#     for the standby duration; now that C1 IS primary, gunicorn should
#     write to LOCAL postgres). Idempotent — silent if dropin already gone.
DROPIN=/etc/systemd/system/expwholesale.service.d/db_url.conf
if [[ -f "$DROPIN" ]]; then
    log 'removing failover-state db_url.conf dropin (C1 now writes to local PG)'
    rm -f "$DROPIN"
    systemctl daemon-reload
fi

# 4b. Start EW gunicorn + bouncer-killer
log 'starting expwholesale + ew-bouncer-killer'
systemctl start expwholesale ew-bouncer-killer
sleep 6
if ! systemctl is-active expwholesale > /dev/null; then
    log 'ERROR: expwholesale.service failed to start'
    tg '🔥 EW FAILOVER: postgres promoted OK but expwholesale.service did NOT start. Check systemctl status expwholesale on C1.'
    exit 1
fi
sleep 2
HZ=$(curl -sS http://127.0.0.1:9001/healthz 2>&1 | head -c 200)
log "healthz: $HZ"

tg "✅ EW FAILOVER COMPLETE: C1 is now primary. healthz=${HZ}. Cloudflare LB should route to C1 within ~30s."
log '=== DONE ==='
