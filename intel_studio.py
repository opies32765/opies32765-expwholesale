# -*- coding: utf-8 -*-
"""
INTEL_REPORT_STUDIO_2026_06_19 — natural-language report engine over EW + DIA + LSL.

Internal EW staff type any request; the 9B picks the right source, writes a
READ-ONLY SQL query, we validate + run it (read-only, timeout, auto-LIMIT), and
the 9B writes a short narrative. Three separate engines (cannot cross-JOIN in
SQL): EW PostgreSQL :5433, DIA PostgreSQL :5432 (STALE ~2026-04), LSL SQLite (ro).

Self-contained except the 9B shim + Flask app helpers, imported lazily from app.
"""
import json
import re
import time

# ── source registry (read-only access) ──────────────────────────────────────
SOURCES = {
    'EW':  {'engine': 'pg', 'dsn': "host=localhost port=5433 dbname=expwholesale user=expuser password=ExpWholesale2026!",
            'label': 'EW wholesale bid/enrichment platform (live)'},
    'LSL': {'engine': 'sqlite', 'path': '/opt/livesaleslog/crm.db',
            'label': 'EW CRM/DMS ledger — real booked deals (live)'},
}

# ── the authoritative schema card the 9B writes SQL against ──────────────────
SCHEMA_CARD = r"""
TWO separate engines — pick exactly ONE per query; NO cross-engine JOIN.
Cross-source spine (stitch in app, not SQL): VIN (EW.bids.vin = LSL.deals.vin_no),
bid<->deal EW.bids.lsl_deal_id = LSL.deals.id.

=== EW  (PostgreSQL :5433, db expwholesale) — LIVE wholesale bids/enrichment ===
bids(id PK, vin, year/make/model/trim, canon_year/make/model/trim, mileage, status, source, creation_source, phone,
  asking_price, bid_amount, ai_price, ai_assessment, ai_assessed_at, created_at, bid_sent_at, all_enriched_at,
  partner_dealer_id, lsl_deal_id, lsl_supplier_name, lsl_purchase_cost, lsl_sale_price)
vauto_lookups(bid_id, vin, rbook, black_book, mmr, kbb, jd_power, looked_up_at)
accutrade_lookups(bid_id, vin, guaranteed_offer, trade_in, trade_market, retail, market_avg, not_available, looked_up_at)
ipacket_lookups(bid_id, vin, total_msrp, base_price, not_available, looked_up_at)
ai_assessment_log(id, bid_id, baseline_price, llm_adjustment_pct, llm_reasoning, confidence_low, confidence_high, final_price, created_at)
ai_accuracy(bid_id, vin, year/make/model, ai_recommendation, ai_confidence_low/high, actual_purchase_cost, lsl_deal_id, delta, delta_pct, abs_delta_pct, in_confidence_range)  -- AI price vs actual paid, PRE-JOINED
worker_jobs(id, bid_id, worker_id, job_type, claimed_at, completed_at, status, duration_ms, error)
workers(worker_id, role, last_heartbeat, chrome_alive, paused, consecutive_failures, restart_count)
sms_intake_log(id, created_at, from_phone, body, parsed_vin, parsed_miles, outcome, bid_id)
dealers(id, name, city, state, active, platform, dia_dealer_id)  partner_users(id, dealer_id, email, full_name, last_login_at)
dealer_inventory(id, dealer_id, vin, year/make/model/trim, mileage, price, msrp, status, first_seen_at, sold_at)
dealer_inventory_comps(dealer_inventory_id, snapshot_date, mmr_comp_value, rbook_p50, price_trend_30d)  -- partner master list = dealer_inventory LEFT JOIN this on latest snapshot_date
dealer_opportunities(id, snapshot_date, vin, dealer_id, year/make/model, asking_price, mmr_wholesale_avg, dollars_under_mmr, pct_under_mmr, retail_headroom, score, opportunity_type)
lsl_training(deal_id, vin, year/make_name/model_name/trim_name, odometer, sales_person, supplier_name, sold_at, days_on_lot, purchase_cost, sale_price, gross_dollars)  -- LSL deals mirrored in PG
voice_ymm_master(year, make, model, miles_band, lsl_avg_purchase_cost, lsl_avg_sale_price, lsl_avg_front, mmr_wholesale_avg, mmr_retail_avg, rbook_median_retail, partner_active_count, sonnet_narrative, refreshed_at)  -- one-stop "what is this YMM worth"
partner_activity_summary(dealer_id, dealer_name, pushes_30d/90d, offers_30d/90d, purchases_30d/90d/365d, total_gross_365d, silent_days)

=== LSL  (SQLite, RO) — LIVE real booked deals/ledger ===
deals(id PK, vin_no, make_name, vehicle_info, sale_price, purchase_cost, front_value=PROFIT, recon_cost, sales_person, customer_name, supplier_name, buyer_name, source_name, sale_type, days_on_lot, sold_at)  -- status='Active' for ALL rows: use sold_at/front_value; sold_at is ISO TEXT, substr(sold_at,1,7)=month
inventory(id, vin_no, status, vehicle_make_name, group_model_name/trim/year, asking_price, est_wholesale_price, purchase_cost, recon_cost, days_on_lot, sold, sold_at, deal_id)
payments(id, deal_id, vin_no, vendor_name, amount, amount_paid, payment_status, is_commission, is_paid, paid_at)
customers(id, full_name, company_name, email, mobile)  suppliers(id, name, city, state, approved, trusted)
dealer_profile(norm_name PK, display_name, role, total_deals, total_deals_12mo/90d, combined_revenue, combined_profit, avg_profit_per_deal, buyer_deals, source_deals, last_activity_at, days_since_last_activity)  -- counterparty-360 (richest)
"""

