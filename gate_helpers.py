"""Phone-gate helpers shared by app.py and sourcing_bot.py.

Two gate channels:
  - 'full_broker' — sender gets Phase 1+2 SMS (mini-page + AI report)
  - 'sourcing'    — sender can drive sourcing-bot (Greg/Marino etc.)

Each channel resolves to a digit-set = UNION of:
  1. Env baseline (PHASE2_PHONE_GATE / SOURCING_PHONE_GATE) — kept as a
     safety floor so the long-standing numbers stay live even if the DB
     read fails or the gated_phones table is empty.
  2. Active rows in gated_phones (gate_type=channel, disabled_at IS NULL)
     so we can add/remove numbers from /admin/phone-gates without a
     service restart.

Cached 30s in-process. Call bust_gate_cache() after an admin write so the
next inbound SMS sees the change immediately.
"""
import os
import time
import psycopg2
import psycopg2.extras


_CACHE = {'ts': 0.0, 'full_broker': None, 'sourcing': None}
_TTL = 30.0


def phone_digits(p):
    d = ''.join(c for c in (p or '') if c.isdigit())
    if len(d) == 11 and d[0] == '1':
        d = d[1:]
    return d


def _env_digits(env_name):
    raw = (os.environ.get(env_name) or '').strip()
    if not raw:
        return set()
    return {phone_digits(tok) for tok in raw.replace(',', ' ').split()
            if len(phone_digits(tok)) == 10}


def _db_digits(gate_type):
    digits = set()
    try:
        conn = psycopg2.connect(os.environ['DATABASE_URL'])
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("""SELECT phone_digits FROM gated_phones
                            WHERE gate_type=%s AND disabled_at IS NULL""",
                        (gate_type,))
            digits = {r['phone_digits'] for r in cur.fetchall()}
        finally:
            conn.close()
    except Exception as e:
        print(f'[gated_phones] db read error gate_type={gate_type}: {e}', flush=True)
    return digits


def gate_digits(gate_type):
    """Return UNION(env baseline, active DB rows) for a gate channel.
    30-second in-process cache. gate_type ∈ {'full_broker', 'sourcing'}."""
    now = time.time()
    cached = _CACHE.get(gate_type)
    if cached is not None and (now - _CACHE['ts']) < _TTL:
        return cached
    if gate_type == 'full_broker':
        result = _env_digits('PHASE2_PHONE_GATE') | _db_digits('full_broker')
    elif gate_type == 'sourcing':
        # Historic fallback: SOURCING reverts to PHASE2 env when its own
        # env is empty (matches the old sourcing_bot.py default behavior).
        env = _env_digits('SOURCING_PHONE_GATE') or _env_digits('PHASE2_PHONE_GATE')
        result = env | _db_digits('sourcing')
    else:
        raise ValueError(f'unknown gate_type {gate_type!r}')
    _CACHE[gate_type] = result
    _CACHE['ts'] = now
    return result


def bust_gate_cache():
    _CACHE['ts'] = 0.0
