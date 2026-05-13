#!/bin/bash
# /usr/local/bin/ew_post_failover_finalize_c1.sh
#
# Runs on C1 AFTER ew_promote_c1.sh succeeded. Stabilizes the cluster:
#   1. Rebuild C2 as a streaming standby of C1 (via pg_basebackup)
#   2. Configure C2's failback-state db_url.conf so any EW on C2 would
#      write to C1 (the new primary)
#   3. Start lsyncd on C1 with reverse direction (C1 → C2)
#   4. Create lxc_standby_slot on C1 so the home LXC can reconnect
#   5. Disable EW crons on C2 (they'd write to read-only standby otherwise)
#   6. Print manual LXC reconfig instructions
#
# Default mode is DRY (prints what it would do). Pass --execute to actually act.
# Idempotent: safe to re-run.

set -uo pipefail

MODE="${1:-dry}"
[[ "$MODE" = "--execute" ]] && MODE=execute || MODE=dry

C1_HOST='62.146.226.100'
C2_HOST='84.46.244.0'
LXC_HOST='108.64.163.112'
PG_PORT=5433
LOG=/var/log/ew_post_failover_finalize.log
TG_BOT='8639130743:AAHobws_MAaShpjxaHC0kXMuHZwbebtuYFM'
TG_CHAT='7985611488'
SSH_KEY=/root/.ssh/id_ed25519_failover
SSH_OPTS=(-i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new)
PG_REPLICATOR_PASSWORD=''  # read from /root/replicator_password_20260508.txt if present

log() { echo "$(date -Iseconds) $*" | tee -a "$LOG"; }
tg()  { curl -fsS -X POST "https://api.telegram.org/bot${TG_BOT}/sendMessage" \
        --data-urlencode "chat_id=${TG_CHAT}" \
        --data-urlencode "text=$1" --max-time 10 > /dev/null 2>&1 || true; }

run() {
    if [[ "$MODE" = execute ]]; then
        log "RUN: $*"
        "$@"
    else
        log "DRY: $*"
    fi
}

run_remote() {
    local host=$1; shift
    if [[ "$MODE" = execute ]]; then
        log "RUN @ $host: $*"
        ssh "${SSH_OPTS[@]}" "root@$host" "$@"
    else
        log "DRY @ $host: $*"
    fi
}

log "=== ew_post_failover_finalize_c1.sh starting (mode=$MODE) ==="

# Load replicator password
if [[ -f /root/replicator_password_20260508.txt ]]; then
    PG_REPLICATOR_PASSWORD=$(cat /root/replicator_password_20260508.txt)
fi
if [[ -z "$PG_REPLICATOR_PASSWORD" ]]; then
    log "WARNING: no replicator password — pg_basebackup will fail. Put it at /root/replicator_password_20260508.txt"
fi

# ── Pre-checks ───────────────────────────────────────────────────────────
log "--- Pre-checks ---"
HOSTNAME=$(hostname)
if [[ ! "$HOSTNAME" =~ vmi3197767 ]]; then
    log "ABORT: must run on C1 (got hostname=$HOSTNAME)"
    tg "🔥 finalize aborted: wrong host ($HOSTNAME)"
    exit 1
fi
ROLE=$(sudo -u postgres psql -p $PG_PORT -tc "SELECT CASE WHEN pg_is_in_recovery() THEN 'standby' ELSE 'primary' END" 2>/dev/null | tr -d ' ')
if [[ "$ROLE" != primary ]]; then
    log "ABORT: C1 not primary (role=$ROLE). Run ew_promote_c1.sh first."
    tg "🔥 finalize aborted: C1 is not primary"
    exit 1
fi
log "✓ on C1; role=primary"
tg "🔧 EW post-failover finalize starting on C1 (mode=$MODE)"

