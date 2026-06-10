"""shadow_overlay.py - READ-ONLY helpers for the shadow preview pages.

  apply_shadow_overlay()    - overlay shadow.vauto_api_shadow (Cox API data)
                              onto the bid_detail render context (/shadow/bid/<id>).
  shadow_banner()           - sticky banner on /shadow/bid/<id> (with API time).
  render_shadow_dashboard() - the /shadow landing page (live-updating via JS).
  shadow_data()             - JSON {stats, rows} for the /shadow/data poll endpoint.

Never writes anything.
"""
import html as _html

_BROWSER_MS = 77000  # measured live browser-scrape median, for the "x faster" stat


def apply_shadow_overlay(get_db, bid_id, bid, vauto_data, accutrade_data):
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "SELECT api_series_trim, api_rbook, api_mmr, api_kbb, api_kbb_com, "
        "api_black_book, api_carfax_share_url, api_comps_count_raw, "
        "latency_ms, captured_at "
        "FROM shadow.vauto_api_shadow WHERE bid_id=%s "
        "ORDER BY captured_at DESC LIMIT 1",
        (bid_id,))
    row = cur.fetchone()
    if not row:
        return bid, vauto_data, accutrade_data
    get = row.get if hasattr(row, 'get') else (lambda k: row[k])
    bid = dict(bid)
    if get('api_series_trim'):
        bid['trim'] = get('api_series_trim')
        bid['canon_trim'] = get('api_series_trim')
    vd = dict(vauto_data) if vauto_data else {'bid_id': bid_id}
    for src, dst in (('api_rbook', 'rbook'), ('api_mmr', 'mmr'), ('api_kbb', 'kbb'),
                     ('api_kbb_com', 'kbb_com'), ('api_black_book', 'black_book')):
        if get(src) is not None:
            vd[dst] = get(src)
    if get('api_carfax_share_url'):
        vd['carfax_share_url'] = get('api_carfax_share_url')
    return bid, vd, accutrade_data


def _esc(x):
    return _html.escape(str(x)) if x is not None else ''


def shadow_banner(get_db, bid_id):
    lat = None
    try:
        cur = get_db().cursor()
        cur.execute("SELECT latency_ms FROM shadow.vauto_api_shadow WHERE bid_id=%s "
                    "ORDER BY captured_at DESC LIMIT 1", (bid_id,))
        row = cur.fetchone()
        if row:
            lat = row.get('latency_ms') if hasattr(row, 'get') else row[0]
    except Exception:
        pass
    tstr = ('collected via Cox <b>API in %.1fs</b>' % (lat / 1000.0)) if lat else 'Cox <b>API</b> data'
    return ('<div style="position:sticky;top:0;z-index:99999;background:#0b8a3a;color:#fff;'
            'padding:9px 14px;font:600 14px system-ui;text-align:center">'
            'SHADOW PREVIEW &mdash; %s, not the browser scrape &middot; '
            '<a href="/bid/%d" style="color:#fff;text-decoration:underline">live page</a> &middot; '
            '<a href="/shadow" style="color:#fff;text-decoration:underline">all shadow</a></div>'
            % (tstr, bid_id))


