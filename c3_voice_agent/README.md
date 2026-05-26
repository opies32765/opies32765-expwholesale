# C3 Voice Agent (Bill) — Code Backup

These files live on **C3 (147.93.176.207)** at `/opt/ew_voice/`, NOT on C1.
They're backed up here so they're in git if C3 is wiped.

## Files
- **agent.py** — LiveKit Agents entrypoint, tool wrappers, `_FilteredBill` agent class with TTS filter for GLM-4.7 'None' tokens
- **system_prompt.txt** — Bill's persona prompt with all numbered rules (#1, #1A-F, #2-#5, etc.)
- **ew-voice-agent.env.example** — systemd env file template, secrets REDACTED

## Restoring to C3
```
scp agent.py system_prompt.txt root@147.93.176.207:/opt/ew_voice/
# Fill in real API keys at /etc/default/ew-voice-agent on C3
systemctl restart ew-voice-agent
```

## Sync direction
Manual via scp. Could be automated with a cron job from C1.

## Last sync
2026-05-26 10:16 EST
