#!/bin/bash
# /opt/ew-failover-watchdog/recovery_daemon.sh
#
# Runs continuously on DO droplet alongside the watchdog. Watches for a
# state file written by the watchdog after a failover; when the failed
# host comes back alive, SSHes to the new primary and runs the finalize
# script to rebuild the recovered host as a streaming standby.
#
# State file: /var/lib/ew-watchdog/last_failover.json (written by watchdog)
#   {"survivor":"C2","dead":"C1","at":"2026-05-14T02:00:00+00:00","promote_script":"..."}
#
# Idle when no state file (no recent failover). Becomes active after
# watchdog promotes. Probes the dead host every 5 min. On success: runs
# finalize, sends Telegram, clears state file → returns to idle.
set -uo pipefail

STATE_DIR=/var/lib/ew-watchdog
STATE_FILE="$STATE_DIR/last_failover.json"
SSH_KEY=/root/.ssh/id_ed25519_ewfailover
SSH_OPTS=(-i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new)
PROBE_INTERVAL=300                   # 5 minutes between probes when state file exists
IDLE_INTERVAL=120                    # 2 minutes between checks when no state file
LOG=/var/log/ew_recovery_daemon.log
TG_BOT='8639130743:AAHobws_MAaShpjxaHC0kXMuHZwbebtuYFM'
TG_CHAT='7985611488'

C1_HOST='62.146.226.100'
C2_HOST='84.46.244.0'

log()  { echo "$(date -Iseconds) [recovery] $*"; }
tg()   {
    curl -fsS -X POST "https://api.telegram.org/bot${TG_BOT}/sendMessage" \
        --data-urlencode "chat_id=${TG_CHAT}" \
        --data-urlencode "text=$1" --max-time 10 >/dev/null 2>&1 || true
}

mkdir -p "$STATE_DIR"
log 'recovery daemon starting'
tg "🟢 EW recovery daemon started on DO droplet. Will rebuild any failed host as standby of the survivor automatically."

while true; do
    if [[ ! -f "$STATE_FILE" ]]; then
        sleep $IDLE_INTERVAL
        continue
    fi

    # Parse state file (small JSON — using python because jq might not be installed)
    SURVIVOR=$(python3 -c "import json; print(json.load(open('$STATE_FILE'))['survivor'])" 2>/dev/null)
    DEAD=$(python3 -c "import json; print(json.load(open('$STATE_FILE'))['dead'])" 2>/dev/null)
    AT=$(python3 -c "import json; print(json.load(open('$STATE_FILE')).get('at',''))" 2>/dev/null)

    case "$SURVIVOR" in
        C1) SURVIVOR_HOST=$C1_HOST; DEAD_HOST=$C2_HOST; FINALIZE=/usr/local/bin/ew_post_failover_finalize_c1.sh ;;
        C2) SURVIVOR_HOST=$C2_HOST; DEAD_HOST=$C1_HOST; FINALIZE=/usr/local/bin/ew_post_failover_finalize_c2.sh ;;
        *)  log "invalid survivor=$SURVIVOR in state file; sleeping"; sleep $PROBE_INTERVAL; continue ;;
    esac

    # Probe dead host via SSH
    if ! ssh "${SSH_OPTS[@]}" "root@$DEAD_HOST" 'true' 2>/dev/null; then
        log "$DEAD ($DEAD_HOST) still unreachable; will retry in ${PROBE_INTERVAL}s"
        sleep $PROBE_INTERVAL
        continue
    fi

    log "$DEAD is back online! triggering finalize on $SURVIVOR ($SURVIVOR_HOST)"
    tg "🔧 EW: $DEAD recovered (back online since failover at $AT). Starting automatic rebuild as standby of $SURVIVOR..."

    # Run finalize on survivor — it'll SSH to dead host, wipe + pg_basebackup,
    # start postgres as standby.
    if ssh "${SSH_OPTS[@]}" "root@$SURVIVOR_HOST" "$FINALIZE --execute" 2>&1; then
        tg "✅ EW: $DEAD rebuilt as streaming standby of $SURVIVOR. Cluster fully redundant again. (Manual planned failback to preferred primary still up to you.)"
        log "finalize succeeded; clearing state file"
        rm -f "$STATE_FILE"
    else
        EC=$?
        tg "🔥 EW: finalize_$SURVIVOR exited $EC. $DEAD recovery incomplete; will retry in ${PROBE_INTERVAL}s. Manual investigation may be needed."
        log "finalize failed (ec=$EC); will retry"
        sleep $PROBE_INTERVAL
    fi
done
