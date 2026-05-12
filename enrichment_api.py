"""enrichment_api.py — claim/submit endpoints for the dedicated enrichment
worker fleet on pve-pc1 (vm-oscar-worker-1 / vm-oscar-worker-2).

Job types:
  - rbook:    scrape vAuto rBook "show my vehicle" competitive-set table
              (other dealers retailing this YMM, with prices + days on lot)
  - manheim:  scrape vAuto MMR "View Transactions" table
              (recent Manheim auction sales for this YMM, with hammer prices)

Workers poll /api/enrichment/claim, navigate the bid's saved appraisal URL
in Playwright, scrape the requested table, POST to /api/enrichment/submit.

A bid becomes eligible when:
  - vauto_lookups.appraisal_url contains a real saved-vAuto URL
    (https://provision.vauto.app.coxautoinc.com/Va/Appraisal/Default.aspx?Id=…)
  - The job type's *_completed_at column is NULL
  - The job is not currently leased (claim < 5 min ago)

Lease semantics: a worker holding a job has 5 minutes to submit. After
that another worker can re-claim. Retry/error tracking lives in the
enrichment_state JSONB on vauto_lookups.
"""

from __future__ import annotations
import os
import json
import time
from flask import Blueprint, request, jsonify
import psycopg2
import psycopg2.extras


bp = Blueprint('enrichment', __name__)


# Lease window: how long a worker has to complete a claim before the row
# becomes re-claimable by another worker. Keep generous — vAuto navigation
# can be slow under load (Cox Akamai challenges, virtualized rows, etc.).
LEASE_SECONDS = 300

# Saved-appraisal URL prefix — only bids that have this as their
# appraisal_url are eligible for enrichment.
SAVED_URL_PREFIX = 'https://provision.vauto.app.coxautoinc.com/Va/Appraisal/Default.aspx'

VALID_JOB_TYPES = ('rbook', 'manheim')


def _db():
    return psycopg2.connect(os.environ['DATABASE_URL'],
                            cursor_factory=psycopg2.extras.RealDictCursor)


