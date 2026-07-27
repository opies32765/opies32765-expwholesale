#!/bin/bash
# ew_ssh_hardening_apply.sh — activate SSH hardening on C1 + install fail2ban.
# Run ON C1 as root.  2026-07-27
#
# The drop-in /etc/ssh/sshd_config.d/10-ew-hardening.conf is ALREADY installed and has
# already passed `sshd -t`. It is inert until sshd reloads. This script reloads it, then
# adds fail2ban, then verifies both.
#
# BACKUP: /root/ssh_backup_20260727/  (original sshd_config + sshd_config.d)
# REVERT: rm /etc/ssh/sshd_config.d/10-ew-hardening.conf && systemctl reload ssh
#
# SAFETY: the reload is verified from C2 over a genuinely new connection before the script
# will proceed. If that check fails, it reverts automatically and stops.
set -uo pipefail
C2=84.46.244.0
DROPIN=/etc/ssh/sshd_config.d/10-ew-hardening.conf
say() { echo; echo "=== $* ==="; }

say "0. pre-flight"
[[ -f "$DROPIN" ]] || { echo "FATAL: $DROPIN missing"; exit 1; }
sshd -t || { echo "FATAL: sshd config invalid, refusing"; exit 1; }
echo "config valid; current: $(sshd -T | grep -c '^passwordauthentication no') (1 = hardening parsed)"

say "1. reload sshd (existing sessions are NOT dropped)"
systemctl reload ssh && echo "reloaded"
sleep 2
sshd -T 2>/dev/null | grep -E '^(passwordauthentication|permitrootlogin|kbdinteractive|maxstartups|logingracetime|maxauthtries)'

say "2. PROVE a brand-new inbound connection still works (asked from C2)"
# This is the check that matters: it opens a fresh SSH session from another host.
if ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=no "root@$C2" \
      "ssh -o BatchMode=yes -o ConnectTimeout=10 root@62.146.226.100 'echo NEW-CONNECTION-OK'" 2>/dev/null | grep -q NEW-CONNECTION-OK; then
    echo "PASS - key auth still works from a new connection"
else
    echo "FAIL - could not open a new session. REVERTING."
    rm -f "$DROPIN"; systemctl reload ssh
    echo "reverted; sshd back to previous config"; exit 1
fi

say "3. install fail2ban"
if command -v fail2ban-client >/dev/null; then
    echo "already installed"
else
    DEBIAN_FRONTEND=noninteractive apt-get install -y fail2ban >/dev/null 2>&1 \
        && echo "installed" || { echo "apt install FAILED"; exit 1; }
fi

say "4. jail config — WHITELIST OUR OWN HOSTS FIRST"
# Getting this wrong bans our own automation. Every IP below was verified as ours by
# mapping its successful-login key fingerprint back to /root/.ssh/authorized_keys, and
# each has ZERO auth failures in 24h.
cat > /etc/fail2ban/jail.local <<'CONF'
[DEFAULT]
# ignoreip: never ban these.
#   108.64.163.112  home / workstation / home LXC (Home@DESKTOP-91CRPUR, lxc-pull)
#   84.46.244.0     C2  (c2-failover)
#   147.93.176.207  C3  (c3-to-c1-ollama-tunnel, failover watchdog)
#   155.117.46.160  OpsWatch monitor (opswatch-vps) - 28,964 logins/7d, our single
#                   biggest source of SSH connections. Banning this blinds monitoring.
#   69.30.204.35    dp-sync (DealerPrice export, command-restricted key)
#   143.59.232.45   ours, low volume, zero failures
#   147.182.230.160 retired DO droplet (kept in case it is powered back on)
#   38.247.189.248  the .248 GPU box
ignoreip = 127.0.0.1/8 ::1 192.168.1.0/24 108.64.163.112 84.46.244.0 147.93.176.207 155.117.46.160 69.30.204.35 143.59.232.45 147.182.230.160 38.247.189.248

bantime  = 1h
findtime = 10m
maxretry = 5
backend  = systemd

[sshd]
enabled = true
port    = ssh
maxretry = 4
bantime  = 2h
CONF
echo "wrote /etc/fail2ban/jail.local"

say "5. start fail2ban"
systemctl enable --now fail2ban >/dev/null 2>&1
sleep 4
systemctl is-active fail2ban

say "6. verify the jail is live and the whitelist took"
fail2ban-client status sshd 2>&1 | head -12
echo
echo "ignoreip in effect:"
fail2ban-client get sshd ignoreip 2>&1 | head -20

say "DONE"
echo "Watch it work:   fail2ban-client status sshd"
echo "Unban an IP:     fail2ban-client set sshd unbanip <IP>"
echo "Revert SSH:      rm $DROPIN && systemctl reload ssh"
