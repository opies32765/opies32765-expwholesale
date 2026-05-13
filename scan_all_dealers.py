#!/usr/bin/env python3
"""
Daily scan runner — loops every active dealer, scans each serially.

Designed for cron. Logs to stdout (cron redirects to /var/log/ew-dealer-scans.log).
One scan at a time to stay polite to dealer sites and stay under Gemini RPM.

Usage:
    venv/bin/python scan_all_dealers.py
    venv/bin/python scan_all_dealers.py --dealer-id 1,2   # specific subset
"""
import argparse
import os
import sys
import time
from datetime import datetime

import requests

import dealer_scanner
import os as _os_for_timeout
# Wall-clock budget per dealer scan. Stuck WAF (Sucuri/CF) can otherwise
# hang the entire daily run indefinitely. 30 min covers the slowest
# legitimate scans (Napletons today: 66 min on CF challenge — exceptions
# happen but we'd rather skip + retry than block the queue).
PER_DEALER_TIMEOUT_SEC = int(_os_for_timeout.environ.get('DEALER_SCAN_PER_DEALER_TIMEOUT', '1800'))

# Reuse Orlando AI Solutions Telegram bot (@OrlandoAISolutionsBOT → Oscar's chat).
# Tokens live in /opt/orlando-chatbot/.env on Contabo 2; we read once at startup
# so the cron run is self-contained without needing systemd env additions.
TG_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN') or '8639130743:AAHobws_MAaShpjxaHC0kXMuHZwbebtuYFM'
TG_CHAT  = os.environ.get('TELEGRAM_CHAT_ID')   or '7985611488'


def tg_send(text):
    """Fire-and-forget Telegram notification. Never raises — we don't want a TG
    glitch to break the scan run."""
    if not (TG_TOKEN and TG_CHAT):
        return
    try:
        requests.post(
            f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
            json={'chat_id': TG_CHAT, 'text': text, 'parse_mode': 'HTML',
                  'disable_web_page_preview': True},
            timeout=10,
        )
    except Exception:
        pass


def _status_emoji(status):
    return {'ok': '🌅', 'blocked': '🚧', 'error': '🛑'}.get(status, '❔')


