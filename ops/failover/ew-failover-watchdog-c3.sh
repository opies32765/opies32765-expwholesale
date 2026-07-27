#!/bin/bash
# ew-failover-watchdog — runs on C3 (147.93.176.207). Replaces the version that lived on the
# DO droplet 147.182.230.160 (shut off 2026-07-26; it had also filled its disk to 100%).
#
# WHY IT ALSO CHECKS REPLICATION, not just liveness:
#   On 2026-07-21 C2's replicator password went stale, C2 rebooted without standby.signal and
#   came up as its OWN primary. It sat there 5 days, 135 bids behind, while C1 piled up 2.2GB
#   of WAL for a slot nobody read. Every liveness check passed the whole time — C2 was "up".
#   A watchdog that would have promoted that C2 would have silently rolled the business back
#   five days. So: a standby that is UP but NOT STREAMING is a fault, and this reports it.
#
# WHAT IT DOES
#   every CHECK_INTERVAL: probe C1 health.
#     - C1 healthy  -> also verify C2 is a streaming standby; alert (once) if it is not.
#     - C1 unhealthy FAIL_THRESHOLD times in a row -> SPLIT-BRAIN GUARD: only promote if C2 is
#       reachable AND currently in recovery AND its replay lag is small. Otherwise alert only.
#
# It will NEVER promote a C2 that is not a healthy standby. That is the whole point.
#
# Kill switch:  touch /tmp/ew_failover_disabled
set -uo pipefail

C1=62.146.226.100
C2=84.46.244.0
HEALTH_URL='https://experience-wholesale.net/healthz'
SSH_KEY=/root/.ssh/id_ed25519_failover
SSH_OPTS=(-i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new)
CHECK_INTERVAL=30
FAIL_THRESHOLD=4          # ~2 minutes before acting
MAX_PROMOTE_LAG_MB=64     # refuse to promote a standby further behind than this
TG_BOT='8639130743:AAHobws_MAaShpjxaHC0kXMuHZwbebtuYFM'
TG_CHAT='7985611488'
STATE=/var/lib/ew-watchdog
mkdir -p "$STATE"

log() { echo "$(date -Iseconds) $*"; }
tg()  { curl -fsS -X POST "https://api.telegram.org/bot${TG_BOT}/sendMessage" \
          --data-urlencode "chat_id=${TG_CHAT}" --data-urlencode "text=$1" --max-time 10 >/dev/null 2>&1 || true; }
# fire an alert at most once per hour per key, so a persistent fault does not spam
alert_once() {
  # NB: these MUST be separate `local` statements. `local a="$1" f="...$a"` evaluates every
  # right-hand side before assigning, so `$a` is still unbound and `set -u` aborts the script.
  local key="$1"
  local msg="$2"
  local f="$STATE/alert_$key"
  local now
  now=$(date +%s)
  if [[ -f "$f" ]] && (( now - $(cat "$f" 2>/dev/null || echo 0) < 3600 )); then return; fi
  echo "$now" > "$f"; log "ALERT[$key] $msg"; tg "$msg"
}
clear_alert() { rm -f "$STATE/alert_$1" 2>/dev/null || true; }

c1_healthy() { curl -fsS --max-time 10 "$HEALTH_URL" >/dev/null 2>&1; }

# echoes: STREAMING | NOT_STREAMING | PRIMARY | UNREACHABLE ; plus lag bytes
c2_status() {
  local out
  out=$(ssh "${SSH_OPTS[@]}" "root@${C2}" \
        "sudo -u postgres psql -p 5433 -tAc \"select pg_is_in_recovery()||'|'||coalesce((select count(*) from pg_stat_wal_receiver),0)||'|'||coalesce(pg_wal_lsn_diff(pg_last_wal_receive_lsn(), pg_last_wal_replay_lsn()),0)\"" 2>/dev/null)
  [[ -z "$out" ]] && { echo "UNREACHABLE|0"; return; }
  local rec rcv lag; IFS='|' read -r rec rcv lag <<<"$out"
  # Accept BOTH forms. psql prints a bare boolean as t/f, but `bool || text` casts it to
  # 'true'/'false' -- and this query concatenates, so it returns "true". Comparing only to "t"
  # made a healthy standby read as PRIMARY: constant false alarms, and worse, it would have
  # REFUSED to promote during a real C1 outage.
  if [[ "$rec" != "t" && "$rec" != "true" ]]; then echo "PRIMARY|0"; return; fi
  if [[ "${rcv:-0}" -lt 1 ]]; then echo "NOT_STREAMING|${lag:-0}"; return; fi
  echo "STREAMING|${lag:-0}"
}