def _ensure_workers_table():
    """Optional: track enrichment worker heartbeats separately so the
    /admin/workers dashboard can show them next to main fleet."""
    try:
        db = _db()
        cur = db.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS enrichment_workers (
                worker_id TEXT PRIMARY KEY,
                last_heartbeat TIMESTAMPTZ DEFAULT NOW(),
                last_claim_id INTEGER,
                last_claim_type TEXT,
                jobs_completed INTEGER DEFAULT 0,
                jobs_failed INTEGER DEFAULT 0,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        db.commit()
        db.close()
    except Exception as e:
        print(f'[enrichment] table ensure error: {e}', flush=True)


_ensure_workers_table()


# ── /api/enrichment/claim ────────────────────────────────────────────────

@bp.route('/api/enrichment/claim', methods=['POST'])
def claim_job():
    """Worker requests a job. Body:
       {worker_id: "vm-oscar-worker-1", job_types: ["rbook"]}
    Returns either {"job": {...}} or {"job": null}.

    Atomic claim via SELECT FOR UPDATE SKIP LOCKED to handle multiple
    workers polling at once. Lease is recorded in enrichment_state JSONB.
    """
    body = request.get_json(silent=True) or {}
    worker_id = (body.get('worker_id') or '').strip()
    job_types = body.get('job_types') or list(VALID_JOB_TYPES)
    # Optional: pin to a single bid_id for iteration/debug
    only_bid = body.get('bid_id')
    try: only_bid = int(only_bid) if only_bid is not None else None
    except (TypeError, ValueError): only_bid = None

    if not worker_id:
        return jsonify({'error': 'worker_id required'}), 400

    # Sanitize requested job_types
    job_types = [t for t in job_types if t in VALID_JOB_TYPES]
    if not job_types:
        return jsonify({'error': f'job_types must include any of {VALID_JOB_TYPES}'}), 400

    db = _db()
    cur = db.cursor()
    try:
        # Heartbeat the worker
        cur.execute("""
            INSERT INTO enrichment_workers (worker_id, last_heartbeat)
            VALUES (%s, NOW())
            ON CONFLICT (worker_id)
            DO UPDATE SET last_heartbeat = NOW()
        """, (worker_id,))

        # Try each requested job type in order. First one with eligible work wins.
        # Per-type completion gate AND lease check, both inline in the WHERE.
        for jtype in job_types:
            completed_col = f'{jtype}_completed_at'
            # SELECT for UPDATE SKIP LOCKED so concurrent workers don't
            # claim the same row. Excludes rows where someone holds an
            # active (< LEASE_SECONDS) claim on this same job type.
            bid_clause = ' AND bid_id = %s' if only_bid else ''
            # rbook is now demoted to true-fallback only:
            # - direct vAuto BFF gets first crack (kicked from
            #   /api/vauto/submit + /api/vauto/url_capture_result, which
            #   stamp enrichment_state.rbook.direct_started_at synchronously
            #   BEFORE spawning the daemon thread).
            # - legacy oscar-worker may claim only when direct has had a
            #   chance: either direct_started_at is set AND >= 60s old, OR
            #   no direct attempt happened at all AND the bid is old enough
            #   (5 min from looked_up_at) that we assume direct won't fire.
            # manheim is unchanged — direct API doesn't return the
            # transaction-level Manheim rows that the scraper produces, so
            # legacy MUST run for manheim.
            if jtype == 'rbook':
                direct_defer_clause = """
                  AND (
                       (enrichment_state->'rbook'->>'direct_started_at' IS NOT NULL
                        AND (enrichment_state->'rbook'->>'direct_started_at')::timestamptz
                            < NOW() - INTERVAL '60 seconds')
                    OR (enrichment_state->'rbook'->>'direct_started_at' IS NULL
                        AND looked_up_at < NOW() - INTERVAL '5 minutes')
                  )
                """
            else:
                direct_defer_clause = ''
            sql = f"""
                SELECT id, bid_id, vin, appraisal_url, enrichment_state
                FROM vauto_lookups
                WHERE appraisal_url LIKE %s
                  AND {completed_col} IS NULL
                  {bid_clause}
                  {direct_defer_clause}
                  AND (
                       enrichment_state IS NULL
                    OR enrichment_state->>%s IS NULL
                    OR (enrichment_state->%s->>'claimed_at')::timestamptz
                       < NOW() - (%s || ' seconds')::interval
                    OR enrichment_state->%s->>'status' = 'failed'
                  )
                ORDER BY looked_up_at DESC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            """
            params = [f'{SAVED_URL_PREFIX}%']
            if only_bid:
                params.append(only_bid)
            params += [jtype, jtype, str(LEASE_SECONDS), jtype]
            cur.execute(sql, tuple(params))
            row = cur.fetchone()
            if not row:
                continue

            # Stamp the lease into enrichment_state JSONB
            new_state = dict(row.get('enrichment_state') or {})
            new_state[jtype] = {
                'status': 'claimed',
                'claimed_by': worker_id,
                'claimed_at': time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime()),
                'attempts': int((new_state.get(jtype) or {}).get('attempts', 0)) + 1,
            }
            cur.execute("""
                UPDATE vauto_lookups
                SET enrichment_state = %s::jsonb
                WHERE id = %s
            """, (json.dumps(new_state), row['id']))

            cur.execute("""
                UPDATE enrichment_workers
                SET last_claim_id = %s, last_claim_type = %s
                WHERE worker_id = %s
            """, (row['id'], jtype, worker_id))

            db.commit()
            return jsonify({'job': {
                'vauto_lookups_id': row['id'],
                'bid_id': row['bid_id'],
                'vin': row['vin'],
                'appraisal_url': row['appraisal_url'],
                'type': jtype,
                'lease_seconds': LEASE_SECONDS,
            }})

        # No work for any of the requested types
        db.commit()
        return jsonify({'job': None})
    except Exception as e:
        db.rollback()
        print(f'[enrichment claim] error: {e}', flush=True)
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500
    finally:
        db.close()


# ── /api/enrichment/submit ──────────────────────────────────────────────