def _row(r):
    g = r.get if hasattr(r, 'get') else (lambda k: r[k])
    bid = g('bid_id')
    veh = ('%s %s %s' % (_esc(g('year')), _esc(g('make')), _esc(g('model')))).strip()
    if g('latency_ms') is None:
        return ('<tr style="opacity:.65"><td><b>%d</b><br>'
                '<a href="/shadow/bid/%d" style="color:#60a5fa">shadow &rarr;</a> '
                '<a href="/bid/%d" style="color:#94a3b8">live</a></td><td>%s</td>'
                '<td colspan="9" style="color:#fbbf24">&#9203; collecting via API&hellip;</td></tr>'
                % (bid, bid, bid, veh))
    api_t = _esc(g('api_series_trim')) or '<span style="color:#f87171">none</span>'
    lat = g('latency_ms') or 0
    browser = g('browser_ms')
    if browser:
        x = max(1, round(browser / max(lat, 1)))
        timecell = ('<b style="color:#34d399">%.1fs</b> API &middot; '
                    '<span style="color:#f87171">%.0fs browser</span> &middot; '
                    '<b style="color:#fbbf24">%d&times; faster</b>'
                    % (lat / 1000.0, browser / 1000.0, x))
    else:
        timecell = '<b style="color:#34d399">%.1fs</b> API' % (lat / 1000.0)
    kbb = ('$%s' % format(g('api_kbb'), ',')) if g('api_kbb') else '-'
    mmr = ('$%s' % format(g('api_mmr'), ',')) if g('api_mmr') else '-'
    cf = 'ok' if g('api_carfax_ok') else '-'
    ac = 'ok' if g('api_autocheck_ok') else '-'
    err = _esc(g('error'))[:50] if g('error') else ''
    return (
        '<tr><td><b>%d</b><br><a href="/shadow/bid/%d" style="color:#60a5fa">shadow &rarr;</a> '
        '<a href="/bid/%d" style="color:#94a3b8">live</a></td>'
        '<td>%s</td><td style="color:#a7f3d0">%s</td><td style="color:#94a3b8">%s</td>'
        '<td>%s</td><td>%s</td><td>%s</td><td>%s/%s</td><td>%s</td>'
        '<td style="color:#64748b">%s</td><td style="color:#f87171">%s</td></tr>'
        % (bid, bid, bid, veh, api_t, _esc(g('live_trim')), kbb, mmr,
           _esc(g('api_comps_count_raw')), cf, ac, timecell, _esc(g('cap')), err))


def _build_stats(cur):
    cur.execute("SELECT count(*) AS n, round(avg(latency_ms)) AS avgms, "
                "count(*) FILTER (WHERE error IS NOT NULL) AS errs FROM shadow.vauto_api_shadow")
    c = cur.fetchone()
    n = (c.get('n') if hasattr(c, 'get') else c[0]) or 0
    avg_ms = (c.get('avgms') if hasattr(c, 'get') else c[1]) or 0
    errs = (c.get('errs') if hasattr(c, 'get') else c[2]) or 0
    cur.execute(
        "SELECT count(*) AS n, min(latency_ms) AS mn, max(latency_ms) AS mx, "
        "percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS med, "
        "count(*) FILTER (WHERE latency_ms < %s) AS beat, "
        "coalesce(sum(greatest(0, %s - latency_ms)), 0) AS saved "
        "FROM shadow.vauto_api_shadow "
        "WHERE captured_at > now() - interval '24 hours' AND latency_ms IS NOT NULL",
        (_BROWSER_MS, _BROWSER_MS))
    t = cur.fetchone()
    tv = (lambda k, i: (t.get(k) if hasattr(t, 'get') else t[i]))
    t_n = tv('n', 0) or 0
    t_mn = (tv('mn', 1) or 0) / 1000.0
    t_mx = (tv('mx', 2) or 0) / 1000.0
    t_med = float(tv('med', 3) or 0) / 1000.0
    t_beat = tv('beat', 4) or 0
    t_saved_min = float(tv('saved', 5) or 0) / 60000.0

    def chip(txt, cls=''):
        return '<span class="chip ' + cls + '">' + txt + '</span>'
    sub = ('<div class="sub">' + str(n) + ' bids shadowed all-time &middot; avg API collection <b>'
           + ('%.1f' % (float(avg_ms) / 1000.0)) + 's</b> vs live browser scrape <b>77s&ndash;6min</b> &middot; '
           + str(errs) + ' errors &middot; live (updates every 1.5s)</div>')
    strip = ('<div class="strip">'
             + chip('<b>' + str(t_n) + '</b> bids in 24h')
             + chip('fastest <b>%.1fs</b>' % t_mn, 'ok')
             + chip('median <b>%.1fs</b>' % t_med)
             + chip('slowest <b>%.1fs</b>' % t_mx, ('warn' if t_mx >= 8 else ''))
             + chip('<b>' + str(t_beat) + '/' + str(t_n) + '</b> beat the ~77s browser', 'ok')
             + chip('~<b>%.0f min</b> of operator wait saved' % t_saved_min, 'save')
             + '</div>')
    return sub + strip


