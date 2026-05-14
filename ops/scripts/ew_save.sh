#!/bin/bash
# ew_save.sh — operator's "save everything NOW" magic word for EW.
#
# When the operator says "save" to a Claude session, Claude SSHs to C1
# and runs this. It accelerates the two manual propagation paths so the
# operator doesn't have to wait for nightly crons:
#
#   1. git commit + push any dirty /opt/expwholesale tree to GitHub
#   2. Run ew_remote_snapshot.sh synchronously (full tar of code+DB+ops
#      to DO droplet at 147.182.230.160)
#
# Plus a health summary of every other propagation path that runs
# automatically (so the operator can see at a glance that nothing has
# silently stopped).
#
# Lives on BOTH C1 and C2 via ops/scripts/. Only the current PG primary
# actually executes — standby exits early (matches ew_remote_snapshot.sh
# primary-only pattern). After failover, save-duty transfers automatically.
#
# Usage:  ew_save.sh ["optional commit message"]
# Logs:   /var/log/ew_save.log

set -uo pipefail
REPO=/opt/expwholesale
LOG=/var/log/ew_save.log
TG_BOT='8639130743:AAHobws_MAaShpjxaHC0kXMuHZwbebtuYFM'
TG_CHAT='7985611488'

log()  { echo "$(date -Iseconds) $*" >> "$LOG"; }
tg()   { curl -fsS -X POST "https://api.telegram.org/bot${TG_BOT}/sendMessage" \
            --data-urlencode "chat_id=${TG_CHAT}" --data-urlencode "text=$1" \
            --max-time 10 >/dev/null 2>&1 || true; }

# Only the current PG primary actually saves
IS_PRIMARY=$(sudo -u postgres psql -p 5433 -tAc \
    'SELECT NOT pg_is_in_recovery()' 2>/dev/null | tr -d ' ')
if [[ "$IS_PRIMARY" != "t" ]]; then
    echo "skipping — this host is PG standby, not primary"
    log "skipping — standby"
    exit 0
fi

MSG="${*:-save $(date '+%Y-%m-%d %H:%M %Z')}"
log "=== ew_save start: $MSG ==="
echo "=== ew_save: $MSG ==="

# ── Step 1: git commit + push ────────────────────────────────────────────
cd "$REPO" || { echo "ERROR: cd $REPO failed"; exit 1; }

echo ""
if [[ -n "$(git status --porcelain)" ]]; then
    DIRTY=$(git status --porcelain | wc -l)
    echo "[1/3] git: $DIRTY dirty file(s) — committing"
    git add -A
    git -c user.email=ops@experience-wholesale.net \
        -c user.name='EW Ops (save)' \
        commit -m "save $(date '+%Y-%m-%d %H:%M'): $MSG" 2>&1 | tail -2
    log "git committed $DIRTY files"
else
    echo "[1/3] git: working tree clean (no commit)"
fi

git fetch origin --quiet 2>/dev/null
LOCAL=$(git rev-parse @ 2>/dev/null || echo "?")
REMOTE=$(git rev-parse @{u} 2>/dev/null || echo "?")
if [[ "$LOCAL" == "$REMOTE" ]]; then
    echo "      origin/master: up-to-date"
else
    AHEAD=$(git rev-list --count origin/master..HEAD 2>/dev/null || echo "?")
    echo "      pushing $AHEAD commit(s) to origin/master..."
    if git push origin master 2>&1 | tail -1; then
        log "git push OK ($AHEAD commits)"
    else
        log "git push FAILED"
        tg "🔥 ew_save: git push failed on $(hostname)"
    fi
fi

# ── Step 2: full snapshot to DO ──────────────────────────────────────────
echo ""
echo "[2/3] full snapshot to DO droplet (code + DB + ops tarball, ~90s)..."
if /usr/local/bin/ew_remote_snapshot.sh >> "$LOG" 2>&1; then
    SNAP_INFO=$(grep '=== snapshot' "$LOG" | tail -1 | awk -F'|' '{print $1}' | awk '{print $NF}')
    echo "      snapshot complete: $SNAP_INFO"
    log "snapshot OK"
else
    echo "      snapshot FAILED — see $LOG"
    log "snapshot FAILED"
    tg "🔥 ew_save: ew_remote_snapshot.sh failed on $(hostname)"
fi

# ── Step 3: health summary ───────────────────────────────────────────────
echo ""
echo "[3/3] replication + backup health:"

# 3a. PG streaming replicas (C2 + home LXC)
sudo -u postgres psql -p 5433 -tAc "
SELECT application_name || ': ' || state ||
       ' (replay_lag=' || COALESCE(EXTRACT(EPOCH FROM replay_lag)::INT::TEXT, '0') || 's)'
  FROM pg_stat_replication
  ORDER BY application_name" 2>/dev/null \
  | sed 's/^/      PG stream → /' \
  | head -5

# Fallback: print 'no replicas' if query empty
if ! sudo -u postgres psql -p 5433 -tAc 'SELECT 1 FROM pg_stat_replication LIMIT 1' 2>/dev/null | grep -q 1; then
    echo "      PG stream → no replicas connected ⚠"
fi

# 3b. DO snapshot freshness
DO_INFO=$(ssh -i /root/.ssh/id_ed25519_failover \
    -o ConnectTimeout=5 -o BatchMode=yes \
    root@147.182.230.160 \
    'cd /var/backups/ew_snapshots 2>/dev/null && ls -t *.tar 2>/dev/null | head -1 | xargs -I{} stat -c "%y %s" {} 2>/dev/null' \
    2>/dev/null)
if [[ -n "$DO_INFO" ]]; then
    echo "      DO snapshot →   $DO_INFO"
else
    echo "      DO snapshot →   (unreachable or empty)"
fi

# 3c. C2 file rsync freshness — pick a recently-edited file
C2_MTIME=$(ssh -o ConnectTimeout=5 -o BatchMode=yes root@84.46.244.0 \
    'stat -c "%y" /opt/expwholesale/app.py 2>/dev/null' 2>/dev/null)
if [[ -n "$C2_MTIME" ]]; then
    echo "      C2 app.py mtime → $C2_MTIME"
else
    echo "      C2 →            (unreachable)"
fi

echo ""
echo "done."
log "=== ew_save done ==="