# Delegate to C2's OWN promote script instead of issuing pg_promote() from here. Promoting the
# database is only step one of a failover: ew_promote_c2.sh also guards on standby.signal (so it
# is idempotent), verifies the role actually flipped, re-enables the crons that were commented
# out with "# DISABLED ... failover-state", re-enables thalist-scrape.timer and
# dealer-completion.service, REMOVES /etc/systemd/system/expwholesale.service.d/db_url.conf so
# gunicorn writes to the local database rather than back to the dead C1, starts expwholesale and
# ew-bouncer-killer, and health-checks. A raw pg_promote leaves every one of those undone.
promote_c2() {
  log "PROMOTING C2 via /usr/local/bin/ew_promote_c2.sh"
  tg "🚨 EW FAILOVER: C1 unreachable ${FAIL_THRESHOLD}x. Running ew_promote_c2.sh on C2 ($C2)."
  local out rc
  out=$(ssh "${SSH_OPTS[@]}" "root@${C2}" "/usr/local/bin/ew_promote_c2.sh" 2>&1 | tail -8)
  rc=$?
  log "promote rc=$rc output: $out"
  if (( rc != 0 )); then
    tg "🔥 EW FAILOVER: ew_promote_c2.sh exited $rc — MANUAL INTERVENTION NEEDED. Tail: $out"
  else
    # finalize is deliberately NOT automatic. It rebuilds C1 as a standby OF C2, reverses lsyncd
    # and disables C1's crons — one-way changes that need a human to first decide the cluster is
    # stable and that C1 is not coming back on its own.
    tg "✅ EW FAILOVER: C2 promoted and serving. NEXT (manual, once stable): ssh root@${C2} '/usr/local/bin/ew_post_failover_finalize_c2.sh' to PREVIEW, then re-run with --execute to rebuild C1 as a standby of C2."
  fi
  touch "$STATE/promoted_at_$(date +%s)"
}

log "watchdog starting on $(hostname) — C1=$C1 C2=$C2 interval=${CHECK_INTERVAL}s threshold=$FAIL_THRESHOLD"
tg "🟢 EW watchdog now running on C3 ($(hostname)) — took over from the retired DO droplet."
fails=0
while true; do
  if [[ -f /tmp/ew_failover_disabled ]]; then
    log "kill switch present — idle"; sleep "$CHECK_INTERVAL"; continue
  fi

  if c1_healthy; then
    (( fails > 0 )) && log "C1 recovered after $fails failed check(s)"
    fails=0; clear_alert c1_down
    # C1 is fine — now make sure the standby is actually a standby.
    IFS='|' read -r st lag <<<"$(c2_status)"
    case "$st" in
      STREAMING)
        if (( lag > MAX_PROMOTE_LAG_MB * 1024 * 1024 )); then
          alert_once c2_lag "⚠️ EW: C2 is streaming but $((lag/1024/1024))MB behind."
        else clear_alert c2_lag; clear_alert c2_broken; fi ;;
      NOT_STREAMING)
        alert_once c2_broken "🔴 EW: C2 is UP but NOT STREAMING from C1. It is NOT a usable failover target. (This is exactly the 2026-07-21 fault.)" ;;
      PRIMARY)
        alert_once c2_broken "🔴 EW SPLIT-BRAIN RISK: C2 is running as its OWN PRIMARY, not a standby. Promoting it would lose data. Rebuild it." ;;
      UNREACHABLE)
        alert_once c2_unreach "⚠️ EW: C2 unreachable from the watchdog." ;;
    esac
  else
    fails=$((fails+1))
    log "C1 health check failed ($fails/$FAIL_THRESHOLD)"
    if (( fails >= FAIL_THRESHOLD )); then
      IFS='|' read -r st lag <<<"$(c2_status)"
      if [[ "$st" == "UNREACHABLE" ]]; then
        alert_once partition "🟠 EW: BOTH C1 and C2 unreachable from C3 — assuming network partition, NOT promoting."
      elif [[ "$st" != "STREAMING" ]]; then
        alert_once refuse "🔴 EW: C1 is DOWN but C2 is '$st', not a healthy standby. REFUSING to promote (would lose data). Manual action required."
      elif (( lag > MAX_PROMOTE_LAG_MB * 1024 * 1024 )); then
        alert_once refuse "🔴 EW: C1 is DOWN but C2 is $((lag/1024/1024))MB behind (limit ${MAX_PROMOTE_LAG_MB}MB). REFUSING to auto-promote."
      else
        promote_c2; fails=0
      fi
    fi
  fi
  sleep "$CHECK_INTERVAL"
done