ROUTING = """
ROUTING (request -> source):
- our inbound bids / what WE priced / enrichment legs (rbook/mmr/accutrade/msrp) -> EW (bids + *_lookups)
- AI pricing accuracy vs actual paid -> EW.ai_accuracy
- worker fleet throughput/latency/health -> EW.worker_jobs + workers
- partner dealers' inventory + suggested sell price -> EW.dealer_inventory + dealer_inventory_comps
- "what should we go buy" sourcing leads -> EW.dealer_opportunities
- partner engagement / who went quiet -> EW.partner_activity_summary
- "what is this YMM worth" one-stop -> EW.voice_ymm_master
- OUR real booked deals / revenue / gross / salesperson volume -> LSL.deals (or EW.lsl_training)
- our inventory aging/recon/current lot -> LSL.inventory ; AP/payments -> LSL.payments
- who we buy from / sell to (counterparty 360) -> LSL.dealer_profile
"""

# ── 9B helpers (lazy import of the app's shim) ───────────────────────────────
def _ask_9b(prompt, max_tokens=1200, temperature=0.2):
    from app import gemini_call  # lazy: avoid circular import
    return gemini_call(prompt, model='gemini-2.5-flash', max_tokens=max_tokens,
                       temperature=temperature, disable_thinking=False)


def _strip_fence(s):
    s = (s or '').strip()
    s = re.sub(r'^```(?:json|sql)?', '', s).strip()
    s = re.sub(r'```$', '', s).strip()
    return s


# ── read-only SQL guard ──────────────────────────────────────────────────────
_FORBIDDEN = re.compile(r'\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|'
                        r'copy|vacuum|reindex|comment|attach|detach|pragma|call|do|merge|'
                        r'replace|upsert)\b', re.I)
_SECRET = re.compile(r'\b(vauto_session|_bak|\.bak|canary_|password|password_hash|auth_token|secret)\b', re.I)


