#!/bin/bash
# Bidirectional EW failover watchdog v2 — auto-detects which Contabo is
# currently primary, monitors it, fails over to the other if primary dies.
# No reconfig needed across failback events.
set -uo pipefail

C1_HOST='62.146.226.100'
C2_HOST='84.46.244.0'
HEALTHZ_PATH='/healthz'
SNI_NAME='experience-wholesale.net'
CHECK_INTERVAL=30
PROMOTE_AFTER_FAILS=4
ALERT_AFTER_FAILS=2
KILL_SWITCH=/tmp/ew_failover_disabled
SSH_KEY=/root/.ssh/id_ed25519_ewfailover
TG_BOT='8639130743:AAHobws_MAaShpjxaHC0kXMuHZwbebtuYFM'
TG_CHAT='7985611488'

log() { echo "$(date -Iseconds) [watchdog] $*"; }
tg() {
    curl -fsS -X POST "https://api.telegram.org/bot${TG_BOT}/sendMessage"         --data-urlencode "chat_id=${TG_CHAT}"         --data-urlencode "text=$1" --max-time 10 > /dev/null 2>&1 || true
}

# Returns 'primary', 'standby', 'down', or 'unreachable' for given host IP.
# Uses --resolve to bypass Cloudflare LB and hit the specific origin.
probe_role() {
    local host=$1
    local body
    body=$(curl -sS -m 8 --resolve "${SNI_NAME}:443:${host}" "https://${SNI_NAME}${HEALTHZ_PATH}" 2>/dev/null)
    local rc=$?
    if (( rc != 0 )); then
        echo 'unreachable'; return
    fi
    if echo "$body" | grep -q '"role":"primary"'; then
        echo 'primary'
    elif echo "$body" | grep -q '"role":"standby"'; then
        echo 'standby'
    elif echo "$body" | grep -q '502 Bad Gateway'; then
        echo 'down'
    else
        echo 'unknown'
    fi
}

verify_ssh_reachable() {
    ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=10 root@"$1" 'true' 2>/dev/null
}

detect_primary() {
    local c1 c2
    c1=$(probe_role "$C1_HOST")
    c2=$(probe_role "$C2_HOST")
    if [[ "$c1" = 'primary' && "$c2" = 'primary' ]]; then echo 'SPLIT_BRAIN'; return; fi
    if [[ "$c1" = 'primary' ]]; then echo 'C1'; return; fi
    if [[ "$c2" = 'primary' ]]; then echo 'C2'; return; fi
    echo 'NONE'
}

log 'bidirectional watchdog v2 starting'
sleep 10

CURRENT_PRIMARY=$(detect_primary)
log "startup detected primary: $CURRENT_PRIMARY"
if [[ "$CURRENT_PRIMARY" = 'SPLIT_BRAIN' ]]; then
    tg "🔥 EW: watchdog startup found BOTH C1 and C2 reporting primary. Possible split-brain. Watchdog will not auto-promote until exactly one is primary."
elif [[ "$CURRENT_PRIMARY" = 'NONE' ]]; then
    tg "🔥 EW: watchdog startup found NEITHER C1 nor C2 reporting primary. Manual investigation required."
else
    tg "🟢 EW failover watchdog v2 started (DO droplet). Monitoring $CURRENT_PRIMARY as primary. Auto-flips on planned failback."
fi

FAILS=0
TICK=0
ALERTED=0

while true; do
    if [[ -f "$KILL_SWITCH" ]]; then sleep $CHECK_INTERVAL; continue; fi
    TICK=$((TICK+1))

    # If we don't have a stable primary yet, keep re-detecting until we do
    if [[ "$CURRENT_PRIMARY" = 'SPLIT_BRAIN' || "$CURRENT_PRIMARY" = 'NONE' ]]; then
        NEW=$(detect_primary)
        if [[ "$NEW" = 'C1' || "$NEW" = 'C2' ]]; then
            log "primary now resolvable: $NEW"
            tg "ℹ️ EW: primary now resolvable as $NEW. Watchdog resuming normal monitoring."
            CURRENT_PRIMARY=$NEW
            FAILS=0
        fi
        sleep $CHECK_INTERVAL
        continue
    fi

    # Set per-direction variables
    case "$CURRENT_PRIMARY" in
        C1) primary_host=$C1_HOST; secondary_host=$C2_HOST; promote_script='/usr/local/bin/ew_promote_c2.sh'; new_primary='C2' ;;
        C2) primary_host=$C2_HOST; secondary_host=$C1_HOST; promote_script='/usr/local/bin/ew_promote_c1.sh'; new_primary='C1' ;;
    esac

    primary_role=$(probe_role "$primary_host")
    if [[ "$primary_role" = 'primary' ]]; then
        log "$CURRENT_PRIMARY healthy (tick=$TICK)"
        if (( FAILS > 0 )); then
            tg "ℹ️ EW: $CURRENT_PRIMARY recovered after $FAILS failed checks"
        fi
        FAILS=0
        ALERTED=0
    else
        # Did the other side get promoted externally (planned failback)?
        secondary_role=$(probe_role "$secondary_host")
        if [[ "$secondary_role" = 'primary' ]]; then
            log "primary moved $CURRENT_PRIMARY → $new_primary (manual failback/failover)"
            tg "ℹ️ EW: primary moved to $new_primary. Watchdog now monitors $new_primary. No auto-action."
            CURRENT_PRIMARY=$new_primary
            FAILS=0
            ALERTED=0
            continue
        fi
        FAILS=$((FAILS + 1))
        log "$CURRENT_PRIMARY UNREACHABLE (fail $FAILS/$PROMOTE_AFTER_FAILS, role=$primary_role)"
        if (( FAILS == ALERT_AFTER_FAILS )) && (( ALERTED == 0 )); then
            tg "⚠️ EW: $CURRENT_PRIMARY unreachable for ${ALERT_AFTER_FAILS} consecutive checks. Will promote $new_primary at ${PROMOTE_AFTER_FAILS} fails."
            ALERTED=1
        fi
        if (( FAILS >= PROMOTE_AFTER_FAILS )); then
            log "verifying $new_primary reachable via SSH before promoting"
            if ! verify_ssh_reachable "$secondary_host"; then
                log "$new_primary ALSO unreachable from DO — likely network partition. NOT promoting."
                tg "🔥 EW: BOTH $CURRENT_PRIMARY and $new_primary unreachable from DO. Network partition? NOT promoting. Manual intervention required."
                FAILS=$((PROMOTE_AFTER_FAILS - 1))
                sleep 60
                continue
            fi
            log "triggering $promote_script on $new_primary ($secondary_host)"
            ssh -i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=20 root@"$secondary_host" "$promote_script" 2>&1
            tg "🚨 EW FAILOVER FIRED: $promote_script triggered on $new_primary."
            # Write state file so recovery daemon can rebuild the failed host
            # once it comes back online.
            mkdir -p /var/lib/ew-watchdog
            cat > /var/lib/ew-watchdog/last_failover.json <<JSON
{"survivor":"$new_primary","dead":"$CURRENT_PRIMARY","at":"$(date -Iseconds)","promote_script":"$promote_script"}
JSON
            log "wrote state file: survivor=$new_primary dead=$CURRENT_PRIMARY"
            log 'watchdog exiting after promote'
            exit 0
        fi
    fi
    sleep $CHECK_INTERVAL
done