# ── Step 1: Reach C2 ─────────────────────────────────────────────────────
log "--- Step 1: probe C2 reachability ---"
if ssh "${SSH_OPTS[@]}" "root@$C2_HOST" 'true' 2>/dev/null; then
    log "✓ C2 reachable via SSH"
    C2_OK=1
else
    log "✗ C2 NOT reachable. Rebuild deferred. Re-run this script later when C2 returns."
    tg "⚠ C2 unreachable. Rebuild deferred; re-run finalize when C2 returns."
    C2_OK=0
fi

# ── Step 2: ensure lxc_standby_slot exists on C1 ─────────────────────────
log "--- Step 2: ensure replication slots exist on C1 ---"
EXISTING_SLOTS=$(sudo -u postgres psql -p $PG_PORT -tc "SELECT slot_name FROM pg_replication_slots" | tr -d ' ')
for SLOT in c2_standby_slot lxc_standby_slot; do
    if echo "$EXISTING_SLOTS" | grep -qx "$SLOT"; then
        log "  $SLOT: already exists"
    else
        run sudo -u postgres psql -p $PG_PORT -tc "SELECT pg_create_physical_replication_slot('$SLOT')"
    fi
done

# ── Step 3: rebuild C2 as standby ────────────────────────────────────────
if [[ $C2_OK -eq 1 ]]; then
    log "--- Step 3: rebuild C2 as streaming standby of C1 ---"
    # Check if C2 already in standby mode
    REMOTE_STANDBY=$(ssh "${SSH_OPTS[@]}" "root@$C2_HOST" \
        'test -f /var/lib/postgresql/16/ewreplica/standby.signal && echo yes || echo no')
    log "C2 has standby.signal: $REMOTE_STANDBY"

    if [[ "$REMOTE_STANDBY" = yes ]]; then
        log "✓ C2 already in standby mode — verifying it streams from C1"
        sleep 3
        STREAMING=$(sudo -u postgres psql -p $PG_PORT -tc \
            "SELECT COUNT(*) FROM pg_stat_replication WHERE client_addr='$C2_HOST'" | tr -d ' ')
        if [[ "$STREAMING" -ge 1 ]]; then
            log "✓ C2 streaming from C1 (clients=$STREAMING)"
        else
            log "⚠ C2 has standby.signal but is not streaming — may need primary_conninfo update"
            log "  Run: ssh root@$C2_HOST 'sudo -u postgres psql -p $PG_PORT -c \"ALTER SYSTEM SET primary_conninfo = ...; SELECT pg_reload_conf();\"'"
        fi
    else
        log "C2 needs full rebuild via pg_basebackup"
        # Stop C2 EW + postgres
        run_remote "$C2_HOST" 'systemctl stop expwholesale ew-bouncer-killer lsyncd 2>&1 || true'
        run_remote "$C2_HOST" 'systemctl stop postgresql@16-ewreplica 2>&1 || true'
        # Wipe + pg_basebackup. Use PGPASSWORD via env.
        if [[ "$MODE" = execute ]]; then
            log "wiping C2 /var/lib/postgresql/16/ewreplica/ ..."
            ssh "${SSH_OPTS[@]}" "root@$C2_HOST" \
                "rm -rf /var/lib/postgresql/16/ewreplica/*"
            log "running pg_basebackup C1 → C2 ..."
            ssh "${SSH_OPTS[@]}" "root@$C2_HOST" \
                "sudo -u postgres PGPASSWORD='$PG_REPLICATOR_PASSWORD' pg_basebackup \
                    -h $C1_HOST -p $PG_PORT -U replicator \
                    -D /var/lib/postgresql/16/ewreplica \
                    -R -S c2_standby_slot --wal-method=stream -P 2>&1 | tail -20"
            run_remote "$C2_HOST" 'chown -R postgres:postgres /var/lib/postgresql/16/ewreplica'
            run_remote "$C2_HOST" 'systemctl start postgresql@16-ewreplica'
            sleep 5
            STREAMING=$(sudo -u postgres psql -p $PG_PORT -tc \
                "SELECT COUNT(*) FROM pg_stat_replication WHERE client_addr='$C2_HOST'" | tr -d ' ')
            log "post-rebuild: C2 streaming? clients=$STREAMING"
        else
            log "DRY: would wipe C2 data dir + run pg_basebackup -h $C1_HOST -U replicator -S c2_standby_slot"
        fi
    fi
