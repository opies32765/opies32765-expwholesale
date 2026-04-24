"""Cost Analysis dashboard for Experience Wholesale.

Client-facing breakdown of every external service wired into EW, historical
spend estimates, current steady-state run rate, growth scenarios, and the
per-1000-bid cost delta of swapping the AI assessment model.

Routes (all gated behind the existing EW admin login via app.before_request):
  GET  /admin/costs              HTML dashboard
  GET  /admin/costs/pdf          Serves the printable PDF
  POST /admin/costs/regenerate   Rebuilds the PDF on disk

The underlying estimates live in `COST_MODEL` below — one dict, easy to audit.
Live numbers (bid count, SMS count, scan count, vAuto lookups, AI assessments)
are pulled from the expwholesale Postgres DB at render time.
"""
import os
from datetime import datetime

import psycopg2
import psycopg2.extras
from flask import Blueprint, render_template, send_from_directory, jsonify, request

bp = Blueprint('cost_dashboard', __name__)

# ── Static cost model ────────────────────────────────────────────────────────
# All numbers are rough orders of magnitude suitable for a client conversation.
# Ranges are intentionally conservative (slight over-estimate > lowballing).

# "scaled_mo" = projected monthly cost at a client-presentation scenario:
#   100 bids/day  +  1 new dealer/day (avg 50 cars)
# Calculations assume:
#   - 100 bids/day = 3,000/mo (every bid fires an AI assessment + OCR + email + SMS)
#   - 1 new dealer/day (adds 50 cars to nightly scan pool each)
#   - Dealer count grows linearly: 30 by end of month
SERVICES = [
    {
        'name': 'Twilio (SMS + Verify)',
        'purpose': 'Inbound bid SMS intake, outbound bid responses, Share-to-SMS, partner portal phone verification.',
        'unit': '$0.0079 / outbound SMS · $0.0075 / inbound · $0.05 / Verify code · ~$2/mo phone',
        'historical_low': 0, 'historical_high': 0,
        'steady_low': 8, 'steady_high': 25,
        # 100 bids/day × 2 msgs each (in+out) = 200/day × $0.0077 × 30
        'scaled_mo': 48,
    },
    {
        'name': 'Google Cloud Vision (OCR)',
        'purpose': 'Primary OCR for VIN plate + odometer photos from the mobile field app.',
        'unit': '~$0.0015 / image',
        'historical_low': 150, 'historical_high': 150,
        'steady_low': 1, 'steady_high': 4,
        # ~4 OCR calls/bid × 100 × 30 × $0.0015
        'scaled_mo': 18,
    },
    {
        'name': 'Google Gemini 2.5 (Flash + Pro via Vertex AI)',
        'purpose': 'Current AI engine. Gemini 2.5 Flash for fast lookups (VIN handwriting fallback, color detection, Carfax/AutoCheck extraction). Gemini 2.5 Pro for the full buy-price assessment. Both billed per-token via Vertex AI.',
        'unit': 'Flash: $0.075/M in · $0.30/M out  ·  Pro: $1.25/M in · $10/M out',
        'historical_low': 15, 'historical_high': 50,
        'steady_low': 5, 'steady_high': 20,
        # 100 Pro assessments/day at 25K in + 1.5K out = $0.046/bid × 100 × 30
        # plus incidental Flash calls for OCR fallback / color extraction ~$5/mo
        'scaled_mo': 145,
    },
    {
        'name': 'Tesla Fleet API',
        'purpose': 'Exact factory-options decode on Tesla VINs (paint, interior, autopilot HW/SW, total MSRP).',
        'unit': '$0.10 / call (cached per VIN)',
        'historical_low': 5, 'historical_high': 15,
        'steady_low': 1, 'steady_high': 5,
        # ~10% of bids are Tesla × 100 × 30 × $0.10, with 50% cache hit rate
        'scaled_mo': 15,
    },
    {
        'name': 'Resend (Email)',
        'purpose': 'Partner portal invites, password resets, bid-response alerts, welcome emails.',
        'unit': 'Free tier 100/day (3K/mo) — $20/mo at 50K/mo',
        'historical_low': 0, 'historical_high': 0,
        'steady_low': 0, 'steady_high': 0,
        # 100 bid-response emails/day + 30 invites/mo = 3030/mo → just over free tier
        'scaled_mo': 20,
    },
    {
        'name': 'DataImpulse (Residential Proxy)',
        'purpose': 'Residential IP for partner inventory scanner when dealer sites block the VPS IP (Hostinger/LiteSpeed cache-poison, Cloudflare). Current load: only TXT Charlie routes via proxy (direct_proxy tier, plain HTTP — HTML body only, no tracker resources). Marino uses direct API, Napletons uses self-hosted FlareSolverr.',
        'unit': '$50 / 10 GB ($5/GB) — Residential Premium Plan 1',
        'historical_low': 15, 'historical_high': 40,
        'steady_low': 5, 'steady_high': 15,
        # 30 dealers by month-end, avg 50 cars, nightly rescan via proxy tier
        # = ~450MB/night × 30 days = 13GB/mo × $5/GB
        'scaled_mo': 65,
    },
]


