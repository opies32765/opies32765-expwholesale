# /opt/expwholesale/ on Contabo 1 (62.146.226.100) — AUTHORITATIVE

This is the single source of truth for the Experience Wholesale codebase.

- All edits, git commits, deploys, and service restarts happen HERE.
- Contabo 2 (84.46.244.0) is a warm-standby mirror — its /opt/expwholesale/ is overwritten hourly at :17 by rsync from this server.
- Editing Contabo 2 directly is destructive: changes vanish within ≤60 min.

Sync architecture:
- /usr/local/bin/sync_expwholesale_to_c2.sh — hourly :17, rsync code (excludes .git/, venv/, *.bak.*)
- /usr/local/bin/ew-failover-sync.sh — every minute, rsyncs to /opt/expwholesale_failover/ on C2
- Per-minute rsync of vauto_reports/, accutrade_reports/, ipacket_reports/, thumb_cache/, static/uploads/
- DB: streaming Postgres standby on C2 + nightly pg_dump (3 AM) rsynced via /usr/local/bin/ew_pg_backup.sh

GitHub: https://github.com/opies32765/opies32765-expwholesale
- Nightly 02:00 cron /usr/local/bin/ew_github_nightly.sh — auto-commits dirty tree + pushes master