def validate_sql(sql):
    """Return (ok, cleaned_sql_or_error). SELECT/WITH-only, single statement,
    no writes, no secret tables, auto-LIMIT."""
    s = _strip_fence(sql).strip().rstrip(';').strip()
    # The 9B sometimes schema-qualifies tables with the SOURCE label (EW./DIA./LSL.);
    # those aren't real schemas — strip them so `FROM EW.bids` -> `FROM bids`.
    s = re.sub(r'(?i)\b(?:EW|DIA|LSL)\.(?=\w)', '', s)
    if not s:
        return False, 'empty query'
    low = s.lower()
    if not (low.startswith('select') or low.startswith('with')):
        return False, 'only SELECT/WITH queries are allowed'
    if ';' in s:
        return False, 'only a single statement is allowed'
    if _FORBIDDEN.search(s):
        return False, 'write/DDL keyword detected — read-only only'
    if _SECRET.search(s):
        return False, 'query references a blocked table/column'
    if not re.search(r'\blimit\s+\d+', low):
        s = s + ' LIMIT 500'
    return True, s


# ── per-source read-only execution ───────────────────────────────────────────
def run_sql(source, sql):
    """Execute a validated read-only query on `source`. Returns (columns, rows)."""
    src = SOURCES[source]
    if src['engine'] == 'pg':
        import psycopg2
        conn = psycopg2.connect(src['dsn'], connect_timeout=8)
        try:
            conn.set_session(readonly=True, autocommit=False)
            cur = conn.cursor()
            cur.execute("SET statement_timeout = '20000'")
            cur.execute(sql)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchmany(500)
        finally:
            conn.rollback()
            conn.close()
        return cols, [list(r) for r in rows]
    else:  # sqlite (LSL)
        import sqlite3
        conn = sqlite3.connect('file:%s?mode=ro' % src['path'], uri=True, timeout=8)
        try:
            cur = conn.cursor()
            cur.execute(sql)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchmany(500)
        finally:
            conn.close()
        return cols, [list(r) for r in rows]


# ── self-heal helpers (real schema on error) ────────────────────────────────
def _tables_in_sql(sql):
    return list({t.lower() for t in re.findall(r'(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)', sql or '', re.I)})


def _introspect_columns(source, tables):
    """Return {table: [real columns]} from the live schema for the given tables."""
    out = {}
    if not tables:
        return out
    src = SOURCES[source]
    try:
        if src['engine'] == 'pg':
            import psycopg2
            conn = psycopg2.connect(src['dsn'], connect_timeout=8)
            try:
                conn.set_session(readonly=True)
                cur = conn.cursor()
                cur.execute("SELECT table_name, column_name FROM information_schema.columns "
                            "WHERE table_schema='public' AND table_name = ANY(%s) "
                            "ORDER BY table_name, ordinal_position", (list(tables),))
                for t, c in cur.fetchall():
                    out.setdefault(t, []).append(c)
            finally:
                conn.close()
        else:
            import sqlite3
            conn = sqlite3.connect('file:%s?mode=ro' % src['path'], uri=True, timeout=8)
            try:
                cur = conn.cursor()
                for t in tables:
                    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', t):
                        continue
                    try:
                        cur.execute("PRAGMA table_info(%s)" % t)
                        cols = [r[1] for r in cur.fetchall()]
                        if cols:
                            out[t] = cols
                    except Exception:
                        pass
            finally:
                conn.close()
    except Exception:
        pass
    return out


