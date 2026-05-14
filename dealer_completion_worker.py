#!/usr/bin/env python3
"""
Dealer completion worker — runs continuously as a systemd service.

Keeps filling in AI-derived fields (currently: ext_color) across every active
dealer until the whole fleet is complete. Decoupled from scans so that:

  - Scans stay fast (snapshot + sold + price drops only).
  - Completion is a separate, steady background process.
  - Adding a new dealer auto-kicks completion in minutes, not at 6 AM.

Rate limiting:
  - 1 Gemini call every ~5s (~12/min, ~720/hr)
  - Exponential backoff on 429: 60s → 120s → 300s → 600s cap
  - When no work is available: sleep 60s, re-check

Stop conditions:
  - SIGTERM / SIGINT — graceful exit
  - Never exits on its own. systemd restarts on crash.
"""
import contextlib
import io
import json
import os
import signal
import sys
import threading
import time
from datetime import datetime

import psycopg2
import psycopg2.extras
import requests


GEMINI_CALL_TIMEOUT_SEC = int(os.environ.get('GEMINI_CALL_TIMEOUT', '30'))


def _call_with_timeout(fn, timeout_sec, *args, **kwargs):
    """Run fn in a daemon thread; return (ok, result) where ok=False on timeout.
    Needed because Google genai SDK can hang indefinitely on half-open connections."""
    result = {'done': False, 'value': None, 'exc': None}

    def _runner():
        try:
            result['value'] = fn(*args, **kwargs)
        except Exception as e:
            result['exc'] = e
        finally:
            result['done'] = True

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout_sec)
    if not result['done']:
        return (False, None)
    if result['exc']:
        raise result['exc']
    return (True, result['value'])

import dealer_scanner                                  # reuses validator + session
from dealer_scanner import _is_valid_color, get_conn

# Tuning
CALL_INTERVAL_SEC = float(os.environ.get('COMPLETION_CALL_INTERVAL', '5'))
EMPTY_QUEUE_SLEEP_SEC = int(os.environ.get('COMPLETION_EMPTY_SLEEP', '60'))
BACKOFF_STEPS = (60, 120, 300, 600)
BATCH_SIZE = int(os.environ.get('COMPLETION_BATCH', '20'))
REQUEST_TIMEOUT = int(os.environ.get('DEALER_HTTP_TIMEOUT', '20'))

_stop = False


def _signal_handler(signum, frame):
    global _stop
    _stop = True
    print(f'[{_ts()}] signal {signum} received — will exit after current task', flush=True)


def _ts():
    return datetime.now().isoformat(timespec='seconds')