def _scan_summary_line(name, stats):
    """One-line dealer summary for the Telegram digest."""
    status = stats.get('status', '?')
    em = _status_emoji(status)
    found = stats.get('vehicles_found', 0)
    new = stats.get('new_count', 0)
    sold = stats.get('sold_count', 0)
    missing = stats.get('missing_count', 0)
    drops = stats.get('price_drop_count', 0)
    tier = stats.get('tier', '?')
    if status == 'ok':
        bits = [f'<b>{name}</b>', f'{found} found']
        if new:     bits.append(f'+{new} new')
        if sold:    bits.append(f'{sold} sold')
        if missing: bits.append(f'{missing} missing')
        if drops:   bits.append(f'{drops} price-drop')
        return f'{em} ' + ' · '.join(bits)
    if status == 'blocked':
        err = (stats.get('error') or 'blocked').strip()[:140]
        return f'{em} <b>{name}</b> · BLOCKED via {tier} — inventory preserved\n   <i>{err}</i>'
    return f'{em} <b>{name}</b> · {status.upper()} ({tier}) — {(stats.get("error") or "")[:140]}'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dealer-id', help='Comma-separated dealer ids to scan (default: all active)')
    args = ap.parse_args()

    with dealer_scanner.get_conn() as conn, conn.cursor() as cur:
        if args.dealer_id:
            ids = tuple(int(x) for x in args.dealer_id.split(',') if x.strip().isdigit())
            cur.execute('SELECT id, name FROM dealers WHERE id = ANY(%s) AND active=TRUE ORDER BY id',
                        (list(ids),))
        else:
            cur.execute('SELECT id, name FROM dealers WHERE active=TRUE ORDER BY id')
        dealers = cur.fetchall()

    print(f'[{datetime.now().isoformat(timespec="seconds")}] scan_all: {len(dealers)} dealers',
          flush=True)

    totals = {'new': 0, 'sold': 0, 'missing': 0, 'colors': 0,
              'price_drops': 0, 'ok': 0, 'blocked': 0, 'error': 0}
    started = time.time()
    summary_lines = []

    for d in dealers:
        d_start = time.time()
        print(f'\n--- dealer {d["id"]}: {d["name"]} ---', flush=True)
        try:
            scanner = dealer_scanner.DealerScanner.from_dealer_id(d['id'])
            _ok, stats = dealer_scanner._call_with_timeout(
                scanner.run, PER_DEALER_TIMEOUT_SEC)
            if not _ok:
                # Wall-clock budget exceeded — mark this dealer as timed_out,
                # don't fail the whole run. Inventory rows are untouched
                # (scan_id never got finalize()'d, no INSERT/UPDATE damage).
                stats = {
                    'status': 'timed_out',
                    'error': f'wall-clock deadline {PER_DEALER_TIMEOUT_SEC}s exceeded',
                    'tier': 'direct',
                    'platform_detected': '?',
                    'vehicles_found': 0,
                    'new_count': 0, 'sold_count': 0, 'missing_count': 0,
                    'colors_detected': 0, 'price_drop_count': 0,
                }
                # Close any 'running' dealer_scans row for this dealer so the
                # next run's _zero_vehicle_abort_check doesn't see a stale
                # in-flight scan and bail.
                try:
                    with dealer_scanner.get_conn() as _cn, _cn.cursor() as _cu:
                        _cu.execute(
                            """UPDATE dealer_scans
                                  SET status='timed_out',
                                      finished_at=NOW(),
                                      error_message=%s
                                WHERE dealer_id=%s AND status='running'""",
                            (stats['error'], d['id']))
                        _cn.commit()
                except Exception as _e:
                    print(f'  failed to close orphan scan row: {_e}', flush=True)
                # Telegram alert — we want to know which dealer choked
                try:
                    tg_send(
                        f'⏱️ <b>{d["name"]} scan TIMED OUT</b>\n'
                        f'<i>exceeded {PER_DEALER_TIMEOUT_SEC // 60}-min budget</i>\n'
                        f'inventory preserved, will retry tomorrow'
                    )
                except Exception:
                    pass
            status = stats.get('status', '?')
            totals[status] = totals.get(status, 0) + 1
            totals['new']         += stats.get('new_count', 0)
            totals['sold']        += stats.get('sold_count', 0)
            totals['missing']     += stats.get('missing_count', 0)
            totals['colors']      += stats.get('colors_detected', 0)
            totals['price_drops'] += stats.get('price_drop_count', 0)
            print(f'  status={status} tier={stats.get("tier","?")} platform={stats.get("platform_detected","?")}'
                  f' vehicles={stats.get("vehicles_found",0)} new={stats.get("new_count",0)}'
                  f' sold={stats.get("sold_count",0)} drops={stats.get("price_drop_count",0)}'
                  f' colors={stats.get("colors_detected",0)} took={int(time.time()-d_start)}s',
                  flush=True)
            if stats.get('error'):
                print(f'  error: {stats["error"]}', flush=True)
            summary_lines.append(_scan_summary_line(d['name'], stats))
            # Immediate Telegram alert on abort — a dealer scan that aborts
            # with status=blocked has preserved inventory but the dealer's
            # data is now stale until the next run. We want Oscar to know
            # within minutes, not at the end of the multi-hour digest.
            if status == 'blocked':
                err = (stats.get('error') or 'blocked').strip()[:200]
                preserved = stats.get('vehicles_found', 0)
                with dealer_scanner.get_conn() as _cn, _cn.cursor() as _cu:
                    _cu.execute(
                        "SELECT COUNT(*) AS n FROM dealer_inventory "
                        "WHERE dealer_id=%s AND status='active'", (d['id'],))
                    _row = _cu.fetchone()
                    preserved = _row['n'] if isinstance(_row, dict) else _row[0]
                tg_send(
                    f'🚧 <b>{d["name"]} scan ABORTED</b>\n'
                    f'<i>{err}</i>\n'
                    f'{preserved} active rows preserved — will retry next scan'
                )
        except Exception as e:
            totals['error'] += 1
            print(f'  EXCEPTION: {type(e).__name__}: {e}', flush=True)
            summary_lines.append(f'🛑 <b>{d["name"]}</b> · EXCEPTION: {type(e).__name__}')
            tg_send(f'🛑 <b>{d["name"]} scan EXCEPTION</b>\n{type(e).__name__}: {str(e)[:200]}')

    elapsed = int(time.time() - started)
    print(f'\n[{datetime.now().isoformat(timespec="seconds")}] scan_all complete in {elapsed}s',
          flush=True)
    print(f'  totals: new={totals["new"]} sold={totals["sold"]} missing={totals["missing"]}'
          f' drops={totals["price_drops"]} colors={totals["colors"]}', flush=True)
    print(f'  outcomes: ok={totals.get("ok",0)} blocked={totals.get("blocked",0)}'
          f' error={totals.get("error",0)}', flush=True)

    # Telegram digest — one line per dealer + a totals footer.
    if summary_lines:
        digest = ['🌅 <b>EW Dealer Scan</b> · ' + datetime.now().strftime('%a %b %d, %I:%M %p ET')]
        digest.extend(summary_lines)
        digest.append('')
        digest.append(
            f'<i>Σ {totals["ok"]} ok · {totals["blocked"]} blocked · {totals["error"]} err'
            f' · +{totals["new"]} new · {totals["sold"]} sold · {totals["missing"]} missing'
            f' · {totals["price_drops"]} drops · {elapsed}s</i>'
        )
        tg_send('\n'.join(digest))


if __name__ == '__main__':
    sys.exit(main() or 0)
