"""claim_reaper.py — CLAIM_REAPER_2026_07_29

Releases worker claims that outlived their work, and texts the operator when it
does. Written after bid 5087 sat claimed by vm-worker-13 for 45 minutes with
ai_price already set: the vAuto close path never ran, so vauto_priority stayed
TRUE and the worker kept re-attaching. Nothing in the system noticed.

WHY NOT JUST RE-ENABLE THE OLD WATCHDOG: `_watchdog_evaluate_once` was stubbed
to `return 0` on 2026-05-01 because the per-phase budgets were killing HEALTHY
workers on slow Cox lookups. This is deliberately not that. It never judges how
long a worker "should" take. It only releases claims where the work is
demonstrably already finished, or where the claim is so old that no live job
could explain it (normal vAuto completes in 23-29s).

Release rules — both conservative:
  R1  ai_price IS NOT NULL and claim older than DONE_MIN   -> the assessment
      already landed, so the claim cannot still be needed
  R2  claim older than DEAD_MIN regardless                 -> 60x the observed
      p100 job duration; nothing real survives this long

SMS: at most one message per run, only when something was released, and never
twice for the same bid (in-process memo). Silence means nothing was wrong.
"""
from __future__ import annotations
import os, time, traceback

DONE_MIN = int(os.environ.get("REAPER_DONE_MIN", "5"))
# 10 min, not 30: measured real vAuto jobs complete in 23-29s (p100 across every
# closed row today), so 10 minutes is ~24x the worst observed duration — far too
# generous to ever kill live work, while capping a hostage bid at 10 min instead
# of the 17 HOURS bid 5064 sat before it was released by hand.
DEAD_MIN = int(os.environ.get("REAPER_DEAD_MIN", "10"))
INTERVAL = int(os.environ.get("REAPER_INTERVAL", "300"))
ALERT_TO = os.environ.get("REAPER_ALERT_PHONE", "+14074309675")
ALERT_ON = os.environ.get("REAPER_ALERT", "1") == "1"

_last_run = [0.0]
_alerted: set[int] = set()

PREDICATE = f"""
    vauto_claimed_by IS NOT NULL
    AND (
          (ai_price IS NOT NULL
           AND vauto_claimed_at < now() - interval '{DONE_MIN} minutes')
       OR vauto_claimed_at < now() - interval '{DEAD_MIN} minutes'
    )
"""

# Read-only preview (dry run / tests).
SELECT_SQL = f"""
    SELECT id, vauto_claimed_by,
           round(EXTRACT(EPOCH FROM (now() - vauto_claimed_at)) / 60) AS age_min,
           ai_price IS NOT NULL AS priced,
           year, make, model
      FROM bids
     WHERE {PREDICATE}
     ORDER BY vauto_claimed_at
"""

# ATOMIC claim-and-release. _watchdog_loop runs in EVERY gunicorn worker (10
# processes), and the throttle below is per-process — so a plain
# SELECT-then-UPDATE would let several processes each see the same stuck bid
# and each text the operator about it. FOR UPDATE SKIP LOCKED means exactly one
# process takes each row; the others match nothing and stay silent.
# The CTE captures the OLD values before the UPDATE nulls them, which a bare
# `UPDATE ... RETURNING` cannot do.
REAP_SQL = f"""
    WITH victims AS (
        SELECT id, vauto_claimed_by,
               round(EXTRACT(EPOCH FROM (now() - vauto_claimed_at)) / 60) AS age_min,
               ai_price IS NOT NULL AS priced,
               year, make, model
          FROM bids
         WHERE {PREDICATE}
         ORDER BY vauto_claimed_at
           FOR UPDATE SKIP LOCKED
    ), released AS (
        UPDATE bids
           SET vauto_priority   = FALSE,
               vauto_claimed_by = NULL,
               vauto_claimed_at = NULL
         WHERE id IN (SELECT id FROM victims)
        RETURNING id
    )
    SELECT * FROM victims
"""

# worker_jobs is history only, but leave it consistent so /admin/workers agrees
CLOSE_JOBS_SQL = """
    UPDATE worker_jobs
       SET completed_at = NOW(),
           status       = 'released_reaper',
           duration_ms  = NULL,
           error        = 'claim reaper: claim outlived the work'
     WHERE bid_id = ANY(%s)
       AND completed_at IS NULL
       AND job_type = 'vauto'
"""


