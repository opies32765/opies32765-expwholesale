#!/bin/bash
# Run sourcing_cron.py with expwholesale.service env injected.
# Use python helper to parse systemctl show output safely (handles quoted
# values that confuse plain shell sourcing).
exec /opt/expwholesale/venv/bin/python -c "
import os, subprocess, sys
out = subprocess.check_output(['systemctl', 'show', 'expwholesale', '--property=Environment'], text=True)
env_line = out.replace('Environment=', '', 1).strip()
# Tokenize: split on space, but respect quoted values.
import shlex
for tok in shlex.split(env_line):
    if '=' in tok:
        k, v = tok.split('=', 1)
        os.environ[k] = v
sys.path.insert(0, '/opt/expwholesale')
import sourcing_cron
sourcing_cron.main()
"