def _fix_sql(request, source, bad_sql, error, real_cols):
    """Ask the 9B to rewrite failed SQL using ONLY columns that actually exist."""
    cols_txt = "\n".join("%s(%s)" % (t, ", ".join(cs)) for t, cs in real_cols.items()) or "(introspection unavailable)"
    _dialect = ("SQLite — use substr()/date()/strftime()/julianday(); NEVER date_trunc/INTERVAL/NOW()/CURRENT_DATE/"
                "FILTER(...)/::cast. 'this month' = substr(col,1,7)=strftime('%Y-%m','now'); use CASE WHEN not FILTER."
                if source == 'LSL' else
                "PostgreSQL — date_trunc/NOW()/CURRENT_DATE/INTERVAL/FILTER/::numeric casts are fine.")
    prompt = (
        "Your previous read-only SQL for source %s FAILED. Rewrite it as ONE valid read-only SELECT using ONLY "
        "columns/tables that actually exist (listed below) AND the correct SQL dialect for this source. "
        "DIALECT: %s\n"
        "Keep the user's intent; if a needed column truly does not exist (e.g. no date column), drop that filter "
        "rather than invent a column. Output ONLY the corrected SQL — no prose, no code fences.\n\n" % (source, _dialect) +
        "REQUEST: " + request + "\n\nFAILED SQL:\n" + bad_sql + "\n\nDB ERROR:\n" + error + "\n\n"
        "ACTUAL COLUMNS of the referenced tables:\n" + cols_txt + "\n\nCorrected SQL:"
    )
    return _strip_fence(_ask_9b(prompt, max_tokens=1200, temperature=0.1) or "")


# ── the engine ───────────────────────────────────────────────────────────────
def _write_sql(request, periods=None):
    """9B picks the source + writes one read-only SQL query. Returns dict.
    If `periods` (list of {label, from, to}) is given, force a per-period UNION
    over those EXACT literal date ranges (calendar-picked comparison)."""
    _pb = ""
    if periods:
        _pl = "; ".join("%s = %s to %s" % (p.get('label') or 'Period %d' % (i + 1), p.get('from'), p.get('to'))
                        for i, p in enumerate(periods) if p.get('from') and p.get('to'))
        if _pl:
            _pb = ("\n\nCOMPARE THESE EXACT DATE RANGES — output ONE ROW PER PERIOD via UNION ALL, inclusive of both "
                   "endpoints, using these LITERAL dates (NOT relative/now dates). For LSL filter "
                   "substr(<date_col>,1,10) BETWEEN 'from' AND 'to'; for EW use <date_col>::date BETWEEN 'from' AND 'to'. "
                   "Label each output row with its period. Date ranges: " + _pl + ".")
    prompt = (
        "You are a senior data analyst writing ONE read-only SQL query to answer an "
        "internal Experience Wholesale staff report request.\n\n"
        "RULES:\n"
        "- Pick EXACTLY ONE source: EW (PostgreSQL) or LSL (SQLite). NEVER cross-join sources.\n"
        "- Output ONLY a single SELECT (or WITH...SELECT). No writes, no semicolons, no comments.\n"
        "- Use BARE table names (bids, ai_accuracy, deals, dealers...). Do NOT prefix with the source "
        "label — write `FROM bids`, NOT `FROM EW.bids`; `FROM deals`, NOT `FROM LSL.deals`.\n"
        "- Prefer the PRE-AGGREGATED tables (EW.ai_accuracy, EW.lsl_training, EW.voice_ymm_master, "
        "EW.partner_activity_summary, LSL.dealer_profile) over GROUP BY on raw fact tables.\n"
        "- Always include a sensible LIMIT (<=200). Add ORDER BY for ranked reports.\n"
        "- COMPARISONS ('compare X vs Y', 'this period vs last'): return ONE ROW PER PERIOD (UNION ALL) with metrics "
        "side by side. For 'so far'/'to-date', clamp BOTH periods to the SAME day-of-period. EXACT SQLite (LSL) recipe "
        "for 'this month so far vs last month same days' — copy this shape:\n"
        "  WITH d AS (SELECT CAST(strftime('%d','now') AS INT) AS dn)\n"
        "  SELECT 'This month so far' AS period, COUNT(*) AS units, SUM(front_value) AS profit\n"
        "  FROM deals, d WHERE substr(sold_at,1,7)=strftime('%Y-%m','now') AND CAST(substr(sold_at,9,2) AS INT)<=d.dn\n"
        "  UNION ALL\n"
        "  SELECT 'Last month same days', COUNT(*), SUM(front_value)\n"
        "  FROM deals, d WHERE substr(sold_at,1,7)=strftime('%Y-%m','now','-1 month') AND CAST(substr(sold_at,9,2) AS INT)<=d.dn\n"
        "Adapt the same shape for year-so-far vs last-year-so-far (strftime('%Y'...) + day-of-year), quarters, and "
        "last-30 vs prior-30 (sold_at BETWEEN date('now','-30 days') AND date('now') vs BETWEEN date('now','-60 days') AND date('now','-30 days')). For Postgres (EW) use date_trunc + INTERVAL equivalents.\n"
        "- LSL.deals.status is 'Active' for ALL rows; use sold_at/front_value, substr(sold_at,1,7) for month.\n"
        "DIALECT IS CRITICAL — match the chosen source's engine EXACTLY:\n"
        "  * LSL = SQLite: use substr(), date(), strftime(), julianday(). NEVER date_trunc / INTERVAL / NOW() / "
        "CURRENT_DATE / FILTER(...) / ::cast. 'this month': WHERE substr(sold_at,1,7)=strftime('%Y-%m','now'). "
        "'last 6 months': WHERE sold_at >= date('now','-6 months'). Use COUNT(*) with CASE WHEN, not FILTER.\n"
        "  * EW = PostgreSQL: date_trunc(), NOW(), CURRENT_DATE, INTERVAL, FILTER (WHERE ...), ::numeric casts are fine.\n\n"
        "OUTPUT EXACTLY this format and NOTHING else (no JSON, no code fences):\n"
        "SOURCE: EW or LSL\n"
        "TITLE: a short report title\n"
        "SQL:\n"
        "<one read-only SELECT; multi-line is fine>\n\n"
        + ROUTING + "\nSCHEMA:\n" + SCHEMA_CARD + _pb +
        "\n\nREQUEST: " + request + "\n\nNow produce the SOURCE / TITLE / SQL block:"
    )
    raw = _ask_9b(prompt, max_tokens=1500, temperature=0.1)
    if not raw:
        return None
    sm = re.search(r'SOURCE:\s*(EW|LSL)', raw, re.I)
    qm = re.search(r'SQL:\s*\n?(.+)', raw, re.S)
    if not sm or not qm:
        return None
    tm = re.search(r'TITLE:\s*(.+)', raw)
    return {'source': sm.group(1).upper(),
            'title': (tm.group(1).strip() if tm else None),
            'sql': _strip_fence(qm.group(1)).strip()}