def _load_batch():
    """Pull the next batch of vehicles that need completion.
    Prioritises rows not touched recently so the queue cycles naturally."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute('''SELECT i.id, i.dealer_id, i.vin, i.photo_url, i.photos,
                              d.name AS dealer_name
                       FROM dealer_inventory i
                       JOIN dealers d ON d.id = i.dealer_id
                       WHERE d.active = TRUE
                         AND i.status = 'active'
                         AND (i.ext_color IS NULL OR i.ext_color = '')
                         AND i.photo_url IS NOT NULL AND i.photo_url <> ''
                       ORDER BY i.updated_at ASC
                       LIMIT %s''', (BATCH_SIZE,))
        return cur.fetchall()


def _fill_color(row, sess):
    """Returns (success, color_string, rate_limited_flag, fetch_failed_flag).
    Captures extract_color_from_file's stdout to detect 429 which otherwise
    gets swallowed into a None return."""
    try:
        from app import extract_color_from_file
    except Exception as e:
        print(f'[{_ts()}] extract_color_from_file unavailable: {e}', flush=True)
        return (False, None, False, True)

    urls = [row['photo_url']]
    photos = row.get('photos')
    if isinstance(photos, list):
        for u in photos[:4]:
            if isinstance(u, str) and u and u not in urls:
                urls.append(u)
            if len(urls) >= 3:
                break

    color = None
    rate_limited = False
    fetch_failed = False
    for u in urls[:3]:
        try:
            resp = sess.get(u, timeout=REQUEST_TIMEOUT)
            if resp.status_code != 200 or not resp.content:
                fetch_failed = True
                continue
            mime = resp.headers.get('Content-Type', 'image/jpeg').split(';')[0].strip().lower()
            # Skip non-image responses. Some dealer sites (e.g. TXT Charlie)
            # serve HTTP 200 with text/html for missing image URLs (a 404
            # page rendered for any unknown path). Sending that HTML to
            # Gemini as image bytes was the cause of the
            # 1095944 > 1048576 token-count INVALID_ARGUMENT errors
            # observed 2026-05-14 — HTML body got binary-tokenized.
            if not mime.startswith('image/'):
                fetch_failed = True
                continue
            # Capture Gemini's stdout + enforce a timeout so a hung SDK call
            # doesn't stall the whole worker.
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ok_call, raw = _call_with_timeout(
                    extract_color_from_file, GEMINI_CALL_TIMEOUT_SEC,
                    resp.content, mime)
            captured = buf.getvalue()
            if not ok_call:
                print(f'[{_ts()}] gemini call timed out after {GEMINI_CALL_TIMEOUT_SEC}s on {u[-60:]}',
                      flush=True)
                raw = None
                continue
            if captured:
                for line in captured.strip().splitlines():
                    print(f'[{_ts()}] gemini: {line}', flush=True)
                if '429' in captured or 'RESOURCE_EXHAUSTED' in captured or 'quota' in captured.lower():
                    rate_limited = True
                    break
        except Exception as e:
            err = str(e).lower()
            if '429' in err or 'resource_exhausted' in err or 'quota' in err:
                rate_limited = True
            print(f'[{_ts()}] fetch/detect failed {u[-60:]}: {e}', flush=True)
            fetch_failed = True
            continue
        if _is_valid_color(raw):
            color = raw
            break

    return (bool(color), color, rate_limited, fetch_failed)


def _persist(inv_id, color):
    with get_conn() as conn, conn.cursor() as cur:
        if color:
            cur.execute('''UPDATE dealer_inventory
                           SET ext_color=%s, updated_at=NOW()
                           WHERE id=%s''', (color, inv_id))
        else:
            # touch updated_at so this row drops to the back of the queue
            cur.execute('UPDATE dealer_inventory SET updated_at=NOW() WHERE id=%s', (inv_id,))
        conn.commit()


def main():
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    print(f'[{_ts()}] completion worker started (interval={CALL_INTERVAL_SEC}s, batch={BATCH_SIZE})',
          flush=True)

    sess = requests.Session()
    sess.headers.update({'User-Agent': os.environ.get(
        'DEALER_SCANNER_UA',
        'Mozilla/5.0 (compatible; EW-DealerCompletion/1.0; +https://experience-wholesale.net)')})

    backoff_idx = 0
    filled_total = 0
    attempted_total = 0
    session_started = time.time()

    while not _stop:
        batch = _load_batch()
        if not batch:
            elapsed = int(time.time() - session_started)
            print(f'[{_ts()}] queue empty (session so far: {filled_total} filled / {attempted_total} tried in {elapsed}s) — sleeping {EMPTY_QUEUE_SLEEP_SEC}s',
                  flush=True)
            for _ in range(EMPTY_QUEUE_SLEEP_SEC):
                if _stop:
                    break
                time.sleep(1)
            continue

        print(f'[{_ts()}] batch of {len(batch)} vehicles needing color', flush=True)
        for row in batch:
            if _stop:
                break
            attempted_total += 1
            ok, color, rate_limited, fetch_failed = _fill_color(row, sess)
            if rate_limited:
                delay = BACKOFF_STEPS[min(backoff_idx, len(BACKOFF_STEPS) - 1)]
                backoff_idx += 1
                print(f'[{_ts()}] Gemini 429 — backing off {delay}s (step {backoff_idx})',
                      flush=True)
                for _ in range(delay):
                    if _stop:
                        break
                    time.sleep(1)
                break  # restart loop, reload batch
            # on a successful call (even if Gemini returned no color), reset backoff
            backoff_idx = 0
            _persist(row['id'], color if ok else None)
            if ok:
                filled_total += 1
                print(f'[{_ts()}] {row["dealer_name"][:20]:<20} inv#{row["id"]} vin={row["vin"][-6:] if row["vin"] else "NO_VIN"} -> {color}',
                      flush=True)
            elif not fetch_failed:
                # Gemini replied but nothing usable — often interior / dashboard shots
                print(f'[{_ts()}] {row["dealer_name"][:20]:<20} inv#{row["id"]} no color (gemini returned nothing usable)',
                      flush=True)
            # Polite pacing between calls
            for _ in range(int(CALL_INTERVAL_SEC)):
                if _stop:
                    break
                time.sleep(1)

    print(f'[{_ts()}] completion worker exiting — filled={filled_total} attempted={attempted_total}',
          flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
