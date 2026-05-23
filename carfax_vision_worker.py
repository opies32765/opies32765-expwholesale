#!/usr/bin/env python3
"""carfax_vision_worker.py — extract trim/year/make/model/accidents/owners/title_status
from Carfax + AutoCheck PNG screenshots via Gemini Vision.

CARFAX_VISION_WORKER_2026_05_23.

Forward-only by design: cutoff is looked_up_at > NOW() - INTERVAL '30 minutes'.
No backfill. Killable via CARFAX_VISION_DISABLED=1 env var.

Writes to vauto_lookups.carfax_json + autocheck_json. Trim overseer
(_evidence_first_trim_pick in app.py) already reads _doc.get('trim') from
these columns since commit b3f39f4.

Screenshot paths in the DB are relative, e.g. /vauto_reports/carfax_<VIN>.png.
Resolved as /opt/expwholesale<path>.

gemini_call signature (from app.py):
    gemini_call(prompt, image_bytes=None, mime='image/jpeg', model='gemini-2.5-flash',
                max_tokens=1024, temperature=0.4, disable_thinking=False)

CARFAX_PROMPT returns JSON with keys:
    vin, year, make, model, trim, mileage, title_status, accidents,
    owners, color, asking_price
"""
import json
import os
import sys
import time
import traceback

# app.py sets GOOGLE_APPLICATION_CREDENTIALS via os.environ.setdefault at import
# time — that fires here when we import gemini_call / CARFAX_PROMPT below.
sys.path.insert(0, '/opt/expwholesale')

DB_DSN = (
    os.environ.get('DATABASE_URL')
    or 'postgresql://expuser:ExpWholesale2026!@127.0.0.1:5433/expwholesale'
)

APP_ROOT = '/opt/expwholesale'


def _killed():
    return os.environ.get('CARFAX_VISION_DISABLED', '0') == '1'


def _resolve(path):
    """Convert DB-stored relative path to absolute. Handles:
      /vauto_reports/carfax_<VIN>.png  ->  /opt/expwholesale/vauto_reports/carfax_<VIN>.png
    """
    if not path:
        return None
    if path.startswith(APP_ROOT):
        return path
    if path.startswith('/'):
        return f'{APP_ROOT}{path}'
    return f'{APP_ROOT}/{path}'


def _extract_via_gemini(file_path):
    """Call Gemini 2.5 Flash with CARFAX_PROMPT on the PNG. Returns dict or None."""
    try:
        from app import gemini_call, CARFAX_PROMPT
        with open(file_path, 'rb') as fh:
            png_bytes = fh.read()
        raw = gemini_call(
            CARFAX_PROMPT,
            image_bytes=png_bytes,
            mime='image/png',
            model='gemini-2.5-flash',
            max_tokens=1024,
            disable_thinking=True,
        )
        if not raw or not isinstance(raw, str):
            return None
        text = raw.strip()
        # Strip markdown code fences if present (gemini_call usually returns
        # clean JSON via CARFAX_PROMPT rules, but be defensive)
        if text.startswith('```'):
            lines = [ln for ln in text.split('\n') if not ln.strip().startswith('```')]
            text = '\n'.join(lines).strip()
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            return None
        return parsed
    except Exception as exc:
        print(f'[carfax-vision] gemini err on {file_path}: {exc}', flush=True)
        return None


def _process_row(conn, cur, r):
    # Carfax
    if not r['carfax_json'] and r['carfax_screenshot']:
        fp = _resolve(r['carfax_screenshot'])
        if fp and os.path.exists(fp):
            payload = _extract_via_gemini(fp)
            if payload is not None:
                cur.execute(
                    'UPDATE vauto_lookups SET carfax_json=%s::jsonb WHERE id=%s',
                    (json.dumps(payload), r['id']),
                )
                conn.commit()
                print(
                    f'[carfax-vision] id={r["id"]} bid={r["bid_id"]} '
                    f'carfax OK trim={payload.get("trim")!r}',
                    flush=True,
                )
            else:
                print(f'[carfax-vision] id={r["id"]} carfax extract returned None', flush=True)
        else:
            print(f'[carfax-vision] id={r["id"]} carfax file missing: {fp}', flush=True)
    # AutoCheck
    if not r['autocheck_json'] and r['autocheck_screenshot']:
        fp = _resolve(r['autocheck_screenshot'])
        if fp and os.path.exists(fp):
            payload = _extract_via_gemini(fp)
            if payload is not None:
                cur.execute(
                    'UPDATE vauto_lookups SET autocheck_json=%s::jsonb WHERE id=%s',
                    (json.dumps(payload), r['id']),
                )
                conn.commit()
                print(
                    f'[carfax-vision] id={r["id"]} bid={r["bid_id"]} '
                    f'autocheck OK trim={payload.get("trim")!r}',
                    flush=True,
                )
            else:
                print(f'[carfax-vision] id={r["id"]} autocheck extract returned None', flush=True)
        else:
            print(f'[carfax-vision] id={r["id"]} autocheck file missing: {fp}', flush=True)
    time.sleep(2)  # gentle pacing between carfax + autocheck on the same row


def main():
    import psycopg2
    import psycopg2.extras

    print(f'[carfax-vision] starting (PID {os.getpid()})', flush=True)

    while True:
        if _killed():
            print('[carfax-vision] kill switch active, sleeping 60s', flush=True)
            time.sleep(60)
            continue
        try:
            conn = psycopg2.connect(DB_DSN)
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("""
                SELECT id, bid_id, vin, carfax_screenshot, autocheck_screenshot,
                       carfax_json, autocheck_json
                FROM vauto_lookups
                WHERE looked_up_at > NOW() - INTERVAL '30 minutes'
                  AND (
                       (carfax_json IS NULL AND carfax_screenshot IS NOT NULL)
                    OR (autocheck_json IS NULL AND autocheck_screenshot IS NOT NULL)
                  )
                ORDER BY looked_up_at DESC
                LIMIT 5
            """)
            rows = cur.fetchall()
            if not rows:
                conn.close()
                print('[carfax-vision] no recent rows — sleeping 30s', flush=True)
                time.sleep(30)
                continue
            print(f'[carfax-vision] processing {len(rows)} row(s)', flush=True)
            for r in rows:
                _process_row(conn, cur, r)
            conn.close()
            time.sleep(5)
        except Exception as exc:
            print(f'[carfax-vision] loop err: {exc}', flush=True)
            traceback.print_exc()
            time.sleep(60)


if __name__ == '__main__':
    main()
