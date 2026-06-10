#!/usr/bin/env bash
# ew_vauto_watchdog.sh — auto-release wedged vAuto worker claims.
#
# Origin: bid 2827 (2026-06-10) — vm-worker-4 wedged 203s in the vAuto browser
# leg. The assess-gate (require_all) renders nothing until vauto lands, and the
# 300s fallback timer is armed ONLY on vauto submit (and still requires vauto),
# so a wedged vauto leg = blank bid until the 5-min stale sweep or a manual
# dashboard reprocess. This watchdog automates the manual reprocess (the
# operator-proven recovery) for claims stuck past STUCK_SEC with no vauto row.
#
# Threshold: 7-day baseline at install = ok-jobs avg 62.6s / p95 84.2s /
# max 101s. 180s is 1.8x the observed max — cannot kill a healthy job.
# Loop safety: 'released_watchdog' is NOT in the autogive exclusion list, so
# repeated releases count toward the 5-strike __not_found__ give-up.
# Complementary net: if vauto POSTED but AccuTrade/iPacket wedge afterwards,
# the in-app 300s fallback timer covers that side; this covers the vauto side.
#
# Canonical: /opt/expwholesale/ops/scripts/ew_vauto_watchdog.sh
# Live copy: /usr/local/bin/ew_vauto_watchdog.sh
# Cron:      /etc/cron.d/ew_vauto_watchdog (every minute)
# Log:       /var/log/ew_vauto_watchdog.log (writes only when it acts)

set -u
STUCK_SEC="${EW_WATCHDOG_STUCK_SEC:-180}"
export PGPASSWORD='ExpWholesale2026!'
PSQL="psql -U expuser -h localhost -p 5433 -d expwholesale -At"

# Standby guard (mirrors ew_save.sh): only the current primary acts.
in_rec=$($PSQL -c 'SELECT pg_is_in_recovery();' 2>/dev/null || echo err)
[ "$in_rec" = "f" ] || exit 0

out=$($PSQL <<EOSQL 2>&1
DO \$\$
DECLARE r RECORD; n INT := 0;
BEGIN
  FOR r IN
    SELECT b.id, b.vauto_claimed_by,
           EXTRACT(EPOCH FROM (NOW()-b.vauto_claimed_at))::int AS stuck_s
      FROM bids b
     WHERE b.vauto_claimed_at IS NOT NULL
       AND b.vauto_claimed_at < NOW() - INTERVAL '${STUCK_SEC} seconds'
       AND b.ai_assessment IS NULL
       AND NOT EXISTS (SELECT 1 FROM vauto_lookups vl
                        WHERE vl.bid_id = b.id
                          AND (vl.raw_json IS NOT NULL
                               OR vl.appraisal_url = '__not_found__'))
       FOR UPDATE OF b SKIP LOCKED
  LOOP
    -- Mirror of /api/bid/<id>/reprocess (operator-proven recovery):
    DELETE FROM ipacket_lookups
     WHERE bid_id = r.id
       AND (not_available = true
            OR (total_msrp IS NULL AND base_price IS NULL
                AND (raw_json->'options') IS NULL));
    DELETE FROM accutrade_lookups WHERE bid_id = r.id;
    DELETE FROM vauto_lookups WHERE bid_id = r.id;
    UPDATE bids SET vauto_claimed_by = NULL, vauto_claimed_at = NULL,
                    ai_assessed_at = NULL, ai_price = NULL, ai_assessment = NULL
     WHERE id = r.id;
    UPDATE worker_jobs
       SET completed_at = NOW(), status = 'released_watchdog',
           error = 'watchdog: vauto claim stuck '||r.stuck_s||'s on '||COALESCE(r.vauto_claimed_by,'?'),
           duration_ms = (EXTRACT(EPOCH FROM (NOW()-claimed_at))::int)*1000
     WHERE bid_id = r.id AND completed_at IS NULL AND job_type = 'vauto';
    RAISE NOTICE 'WATCHDOG_RELEASED bid=% worker=% stuck=%s',
                 r.id, COALESCE(r.vauto_claimed_by,'?'), r.stuck_s;
    n := n + 1;
  END LOOP;
END \$\$;
EOSQL
)

hits=$(printf '%s\n' "$out" | grep -c 'WATCHDOG_RELEASED' || true)
if [ "${hits:-0}" -gt 0 ]; then
  printf '%s %s\n' "$(date '+%F %T')" \
    "$(printf '%s\n' "$out" | grep 'WATCHDOG_RELEASED' | tr '\n' '; ')"
  if [ -f /etc/ew_failover.env ]; then
    . /etc/ew_failover.env 2>/dev/null || true
    if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
      msg="EW vauto-watchdog: auto-released ${hits} wedged claim(s): $(printf '%s\n' "$out" | grep 'WATCHDOG_RELEASED' | tr '\n' ' ')"
      curl -sS -m 10 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
        --data-urlencode "text=${msg}" >/dev/null 2>&1 || true
    fi
  fi
fi
exit 0
