"""porsche_detail_scraper.py — per-VIN detail-page scrape + canonical option classifier.

Phase-2 of the Porsche cross-market arb scanner (PORSCHE_ARB_PHASE2_2026_05_27).
KBB returned nothing, iPacket is dead, Porsche.com VIN decoder 404s — so
the only way to recover the high-impact option flags (PCCB, Sport Chrono,
Burmester, PDCC, carbon roof, Paint-to-Sample, packages) is to scrape the
selling dealer's own listing page and run an LLM classifier on the
description block.

Pipeline:
  1. Fetch detail_uri via curl_cffi (Chrome TLS) → fallback to requests →
     fallback to DataImpulse residential proxy.
  2. Detect sold/404/blocked pages early.
  3. Extract description text + photo URLs with BeautifulSoup.
  4. Send description to Claude Haiku 4.5 with a JSON-mode schema prompt;
     parse canonical option flags.
  5. (Optional, gated by env PORSCHE_VISION_ENABLED=1) Send top-3 photos
     to Gemini Vision for visual confirmation. Merge with text classifier
     via confidence floor / max.
  6. Persist to porsche_arb_detail_scrape (unique on subject_vin) AND
     into porsche_arb_options(source='detail_scrape'), so phase 6.5 can
     merge them with comp_row_signals for like-pool filtering.

Concurrency: ≤4 fetch workers, ≤4 classifier workers. Hard cap 200
scrapes per pipeline run.

CALLABLE INDEPENDENTLY:
  python porsche_detail_scraper.py --limit 50 --snapshot 2026-05-27
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras
import requests

try:
    from curl_cffi import requests as cffi_requests  # type: ignore
    HAS_CFFI = True
except ImportError:
    HAS_CFFI = False

from bs4 import BeautifulSoup

# Anthropic for Haiku classifier
try:
    from anthropic import Anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

log = logging.getLogger('porsche_detail_scraper')

DB = dict(host='localhost', port=5433, user='expuser',
          password='ExpWholesale2026!', dbname='expwholesale')

# DataImpulse residential proxy (reuse the EW dealer-scan plumbing)
DI_HOST = os.environ.get('DATAIMPULSE_HOST', 'gw.dataimpulse.com')
DI_PORT = int(os.environ.get('DATAIMPULSE_PORT', '10000'))
DI_USER = os.environ.get('DATAIMPULSE_USER', '6e88fc8bf9340b01470e')
DI_PASS = os.environ.get('DATAIMPULSE_PASS', '42928cf323560da3')

ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
HAIKU_MODEL = os.environ.get('PORSCHE_DETAIL_HAIKU_MODEL',
                              'claude-haiku-4-5-20251001')

VISION_ENABLED = os.environ.get('PORSCHE_VISION_ENABLED', '0') == '1'
GEMINI_KEY = os.environ.get('GEMINI_API_KEY', '')

FETCH_TIMEOUT_S = 8
FETCH_TIMEOUT_PROXY_S = 18
RESCRAPE_AFTER_DAYS = 7
MAX_HTML_FOR_PARSE = 750_000   # 750KB cap before BS4

UA_POOL = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
]

# ── Canonical option keys (matches porsche_options.py HIGH_IMPACT_OPTIONS) ─
CLASSIFIER_KEYS = [
    'pccb', 'sport_chrono', 'burmester', 'pdcc', 'pasm',
    'carbon_roof', 'front_lift', 'sport_exhaust', 'pts_paint',
    'full_leather', 'weissach', 'clubsport', 'lightweight_package',
    'premium_plus', 'adaptive_cruise', 'manual_transmission', 'cpo',
]

HAIKU_PROMPT = (
    "You are extracting canonical Porsche option flags from a dealer "
    "listing description.\n\n"
    "Return ONLY a JSON object — no markdown, no commentary. Schema:\n"
    "{\n"
    "  \"pccb\": bool,                  // Porsche Ceramic Composite Brakes (yellow calipers)\n"
    "  \"sport_chrono\": bool,          // Sport Chrono Package (dashboard stopwatch)\n"
    "  \"burmester\": bool,             // Burmester High-End Audio\n"
    "  \"pdcc\": bool,                  // Porsche Dynamic Chassis Control\n"
    "  \"pasm\": bool,                  // Porsche Active Suspension Management\n"
    "  \"carbon_roof\": bool,           // Carbon-fiber roof\n"
    "  \"front_lift\": bool,            // Front-axle lift / nose lift\n"
    "  \"sport_exhaust\": bool,         // Sport Exhaust (toggle-able quad tips)\n"
    "  \"pts_paint\": bool,             // Paint to Sample (PTS) special-order color\n"
    "  \"full_leather\": bool,          // Full / Extended / Club Leather Interior\n"
    "  \"weissach\": bool,              // Weissach package (GT3 RS / Turbo S)\n"
    "  \"clubsport\": bool,             // Clubsport package (GT4 / 718)\n"
    "  \"lightweight_package\": bool,   // Lightweight Sport Package\n"
    "  \"premium_plus\": bool,          // Premium Package Plus\n"
    "  \"adaptive_cruise\": bool,       // Adaptive Cruise Control\n"
    "  \"manual_transmission\": bool,   // 6 or 7 speed manual (NOT PDK)\n"
    "  \"cpo\": bool,                   // Porsche-Approved Certified Pre-Owned\n"
    "  \"msrp_estimate\": null | int,   // Original MSRP $ if mentioned\n"
    "  \"confidence\": 0.0 .. 1.0       // overall confidence in the extraction\n"
    "}\n\n"
    "Rules:\n"
    "  - true ONLY if the option is confidently mentioned in the text\n"
    "  - false (NOT null) if not mentioned or ambiguous\n"
    "  - msrp_estimate: only set if a clear original-MSRP $ is in the text\n"
    "  - confidence: 0.0=no info found, 0.5=partial, 1.0=detailed equipment list\n\n"
    "DESCRIPTION TEXT:\n"
)


def conn():
    return psycopg2.connect(**DB)


# ── Fetch ──────────────────────────────────────────────────────────────

def _detect_block_or_gone(html: str, status: int) -> Optional[str]:
    """Return 'sold' / 'gone' / 'block' / None."""
    if status == 404:
        return 'gone'
    if not html or len(html) < 500:
        return 'block'
    lo = html.lower()[:80000]
    sold_pats = (
        r'\bthis\s+vehicle\s+has\s+been\s+sold\b',
        r'\bthis\s+car\s+has\s+been\s+sold\b',
        r'\bthis\s+vehicle\s+has\s+sold\b',
        r'\b(?:this\s+)?vehicle\s+is\s+no\s+longer\s+available\b',
        r'\b(?:this\s+)?vehicle\s+has\s+been\s+removed\b',
        r'\bvehicle\s+no\s+longer\s+in\s+stock\b',
        r'\bsorry,?\s+this\s+vehicle\b',
    )
    for p in sold_pats:
        if re.search(p, lo):
            return 'sold'
    block_pats = (
        'access denied', 'cloudflare', 'datadome',
        'just a moment', 'please verify you are a human',
        'enable javascript and cookies',
    )
    head = lo[:5000]
    for p in block_pats:
        if p in head:
            return 'block'
    return None


def _ua():
    import random
    return random.choice(UA_POOL)


def fetch_one(detail_uri: str) -> tuple[str | None, int, str]:
    """3-tier fetch. Returns (html, http_status, tier).

    tier ∈ {'cffi', 'requests', 'proxy', 'block', 'gone', 'err'}
    """
    if not detail_uri or not detail_uri.startswith(('http://', 'https://')):
        return None, 0, 'err'

    # Tier 1: curl_cffi Chrome impersonation
    if HAS_CFFI:
        try:
            r = cffi_requests.get(
                detail_uri,
                impersonate='chrome',
                timeout=FETCH_TIMEOUT_S,
                allow_redirects=True,
                headers={'User-Agent': _ua()},
            )
            if r.status_code in (200, 202) and r.text and len(r.text) > 1500:
                return r.text, r.status_code, 'cffi'
            if r.status_code == 404:
                return None, 404, 'gone'
        except Exception:
            pass

    # Tier 2: plain requests
    try:
        r = requests.get(
            detail_uri,
            headers={'User-Agent': _ua(),
                     'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'},
            timeout=FETCH_TIMEOUT_S,
            allow_redirects=True,
        )
        if r.status_code in (200, 202) and r.text and len(r.text) > 1500:
            return r.text, r.status_code, 'requests'
        if r.status_code == 404:
            return None, 404, 'gone'
    except Exception:
        pass

    # Tier 3: residential proxy
    proxy_url = f'http://{DI_USER}:{DI_PASS}@{DI_HOST}:{DI_PORT}'
    try:
        r = requests.get(
            detail_uri,
            headers={'User-Agent': _ua(),
                     'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'},
            timeout=FETCH_TIMEOUT_PROXY_S,
            proxies={'http': proxy_url, 'https': proxy_url},
            allow_redirects=True,
        )
        if r.status_code in (200, 202) and r.text and len(r.text) > 1500:
            return r.text, r.status_code, 'proxy'
        if r.status_code == 404:
            return None, 404, 'gone'
        return None, r.status_code, 'block'
    except Exception:
        return None, 0, 'err'


# ── Parse ──────────────────────────────────────────────────────────────

_DESC_SELECTORS = (
    '[itemprop=description]',
    '[itemprop="vehicleDescription"]',
    '[class*=description i]',
    '[class*=vehicle-description i]',
    '[id*=vehicle-description i]',
    '[class*=vdpDescription i]',
    '[class*=vdp-description i]',
    '[class*=vehicleOverview i]',
    '[class*=overview i]',
    # ddc-content / ws-* are Dealer.com (one of the most common dealer platforms)
    '[class*=ws-detailed-specs i]',
    '[class*=ws-pckg-accessories i]',
    '[class*=ws-hl-features i]',
    '[class*=ws-comments i]',
    '[id*=detailed-specs i]',
    '[id*=pckg-accessories i]',
    '[id*=hl-features i]',
    # spec-category panels (Dealer.com expanded equipment lists)
    '[class*=spec-category i]',
    '[class*=spec-group i]',
    # Catch-all for feature/equipment/standard-features blocks
    '[class*=standard-features i]',
    '[class*=optional-equipment i]',
    '[class*=feature-list i]',
    '[class*=feature i]',
    '[class*=equipment i]',
    '[class*=packages i]',
    '[class*=options-list i]',
)


def extract_description(html: str) -> str:
    """Pull out the longest plausible description block + key feature lists."""
    if not html:
        return ''
    # Truncate before BS4 to keep memory predictable
    if len(html) > MAX_HTML_FOR_PARSE:
        html = html[:MAX_HTML_FOR_PARSE]
    soup = BeautifulSoup(html, 'html.parser')
    # Strip junk
    for t in soup(['script', 'style', 'nav', 'header', 'footer', 'noscript']):
        t.decompose()

    chunks: list[str] = []

    # Try JSON-LD description
    for jl in soup.find_all('script', type='application/ld+json'):
        try:
            data = json.loads(jl.string or '{}')
        except Exception:
            continue
        if isinstance(data, list):
            data = data[0] if data else {}
        if isinstance(data, dict):
            desc = data.get('description') or data.get('vehicleConfiguration')
            if desc and isinstance(desc, str) and len(desc) > 50:
                chunks.append(desc)
            feats = data.get('vehicleFeatures') or data.get('itemFeatures')
            if isinstance(feats, list):
                chunks.append(' | '.join(str(f) for f in feats if f))

    # Specific selectors — dedupe by leading 60 chars so we don't
    # capture both the wrapper div AND its inner ul (common pattern).
    seen_starts: set[str] = set()
    for sel in _DESC_SELECTORS:
        try:
            for el in soup.select(sel)[:6]:
                txt = el.get_text(' ', strip=True)
                if not txt or len(txt) < 40:
                    continue
                key = txt[:60]
                if key in seen_starts:
                    continue
                seen_starts.add(key)
                chunks.append(txt)
        except Exception:
            continue

    # Generic long paragraphs (only if we have very little structured content)
    if sum(len(c) for c in chunks) < 1000:
        for p in soup.find_all(['p', 'div', 'li']):
            txt = p.get_text(' ', strip=True)
            if txt and 80 < len(txt) < 4000:
                key = txt[:60]
                if key in seen_starts:
                    continue
                seen_starts.add(key)
                chunks.append(txt)
                if len(chunks) > 12:
                    break

    # OpenGraph description as last-ditch
    og = soup.find('meta', attrs={'property': 'og:description'})
    if og and og.get('content'):
        chunks.append(og['content'])

    text = '\n'.join(chunks)
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:25000]


def extract_photos(html: str, base_uri: str) -> dict:
    """Top-5 listing photos. Returns {'urls': [...], 'top3': [...]}."""
    if not html:
        return {'urls': [], 'top3': []}
    soup = BeautifulSoup(html[:MAX_HTML_FOR_PARSE], 'html.parser')
    found: list[str] = []
    host = urlparse(base_uri).netloc.lower()

    def _add(u):
        if not u or not isinstance(u, str):
            return
        u = u.strip()
        if not u or u.startswith('data:'):
            return
        if u.startswith('//'):
            u = 'https:' + u
        elif u.startswith('/'):
            u = f'https://{host}{u}'
        if u in found:
            return
        # Skip obvious garbage
        if any(b in u.lower() for b in ('placeholder', 'logo', 'sprite',
                                         '.svg', 'icon', 'thumb-')):
            return
        found.append(u)

    # itemprop=image
    for el in soup.select('[itemprop=image]')[:8]:
        _add(el.get('content') or el.get('src') or el.get('data-src'))
    # JSON-LD
    for jl in soup.find_all('script', type='application/ld+json'):
        try:
            data = json.loads(jl.string or '{}')
        except Exception:
            continue
        items = [data] if isinstance(data, dict) else (data or [])
        for d in items:
            if not isinstance(d, dict):
                continue
            for k in ('image', 'images', 'photo'):
                v = d.get(k)
                if isinstance(v, str):
                    _add(v)
                elif isinstance(v, list):
                    for x in v[:10]:
                        if isinstance(x, str):
                            _add(x)
                        elif isinstance(x, dict):
                            _add(x.get('url') or x.get('contentUrl'))
    # og:image
    for og in soup.find_all('meta', attrs={'property': 'og:image'})[:5]:
        _add(og.get('content'))
    # Gallery-class images
    for el in soup.select(
        '[class*=gallery i] img, [class*=photo i] img, '
        '[class*=carousel i] img, [class*=vehicle-image i]'
    )[:20]:
        _add(el.get('src') or el.get('data-src') or el.get('data-lazy-src'))

    urls = found[:5]
    return {'urls': urls, 'top3': urls[:3]}


# ── Classifier ─────────────────────────────────────────────────────────

def classify_text(description: str) -> tuple[dict, float, str | None]:
    """Run Haiku 4.5 on the description text. Returns
    (classifier_jsonb_dict, confidence, error_or_None).
    """
    if not description or len(description) < 40:
        return {}, 0.0, 'no_description'
    if not HAS_ANTHROPIC or not ANTHROPIC_KEY:
        return {}, 0.0, 'no_anthropic'
    try:
        client = Anthropic(api_key=ANTHROPIC_KEY)
        msg = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=500,
            messages=[{
                'role': 'user',
                'content': HAIKU_PROMPT + description[:15000],
            }],
        )
        raw = msg.content[0].text if msg.content else ''
        raw = raw.strip()
        # Strip code fences if present
        if raw.startswith('```'):
            raw = re.sub(r'^```(?:json)?\s*', '', raw)
            raw = re.sub(r'\s*```$', '', raw)
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {}, 0.0, 'bad_shape'
        # Coerce/clean
        out: dict = {}
        for k in CLASSIFIER_KEYS:
            v = data.get(k)
            if isinstance(v, bool):
                out[k] = v
            elif isinstance(v, (int, float)):
                out[k] = bool(v)
            else:
                out[k] = False
        msrp_raw = data.get('msrp_estimate')
        if isinstance(msrp_raw, (int, float)) and msrp_raw > 10000:
            out['msrp_estimate'] = int(msrp_raw)
        conf_raw = data.get('confidence')
        try:
            confidence = float(conf_raw) if conf_raw is not None else 0.5
            confidence = max(0.0, min(1.0, confidence))
        except (TypeError, ValueError):
            confidence = 0.5
        return out, confidence, None
    except json.JSONDecodeError as e:
        return {}, 0.0, f'json:{e!s:.80}'
    except Exception as e:
        return {}, 0.0, f'{type(e).__name__}:{e!s:.80}'


def _classifier_to_options(classifier: dict) -> dict:
    """Map classifier output → porsche_arb_options.options_jsonb shape.
    Only true flags are written (False is the default in the Jaccard
    code; storing False would just bloat the table)."""
    out: dict = {}
    if not classifier:
        return out
    for k in CLASSIFIER_KEYS:
        if classifier.get(k) is True:
            out[k] = True
    return out


# ── Storage ────────────────────────────────────────────────────────────

def upsert_detail_scrape(
    *,
    subject_vin: str, detail_uri: str, uri_host: str | None,
    http_status: int, raw_html_len: int, raw_text: str,
    photos_jsonb: dict, sold_out: bool,
    classifier_jsonb: dict | None,
    classifier_confidence: float | None,
    msrp_estimate: float | int | None,
    classifier_model: str | None,
) -> None:
    classifier_at = datetime.utcnow() if classifier_jsonb else None
    with conn() as c, c.cursor() as cur:
        cur.execute("""
            INSERT INTO porsche_arb_detail_scrape
              (subject_vin, detail_uri, uri_host, http_status,
               raw_html_len, raw_text, photos_jsonb, sold_out,
               classifier_jsonb, classifier_at, classifier_model,
               classifier_confidence, msrp_estimate, scraped_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,
                    %s::jsonb,%s,%s,%s,%s, NOW())
            ON CONFLICT (subject_vin) DO UPDATE SET
              detail_uri = EXCLUDED.detail_uri,
              uri_host = EXCLUDED.uri_host,
              http_status = EXCLUDED.http_status,
              raw_html_len = EXCLUDED.raw_html_len,
              raw_text = EXCLUDED.raw_text,
              photos_jsonb = EXCLUDED.photos_jsonb,
              sold_out = EXCLUDED.sold_out,
              classifier_jsonb = COALESCE(EXCLUDED.classifier_jsonb, porsche_arb_detail_scrape.classifier_jsonb),
              classifier_at = COALESCE(EXCLUDED.classifier_at, porsche_arb_detail_scrape.classifier_at),
              classifier_model = COALESCE(EXCLUDED.classifier_model, porsche_arb_detail_scrape.classifier_model),
              classifier_confidence = COALESCE(EXCLUDED.classifier_confidence, porsche_arb_detail_scrape.classifier_confidence),
              msrp_estimate = COALESCE(EXCLUDED.msrp_estimate, porsche_arb_detail_scrape.msrp_estimate),
              scraped_at = NOW()
        """, (
            subject_vin.upper(), detail_uri, uri_host, http_status,
            raw_html_len, raw_text[:30000] if raw_text else None,
            json.dumps(photos_jsonb or {}),
            bool(sold_out),
            json.dumps(classifier_jsonb) if classifier_jsonb is not None else None,
            classifier_at,
            classifier_model,
            classifier_confidence,
            msrp_estimate,
        ))
        c.commit()


def upsert_options_from_classifier(subject_vin: str, classifier: dict,
                                    msrp: int | None) -> None:
    """Mirror classifier output to porsche_arb_options(source='detail_scrape')
    so phase 6.5 picks it up via _load_options_from_db."""
    options = _classifier_to_options(classifier)
    if not options:
        return
    with conn() as c, c.cursor() as cur:
        cur.execute("""
            INSERT INTO porsche_arb_options
              (subject_vin, source, options_jsonb, options_raw, msrp, fetched_at)
            VALUES (%s, 'detail_scrape', %s::jsonb, %s::jsonb, %s, NOW())
            ON CONFLICT (subject_vin, source) DO UPDATE SET
              options_jsonb = EXCLUDED.options_jsonb,
              options_raw = EXCLUDED.options_raw,
              msrp = COALESCE(EXCLUDED.msrp, porsche_arb_options.msrp),
              fetched_at = NOW()
        """, (subject_vin.upper(), json.dumps(options),
              json.dumps(classifier or {}), msrp))
        c.commit()


# ── Main per-VIN pipeline ──────────────────────────────────────────────

def scrape_one(subject_vin: str, detail_uri: str) -> dict:
    """Full per-VIN pipeline: fetch → parse → classify → persist.
    Returns a stats dict for logging."""
    out = {
        'vin': subject_vin, 'uri': detail_uri,
        'fetch_tier': None, 'http_status': 0,
        'sold': False, 'block': False,
        'classified': False, 'confidence': 0.0,
        'options_found': 0, 'msrp': None,
        'error': None,
    }
    try:
        host = urlparse(detail_uri).netloc.lower()
        html, status, tier = fetch_one(detail_uri)
        out['fetch_tier'] = tier
        out['http_status'] = status

        # Block/sold/gone short-circuit
        sold_out = False
        if tier in ('gone',):
            sold_out = True
        elif tier in ('block', 'err'):
            upsert_detail_scrape(
                subject_vin=subject_vin, detail_uri=detail_uri,
                uri_host=host, http_status=status, raw_html_len=0,
                raw_text='', photos_jsonb={'urls': [], 'top3': []},
                sold_out=False, classifier_jsonb=None,
                classifier_confidence=None, msrp_estimate=None,
                classifier_model=None,
            )
            out['block'] = True
            out['error'] = f'fetch_{tier}'
            return out
        else:
            sentinel = _detect_block_or_gone(html or '', status)
            if sentinel in ('sold', 'gone'):
                sold_out = True

        raw_html_len = len(html or '')
        description = extract_description(html or '') if html else ''
        photos = extract_photos(html or '', detail_uri) if html else {'urls': [], 'top3': []}

        # Classify if we got real content (skip on sold/gone)
        classifier_jsonb: dict | None = None
        confidence: float | None = None
        msrp_est: int | None = None
        classifier_model: str | None = None
        if not sold_out and description and len(description) > 40:
            cdata, conf, cerr = classify_text(description)
            if cdata:
                classifier_jsonb = cdata
                confidence = conf
                msrp_est = cdata.get('msrp_estimate') if isinstance(cdata.get('msrp_estimate'), int) else None
                classifier_model = HAIKU_MODEL
                out['classified'] = True
                out['confidence'] = conf
                out['options_found'] = sum(
                    1 for k in CLASSIFIER_KEYS if cdata.get(k) is True
                )
                out['msrp'] = msrp_est
            else:
                out['error'] = cerr or 'classify_empty'

        upsert_detail_scrape(
            subject_vin=subject_vin, detail_uri=detail_uri,
            uri_host=host, http_status=status,
            raw_html_len=raw_html_len, raw_text=description,
            photos_jsonb=photos, sold_out=sold_out,
            classifier_jsonb=classifier_jsonb,
            classifier_confidence=confidence,
            msrp_estimate=msrp_est,
            classifier_model=classifier_model,
        )
        out['sold'] = sold_out
        # Mirror canonical options into porsche_arb_options
        if classifier_jsonb:
            upsert_options_from_classifier(subject_vin, classifier_jsonb, msrp_est)
        return out
    except Exception as e:
        out['error'] = f'{type(e).__name__}:{e!s:.120}'
        return out


# ── Bulk driver ────────────────────────────────────────────────────────

def select_vins_to_scrape(snapshot_date: str | None = None,
                          limit: int = 200) -> list[tuple[str, str]]:
    """Pick flagged candidates without a recent detail_scrape row, ranked
    by current arb_score (v2 → v1) DESC. Returns [(vin, detail_uri), ...].

    Excludes URIs that previously bounced as sold/gone (sold_out=TRUE) so
    we don't waste an LLM call on a known dead listing.
    """
    # Params order MUST match placeholder order in SQL.
    snap_clause = ''
    if snapshot_date:
        snap_clause = 'AND c.snapshot_date = %s::date'
        params: list = [snapshot_date, str(RESCRAPE_AFTER_DAYS), limit]
    else:
        params = [str(RESCRAPE_AFTER_DAYS), limit]
    with conn() as c, c.cursor() as cur:
        cur.execute(f"""
            SELECT c.subject_vin, c.detail_uri,
                   COALESCE(c.arb_score_v2, c.arb_score, 0) AS sc
              FROM porsche_arb_candidates c
              LEFT JOIN porsche_arb_detail_scrape s
                ON s.subject_vin = c.subject_vin
             WHERE c.flagged = TRUE
               AND c.detail_uri IS NOT NULL
               AND c.detail_uri <> ''
               {snap_clause}
               AND (
                    s.subject_vin IS NULL
                 OR (s.scraped_at < NOW() - (%s || ' days')::interval
                     AND COALESCE(s.sold_out, FALSE) = FALSE)
               )
             ORDER BY sc DESC NULLS LAST
             LIMIT %s
        """, params)
        return [(r[0], r[1]) for r in cur.fetchall()]


def run_bulk(vins_with_uris: list[tuple[str, str]],
             concurrency: int = 4) -> dict:
    """Run scrape_one across VINs with ThreadPoolExecutor. Returns stats."""
    stats = {
        'total': len(vins_with_uris),
        'classified': 0, 'sold_out': 0, 'blocked': 0,
        'errors': 0, 'avg_conf': 0.0,
        'tier_buckets': {},
        'sample_results': [],
    }
    if not vins_with_uris:
        return stats
    t0 = time.monotonic()
    conf_sum = 0.0
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futs = {pool.submit(scrape_one, v, u): (v, u)
                for v, u in vins_with_uris}
        for fut in as_completed(futs):
            r = fut.result()
            tier = r.get('fetch_tier') or 'none'
            stats['tier_buckets'][tier] = stats['tier_buckets'].get(tier, 0) + 1
            if r.get('classified'):
                stats['classified'] += 1
                conf_sum += r.get('confidence') or 0.0
            if r.get('sold'):
                stats['sold_out'] += 1
            if r.get('block'):
                stats['blocked'] += 1
            if r.get('error'):
                stats['errors'] += 1
            if len(stats['sample_results']) < 8:
                stats['sample_results'].append(r)
    elapsed = time.monotonic() - t0
    stats['elapsed_s'] = round(elapsed, 1)
    stats['avg_conf'] = round(conf_sum / max(1, stats['classified']), 3)
    return stats


# ── CLI ────────────────────────────────────────────────────────────────

def _cli():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    )
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--limit', type=int, default=50)
    p.add_argument('--concurrency', type=int, default=4)
    p.add_argument('--snapshot', type=str, default=None,
                   help='YYYY-MM-DD, defaults to latest in porsche_arb_candidates')
    p.add_argument('--vin', type=str, default=None,
                   help='single VIN to scrape (overrides --limit)')
    args = p.parse_args()

    if args.vin:
        with conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT subject_vin, detail_uri
                  FROM porsche_arb_candidates
                 WHERE subject_vin = %s
                 ORDER BY snapshot_date DESC LIMIT 1
            """, (args.vin.upper(),))
            row = cur.fetchone()
            if not row:
                log.error('no candidate row for VIN %s', args.vin)
                return 2
            vins = [(row[0], row[1])]
    else:
        vins = select_vins_to_scrape(args.snapshot, args.limit)

    log.info('scraping %d VINs (concurrency=%d)', len(vins), args.concurrency)
    stats = run_bulk(vins, concurrency=args.concurrency)
    log.info('done: %s', json.dumps(
        {k: v for k, v in stats.items() if k != 'sample_results'},
        default=str))
    for r in stats.get('sample_results', [])[:5]:
        log.info('  sample: %s', json.dumps(r, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(_cli())
