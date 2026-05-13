# /opt/expwholesale/ on Contabo 2 (84.46.244.0) — AUTHORITATIVE (PRIMARY since 2026-05-13)

This is the single source of truth for the Experience Wholesale codebase
and the live EW database (port 5433). Edits, git commits, deploys, and
service restarts happen HERE.

## Current failover state (set 2026-05-13)
- C2 = PRIMARY, serving production traffic via experience-wholesale.net
- C1 (62.146.226.100) = standby in postgres recovery mode (port 5433
  on C1 is a streaming replica of C2's port 5433). expwholesale.service
  on C1 is inactive on purpose.

## Active EW crons (live since 2026-05-13)
See `crontab -l` — 9 EW crons mirror what C1 used to run when it was
primary. ew-bouncer-killer.service, thalist-scrape.timer also running here.

## Failback to C1
When ready to promote C1 back to primary, reverse the steps:
1. Stop EW + crons on C2; promote C1 postgres; start EW on C1.
2. Re-enable thalist-scrape.timer on C1, disable on C2.
3. Move the 9 EW crons from C2's crontab back to C1's.
4. Swap _README files (this file becomes the C2 mirror notice).
5. Set up reverse replication slot from C2 → C1.