fi

# ── Step 4: failback-state db_url dropin on C2 ───────────────────────────
if [[ $C2_OK -eq 1 ]]; then
    log "--- Step 4: write failback-state db_url.conf on C2 ---"
    if [[ "$MODE" = execute ]]; then
        ssh "${SSH_OPTS[@]}" "root@$C2_HOST" 'cat > /etc/systemd/system/expwholesale.service.d/db_url.conf <<DBURL
[Service]
# Failback override: C2 is standby. If EW starts here, write to C1.
Environment=DATABASE_URL=postgresql://expuser:ExpWholesale2026!@62.146.226.100:5433/expwholesale
DBURL
systemctl daemon-reload'
        log "✓ db_url.conf written on C2 + daemon-reload"
    else
        log "DRY: would write db_url.conf on C2 pointing at C1"
    fi
fi

# ── Step 5: disable EW crons on C2 ───────────────────────────────────────
if [[ $C2_OK -eq 1 ]]; then
    log "--- Step 5: disable EW crons on C2 ---"
    if [[ "$MODE" = execute ]]; then
        ssh "${SSH_OPTS[@]}" "root@$C2_HOST" "crontab -l | sed -E '
s|^(0 1 \\* \\* \\* cd /opt/expwholesale.*scan_all_dealers\\.py.*)|# DISABLED 2026-05-13 failback-state (C1 owns this): \\1|
s|^(\\* \\* \\* \\* \\* sleep 5.*sms_safety_net\\.py.*)|# DISABLED 2026-05-13 failback-state (C1 owns this): \\1|
s|^(\\*/3 \\* \\* \\* \\* /usr/local/bin/warm_bids_cache\\.sh.*)|# DISABLED 2026-05-13 failback-state (C1 owns this): \\1|
s|^(0 3 \\* \\* \\* /usr/local/bin/ew_pg_backup\\.sh.*)|# DISABLED 2026-05-13 failback-state (C1 owns this): \\1|
s|^(0 2 \\* \\* \\* /usr/local/bin/ew_github_nightly\\.sh.*)|# DISABLED 2026-05-13 failback-state (C1 owns this): \\1|
s|^(\\*/15 \\* \\* \\* \\* /opt/expwholesale/scripts/run_sourcing_cron\\.sh.*)|# DISABLED 2026-05-13 failback-state (C1 owns this): \\1|
s|^(\\*/15 \\* \\* \\* \\* DATABASE_URL.*awaiting_name_sweep\\.py.*)|# DISABLED 2026-05-13 failback-state (C1 owns this): \\1|
s|^(30 8 \\* \\* \\* cd /opt/expwholesale.*rebuild_all_buy_profiles.*)|# DISABLED 2026-05-13 failback-state (C1 owns this): \\1|
' | crontab -"
        log "✓ EW crons on C2 disabled"
    else
        log "DRY: would comment out 8 EW cron lines on C2"
    fi
fi

# ── Step 6: enable EW crons on C1 ────────────────────────────────────────
log "--- Step 6: ensure EW crons enabled on C1 (un-comment any DISABLED) ---"
if [[ "$MODE" = execute ]]; then
    crontab -l | sed -E 's|^# DISABLED 2026-05-13 failover-state \(C2 owns this now\): ||' | crontab -
    log "✓ C1 EW crons un-commented (idempotent if already active)"
else
    log "DRY: would un-comment EW crons on C1"
fi