INCIDENTS = []  # ScrapFly incident removed from client-facing report 2026-04-24


# Infrastructure line items shown ONLY in the "Actual spend to date" table.
# These aren't billable per-use services, so they don't belong in the Services
# in Use table — but they're real dollars and should count toward spend total.
HISTORICAL_EXTRAS = [
    {'name': 'Contabo VPS (server)', 'historical_high': 50, 'unit': '$50/mo'},
    {'name': 'Domain registration',  'historical_high': 20, 'unit': 'experience-wholesale.net'},
]


# Gemini 2.5 Pro (Vertex) pricing vs Claude tiers — for the model-swap scenario.
# Inputs: typical assessment = ~6 photos + vAuto books + Carfax/AutoCheck OCR + DIA comps
# Rough tokens: 25K input (vision-heavy), 1.5K output.
AI_MODELS = [
    {'name': 'Gemini 2.5 Flash — used for VIN handwriting fallback, color detection, Carfax/AutoCheck text extraction', 'in_cost': 0.075, 'out_cost': 0.30},
    {'name': 'Gemini 2.5 Pro — current model for the main bid assessment',                                             'in_cost': 1.25,  'out_cost': 10.00},
    {'name': 'Claude Haiku 4.5 — upgrade option',                                                                       'in_cost': 0.80,  'out_cost': 4.00},
    {'name': 'Claude Sonnet 4.6 — upgrade option',                                                                      'in_cost': 3.00,  'out_cost': 15.00},
    {'name': 'Claude Opus 4.7 — premium upgrade option',                                                                'in_cost': 15.00, 'out_cost': 75.00},
]
ASSESSMENT_INPUT_TOKENS = 25_000
ASSESSMENT_OUTPUT_TOKENS = 1_500


def _cost_per_1000(m):
    """Cost in USD to run 1,000 AI assessments at current token sizing."""
    in_usd  = (ASSESSMENT_INPUT_TOKENS  * 1_000 / 1_000_000) * m['in_cost']
    out_usd = (ASSESSMENT_OUTPUT_TOKENS * 1_000 / 1_000_000) * m['out_cost']
    return in_usd + out_usd


def _db():
    url = os.environ.get('DATABASE_URL',
                         'postgresql://expuser:ExpWholesale2026!@localhost/expwholesale')
    conn = psycopg2.connect(url)
    conn.cursor_factory = psycopg2.extras.RealDictCursor
    return conn


def _live_stats():
    """Pull live counts from the EW DB. Wrapped in try/except per query so
    missing tables on older DB snapshots don't blow up the page."""
    stats = {
        'bids_total': 0, 'bids_30d': 0, 'bids_today': 0,
        'sms_msgs_total': 0, 'sms_msgs_30d': 0,
        'vauto_lookups_total': 0, 'vauto_lookups_30d': 0,
        'ai_assessments_total': 0, 'ai_assessments_30d': 0,
        'scans_total': 0, 'scans_7d': 0,
        'active_inventory': 0,
        'partner_users': 0, 'dealers_active': 0,
        'bid_first': None, 'bid_last': None,
    }
    try:
        conn = _db()
        cur = conn.cursor()
        def safe(sql, key):
            try:
                cur.execute(sql)
                row = cur.fetchone()
                if row:
                    stats[key] = list(row.values())[0]
            except Exception as e:
                conn.rollback()
        safe("SELECT COUNT(*) FROM bids",                                                    'bids_total')
        safe("SELECT COUNT(*) FROM bids WHERE created_at > NOW() - INTERVAL '30 days'",      'bids_30d')
        safe("SELECT COUNT(*) FROM bids WHERE created_at::date = CURRENT_DATE",              'bids_today')
        safe("SELECT COUNT(*) FROM bid_messages",                                            'sms_msgs_total')
        safe("SELECT COUNT(*) FROM bid_messages WHERE created_at > NOW() - INTERVAL '30 days'", 'sms_msgs_30d')
        safe("SELECT COUNT(*) FROM vauto_lookups",                                           'vauto_lookups_total')
        safe("SELECT COUNT(*) FROM vauto_lookups WHERE looked_up_at > NOW() - INTERVAL '30 days'", 'vauto_lookups_30d')
        safe("SELECT COUNT(*) FROM ai_assessment_log",                                       'ai_assessments_total')
        safe("SELECT COUNT(*) FROM ai_assessment_log WHERE created_at > NOW() - INTERVAL '30 days'", 'ai_assessments_30d')
        safe("SELECT COUNT(*) FROM dealer_scans",                                            'scans_total')
        safe("SELECT COUNT(*) FROM dealer_scans WHERE started_at > NOW() - INTERVAL '7 days'", 'scans_7d')
        safe("SELECT COUNT(*) FROM dealer_inventory WHERE status='active'",                  'active_inventory')
        safe("SELECT COUNT(*) FROM partner_users",                                           'partner_users')
        safe("SELECT COUNT(*) FROM dealers WHERE COALESCE(active, TRUE) = TRUE",             'dealers_active')
        try:
            cur.execute("SELECT MIN(created_at)::date AS a, MAX(created_at)::date AS b FROM bids")
            row = cur.fetchone()
            if row:
                stats['bid_first'] = row['a']
                stats['bid_last']  = row['b']
        except Exception:
            conn.rollback()
        conn.close()
    except Exception as e:
        stats['_error'] = str(e)
    return stats


