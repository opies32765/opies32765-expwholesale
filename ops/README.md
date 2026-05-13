# EW operational files (failover + infrastructure)

These files live OUTSIDE `/opt/expwholesale/` on the actual servers (in
`/etc/systemd/`, `/usr/local/bin/`, `/opt/ew-failover-watchdog/`) but are
mirrored here so they're version-controlled with the rest of EW.

## Failover architecture (2026-05-13)
- C2 (84.46.244.0) is current primary; C1 (62.146.226.100) is read-only standby.
- DO droplet (147.182.230.160) runs the failover watchdog v2.
- See `failover/watchdog.sh` and the README inside.

## Files

### failover/
- `watchdog.sh` — runs on DO droplet at `/opt/ew-failover-watchdog/watchdog.sh`.
  Bidirectional: auto-detects current primary, promotes the other if primary dies.
- `ew-failover-watchdog.service` — systemd unit on DO at
  `/etc/systemd/system/ew-failover-watchdog.service`.
- `ew_promote_c1.sh` — runs on C1 at `/usr/local/bin/ew_promote_c1.sh`.
  Triggered by watchdog when C2 dies. Does pg_promote(), un-comments crons,
  enables thalist timer, starts EW.
- `ew_promote_c2.sh` — runs on C2 at `/usr/local/bin/ew_promote_c2.sh`.
  Mirror of above for the failback direction.

### dropins/
Systemd dropin files at `/etc/systemd/system/expwholesale.service.d/` on
each host. Keep these symmetric so failover doesn't lose env vars.

### services/
- `ew-bouncer-killer.service` — kills runaway `systemctl restart expwholesale`
  callers. Runs on BOTH C1 and C2 at `/etc/systemd/system/ew-bouncer-killer.service`.

### scripts/
Shell scripts at `/usr/local/bin/` on whichever server is primary.
- `ew_github_nightly.sh` — 02:00 cron, commits + pushes EW source to GitHub.
- `ew_pg_backup.sh` — 03:00 cron, pg_dump + rsync to standby for off-site copy.
  Note: `REMOTE_HOST` is host-specific — must point at the OTHER server.
- `warm_bids_cache.sh` — every-3-min cron, pre-warms PG buffer cache.
- `ew-bouncer-killer.sh` — driven by ew-bouncer-killer.service.

### lsyncd.conf.lua
Real-time C2→C1 file replication. Pushes `/opt/expwholesale/` source + state
+ report directories. No `--delete` (prevents destructive sync).
Lives at `/etc/lsyncd/lsyncd.conf.lua` on the current primary.

## Failback procedure (when promoting C1 back to primary)
TODO: document the manual steps.