def _narrate(request, title, source, cols, rows):
    """9B writes a short executive summary of the result set."""
    sample = [dict(zip(cols, r)) for r in rows[:40]]
    prompt = (
        "Write a concise, professional 2-4 sentence executive summary of this report "
        "for an internal car-wholesale operator. State the key numbers/findings plainly. "
        "No preamble, no markdown headers.\n"
        "REPORT: " + title + "\nREQUEST: " + request + "\n"
        "ROWS RETURNED: %d (sample below)\n" % len(rows) +
        json.dumps(sample, default=str)[:6000]
    )
    return _ask_9b(prompt, max_tokens=400, temperature=0.4) or ''


def run_report(request, periods=None):
    """End-to-end: request -> 9B SQL -> validate -> run -> 9B narrative."""
    t0 = time.time()
    plan = _write_sql(request, periods)
    if not plan or not plan.get('sql') or plan.get('source') not in SOURCES:
        return {'error': 'Could not turn that into a query. Try rephrasing.',
                'detail': str(plan)[:300]}
    source = plan['source']
    ok, sql = validate_sql(plan['sql'])
    if not ok:
        return {'error': 'Generated query failed safety check: %s' % sql,
                'sql': plan.get('sql'), 'source': source}
    # SELF_HEAL: on a schema/syntax error, feed the 9B the real error + actual
    # columns of the referenced tables and let it rewrite (up to 2 retries).
    cols = rows = None
    last_err = None
    _heal = ('does not exist', 'no such column', 'no such table', 'syntax error',
             'undefinedcolumn', 'undefinedtable', 'undefinedfunction', 'operator does not exist')
    for _attempt in range(3):
        try:
            cols, rows = run_sql(source, sql)
            last_err = None
            break
        except Exception as e:
            last_err = str(e)
            if _attempt < 2 and any(k in last_err.lower() for k in _heal):
                real = _introspect_columns(source, _tables_in_sql(sql))
                fixed = _fix_sql(request, source, sql, last_err, real)
                ok2, fixed2 = validate_sql(fixed)
                if ok2 and fixed2 and fixed2.strip() != sql.strip():
                    sql = fixed2
                    continue
            break
    if last_err is not None:
        return {'error': 'Query failed: %s' % last_err[:200], 'sql': sql, 'source': source}
    narrative = _narrate(request, plan.get('title') or request, source, cols, rows)
    return {
        'title': plan.get('title') or request,
        'request': request,
        'source': source,
        'source_label': SOURCES[source]['label'],
        'stale_warning': '',
        'sql': sql,
        'columns': cols,
        'rows': rows,
        'row_count': len(rows),
        'narrative': narrative,
        'elapsed_s': round(time.time() - t0, 1),
    }


