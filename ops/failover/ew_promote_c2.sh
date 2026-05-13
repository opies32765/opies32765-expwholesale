#!/bin/bash
# Called by ew-failover-watchdog (DO droplet) when C1 is determined dead.
# Mirror of ew_promote_c1.sh — C2 variant uses port 9000 for healthz.
set -uo pipefail

LOG=/var/log/ew_promote_c2.log
TG_BOT='8639130743:AAHobws_MAaShpjxaHC0kXMuHZwbebtuYFM'
TG_CHAT='7985611488'

log() { echo "$(date -Iseconds) $*" | tee -a "$LOG"; }
tg() {
    curl -fsS -X POST "https://api.telegram.org/bot${TG_BOT}/sendMessage"         --data-urlencode "chat_id=${TG_CHAT}"         --data-urlencode "text=$1" --max-time 10 > /dev/null 2>&1 || true
}

if [[ ! -f /var/lib/postgresql/16/ewreplica/standby.signal ]]; then
    log 'C2 standby.signal missing — already primary? exiting'
    exit 0
fi

log '=== PROMOTING C2 to primary (failover from C1) ==='
tg '🚨 EW FAILOVER STARTED: promoting C2 to primary (C1 dead)...'

# 1. Promote postgres
log 'pg_promote()...'
sudo -u postgres psql -p 5433 -tc 'SELECT pg_promote(true, 60);' 2>&1 | tee -a "$LOG"
sleep 3
ROLE=$(sudo -u postgres psql -p 5433 -tc "SELECT CASE WHEN pg_is_in_recovery() THEN 'standby' ELSE 'primary' END" | tr -d ' ')
log "postgres role: $ROLE"
if [[ "$ROLE" != 'primary' ]]; then
    log 'ERROR: pg_promote did not flip role'
    tg '🔥 EW FAILOVER: pg_promote() did NOT flip C2 role. MANUAL intervention required.'
    exit 1
fi

# 2. Re-enable any disabled crons (idempotent — only acts if there are disabled lines)
log 're-enabling any disabled EW crons (best-effort)'
crontab -l | sed -E 's|^# DISABLED 2026-05-13 failover-state[^:]*: ||' | crontab -

# 3. Re-enable thalist timer
log 'enabling thalist-scrape.timer'
systemctl enable --now thalist-scrape.timer

# 4. Start EW gunicorn + bouncer-killer
log 'starting expwholesale + ew-bouncer-killer'
systemctl start expwholesale ew-bouncer-killer
sleep 6
if ! systemctl is-active expwholesale > /dev/null; then
    log 'ERROR: expwholesale.service failed to start'
    tg '🔥 EW FAILOVER: postgres promoted OK but expwholesale.service did NOT start on C2. Check systemctl status expwholesale.'
    exit 1
fi
sleep 2
HZ=$(curl -sS http://127.0.0.1:9000/healthz 2>&1 | head -c 200)
log "healthz: $HZ"

tg "✅ EW FAILOVER COMPLETE: C2 is now primary. healthz=${HZ}. Cloudflare LB should route to C2 within ~30s."
log '=== DONE ==='
