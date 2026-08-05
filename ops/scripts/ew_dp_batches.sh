#!/bin/bash
# Work through the remaining non-deferred targets in batches of 50.
#
# A pause between batches lets bounce webhooks land, so each batch's decision is
# made on data that includes the previous batch rather than on a stale number.
# The 20% guard is not the circuit breaker the operator declined for the
# campaign -- it only stops THIS unattended loop, so a surprise does not run
# through 500 addresses before anyone looks at it.
set -uo pipefail
LOG=/var/log/ew_dp_batches.log
exec >>"$LOG" 2>&1
cd /opt/expwholesale || exit 1

E() { systemctl show expwholesale --property=Environment --value \
      | tr ' ' '\n' | tr -d '"' | grep -oP "(?<=^$1=).*" | head -1; }
export DATABASE_URL="$(E DATABASE_URL)"
export RESEND_API_KEY="$(E RESEND_API_KEY)"
[ -z "$RESEND_API_KEY" ] && { echo "FATAL: no key"; exit 1; }
PW=$(echo "$DATABASE_URL" | sed -E 's#.*://[^:]+:([^@]+)@.*#\1#')

q() { PGPASSWORD="$PW" psql -h localhost -p 5433 -U expuser -d expwholesale -tAc "$1"; }

echo "############ batch run started $(date) ############"
for i in $(seq 1 12); do
  LEFT=$(q "SELECT count(*) FROM dp_outreach_targets t
             WHERE t.removed_at IS NULL
               AND (t.send_after IS NULL OR t.send_after <= now())
               AND NOT EXISTS (SELECT 1 FROM dp_outreach_email e WHERE lower(e.email)=lower(t.email))
               AND NOT EXISTS (SELECT 1 FROM dp_outreach_suppression s WHERE s.email=lower(t.email))")
  [ "$LEFT" -le 0 ] && { echo ">>> nothing left to send"; break; }

  PCT=$(q "SELECT COALESCE(round(100.0*count(*) FILTER (WHERE bounce_type='hard')/NULLIF(count(*),0),1),0) FROM dp_outreach_email")
  echo ">>> batch $i | remaining=$LEFT | cumulative hard-bounce=${PCT}%"
  if awk "BEGIN{exit !($PCT > 20)}"; then
    echo ">>> STOPPING: hard-bounce rate ${PCT}% exceeded 20%. Not sending further unattended."
    break
  fi

  venv/bin/python3 dp_campaign.py --limit 50 --order low --send 2>&1 \
    | grep -E "^sent |^SENT|failed" | tail -3
  sleep 45
done
echo "############ batch run finished $(date) ############"
PGPASSWORD="$PW" psql -h localhost -p 5433 -U expuser -d expwholesale -c "
  SELECT count(*) AS sent, count(*) FILTER (WHERE status='delivered') AS delivered,
         count(*) FILTER (WHERE bounce_type='hard') AS dead,
         count(*) FILTER (WHERE bounce_type='soft') AS soft,
         round(100.0*count(*) FILTER (WHERE bounce_type='hard')/count(*),1) AS dead_pct
    FROM dp_outreach_email;"