def reap(get_db, send_sms=None, force: bool = False) -> list[dict]:
    """One pass. Returns the released rows. Never raises."""
    if not force and (time.time() - _last_run[0]) < INTERVAL:
        return []
    _last_run[0] = time.time()
    released: list[dict] = []
    # ── DB work: the ONLY thing in this try. Nothing non-essential belongs here.
    # v1 had the progress print()s inside this block, upstream of the alert. The
    # commit had already landed, so a throw left the claim released but returned
    # [] before the SMS ever ran — the reaper silently fixed bid 5102 at 13:17
    # and never told anyone. Logging must never be able to suppress an alarm.
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute(REAP_SQL)                 # atomic: only one process wins a row
        rows = [dict(r) for r in cur.fetchall()]
        if rows:
            cur.execute(CLOSE_JOBS_SQL, ([r["id"] for r in rows],))
        db.commit()
        db.close()
        released = rows
    except Exception:
        try: traceback.print_exc()
        except Exception: pass
        return []

    if not released:
        return []

    # ── alerting: independent of logging, and RECORDED so it can be audited.
    # stdout from this daemon thread demonstrably does not reach the app log, so
    # "it printed" is not evidence of anything. The DB row is.
    sent_ok = None
    fresh = [r for r in released if r["id"] not in _alerted]
    if fresh and ALERT_ON and send_sms:
        for r in fresh:
            _alerted.add(r["id"])
        try:
            sent_ok = bool(send_sms(ALERT_TO, _compose(fresh)))
        except Exception:
            sent_ok = False
            try: traceback.print_exc()
            except Exception: pass

    try:
        db = get_db()
        cur = db.cursor()
        cur.execute(
            "UPDATE worker_jobs SET error = %s WHERE bid_id = ANY(%s) "
            "AND status = 'released_reaper'",
            (f"claim reaper: released; alert_sent={sent_ok}",
             [r["id"] for r in released]))
        db.commit()
        db.close()
    except Exception:
        pass

    try:
        for r in released:
            print(f"[claim-reaper] released bid={r['id']} worker={r['vauto_claimed_by']} "
                  f"age={r['age_min']}m priced={r['priced']} alert_sent={sent_ok}",
                  flush=True)
    except Exception:
        pass
    return released


def _compose(rows: list[dict]) -> str:
    head = rows[0]
    ymm = " ".join(str(x) for x in (head.get("year"), head.get("make"),
                                    head.get("model")) if x)
    if len(rows) == 1:
        return (f"EW worker stuck: bid #{head['id']} ({ymm or 'unknown'}) was held by "
                f"{head['vauto_claimed_by']} for {int(head['age_min'])} min. "
                f"Claim released automatically; the bid is fine.")
    ids = ", ".join(f"#{r['id']}" for r in rows[:6])
    more = f" +{len(rows)-6} more" if len(rows) > 6 else ""
    return (f"EW workers stuck: {len(rows)} bids held too long ({ids}{more}). "
            f"Claims released automatically.")


if __name__ == "__main__":
    # Standalone dry-run / manual pass: DRY=1 to only report.
    import sys
    sys.path.insert(0, "/opt/expwholesale")
    import psycopg2, psycopg2.extras

    DSN = os.environ.get("DATABASE_URL")
    if not DSN:
        for ln in open("/etc/default/expwholesale-mcp"):
            if ln.strip().startswith("DATABASE_URL="):
                DSN = ln.strip().split("=", 1)[1].strip().strip('"').strip("'")

    def _db():
        c = psycopg2.connect(DSN)
        c.cursor_factory = psycopg2.extras.RealDictCursor
        return _Wrap(c)

    class _Wrap:
        def __init__(self, c): self._c = c
        def cursor(self): return self._c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        def commit(self): self._c.commit()
        def close(self): self._c.close()

    if os.environ.get("DRY", "1") == "1":
        c = psycopg2.connect(DSN)
        cu = c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cu.execute(SELECT_SQL)
        rows = [dict(r) for r in cu.fetchall()]
        c.close()
        print(f"DRY RUN — rules: priced&>{DONE_MIN}m OR any>{DEAD_MIN}m")
        print(f"would release {len(rows)} bid(s):")
        for r in rows:
            print("  ", r)
        if rows:
            print("\nSMS that would be sent to", ALERT_TO, ":")
            print("  ", _compose(rows))
    else:
        out = reap(_db, send_sms=None, force=True)
        print(f"released {len(out)}")
