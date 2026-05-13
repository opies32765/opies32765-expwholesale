#!/bin/bash
# Nightly EW code -> GitHub push. Auto-commits any dirty working tree
# as a checkpoint, then pushes master. Skips silently if nothing to do.
# Cron: 0 2 * * * /usr/local/bin/ew_github_nightly.sh

set -uo pipefail
LOG=/var/log/ew_github_nightly.log
REPO=/opt/expwholesale
TS=$(date -Iseconds)

exec >> "$LOG" 2>&1
echo ""
echo "[$TS] === START nightly GitHub push ==="

cd "$REPO" || { echo "[$TS] ERROR: cannot cd $REPO"; exit 1; }

# Auto-commit dirty working tree as a checkpoint
if [[ -n "$(git status --porcelain)" ]]; then
    DIRTY_COUNT=$(git status --porcelain | wc -l)
    echo "[$TS] dirty tree: $DIRTY_COUNT files — auto-committing as checkpoint"
    git add -A
    git -c user.email=admin@experience-wholesale.net \
        -c user.name="Experience Wholesale (nightly)" \
        commit -m "nightly checkpoint $(date +%Y-%m-%d): auto-commit of working tree

Automated commit from /usr/local/bin/ew_github_nightly.sh.
$DIRTY_COUNT files changed at end of $(date +%Y-%m-%d)." 2>&1 | tail -3
else
    echo "[$TS] working tree clean"
fi

# Push if HEAD is ahead of origin/master
git fetch origin --quiet
LOCAL=$(git rev-parse @)
REMOTE=$(git rev-parse @{u} 2>/dev/null || echo "no-upstream")

if [[ "$LOCAL" == "$REMOTE" ]]; then
    echo "[$TS] up to date with origin/master — nothing to push"
else
    AHEAD=$(git rev-list --count origin/master..HEAD)
    echo "[$TS] pushing $AHEAD commit(s) to origin/master"
    if git push origin master 2>&1 | tail -10; then
        echo "[$TS] push OK"
    else
        echo "[$TS] ERROR: push failed"
        exit 2
    fi
fi

# Telegram alert on success (optional — silent if env not set)
if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
    HEAD_SHA=$(git rev-parse --short HEAD)
    HEAD_MSG=$(git log -1 --pretty=%s)
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="${TELEGRAM_CHAT_ID}" \
        -d text="EW nightly GitHub push OK — ${HEAD_SHA} ${HEAD_MSG}" >/dev/null
fi

echo "[$TS] === DONE ==="