def _totals():
    """Roll up service table into totals. Historical extras (infra line
    items like server + domain) roll into hist_hi but not steady/scaled."""
    hist_lo = sum(s['historical_low']  for s in SERVICES)
    hist_hi = (sum(s['historical_high'] for s in SERVICES)
               + sum(e['historical_high'] for e in HISTORICAL_EXTRAS))
    stea_lo = sum(s['steady_low']      for s in SERVICES)
    stea_hi = sum(s['steady_high']     for s in SERVICES)
    scaled  = sum(s.get('scaled_mo', 0) for s in SERVICES)
    return {
        'hist_lo': hist_lo, 'hist_hi': hist_hi,
        'steady_lo': stea_lo, 'steady_hi': stea_hi,
        'scaled_mo': scaled,
    }


def _scenarios():
    """Named scenarios — dicts of label/description/estimated-cost."""
    return [
        {
            'label': 'Steady-state (today)',
            'detail': '3 partner dealers · ~281 active cars · 5–15 bids/day · 5–10 new VINs/day · nightly scans',
            'daily_lo': 1.00, 'daily_hi': 3.00,
            'monthly_lo': 30, 'monthly_hi': 90,
        },
        {
            'label': '+1 new dealer per day (50 cars avg)',
            'detail': 'Scanner scales on existing infrastructure (FlareSolverr + DataImpulse). ~35 min vAuto verifier runtime/dealer on first pass. No per-car AI cost unless they bid.',
            'daily_lo': 2.00, 'daily_hi': 5.00,
            'monthly_lo': 60, 'monthly_hi': 150,
        },
        {
            'label': 'Partner bids on 50 of their cars in one day',
            'detail': '50× bid intake (free ingress) · 50× AI assessment · 50× Resend email · 50× Twilio SMS if opted in · one counter round doubles SMS+AI.',
            'daily_lo': 3.00, 'daily_hi': 8.00,
            'monthly_lo': None, 'monthly_hi': None,
        },
        {
            'label': '10× scale (30 dealers, 150 bids/day)',
            'detail': 'Full service mix grows linearly; FlareSolverr remains self-hosted; DataImpulse bandwidth climbs; vAuto / iPacket / AccuTrade stay flat (client license).',
            'daily_lo': 10.00, 'daily_hi': 25.00,
            'monthly_lo': 300, 'monthly_hi': 750,
        },
    ]


def _ai_model_table():
    rows = []
    for m in AI_MODELS:
        cost_1 = _cost_per_1000(m) / 1000.0
        cost_1k = _cost_per_1000(m)
        rows.append({
            'name': m['name'],
            'in_cost': m['in_cost'],
            'out_cost': m['out_cost'],
            'per_call': cost_1,
            'per_1000': cost_1k,
        })
    return rows


# ── Routes ───────────────────────────────────────────────────────────────────

@bp.route('/admin/costs')
def admin_costs():
    totals = _totals()
    ai_rows = _ai_model_table()
    return render_template('cost_dashboard.html',
                           services=SERVICES,
                           historical_extras=HISTORICAL_EXTRAS,
                           incidents=INCIDENTS,
                           totals=totals,
                           ai_rows=ai_rows,
                           today=datetime.now().strftime('%Y-%m-%d'),
                           generated_at=datetime.now())


def _pdf_path():
    """Unused after PDF removal 2026-04-24. Kept for reference in case we
    re-enable PDF export later. Stored outside /static to prevent public
    access — only /admin/* served it, behind admin login."""
    private_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'private_reports')
    os.makedirs(private_dir, exist_ok=True)
    return os.path.join(private_dir, 'ew_cost_analysis_2026-04-24.pdf')


# PDF routes removed 2026-04-24 — live dashboard at /admin/costs is the only
# deliverable now. cost_pdf_gen.py left in repo but not imported anywhere.
