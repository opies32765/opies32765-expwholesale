# ops/failover + ops/scripts — canonical copies

These files are the SOURCE OF TRUTH for scripts that run OUTSIDE
/opt/expwholesale. `ew_save.sh` only commits this repo, so anything edited
directly at its live path is NOT backed up and will be lost.

| repo path | live path | host |
|---|---|---|
| `ops/failover/ew-failover-watchdog-c3.sh` | `/opt/ew-failover-watchdog.sh` | C3 147.93.176.207 |
| `ops/scripts/sync_expwholesale_to_c2.sh`  | `/usr/local/bin/sync_expwholesale_to_c2.sh` | C1 |
| `ops/scripts/ew_save.sh`                  | `/usr/local/bin/ew_save.sh` | C1 |

After editing a live copy, copy it back here before running `save`, or the
change exists on exactly one machine.

## Alerting (2026-07-31)
Both the C3 watchdog and the C2 code mirror alert by **Telegram + email**.
Email sends from `alerts@experience-wholesale.net` — deliberately NOT `info@`,
which is DealerPrice's dealer-facing sender.

- C3 watchdog reads `RESEND_API_KEY` / `EW_ALERT_TO` from `/etc/ew-watchdog.env`
  (mode 600), wired in via the drop-in
  `/etc/systemd/system/ew-failover-watchdog.service.d/10-alert-email.conf`.
- The mirror reads the key from the `expwholesale` systemd unit.

⚠ `/etc/ew-watchdog.env` holds a secret and is NOT in this repo. If C3 is ever
rebuilt, recreate it or the watchdog silently loses its email channel (it
degrades to Telegram-only rather than failing, so nothing will complain).
