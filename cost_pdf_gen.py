"""Standalone PDF builder for the EW cost analysis report.

Kept separate from cost_dashboard.py so it can be run from the command line:
    python cost_pdf_gen.py

Output: static/ew_cost_analysis_2026-04-24.pdf
"""
import os
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, KeepTogether,
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER


# ── Palette ────────────────────────────────────────────────────────────────
ASSESSMENT_INPUT_TOKENS = 25_000
ASSESSMENT_OUTPUT_TOKENS = 1_500

NAVY   = colors.HexColor('#0b1e3a')
BLUE   = colors.HexColor('#1d4ed8')
ACCENT = colors.HexColor('#3b82f6')
SLATE  = colors.HexColor('#1f2937')
MUTED  = colors.HexColor('#6b7280')
PALE   = colors.HexColor('#f1f5f9')
LINE   = colors.HexColor('#cbd5e1')
GREEN  = colors.HexColor('#059669')
RED    = colors.HexColor('#b91c1c')


def _usd(n):
    if n is None: return '—'
    if n == 0:    return '$0'
    if n < 1:     return f'${n:.2f}'
    if n < 10:    return f'${n:.2f}'
    if n < 1000:  return f'${n:,.0f}'
    return f'${n:,.0f}'


def _range(lo, hi, mo=False):
    suf = '/mo' if mo else ''
    if lo == hi:
        return f'{_usd(lo)}{suf}'
    return f'{_usd(lo)} – {_usd(hi)}{suf}'


def _styles():
    ss = getSampleStyleSheet()
    body = ParagraphStyle('body', parent=ss['BodyText'],
                          fontName='Helvetica', fontSize=10, leading=14,
                          textColor=SLATE, spaceAfter=6)
    h1 = ParagraphStyle('h1', parent=ss['Heading1'], fontName='Helvetica-Bold',
                        fontSize=22, leading=26, textColor=NAVY, spaceAfter=6)
    h2 = ParagraphStyle('h2', parent=ss['Heading2'], fontName='Helvetica-Bold',
                        fontSize=14, leading=18, textColor=NAVY,
                        spaceBefore=14, spaceAfter=6,
                        borderPadding=0)
    h3 = ParagraphStyle('h3', parent=ss['Heading3'], fontName='Helvetica-Bold',
                        fontSize=11, leading=15, textColor=BLUE,
                        spaceBefore=8, spaceAfter=2)
    small = ParagraphStyle('small', parent=body, fontSize=8.5, leading=11,
                           textColor=MUTED)
    cell = ParagraphStyle('cell', parent=body, fontSize=9, leading=12,
                          spaceAfter=0)
    cell_small = ParagraphStyle('cell_small', parent=body, fontSize=8, leading=10,
                                textColor=MUTED, spaceAfter=0)
    kicker = ParagraphStyle('kicker', parent=body, fontSize=9, leading=12,
                            textColor=ACCENT, spaceAfter=2,
                            fontName='Helvetica-Bold')
    return dict(body=body, h1=h1, h2=h2, h3=h3, small=small,
                cell=cell, cell_small=cell_small, kicker=kicker)


def _header(canvas, doc):
    """Running header + footer on every page."""
    canvas.saveState()
    w, h = LETTER
    # Top band
    canvas.setFillColor(NAVY)
    canvas.rect(0, h - 0.55 * inch, w, 0.55 * inch, fill=1, stroke=0)
    canvas.setFillColor(colors.white)
    canvas.setFont('Helvetica-Bold', 12)
    canvas.drawString(0.6 * inch, h - 0.35 * inch, 'EXPERIENCE WHOLESALE')
    canvas.setFont('Helvetica', 9)
    canvas.drawRightString(w - 0.6 * inch, h - 0.35 * inch,
                           'Cost Analysis   ·   2026-04-24')
    # Footer
    canvas.setFillColor(MUTED)
    canvas.setFont('Helvetica', 8)
    canvas.drawString(0.6 * inch, 0.35 * inch,
                      'Prepared for Experience Wholesale. Estimates — conservative ranges for planning.')
    canvas.drawRightString(w - 0.6 * inch, 0.35 * inch, f'Page {doc.page}')
    canvas.restoreState()


