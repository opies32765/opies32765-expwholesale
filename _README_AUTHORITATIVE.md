# /opt/expwholesale/ on Contabo 1 (62.146.226.100) — AUTHORITATIVE (PRIMARY)

This is the single source of truth for the Experience Wholesale codebase
and the live EW database (port 5433). Edits, git commits, deploys, and
service restarts happen HERE.

## Current state (set 2026-05-14, after failback from C2-emergency)
- C1 = PRIMARY, serving production traffic via experience-wholesale.net
- C2 (84.46.244.0) = streaming standby of C1 (port 5433, in recovery mode)
- expwholesale.service on C2 is inactive on purpose; if it ever starts,
  the db_url.conf dropin points DATABASE_URL at C1 (so even an accidental
  start wouldn't trigger localhost writes against C2's read-only standby).

## Active EW crons on C1 (the primary)
9 EW crons un-commented during failback's promote_c1.sh. Same list lives
in C2's crontab but commented out as failover-state. See ops/scripts/.

## If C1 dies
DO droplet watchdog v2 auto-promotes C2 within ~2min. Recovery daemon
then re-seeds C1 as standby of C2 once C1 returns.

## Planned failback to C2 (if you ever want to swap roles)
ssh root@84.46.244.0 /usr/local/bin/ew_promote_c2.sh
ssh root@84.46.244.0 /usr/local/bin/ew_post_failover_finalize_c2.sh --execute