# ── delivery formatters (SMS text + printable HTML for PDF) ──────────────────
def _fmt_val(v):
    try:
        if isinstance(v, bool):
            return str(v)
        if isinstance(v, float):
            return ('{:,.0f}'.format(v)) if abs(v) >= 100 else ('{:,.2f}'.format(v))
        if isinstance(v, int):
            return '{:,}'.format(v)
    except Exception:
        pass
    return '' if v is None else str(v)


_MONEY_KW = ('profit', 'pvr', 'revenue', 'cost', 'price', 'gross', 'front', 'mmr', 'msrp', 'value', 'spend')
_UPPER_COL = {'pvr', 'mmr', 'msrp', 'vin', 'ai', 'id', 'ymm', 'roi'}


def _pretty_col(nm):
    s = str(nm).replace('_', ' ').strip()
    return ' '.join(w.upper() if w.lower() in _UPPER_COL else w.capitalize() for w in s.split())


def report_sms_text(rep):
    """Clean sectioned SMS body: title + '## Period' headers + metric bullets."""
    title = (rep.get('title') or 'Report').strip()
    cols = rep.get('columns') or []
    rows = rep.get('rows') or []
    out = [title, '']
    for r in rows[:8]:
        if not cols:
            break
        out.append('## ' + _fmt_val(r[0]))
        for i in range(1, min(len(cols), len(r))):
            nm = str(cols[i])
            money = any(k in nm.lower() for k in _MONEY_KW)
            out.append('• %s: %s%s' % (_pretty_col(nm), '$' if money else '', _fmt_val(r[i])))
        out.append('')
    narr = (rep.get('narrative') or '').strip()
    if narr:
        out.append(narr)
    return '\n'.join(out).strip()[:1600]


def report_html(rep):
    """Clean print/PDF-friendly HTML for a report."""
    def e(s):
        s = '' if s is None else str(s)
        return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    title = rep.get('title') or 'Report'
    cols = rep.get('columns') or []
    rows = rep.get('rows') or []
    narr = rep.get('narrative') or ''
    th = ''.join('<th>%s</th>' % e(c) for c in cols)
    trs = ''.join('<tr>%s</tr>' % ''.join('<td>%s</td>' % e(_fmt_val(v)) for v in r) for r in rows)
    return ("<!doctype html><html><head><meta charset='utf-8'><style>"
            "body{font-family:Arial,Helvetica,sans-serif;color:#111;padding:30px;}"
            "h1{font-size:20px;margin:0 0 3px;} .sub{color:#666;font-size:12px;margin-bottom:18px;}"
            ".narr{background:#f4f7fb;border-left:3px solid #2563eb;padding:12px 14px;font-size:13px;"
            "margin:14px 0;line-height:1.55;}"
            "table{border-collapse:collapse;width:100%%;font-size:13px;} "
            "th,td{border:1px solid #dde3ea;padding:8px 12px;text-align:left;} "
            "th{background:#eef3f9;font-size:11px;text-transform:uppercase;letter-spacing:.4px;color:#556;}"
            ".ft{margin-top:22px;color:#999;font-size:10px;}"
            "</style></head><body>"
            "<h1>%s</h1><div class='sub'>Experience Wholesale &middot; Report Studio &middot; %s</div>"
            "<div class='narr'>%s</div>"
            "<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>"
            "<div class='ft'>Generated by EW Report Studio</div>"
            "</body></html>") % (e(title), e(rep.get('source_label') or ''), e(narr), th, trs)


