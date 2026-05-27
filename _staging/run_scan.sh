#!/bin/bash
# Wrapper to run dealer_scanner with the expwholesale service env vars
# (DATABASE_URL on port 5433, ANTHROPIC_API_KEY, etc.) WITHOUT exposing
# credentials in argv or shell history.
set -e
# Pull every Environment= line from the systemd unit, export them
while IFS= read -r line; do
  # strip the leading 'Environment=' and any surrounding quotes
  kv="${line#Environment=}"
  kv="${kv%\"}"; kv="${kv#\"}"
  export "$kv"
done < <(grep '^Environment=' /etc/systemd/system/expwholesale.service)

cd /opt/expwholesale && exec python3 dealer_scanner.py "$@"
