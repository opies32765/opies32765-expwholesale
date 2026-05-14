#!/bin/bash
# Twice-daily complete EW snapshot push → DO droplet.
#
# Lives on BOTH C1 and C2; cron on both hosts. The IS_PRIMARY guard
# below ensures only whichever server is currently primary actually
# produces the snapshot. After failover, snapshot duty transfers
# automatically — no reconfig needed.
#
# Contents of each bundle:
#   - db_<TS>.dump          PostgreSQL custom-format dump
#   - code_<TS>.tar.gz      /opt/expwholesale (no venv, no report dirs, no uploads, no .git/objects)
#   - ops_<TS>.tar.gz       /etc/systemd/system/{expwholesale.service,*.d}, /etc/lsyncd, /usr/local/bin/ew*, /etc/postgresql/*, replicator password
#   - MANIFEST.txt
# Retention on DO: 14 most recent (7 days of 2x daily).
set -uo pipefail

DO_HOST='147.182.230.160'
SSH_KEY='/root/.ssh/id_ed25519_failover'
SSH_OPTS=(-i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new)
REMOTE_DIR='/var/backups/ew_snapshots'
TS=$(date '+%Y-%m-%d_%H%M')
TG_BOT='8639130743:AAHobws_MAaShpjxaHC0kXMuHZwbebtuYFM'
TG_CHAT='7985611488'
LOG='/var/log/ew_remote_snapshot.log'
WORK="/tmp/ew_snapshot_$$"

log()  { echo "$(date -Iseconds) [$(hostname)] $*" | tee -a "$LOG"; }
tg()   { curl -fsS -X POST "https://api.telegram.org/bot${TG_BOT}/sendMessage" --data-urlencode "chat_id=${TG_CHAT}" --data-urlencode "text=$1" --max-time 10 >/dev/null 2>&1 || true; }
cleanup() { rm -rf "$WORK" /tmp/ew_remote_snapshot_*.tar; }
trap cleanup EXIT

# Only the current primary actually snapshots.
IS_PRIMARY=$(sudo -u postgres psql -p 5433 -tAc 'SELECT NOT pg_is_in_recovery()' 2>/dev/null | tr -d ' ')
if [[ "$IS_PRIMARY" != "t" ]]; then
    log "skipping — this host is standby (pg_is_in_recovery returned non-primary)"
    exit 0
fi

mkdir -p "$WORK"
log "=== snapshot $TS starting (primary) ==="

# 1. DB dump
log 'pg_dump...'
export PGPASSWORD='ExpWholesale2026!'
if ! pg_dump -h localhost -p 5433 -U expuser -d expwholesale -F custom -f "$WORK/db_${TS}.dump" 2>>"$LOG"; then
    log 'ERROR: pg_dump failed'
    tg "🔥 EW snapshot FAILED ($TS): pg_dump error. See $LOG"
    exit 1
fi
DB_SIZE=$(stat -c %s "$WORK/db_${TS}.dump")

# 2. Code tarball
log 'tar /opt/expwholesale...'
tar --exclude=venv --exclude=__pycache__ --exclude='*.pyc' --exclude='*.bak.*' \
    --exclude=vauto_reports --exclude=accutrade_reports --exclude=ipacket_reports \
    --exclude=static/uploads --exclude=thumb_cache --exclude=.git/objects \
    -czf "$WORK/code_${TS}.tar.gz" -C /opt expwholesale thalist 2>>"$LOG"
CODE_SIZE=$(stat -c %s "$WORK/code_${TS}.tar.gz")

# 3. Operational files
log 'tar operational files...'
tar -czf "$WORK/ops_${TS}.tar.gz" \
    /etc/systemd/system/expwholesale.service \
    /etc/systemd/system/expwholesale.service.d \
    /etc/systemd/system/ew-bouncer-killer.service \
    /etc/systemd/system/thalist-scrape.service \
    /etc/systemd/system/thalist-scrape.timer \
    /etc/lsyncd/lsyncd.conf.lua \
    /usr/local/bin/ew_pg_backup.sh \
    /usr/local/bin/ew_github_nightly.sh \
    /usr/local/bin/warm_bids_cache.sh \
    /usr/local/bin/ew-bouncer-killer.sh \
    /usr/local/bin/ew_promote_c1.sh \
    /usr/local/bin/ew_promote_c2.sh \
    /usr/local/bin/ew_post_failover_finalize_c1.sh \
    /usr/local/bin/ew_post_failover_finalize_c2.sh \
    /usr/local/bin/ew_remote_snapshot.sh \
    /etc/postgresql/16/ewreplica/postgresql.conf \
    /etc/postgresql/16/ewreplica/pg_hba.conf \
    /root/replicator_password_20260508.txt \
    2>>"$LOG" || log '(some operational files missing — non-fatal)'
OPS_SIZE=$(stat -c %s "$WORK/ops_${TS}.tar.gz" 2>/dev/null || echo 0)

# 4. Manifest
cat > "$WORK/MANIFEST.txt" <<MANIFEST
EW complete snapshot — $TS
Produced on: $(hostname) (primary at snapshot time)
Sizes: db=$DB_SIZE code=$CODE_SIZE ops=$OPS_SIZE

To restore on a fresh host:
  1. pg_restore --create -d postgres db_${TS}.dump
  2. tar xzf code_${TS}.tar.gz -C /opt
  3. tar xzf ops_${TS}.tar.gz -C /
  4. cd /opt/expwholesale && python3 -m venv venv && venv/bin/pip install -r requirements.txt
  5. systemctl daemon-reload && systemctl enable --now expwholesale
MANIFEST

# 5. Bundle
BUNDLE="/tmp/ew_remote_snapshot_${TS}.tar"
tar -cf "$BUNDLE" -C "$WORK" .
BUNDLE_SIZE=$(stat -c %s "$BUNDLE")
log "bundle size $(numfmt --to=iec $BUNDLE_SIZE)"

# 6. SCP to DO
log "scp → ${DO_HOST}:${REMOTE_DIR}/"
if ! scp "${SSH_OPTS[@]}" "$BUNDLE" "root@${DO_HOST}:${REMOTE_DIR}/" 2>>"$LOG"; then
    log 'ERROR: scp to DO failed'
    tg "🔥 EW snapshot FAILED ($TS): scp to DO failed. See $LOG"
    exit 1
fi
log 'scp complete'

# 7. Retention: keep 14 most recent on DO
ssh "${SSH_OPTS[@]}" "root@${DO_HOST}" \
    "cd ${REMOTE_DIR} && ls -t ew_remote_snapshot_*.tar 2>/dev/null | tail -n +15 | xargs -r rm -v" 2>>"$LOG"

REMOTE_COUNT=$(ssh "${SSH_OPTS[@]}" "root@${DO_HOST}" "ls ${REMOTE_DIR}/ew_remote_snapshot_*.tar 2>/dev/null | wc -l" 2>/dev/null)
REMOTE_FREE=$(ssh "${SSH_OPTS[@]}" "root@${DO_HOST}" "df -h / | awk 'NR==2 {print \$4}'" 2>/dev/null)

log "=== snapshot $TS complete | $(numfmt --to=iec $BUNDLE_SIZE) | $REMOTE_COUNT snapshots on DO | $REMOTE_FREE free ==="
tg "📦 EW snapshot pushed to DO from $(hostname): $TS, $(numfmt --to=iec $BUNDLE_SIZE). DO has $REMOTE_COUNT snapshots, $REMOTE_FREE free."