# ── MTD 3-way preset (deterministic; this MTD vs prior-month MTD vs last-year MTD) ──
_MONTHS = ['', 'January', 'February', 'March', 'April', 'May', 'June',
           'July', 'August', 'September', 'October', 'November', 'December']


def _nice_range(lo, hi):
    try:
        ly, lm, ld = lo.split('-'); hy, hm, hd = hi.split('-')
        if lm == hm and ly == hy:
            return '%s %d-%d, %s' % (_MONTHS[int(lm)], int(ld), int(hd), ly)
        return '%s %d, %s - %s %d, %s' % (_MONTHS[int(lm)], int(ld), ly, _MONTHS[int(hm)], int(hd), hy)
    except Exception:
        return '%s to %s' % (lo, hi)


def _mtd_narr(m):
    t = m.get('This Month')
    if not t:
        return ''
    def d(a, b):
        return '' if not b else ' (%+.0f%%)' % ((a - b) / b * 100.0)
    parts = ['Month-to-date: %d units, $%s profit, $%s PVR.' % (t[0], '{:,}'.format(t[1]), '{:,}'.format(t[2]))]
    l = m.get('Last Month'); y = m.get('Last Year')
    if l:
        parts.append('Prior-month MTD $%s%s.' % ('{:,}'.format(l[1]), d(t[1], l[1])))
    if y:
        parts.append('Same period last year $%s%s.' % ('{:,}'.format(y[1]), d(t[1], y[1])))
    return ' '.join(parts)


def mtd_report():
    """Month-to-date sales: this month vs prior-month MTD vs same period last year.
    Deterministic (no 9B) so the numbers + column names are exact. LSL ledger."""
    import sqlite3
    conn = sqlite3.connect('file:%s?mode=ro' % SOURCES['LSL']['path'], uri=True)
    cur = conn.cursor()
    sql = (
        "WITH r(ord,label,lo,hi) AS (VALUES "
        "(1,'This Month', date('now','start of month'), date('now')), "
        "(2,'Last Month', date('now','start of month','-1 month'), date('now','-1 month')), "
        "(3,'Last Year',  date('now','start of month','-1 year'),  date('now','-1 year'))) "
        "SELECT r.label, r.lo, r.hi, "
        "(SELECT COUNT(*) FROM deals d WHERE substr(d.sold_at,1,10) BETWEEN r.lo AND r.hi), "
        "(SELECT COALESCE(SUM(front_value),0) FROM deals d WHERE substr(d.sold_at,1,10) BETWEEN r.lo AND r.hi) "
        "FROM r ORDER BY r.ord")
    cur.execute(sql)
    raw = cur.fetchall()
    conn.close()
    rows = []
    metrics = {}
    for label, lo, hi, units, profit in raw:
        units = int(units or 0); profit = round(float(profit or 0)); pvr = round(profit / units) if units else 0
        rows.append(['%s (%s)' % (label, _nice_range(lo, hi)), units, profit, pvr])
        metrics[label] = (units, profit, pvr)
    return {'title': 'EW Sales Comparison (MTD)',
            'source': 'LSL', 'source_label': SOURCES['LSL']['label'],
            'columns': ['Period', 'Units Sold', 'Total Profit', 'PVR'],
            'rows': rows, 'row_count': len(rows),
            'narrative': _mtd_narr(metrics), 'sql': sql, 'elapsed_s': 0, 'stale_warning': ''}
