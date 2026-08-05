#!/bin/bash
# DP_DEFER_2026-08-05 — one-shot send of the deferred cohort at its window.
#
# Credentials are read from the running unit's own Environment rather than
# duplicated here: this file is under /usr/local/bin but its source lives in the
# repo, and ew_save.sh pushes the repo to GitHub. We have leaked keys that way
# before.
#
# BATCH is a parameter. The deferred cohort is the verifier's `invalid` bucket
# and is expected to bounce heavily; the operator chose to send all 90 with that
# understood.
set -uo pipefail
BATCH="${1:-20}"
LOG=/var/log/ew_dp_deferred.log

exec >>"$LOG" 2>&1
echo "=== deferred send $(date) batch=$BATCH ==="

cd /opt/expwholesale || { echo "FATAL: no /opt/expwholesale"; exit 1; }

envval() {  # pull one Environment= value out of the systemd unit
  systemctl show expwholesale --property=Environment --value \
    | tr ' ' '\n' | tr -d '"' | grep -oP "(?<=^$1=).*" | head -1
}
DATABASE_URL="$(envval DATABASE_URL)"
RESEND_API_KEY="$(envval RESEND_API_KEY)"
export DATABASE_URL RESEND_API_KEY

if [ -z "$RESEND_API_KEY" ] || [ -z "$DATABASE_URL" ]; then
  echo "FATAL: could not read credentials from the expwholesale unit"; exit 1
fi

venv/bin/python3 dp_campaign.py --deferred --limit "$BATCH" --send
rc=$?
echo "=== exit $rc at $(date) ==="

# Report the damage so the morning's first question is already answered.
PGPASSWORD="$(echo "$DATABASE_URL" | sed -E 's#.*://[^:]+:([^@]+)@.*#\1#')" \
psql -h localhost -p 5433 -U expuser -d expwholesale -c "
  SELECT count(*) AS sent_total,
         count(*) FILTER (WHERE bounce_type='hard') AS dead,
         round(100.0*count(*) FILTER (WHERE bounce_type='hard')/count(*),1) AS dead_pct
    FROM dp_outreach_email;"
exit $rc