def _kv_card(styles, label, value, sub=None, color=NAVY):
    """A small label/value card used in the exec summary grid."""
    tbl = Table([[Paragraph(f'<font color="{MUTED.hexval()}" size=8><b>{label.upper()}</b></font>', styles['cell_small'])],
                 [Paragraph(f'<font color="{color.hexval()}" size=16><b>{value}</b></font>', styles['cell'])],
                 [Paragraph(f'<font color="{MUTED.hexval()}" size=8>{sub or ""}</font>', styles['cell_small'])]],
                colWidths=[2.2 * inch])
    tbl.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), PALE),
        ('BOX', (0, 0), (-1, -1), 0.5, LINE),
        ('LEFTPADDING', (0, 0), (-1, -1), 10),
        ('RIGHTPADDING', (0, 0), (-1, -1), 10),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
    ]))
    return tbl


def build_pdf(out_path, services, totals, scenarios, ai_rows, live=None):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    doc = SimpleDocTemplate(out_path, pagesize=LETTER,
                            leftMargin=0.6 * inch, rightMargin=0.6 * inch,
                            topMargin=0.85 * inch, bottomMargin=0.6 * inch,
                            title='Experience Wholesale — Cost Analysis',
                            author='Experience Wholesale')
    st = _styles()
    story = []

    # ── Cover / Executive Summary ─────────────────────────────────────────
    story.append(Paragraph('Cost Analysis', st['h1']))
    story.append(Paragraph('Full accounting of every external service that powers the '
                           'Experience Wholesale bid management platform — what each one '
                           'costs today, what historical development spend looked like, and '
                           'how costs scale as we onboard more dealers.',
                           st['body']))
    story.append(Spacer(1, 12))

    # Exec summary cards — 3 across
    cards = [
        _kv_card(st, 'Current run rate',
                 _range(totals['steady_lo'], totals['steady_hi'], mo=True),
                 '3 dealers · ~281 cars · 5–15 bids/day'),
        _kv_card(st, 'At 10× scale',
                 '$300 – $750/mo',
                 '30 dealers · 150 bids/day'),
        _kv_card(st, 'Biggest cost lever',
                 'AI model tier',
                 'Gemini Pro vs Claude Opus = 50× cost swing'),
    ]
    card_tbl = Table([cards], colWidths=[2.3 * inch] * 3)
    card_tbl.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 2),
        ('RIGHTPADDING', (0, 0), (-1, -1), 2),
    ]))
    story.append(card_tbl)
    story.append(Spacer(1, 14))

    story.append(Paragraph('Executive summary', st['h2']))
    exec_pts = [
        '<b>Run rate is low.</b> Current steady-state operating cost is approximately '
        f'{_range(totals["steady_lo"], totals["steady_hi"], mo=True)} — driven mostly by Twilio SMS, '
        'Google Gemini (AI assessments), and the Contabo VPS. Everything else is free-tier or fixed.',
        '<b>Development spend was modest</b> — roughly '
        f'{_range(totals["hist_lo"], totals["hist_hi"])} across the full build, '
        'with one notable outlier: a runaway ScrapFly worker accrued ~$1,127 in April 2026 before '
        'being detected and killed. The underlying service has been decommissioned and replaced with '
        'self-hosted FlareSolverr (zero marginal cost).',
        '<b>Scaling is linear and cheap.</b> Our scanner already handles 23K+ dealer sites across '
        'sister projects — adding a dealer per day does not strain infrastructure. Bid volume drives '
        'the only meaningful variable cost: AI assessments.',
        '<b>The one lever that matters: AI model tier.</b> The same assessment can cost anywhere from '
        '$0.03 to $1.24 depending on the model. Current choice (Gemini 2.5 Pro) is ~92% cheaper than '
        'the original Claude Opus setup while still producing equivalent pricing. Moving to Opus 4.7 '
        'across the board would be a defensible $300–400/mo decision at current volume.',
    ]
    for p in exec_pts:
        story.append(Paragraph('• ' + p, st['body']))

    story.append(PageBreak())

    # ── Services table ────────────────────────────────────────────────────
    story.append(Paragraph('Services in use', st['h2']))
    story.append(Paragraph('Every external service wired into EW today. Pricing is public rate — '
                           'no volume discounts assumed.', st['small']))
    story.append(Spacer(1, 6))

    header = ['Service', 'Purpose', 'Unit pricing', 'Historical', 'Monthly']
    rows = [[Paragraph(f'<b>{h}</b>', st['cell']) for h in header]]
    for s in services:
        rows.append([
            Paragraph(f'<b>{s["name"]}</b>', st['cell']),
            Paragraph(s['purpose'], st['cell_small']),
            Paragraph(s['unit'], st['cell_small']),
            Paragraph(_range(s['historical_low'], s['historical_high']), st['cell']),
            Paragraph(_range(s['steady_low'], s['steady_high'], mo=True), st['cell']),
        ])
    svc_tbl = Table(rows, colWidths=[1.2 * inch, 2.6 * inch, 1.5 * inch, 0.9 * inch, 1.0 * inch])
    svc_tbl.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), NAVY),
        ('TEXTCOLOR',  (0, 0), (-1, 0), colors.white),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('FONTSIZE', (0, 0), (-1, 0), 9),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, PALE]),
        ('LINEBELOW', (0, 0), (-1, 0), 0.5, LINE),
        ('BOX', (0, 0), (-1, -1), 0.5, LINE),
    ]))
    story.append(svc_tbl)
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        f'<b>Totals:</b>  Historical (estimated lifetime): '
        f'{_range(totals["hist_lo"], totals["hist_hi"])}   ·   '
        f'Steady-state: {_range(totals["steady_lo"], totals["steady_hi"], mo=True)}',
        st['body']))

    story.append(PageBreak())

    # ── Historical spend narrative ────────────────────────────────────────
    story.append(Paragraph('Historical spend to date', st['h2']))
    story.append(Paragraph(
        'Experience Wholesale began development in early April 2026 and went into production '
        'later the same month. The figures below are best-effort reconstructions based on active '
        'API keys, commit history, and known service invoices.',
        st['body']))

    hist_rows = [
        ['Category', 'Estimated spend', 'Notes'],
        ['AI (Claude → Gemini migration)', '$135 – $350',
         'Heavy Claude Opus usage during first two weeks of AI assessment tuning. Migrated to Gemini 2.5 Pro on Apr 16 for ~92% cost reduction.'],
        ['Twilio (SMS + Verify)', '$40 – $120',
         'A2P campaign registration, phone rental, testing, ~7 bids sent to-date plus inbound volume.'],
        ['Google Cloud (Vision + Gemini)', '$20 – $70',
         'OCR + Gemini calls since Apr 16. Very low per-call cost; shared paid project with DIA scanner.'],
        ['ScrapFly (decommissioned)', '$1,127 – $1,200',
         'Runaway worker on a sister project leaked $1,127 in overage charges before detection. Key revoked, plan cancelled Apr 23. Does not recur.'],
        ['DataImpulse proxy', '$10 – $30',
         'Light usage — scanner bandwidth for 3 dealers across 2 weeks.'],
        ['Tesla Fleet API', '$5 – $15',
         '$0.10/call, cached per VIN. Handful of test lookups.'],
        ['Hosting (Contabo VPS + domain)', '$25 – $35',
         '~$12/mo VPS since mid-March + one-time domain.'],
        ['Resend email', '$0',
         'Free tier, never exceeded.'],
    ]
    h_tbl = Table(hist_rows, colWidths=[1.8 * inch, 1.2 * inch, 4.2 * inch])
    h_tbl.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), NAVY),
        ('TEXTCOLOR',  (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, PALE]),
        ('BOX', (0, 0), (-1, -1), 0.5, LINE),
    ]))
    story.append(h_tbl)
    story.append(Spacer(1, 10))
    story.append(Paragraph(
        '<b>Total estimated historical spend: $1,360 – $1,820</b>  — of which roughly '
        '$1,127 was the one-time ScrapFly incident. Excluding that event, the full build '
        'ran under ~$700 in third-party services.',
        st['body']))

    story.append(PageBreak())

    # ── Current daily/monthly run rate ────────────────────────────────────
    story.append(Paragraph('Current daily / monthly run rate', st['h2']))
    story.append(Paragraph(
        'Operating parameters as of 2026-04-24: 3 active partner dealers '
        '(TXT Charlie · Marino Performance · Napletons Aston Martin Chicago), '
        '~281 active inventory rows, ~5–15 bids per day, ~7 AI assessments per day, '
        'nightly cron scanners at 06:00 UTC.',
        st['body']))

    day_rows = [
        ['Service',            'Per day',       'Per month',    'Driver'],
        ['Twilio SMS',         '$0.10 – $0.40', '$3 – $12',     '~10–50 messages/day (inbound + outbound)'],
        ['Gemini (Vertex AI)', '$0.20 – $0.80', '$6 – $24',     '5–15 AI assessments @ ~$0.015 each + OCR'],
        ['Google Vision OCR',  '$0.05 – $0.20', '$1 – $4',      'Mobile field-app photo uploads'],
        ['DataImpulse proxy',  '$0.10 – $0.30', '$3 – $10',     'Nightly dealer scans (bandwidth-billed)'],
        ['Tesla Fleet API',    '$0.00 – $0.20', '$0 – $5',      'Only Tesla VINs; cached'],
        ['Contabo VPS',        '$0.33',         '$10 – $15',    'Fixed'],
        ['Resend email',       '$0',            '$0',           'Free tier (< 100/day)'],
        ['vAuto / Carfax / iPacket / AccuTrade', '$0', '$0',    'Client-held subscriptions'],
    ]
    d_tbl = Table(day_rows, colWidths=[2.0 * inch, 1.3 * inch, 1.2 * inch, 2.7 * inch])
    d_tbl.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), NAVY),
        ('TEXTCOLOR',  (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, PALE]),
        ('BOX', (0, 0), (-1, -1), 0.5, LINE),
    ]))
    story.append(d_tbl)
    story.append(Spacer(1, 10))
    story.append(Paragraph(
        f'<b>Total current run rate:</b> ~$1 – $3 per day  ·  '
        f'{_range(totals["steady_lo"], totals["steady_hi"], mo=True)} all-in.',
        st['body']))

    if live and isinstance(live, dict) and not live.get('_error'):
        story.append(Spacer(1, 8))
        story.append(Paragraph('Live usage (from production database)', st['h3']))
        lv_rows = [
            ['Bids (lifetime)',              str(live.get('bids_total', 0))],
            ['Bids (last 30 days)',          str(live.get('bids_30d', 0))],
            ['Bid messages (SMS + portal)',  str(live.get('sms_msgs_total', 0))],
            ['vAuto lookups',                str(live.get('vauto_lookups_total', 0))],
            ['AI assessments logged',        str(live.get('ai_assessments_total', 0))],
            ['Dealer scans run',             str(live.get('scans_total', 0))],
            ['Active partner inventory',     str(live.get('active_inventory', 0))],
        ]
        lv_tbl = Table(lv_rows, colWidths=[3.0 * inch, 2.0 * inch])
        lv_tbl.setStyle(TableStyle([
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
            ('RIGHTPADDING', (0, 0), (-1, -1), 6),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('ROWBACKGROUNDS', (0, 0), (-1, -1), [colors.white, PALE]),
            ('BOX', (0, 0), (-1, -1), 0.5, LINE),
            ('LINEBELOW', (0, 0), (-1, -1), 0.25, LINE),
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica'),
            ('FONTNAME', (1, 0), (1, -1), 'Helvetica-Bold'),
        ]))
        story.append(lv_tbl)

    story.append(PageBreak())

    # ── Growth scenarios ──────────────────────────────────────────────────
    story.append(Paragraph('Growth scenarios', st['h2']))
    story.append(Paragraph(
        'Four forward-looking scenarios illustrating how costs scale. All figures are '
        'incremental above baseline and include a safety margin.',
        st['body']))
    story.append(Spacer(1, 6))

    sc_rows = [['Scenario', 'Daily', 'Monthly', 'Notes']]
    for s in scenarios:
        daily = _range(s['daily_lo'], s['daily_hi'])
        monthly = _range(s['monthly_lo'], s['monthly_hi'], mo=True) if s['monthly_lo'] is not None else '—'
        sc_rows.append([
            Paragraph(f'<b>{s["label"]}</b>', st['cell']),
            Paragraph(daily, st['cell']),
            Paragraph(monthly, st['cell']),
            Paragraph(s['detail'], st['cell_small']),
        ])
    sc_tbl = Table(sc_rows, colWidths=[2.0 * inch, 1.1 * inch, 1.1 * inch, 3.0 * inch])
    sc_tbl.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), NAVY),
        ('TEXTCOLOR',  (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, PALE]),
        ('BOX', (0, 0), (-1, -1), 0.5, LINE),
    ]))
    story.append(sc_tbl)

    story.append(Spacer(1, 14))
    story.append(Paragraph('What breaks at 10× scale?', st['h3']))
    story.append(Paragraph(
        'Nothing structural. The scanner architecture is already validated against 23,000+ dealer '
        'sites in a sister project. The only real question is whether one VPS handles traffic — and '
        'a Contabo upgrade is ~$20/mo, not a refactor. The bigger story at scale is <i>marketing cost '
        'per dealer acquired</i>, not infrastructure.',
        st['body']))

    story.append(PageBreak())

    # ── AI model upgrade analysis ─────────────────────────────────────────
    story.append(Paragraph('AI model tier — where the real leverage is', st['h2']))
    story.append(Paragraph(
        f'A single bid assessment currently sends ~{ASSESSMENT_INPUT_TOKENS:,} input tokens '
        f'(photos + vAuto books + Carfax/AutoCheck OCR + DIA comps) and receives ~{ASSESSMENT_OUTPUT_TOKENS:,} '
        'output tokens. Public API pricing for each realistic model tier:',
        st['body']))

    ai_header = ['Model', 'Input $/M tok', 'Output $/M tok', 'Per assessment', 'Per 1,000 assessments']
    ai_tab = [ai_header]
    for r in ai_rows:
        ai_tab.append([
            Paragraph(f'<b>{r["name"]}</b>', st['cell']),
            f'${r["in_cost"]:.2f}',
            f'${r["out_cost"]:.2f}',
            f'${r["per_call"]:.4f}',
            f'${r["per_1000"]:.2f}',
        ])
    ai_tbl = Table(ai_tab, colWidths=[2.3 * inch, 1.05 * inch, 1.15 * inch, 1.25 * inch, 1.55 * inch])
    ai_tbl.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), NAVY),
        ('TEXTCOLOR',  (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, PALE]),
        ('BOX', (0, 0), (-1, -1), 0.5, LINE),
    ]))
    story.append(ai_tbl)

    story.append(Spacer(1, 10))
    # Find Gemini Pro current vs Claude Opus for delta story
    cur = next((r for r in ai_rows if 'Gemini 2.5 Pro' in r['name']), None)
    opus = next((r for r in ai_rows if 'Opus' in r['name']), None)
    sonnet = next((r for r in ai_rows if 'Sonnet' in r['name']), None)
    haiku = next((r for r in ai_rows if 'Haiku' in r['name']), None)
    if cur and opus and sonnet and haiku:
        story.append(Paragraph('Plain-English delta', st['h3']))
        story.append(Paragraph(
            f'At today\'s volume (~7 assessments/day = ~210/month), the choice of model is '
            f'essentially <b>rounding noise</b>: Gemini Pro costs ~${cur["per_call"] * 210:.2f}/mo, '
            f'Opus would cost ~${opus["per_call"] * 210:.2f}/mo. At 1,000 assessments/month '
            f'(~50× scale, busy buy center), the gap widens: '
            f'Gemini Pro <b>${cur["per_1000"]:.2f}</b> vs '
            f'Sonnet <b>${sonnet["per_1000"]:.2f}</b> vs '
            f'Opus <b>${opus["per_1000"]:.2f}</b>. '
            f'The argument for Opus is pricing accuracy on exotic or edge-case cars — if it catches '
            f'one extra $500 deduction per 100 assessments, it pays for itself '
            f'several times over.',
            st['body']))

    story.append(PageBreak())

    # ── Recommendations ───────────────────────────────────────────────────
    story.append(Paragraph('Recommendations', st['h2']))

    recs = [
        ('Stay on Gemini 2.5 Pro for general assessments.',
         'It\'s delivering client-acceptable pricing at ~1/50th the cost of Opus. Revisit only if we see '
         'systematic underperformance on a specific vehicle class.'),
        ('Reserve Claude Opus 4.7 for a premium "second opinion" button.',
         'At ~$1.24 per assessment it\'s cheap enough to offer as an on-demand option when the manager '
         'wants a tie-breaker on a high-dollar bid. No change to default path.'),
        ('Keep the ScrapFly lesson institutional.',
         'A single unbounded worker cost >$1,100 before detection. Add per-key daily-spend alarms on '
         'any metered API we re-adopt. We\'ve moved to self-hosted FlareSolverr so this class of risk '
         'is effectively gone, but the alarm pattern is still good hygiene.'),
        ('Lock in Twilio A2P before we grow SMS volume.',
         'Verified campaign is already live. At 10× scale SMS becomes our single biggest per-message '
         'cost — worth monitoring monthly.'),
        ('Reassess Contabo sizing at 10 dealers.',
         'FlareSolverr + Postgres + scanner + Gunicorn on one VPS is comfortable today. Around 10 '
         'dealers we should split scanner off to a second droplet (~$10/mo). Not urgent.'),
        ('Don\'t reintroduce MarketCheck yet.',
         '$100/mo for comparable-vehicle data was valuable but the comp UI was paused for freshness '
         'issues. Solve the freshness problem with scanner data first; reach for a paid API only if '
         'coverage gaps remain.'),
    ]
    for title, body in recs:
        story.append(Paragraph(title, st['h3']))
        story.append(Paragraph(body, st['body']))

    story.append(Spacer(1, 18))
    story.append(Paragraph(
        f'<i>Report generated {datetime.now().strftime("%Y-%m-%d")} · '
        f'Experience Wholesale · Internal planning document.</i>',
        st['small']))

    doc.build(story, onFirstPage=_header, onLaterPages=_header)
    return out_path


if __name__ == '__main__':
    # Allow standalone generation: `python cost_pdf_gen.py`
    from cost_dashboard import SERVICES, _totals, _scenarios, _ai_model_table
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static',
                       'ew_cost_analysis_2026-04-24.pdf')
    p = build_pdf(out, SERVICES, _totals(), _scenarios(), _ai_model_table(), None)
    print(f'Wrote {p} ({os.path.getsize(p):,} bytes)')