# ── Step 7: lsyncd on C1 (reverse direction C1 → C2) ─────────────────────
log "--- Step 7: configure lsyncd on C1 (C1 → C2 direction) ---"
if [[ "$MODE" = execute ]]; then
    mkdir -p /etc/lsyncd /var/log/lsyncd
    if ! grep -q 'C1 .* C2 real-time' /etc/lsyncd/lsyncd.conf.lua 2>/dev/null; then
        cat > /etc/lsyncd/lsyncd.conf.lua <<'LUA'
-- C1 → C2 real-time file replication (C1 is primary; mirror to standby).
-- Mirror of C2's previous config with the host swapped.
settings {
    logfile    = '/var/log/lsyncd/lsyncd.log',
    statusFile = '/var/log/lsyncd/lsyncd.status',
    statusInterval = 20,
    nodaemon   = false,
}
local report_dirs = {
    '/opt/expwholesale/vauto_reports/',
    '/opt/expwholesale/accutrade_reports/',
    '/opt/expwholesale/ipacket_reports/',
    '/opt/expwholesale/static/uploads/',
    '/opt/expwholesale/thumb_cache/',
}
for _, d in ipairs(report_dirs) do
    sync { default.rsync, source = d, target = 'root@84.46.244.0:' .. d,
           rsync = { archive = true, compress = true, verbose = false }, delay = 5 }
end
sync {
    default.rsync,
    source = '/opt/expwholesale/',
    target = 'root@84.46.244.0:/opt/expwholesale/',
    rsync = {
        archive = true, compress = true, verbose = false,
        _extra = {
            '--exclude=venv/', '--exclude=__pycache__/', '--exclude=.git/',
            '--exclude=vauto_reports/', '--exclude=accutrade_reports/',
            '--exclude=ipacket_reports/', '--exclude=static/uploads/',
            '--exclude=thumb_cache/', '--exclude=*.pyc', '--exclude=*.bak.*',
        },
    },
    delay = 5,
}
LUA
        log "✓ lsyncd config written on C1"
    else
        log "lsyncd config already present"
    fi
    systemctl restart lsyncd
    systemctl enable lsyncd
    sleep 2
    log "lsyncd on C1: $(systemctl is-active lsyncd)"
else
    log "DRY: would write /etc/lsyncd/lsyncd.conf.lua on C1 + restart lsyncd"
fi

# ── Step 8: LXC reconfig (manual) ────────────────────────────────────────
log "--- Step 8: home LXC ($LXC_HOST) reconfig — MANUAL STEP ---"
log "  The home LXC's postgres standby still points at C2 (its previous primary)."
log "  To follow C1 instead, on the LXC run:"
log "    sudo -u postgres psql -p 5433 -c \"ALTER SYSTEM SET primary_conninfo = 'host=$C1_HOST port=$PG_PORT user=replicator password=<REPLICATOR_PASSWORD> application_name=lxc_standby_slot';\""
log "    sudo systemctl restart postgresql@16-ewreplica"
log "  Replicator password is in /root/replicator_password_20260508.txt on C1."
tg "📌 Home LXC needs manual reconfig to follow C1. See $LOG for the ALTER SYSTEM command."

# ── Done ─────────────────────────────────────────────────────────────────
log "=== finalize complete ==="
log "Summary:"
log "  - C2 state:     $([[ $C2_OK -eq 1 ]] && echo 'reachable, rebuild attempted' || echo 'unreachable, rebuild deferred')"
log "  - C1 lsyncd:    $([[ "$MODE" = execute ]] && systemctl is-active lsyncd || echo '(dry-run)')"
log "  - Home LXC:     MANUAL reconfig pending"
log "  - Mode:         $MODE"

if [[ "$MODE" = dry ]]; then
    log "DRY RUN. To actually execute: $0 --execute"
fi

SUMMARY="✅ EW finalize complete (mode=$MODE). C2: $([[ $C2_OK -eq 1 ]] && echo 'rebuilt' || echo 'pending'). LXC: manual reconfig pending."
tg "$SUMMARY"