@bp.route('/api/enrichment/submit', methods=['POST'])
def submit_job():
    """Worker submits scraped result. Body:
      {worker_id, vauto_lookups_id, bid_id, type, status: "done"|"failed",
       data: [...] | {...}, error: null|"...", duration_ms: 45000}
    """
    body = request.get_json(silent=True) or {}
    worker_id = (body.get('worker_id') or '').strip()
    vl_id = body.get('vauto_lookups_id')
    jtype = body.get('type')
    status = body.get('status')
    data = body.get('data')
    err = body.get('error')
    duration_ms = body.get('duration_ms')

    if jtype not in VALID_JOB_TYPES:
        return jsonify({'error': f'invalid type {jtype!r}'}), 400
    if status not in ('done', 'failed'):
        return jsonify({'error': f'status must be done|failed'}), 400
    if not vl_id:
        return jsonify({'error': 'vauto_lookups_id required'}), 400

    # data column to write into
    data_col = 'rbook_competitive_set' if jtype == 'rbook' else 'manheim_transactions'
    completed_col = f'{jtype}_completed_at'

    db = _db()
    cur = db.cursor()
    try:
        cur.execute("SELECT enrichment_state FROM vauto_lookups WHERE id=%s",
                    (vl_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({'error': 'vauto_lookups_id not found'}), 404

        new_state = dict(row.get('enrichment_state') or {})
        new_state[jtype] = {
            **(new_state.get(jtype) or {}),
            'status': status,
            'finished_by': worker_id,
            'finished_at': time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime()),
            'duration_ms': duration_ms,
        }
        if err:
            new_state[jtype]['error'] = str(err)[:500]

        if status == 'done':
            # Guard: for rbook jobs, skip the write if direct_api has
            # already populated this bid. Without this, a legacy
            # EWEnrichRbook scrape that started before the direct_api
            # kick would later overwrite the fresher direct_api data
            # with its 99s+ result. Manheim has no parallel direct path,
            # so it's unaffected.
            guard = ''
            if jtype == 'rbook':
                guard = (" AND (rbook_completed_at IS NULL OR "
                         "COALESCE(enrichment_state->'rbook'->>'source', '') "
                         "!= 'direct_api')")
            cur.execute(f"""
                UPDATE vauto_lookups
                SET {data_col} = %s::jsonb,
                    {completed_col} = NOW(),
                    enrichment_state = %s::jsonb
                WHERE id = %s{guard}
            """, (json.dumps(data) if data is not None else None,
                  json.dumps(new_state), vl_id))
            if jtype == 'rbook' and cur.rowcount == 0:
                print(f'[enrichment submit] bid={body.get("bid_id")} '
                      f'rbook from {worker_id} skipped — direct_api owns this bid',
                      flush=True)
            cur.execute("""
                UPDATE enrichment_workers
                SET jobs_completed = jobs_completed + 1
                WHERE worker_id = %s
            """, (worker_id,))
        else:
            cur.execute("""
                UPDATE vauto_lookups
                SET enrichment_state = %s::jsonb
                WHERE id = %s
            """, (json.dumps(new_state), vl_id))
            cur.execute("""
                UPDATE enrichment_workers
                SET jobs_failed = jobs_failed + 1
                WHERE worker_id = %s
            """, (worker_id,))

        db.commit()

        # Poke the assess gate. If rbook+manheim are now both done (and
        # vauto/accu/ipkt also present), this fires Gemini immediately.
        # If not, it's a no-op and the 5-minute fallback timer remains armed.
        try:
            bid_id = body.get('bid_id')
            if bid_id and status == 'done':
                from app import _maybe_fire_assessment
                _maybe_fire_assessment(int(bid_id), require_all=True,
                                       source=f'enrichment_{jtype}')
        except Exception as _trig_err:
            print(f'[enrichment submit] gate poke err: {_trig_err}', flush=True)

        return jsonify({'ok': True, 'status': status})
    except Exception as e:
        db.rollback()
        print(f'[enrichment submit] error: {e}', flush=True)
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500
    finally:
        db.close()


# ── /api/enrichment/status — admin/debug visibility ─────────────────────

@bp.route('/api/enrichment/status', methods=['GET'])
def status_dashboard():
    """Quick view of the queue state + worker heartbeats."""
    db = _db()
    cur = db.cursor()
    try:
        cur.execute("""
            SELECT
              COUNT(*) FILTER (WHERE rbook_completed_at IS NULL)
                AS rbook_pending,
              COUNT(*) FILTER (WHERE rbook_completed_at IS NOT NULL)
                AS rbook_done,
              COUNT(*) FILTER (WHERE manheim_completed_at IS NULL)
                AS manheim_pending,
              COUNT(*) FILTER (WHERE manheim_completed_at IS NOT NULL)
                AS manheim_done
            FROM vauto_lookups
            WHERE appraisal_url LIKE %s
        """, (f'{SAVED_URL_PREFIX}%',))
        queue = dict(cur.fetchone())

        cur.execute("""
            SELECT worker_id, last_heartbeat,
                   AGE(NOW(), last_heartbeat) AS staleness,
                   last_claim_id, last_claim_type,
                   jobs_completed, jobs_failed
            FROM enrichment_workers
            ORDER BY worker_id
        """)
        workers = [dict(r) for r in cur.fetchall()]
        # Convert intervals/timestamps to strings
        for w in workers:
            for k in ('last_heartbeat', 'staleness'):
                if w.get(k) is not None and hasattr(w[k], 'isoformat'):
                    w[k] = w[k].isoformat()
                elif w.get(k) is not None:
                    w[k] = str(w[k])

        return jsonify({'queue': queue, 'workers': workers})
    finally:
        db.close()