def _build_rows(cur):
    cur.execute(
        "SELECT b.id AS bid_id, b.year, b.make, b.model, s.api_series_trim, b.trim AS live_trim, "
        "s.api_kbb, s.api_mmr, s.api_comps_count_raw, s.api_carfax_ok, s.api_autocheck_ok, "
        "s.latency_ms, s.error, to_char(s.captured_at,'HH24:MI:SS') AS cap, "
        "(SELECT max(duration_ms) FROM worker_jobs wj WHERE wj.bid_id=b.id AND wj.job_type='vauto' AND wj.status='ok') AS browser_ms "
        "FROM bids b LEFT JOIN shadow.vauto_api_shadow s ON s.bid_id = b.id "
        "WHERE b.vin IS NOT NULL AND length(b.vin)=17 AND b.created_at > now() - interval '7 days' "
        "ORDER BY b.created_at DESC LIMIT 60")
    return '\n'.join(_row(r) for r in cur.fetchall())


def shadow_data(get_db):
    cur = get_db().cursor()
    return {'stats': _build_stats(cur), 'rows': _build_rows(cur)}


_JS = ('<script>\n'
       'async function _shadowTick(){try{var r=await fetch("/shadow/data",{cache:"no-store"});'
       'if(!r.ok)return;var d=await r.json();'
       'document.getElementById("stats").innerHTML=d.stats;'
       'document.getElementById("rows").innerHTML=d.rows;}catch(e){}}\n'
       'setInterval(_shadowTick,1500);\n'
       '</script>')

_HEAD = (
    '<!DOCTYPE html><html><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1">'
    '<title>EW Shadow - API path</title>'
    '<style>*{box-sizing:border-box}body{margin:0;font:14px system-ui,sans-serif;'
    'background:#0b0f19;color:#e2e8f0}header{background:#0b8a3a;color:#fff;padding:14px 18px}'
    'h1{margin:0;font-size:18px}.sub{font-size:13px;opacity:.92;margin-top:4px}'
    '.strip{display:flex;flex-wrap:wrap;gap:8px;margin-top:11px}'
    '.chip{background:rgba(255,255,255,.15);border-radius:20px;padding:4px 12px;font-size:13px}'
    '.chip.ok{background:rgba(16,185,129,.30)}.chip.warn{background:rgba(251,191,36,.30)}'
    '.chip.save{background:rgba(96,165,250,.34)}'
    '.wrap{padding:14px;overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:13px}'
    'th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #1e293b;vertical-align:top;white-space:nowrap}'
    'th{color:#64748b;text-transform:uppercase;font-size:11px;letter-spacing:.5px}'
    'tr:hover{background:#111827}a{text-decoration:none}</style></head><body>')

_THEAD = ('<th>Bid</th><th>Vehicle</th><th>API trim</th><th>Live trim</th><th>KBB</th>'
          '<th>MMR</th><th>Comps</th><th>CF/AC</th><th>Collection: API vs browser scrape</th>'
          '<th>Captured</th><th>Err</th>')


def render_shadow_dashboard(get_db):
    cur = get_db().cursor()
    stats = _build_stats(cur)
    rows = _build_rows(cur)
    return (_HEAD
            + '<header><h1>EW Shadow &mdash; Cox API enrichment path</h1>'
            + '<div id="stats">' + stats + '</div></header>'
            + '<div class="wrap"><table><thead><tr>' + _THEAD + '</tr></thead>'
            + '<tbody id="rows">' + rows + '</tbody></table></div>'
            + _JS + '</body></html>')
