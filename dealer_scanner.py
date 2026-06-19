"""
Dealer inventory scanner — Phase 1.

Runs as CLI or imported by the Flask blueprint.
- Platform detection from HTML fingerprint
- Sitemap-first URL discovery, HTML crawl fallback
- JSON-LD Vehicle schema extractor, OpenGraph + HTML fallback
- Multi-signal sold detection (missing scans, URL 404, text regex, sitemap drop)
- Price-drop detection
- COALESCE smart-merge upsert (never overwrites VIN/YMM once set)

Usage:
    python dealer_scanner.py --url https://example.com --name "Example Motors"
    python dealer_scanner.py --dealer-id 1
"""
import argparse
import html as _html
import json as _json
import os
import re
import sys
import threading
import time
import traceback
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET

import psycopg2
import psycopg2.extras
import requests

import dealer_fetchers  # platform-aware fetch tiers (direct / flaresolverr / proxy)

# ── Config ───────────────────────────────────────────────────────────────
DB_URL = os.environ.get('DATABASE_URL', 'postgresql://expuser:ExpWholesale2026!@localhost/expwholesale')
USER_AGENT = os.environ.get('DEALER_SCANNER_UA',
    'Mozilla/5.0 (compatible; EW-DealerScanner/1.0; +https://experience-wholesale.net)')
REQUEST_TIMEOUT = 20
CRAWL_MAX_URLS = 2000          # hard cap per scan
# CRAWL_WALLCLOCK_2026_06_11: hard wall-clock budget for ONE universal per-VDP
# crawl pass. The per-FlareSolverr-call deadline (dealer_fetchers, 120s) caps a
# single fetch, but a CRAWL_MAX_URLS-deep loop of slow-but-not-timing-out fetches
# can still run for hours and is re-run once per escalation tier. This stops a
# pass mid-loop so the scan returns whatever it has rather than wedging the queue
# (Tamiami Ford dealer 98: 781 generic URLs crawled to urls_fetched=555, never
# finished). 25 min default leaves headroom under scan_all's 30-min per-dealer cap.
CRAWL_PASS_DEADLINE_SEC = int(os.environ.get('CRAWL_PASS_DEADLINE_SEC', '1500'))
SOLD_CONFIDENCE_THRESHOLD = 0.6
# Probe the URL on the FIRST missing scan. Previously was 2 (wait 2 scans
# before even checking URL), which meant sitemap-only dealers (WordPress)
# needed 3 days of history before a sold ever fired — unacceptable for
# high-turnover lots. A URL 404 or page-text "sold" is authoritative on its
# own; combining with missing_from_scan=0.25 easily clears the 0.60 threshold.
MISSING_SCANS_BEFORE_PROBE = 1
COLORS_PER_SCAN = int(os.environ.get('DEALER_COLORS_PER_SCAN', '10'))  # small burst on scan; continuous worker handles the rest
GEMINI_CALL_TIMEOUT_SEC = int(os.environ.get('GEMINI_CALL_TIMEOUT', '30'))


def _call_with_timeout(fn, timeout_sec, *args, **kwargs):
    """Run fn in a daemon thread; return (ok, result) where ok=False on timeout.
    Google genai SDK has no built-in timeout and can hang forever on a
    half-open connection — without this guard, a single stalled photo can
    freeze the entire scan."""
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

# VDP (vehicle detail page) hints — matching one of these + more path segments means it's a VDP
VDP_HINTS = (
    '/vehicle/', '/vehicle-', '/vehicles/', '/vdp/',
    '/detail/', '/details/', '/vehicle-details/',
    '/car/', '/cars/', '-vin-', '?vin=', '?stock=',
    '/used-vehicles/', '/new-vehicles/', '/certified-vehicles/',
    '/preowned/', '/pre-owned/',
)
# List/index pages — queue these for crawl but never treat as a vehicle
LIST_HINTS = (
    '/inventory', '/vehicles', '/used-cars', '/new-cars',
    '/pre-owned', '/preowned', '/certified', '/search', '/stock',
    '/showroom', '/our-inventory',
)
NON_VEHICLE_HINTS = (
    '/about', '/contact', '/service', '/parts', '/finance',
    '/privacy', '/terms', '/careers', '/staff', '/directions',
    '/specials', '/blog', '/news', '.jpg', '.jpeg', '.png',
    '.gif', '.webp', '.css', '.js', '.pdf',
    '/login', '/logout', '/account', '/cart', '/checkout',
    '/sell-or-trade', '/trade-in', '/appraisal', '/test-drive',
    '/schedule-service', '/sitemap', '/wp-admin', '/wp-json',
    '/author/', '/tag/', '/category/', '/feed',
)
# Real VINs always contain at least one letter (manufacturer code + model year
# code are alphabetic). The earlier regex `[A-HJ-NPR-Z0-9]{17}` accepted pure-
# digit 17-char strings, which let Unix timestamps (e.g., 17769403640177593 =
# 2026-04-23) and other inline-JSON numerics get captured as VINs. That broke
# dedup on TXT Charlie scan 33 (60 fake-VIN duplicate rows). Lookahead
# `(?=[^\b]*[A-HJ-NPR-Z])` would be cleaner but Python's re doesn't support
# that — instead we capture and validate post-match with `_is_valid_vin`.
VIN_RE = re.compile(r'\b([A-HJ-NPR-Z0-9]{17})\b')


def _is_valid_vin(s):
    """A real VIN has at least one letter (manufacturer + model-year codes)."""
    return bool(s) and len(s) == 17 and any(c.isalpha() for c in s)
PRICE_RE = re.compile(r'\$\s?([\d,]{3,7})')
MILES_RE = re.compile(r'([\d,]{1,7})\s*(?:mi|miles|mil\.|km)\b', re.I)
YEAR_RE = re.compile(r'\b(19[89]\d|20[0-3]\d)\b')
SOLD_TEXT_RE = re.compile(r'\b(?:sold|sale pending|under contract|deal pending|reserved)\b', re.I)
H1_RE = re.compile(r'<h1[^>]*>([^<]{3,120})</h1>', re.I)
TITLE_RE = re.compile(r'<title[^>]*>([^<]{3,200})</title>', re.I)

# Multi-token makes that need to be rejoined from hyphen/space tokens.
KNOWN_COMPOUND_MAKES = (
    'aston martin', 'alfa romeo', 'rolls royce', 'land rover',
    'range rover', 'mercedes benz', 'aston-martin', 'alfa-romeo',
    'rolls-royce', 'land-rover', 'range-rover', 'mercedes-benz',
)
MAKE_NORMALIZE = {
    'aston-martin': 'Aston Martin', 'alfa-romeo': 'Alfa Romeo',
    'rolls-royce': 'Rolls-Royce', 'land-rover': 'Land Rover',
    'range-rover': 'Range Rover', 'mercedes-benz': 'Mercedes-Benz',
    'vw': 'Volkswagen', 'bmw': 'BMW', 'gmc': 'GMC',
    'chevy': 'Chevrolet', 'mb': 'Mercedes-Benz',
}

# Whitelist of valid single-token makes (lowercase). Used by _parse_ymm_from_url
# to reject when the post-year token isn't a real make — e.g. Bentley Denver's
# slug `bentayga-ewb-bentley-2024-azure-rc026442` puts the make mid-slug, so
# the post-year token is `azure` (a color). Without this gate the URL parser
# emitted `make=Azure, model=Rc026442` and the og:title parser (which would
# have produced `Bentley Bentayga`) was blocked by the existing-value guard
# in extract_vehicle. Set covers the makes we'd plausibly see at the kinds
# of dealers EW onboards (mass-market + luxury + exotic); extend as needed.
_KNOWN_SINGLE_MAKES = frozenset({
    'acura', 'audi', 'bentley', 'bmw', 'bugatti', 'buick', 'cadillac',
    'chevrolet', 'chevy', 'chrysler', 'dodge', 'ferrari', 'fiat', 'ford',
    'genesis', 'gmc', 'honda', 'hummer', 'hyundai', 'infiniti', 'jaguar',
    'jeep', 'kia', 'koenigsegg', 'lamborghini', 'lexus', 'lincoln',
    'lotus', 'maserati', 'maybach', 'mazda', 'mclaren', 'mercedes', 'mini',
    'mitsubishi', 'nissan', 'pagani', 'plymouth', 'polestar', 'pontiac',
    'porsche', 'ram', 'rivian', 'saab', 'saturn', 'scion', 'shelby',
    'smart', 'subaru', 'suzuki', 'tesla', 'toyota', 'volkswagen', 'volvo',
    'vw', 'mb', 'ineos',
})


# ── DB helpers ───────────────────────────────────────────────────────────
def get_conn():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


# ── Color validator ─────────────────────────────────────────────────────
_BAD_COLOR_SUBSTRINGS = (
    'thought', 'the user', 'i need', 'i cannot', 'unable to',
    "i don't", 'i do not', 'let me', 'looking at', 'in this photo',
    'appears to', 'cannot determine', 'exterior color', 'the vehicle',
    'identify', 'analysis', 'based on', 'this image', 'this photo',
)

def _is_valid_color(s):
    """Reject Gemini reasoning leaks. A real color is 1-4 words, 3-40 chars,
    mostly alphabetic, and doesn't contain chain-of-thought fragments."""
    if not s or not isinstance(s, str):
        return False
    s = s.strip()
    if len(s) < 3 or len(s) > 40:
        return False
    low = s.lower()
    if any(b in low for b in _BAD_COLOR_SUBSTRINGS):
        return False
    if low in ('unknown', 'unavailable', 'n/a', 'none', 'null'):
        return False
    if not re.search(r'[A-Za-z]', s):
        return False
    if len(s.split()) > 4:
        return False
    return True


def now_utc():
    return datetime.now(timezone.utc)


# ── HTTP ─────────────────────────────────────────────────────────────────
def _session():
    s = requests.Session()
    # Accept-Encoding: only advertise br when the `brotli` package is installed
    # so requests can decompress the response. Sucuri/LiteSpeed WAFs (Ferrari of
    # Washington, etc.) will serve Brotli regardless of what we ask for, and a
    # missing brotli decoder turns a 200 OK into binary garbage and the regex
    # crawl returns 0 hrefs (2026-05-13 Ferrari hang root cause).
    try:
        import brotli  # noqa: F401
        accept_enc = 'gzip, deflate, br'
    except ImportError:
        accept_enc = 'gzip, deflate'
    s.headers.update({
        'User-Agent': USER_AGENT,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': accept_enc,
    })
    return s


# The scanner threads a `tier` through fetch calls via this module-level default.
# DealerScanner.run() sets it per-dealer based on detected platform.
_CURRENT_TIER = {'tier': 'direct'}


def fetch(url, sess, method='GET', tier=None):
    """Routes through dealer_fetchers with the active tier. Stable signature
    preserved so legacy callers keep working."""
    t = tier or _CURRENT_TIER.get('tier') or 'direct'
    return dealer_fetchers.fetch(url, sess, method=method, tier=t)


# ── Platform detection ───────────────────────────────────────────────────
def detect_platform(html):
    """Returns (platform_name, scrape_method_hint). Checks most-specific fingerprints
    first — AAN > dealer.com > generic WordPress — because AAN dealers run WordPress."""
    if not html:
        return ('unknown', 'sitemap+jsonld')
    h = html.lower()
    # AAN (Automotive Advertising Network) — aanWordpress theme, /api/cars JSON feed.
    # Must be checked BEFORE generic WordPress detection.
    if 'aanwordpress' in h or '/themes/aan' in h or '/api/cars' in h:
        return ('aan', 'api')
    # dealer.com — fingerprint ONLY the CMS markers, not the analytics tag.
    # `analytics.dealer.com` is loaded by many non-dealer.com sites for ad
    # attribution (e.g. Mark Motors, dealer 18 — false-positive 2026-05-10).
    if 'ddc-' in h or 'cdn.dealer.com' in h or 'cms.dealer.com' in h \
            or 'static.dealer.com' in h:
        return ('dealer.com', 'jsonld')
    if 'dealerinspire' in h or 'di-sites' in h:
        return ('dealerinspire', 'jsonld')
    if 'autorevo' in h:
        return ('autorevo', 'jsonld')
    if 'dealercenter' in h or 'dealercenterpro' in h:
        return ('dealercenter', 'jsonld')
    if 'frazer' in h or 'dcsinternet' in h:
        return ('frazer', 'jsonld')
    if 'autotrader.com/dealers/embed' in h or 'atcembed' in h:
        return ('autotrader-embed', 'jsonld')
    if 'cdn.shopify.com' in h or 'shopify-section' in h:
        return ('shopify', 'shopify-products')
    if 'eprocess' in h or 'dealer-eprocess' in h:
        return ('dealer-eprocess', 'sitemap+jsonld')
    # DealerOn — server-rendered Vehicle + Product JSON-LD per VDP, sitemap.xml
    # carries the full VDP list. Vendor CDN: dealeron.com / dlron.us; theme:
    # harmoniq. Standard universal-discover path (sitemap → fetch → JSON-LD)
    # handles it; this fingerprint exists to skip the $1-3 AI-discovery spawn.
    if 'dealeron.com' in h or 'dlron.us' in h or '/dealeron.js' in h:
        return ('dealeron', 'sitemap+jsonld')
    # Greenlight Automotive Solutions — custom React-CRA SPA hosted by
    # greenlightautomotivesolutions.com (Brooklyn agency). Their VDP image
    # CDN is s3-us-west-2.amazonaws.com/ethosautos/vdp/. Mark Motors uses
    # it; any future Greenlight dealer will match the same fingerprints.
    if 'greenlightautomotivesolutions.com' in h \
            or 'ethosautos/vdp/' in h \
            or '/bridge/inventory/inventory.php' in h:
        return ('greenlight', 'api')
    # RideMotive — Next.js App Router + RideMotive backend (Tactical Fleet,
    # tfc.app.ridemotive.com). SSR HTML is empty; data hydrates client-side.
    # Detail pages emit clean schema.org Car JSON-LD once rendered. Scanner
    # must run all fetches through FlareSolverr (preferred_tier='flaresolverr').
    # Fingerprint: 'omniscience.ridemotive.com' is in every page's analytics
    # bundle — check it BEFORE the generic-Next.js path so we get the
    # ridemotive handler instead of falling through to 'custom'.
    if 'app.ridemotive.com' in h or 'omniscience.ridemotive.com' in h:
        return ('ridemotive', 'jsonld+flaresolverr')
    # JAZEL_FINGERPRINT_2026_06_11: Jazel 'auto5' sites are WordPress-based
    # but render the SRP server-side with per-card data-vehicle JSON blobs.
    # Detect BEFORE the generic wordpress branch so the jazel SRP fast-path
    # (platform=='jazel') fires -- otherwise they fall to the universal crawl
    # and hang 10+ min returning 0 (as Tamiami Ford dealer 98 did). 200+ jazel
    # markers on a real jazel homepage, 0 on DealerOn dealers (verified).
    if 'jazel' in h or 'auto5' in h or 'jzlrocket' in h:
        return ('jazel', 'srp-data-vehicle')
    if 'wp-content' in h or 'wp-includes' in h:
        return ('wordpress', 'jsonld+html')
    if 'vinsolutions' in h:
        return ('vinsolutions', 'jsonld')
    if 'data-class="vehicle"' in h:
        return ('jsonvehicle', 'json_script')
    return ('custom', 'sitemap+jsonld')


def _build_jsonvehicle_config(base_url):
    """Default config for schema.org/Vehicle JSON-in-<script> sites (HGreg-type).
    Scopes to one rooftop via the /used-car-<slug> URL (these sites paginate a GLOBAL feed)."""
    import re as _re
    cfg = {
        "platform": "jsonvehicle",
        "inventory_source": {
            "type": "json_script",
            "url_template": "{base}?page={page}",
            "script_selector": 'script[data-class="Vehicle"]',
            "pagination": {"type": "page_param", "start": 1, "max_pages": 60},
        },
        "extraction": {
            "list_path": "",
            "fields": {
                "vin": "VIN", "year": "Year", "make": "Make", "model": "BestModelName",
                "trim": "Trim", "price": "Price", "miles": "Odometer",
                "exterior_color": "ExteriorColor", "interior_color": "InteriorColor",
                "stock_number": "Stock", "photos": "MainImage",
            },
        },
    }
    m = _re.search(r'/used-car-([a-z0-9\-]+)', (base_url or '').lower())
    if m:
        cfg["inventory_source"]["record_filter"] = {"path": "FullLocation.city_slug", "equals": m.group(1)}
    return cfg


# ── AAN platform — JSON API extractor ───────────────────────────────────
def _fetch_aan_feed(base_url, sess):
    """Return the AAN inventory JSON list, trying both endpoint variants.

    Older AAN sites (Marino, Marshall Goldman): GET /api/cars → JSON array.
    Newer AAN sites (Nuccio, post-2024 aanWordpress build): /api/cars is
    routed to a 404 PNG and the live feed is at
    /isapi_xml.php?module=inventory&pageID=<N>, where <N> is hardcoded
    per-dealer inside scripts/inventory_base.js' caller — the dealer's
    /inventory/ page exposes it as `let pageID = 'NNN'`. Field shape is
    identical, so _normalize_aan_vehicle handles either source.

    Returns a list (possibly empty) on success, None when neither endpoint
    yields valid JSON — matches the contract fetch_aan_inventory expects.
    """
    code, _f, body = fetch(urljoin(base_url, '/api/cars'), sess)
    if code == 200 and body:
        try:
            d = _json.loads(body)
            if isinstance(d, list):
                return d
        except Exception:
            pass
    # Fallback: extract pageID from /inventory/ HTML and hit isapi_xml.
    code, _f, body = fetch(urljoin(base_url, '/inventory/'), sess)
    if code != 200 or not body:
        return None
    text = body if isinstance(body, str) else body.decode('utf-8', 'ignore')
    m = re.search(r"pageID\s*=\s*['\"](\d+)['\"]", text)
    if not m:
        return None
    page_id = m.group(1)
    code, _f, body = fetch(
        urljoin(base_url, f'/isapi_xml.php?module=inventory&pageID={page_id}'),
        sess)
    if code != 200 or not body:
        return None
    try:
        d = _json.loads(body)
    except Exception:
        return None
    return d if isinstance(d, list) else None


def fetch_aan_inventory(base_url, sess):
    """AAN dealers expose the full live inventory at /api/cars as a JSON array.
    One call returns everything — VIN, YMM, trim, colors, miles, price, photos,
    sold/pending flags. No per-VDP fetching needed.

    Returns a list of normalized vehicle dicts, or None if the API is unreachable.
    """
    data = _fetch_aan_feed(base_url, sess)
    if data is None:
        return None
    if not isinstance(data, list):
        return None

    vehicles = []
    for item in data:
        if not isinstance(item, dict):
            continue
        v = _normalize_aan_vehicle(item, base_url)
        if v:
            vehicles.append(v)

    # Title-tag price fallback. AAN's /api/cars is occasionally inconsistent —
    # some vehicles come back with price=null or 0 even though the VDP shows
    # the price. The VDP <title> always contains "For Sale ($XXX,XXX)", so
    # for any active vehicle missing a price, fetch the VDP and parse it out.
    # Only triggers for available vehicles with a URL — sold/pending skipped.
    for v in vehicles:
        if v.get('price'):
            continue
        if v.get('_aan_sold') or v.get('_aan_pending') or v.get('_aan_coming_soon'):
            continue
        url = v.get('url')
        if not url:
            continue
        try:
            code, _f, body = fetch(url, sess)
            if code != 200 or not body:
                continue
            recovered = _aan_price_from_title(body)
            if recovered:
                v['price'] = recovered
                print(f'  [aan] price recovered from title: {v.get("vin") or url[-30:]} → ${recovered:,}', flush=True)
        except Exception:
            continue

    return vehicles


def _aan_price_from_title(html):
    """Parse the AAN VDP <title> tag for a price. Marino-style format:
        <title>Used 2017 Ferrari California T For Sale ($134,900) | ...</title>
    Returns int price or None."""
    m = re.search(r'<title[^>]*>([^<]+)</title>', html, re.I)
    if not m:
        return None
    title = m.group(1)
    pm = re.search(r'\(\s*\$\s*([\d,]+)\s*\)', title)
    if not pm:
        return None
    try:
        return int(pm.group(1).replace(',', ''))
    except ValueError:
        return None


def _normalize_aan_vehicle(item, base_url):
    vin = (item.get('vin') or '').strip().upper()
    url_link = item.get('url_link') or ''
    url = urljoin(base_url, url_link) if url_link else None
    sold_raw = (item.get('sold') or '').strip().lower()
    pending = str(item.get('pending_sale') or '0').strip() == '1'
    coming_soon = str(item.get('coming_soon') or '0').strip() == '1'

    def _int(k):
        raw = item.get(k)
        if raw in (None, '', '0'):
            return None
        try:
            return int(str(raw).replace(',', '').replace('$', '').strip())
        except (ValueError, TypeError):
            return None

    year_raw = item.get('year')
    year = None
    if year_raw and str(year_raw).isdigit():
        year = int(year_raw)

    # AAN trim cleanup. Multi-location chains (e.g. Marshall Goldman) stuff
    # marketing copy into the trim field as "<real-trim> -<sales pitch>".
    # Examples: "GT3 -Full Matte PPF, Carbon Roof", "-Daytona Seats, Low Miles".
    # Pattern is always SPACE+DASH as the separator, never a legit trim hyphen
    # (Marino verified to have zero ' -' trims; hyphenated badges like
    # "Mercedes-Maybach" live in `make`, not `trim`). Strip from ' -' onward.
    raw_trim = (item.get('trim') or '').strip()
    if ' -' in raw_trim:
        raw_trim = raw_trim.split(' -', 1)[0].strip()
    elif raw_trim.startswith('-'):
        # Trim was pure marketing copy with no real trim prefix — drop it.
        raw_trim = ''

    photos = []
    img = item.get('image_link')
    if img:
        photos.append(img)

    # AAN's image_link includes a Unix-timestamp suffix (e.g.,
    # "...-1533753420.jpg") that = when the dealer first uploaded the photo,
    # which is a strong proxy for vehicle intake. Mine it here since the AAN
    # path skips extract_vehicle (which handles this for universal dealers).
    source_added_at = _extract_photo_timestamp(photos)

    out = {
        'vin': vin,
        'year': year,
        'make': (item.get('make') or '').strip() or None,
        'model': (item.get('model') or '').strip() or None,
        'trim': raw_trim or None,
        'ext_color': (item.get('ext_color') or '').strip() or None,
        'int_color': (item.get('int_color') or '').strip() or None,
        'body_style': (item.get('body') or '').strip() or None,
        'stock_number': (item.get('stockno') or '').strip() or None,
        'mileage': _int('mileage'),
        'price': _int('price'),
        'url': url,
        'photo_url': img,
        'photos': photos,
        'source_added_at': source_added_at,
        # Multi-location AAN dealers (e.g. Marshall Goldman: Cleveland/Beverly
        # Hills/Maryland/Newport Beach) tag each vehicle with its store. Single
        # source of truth for per-store dashboards if we later split the chain
        # into separate dealer cards.
        'location': (item.get('location') or '').strip() or None,
        # Direct signals from the API — sold/pending flags we can trust.
        '_aan_sold': sold_raw == 'sold',
        '_aan_pending': pending,
        '_aan_coming_soon': coming_soon,
    }
    if not (out.get('vin') or out.get('make') or out.get('year')):
        return None
    return out


# ── Greenlight Automotive Solutions platform extractor ─────────────────
# Custom React-CRA SPA shipped by greenlightautomotivesolutions.com (Brooklyn
# agency). Frontend hits a per-origin `/bridge/inventory/inventory.php` PHP
# endpoint with `?dealership_id=<slug>&sort_inventory=true` and gets back the
# entire inventory as plain JSON — no auth, no pagination, no FlareSolverr
# needed. dealership_id is embedded in the JS bundle as `dealership_id=<slug>`
# (e.g. `wholesale-262` for Mark Motors). We auto-extract on first scan and
# cache it in dealers.scrape_config->>'dealership_id' to avoid the bundle
# round-trip on every run.

_GREENLIGHT_BUNDLE_RE = re.compile(
    r'<script[^>]+src=["\']([^"\']*static/js/main\.[^"\']+\.js)["\']', re.I)
# Bundle stores it as an object literal (`dealership_id:"wholesale-262"`)
# in the axios params block — NOT as a URL query string. Anchor near the
# `dealership_id` token so we don't pick up unrelated slug-shaped strings
# elsewhere in the 2MB bundle (e.g. CSS class names, asset hashes).
_GREENLIGHT_ID_RE = re.compile(
    r'dealership_id\s*[:=]\s*["\']([a-z]+-\d{2,6})["\']', re.I)


def _greenlight_dealership_id(base_url, sess):
    """Find the dealership_id slug for this Greenlight-hosted dealer by
    pulling the main bundle and grepping for the inventory.php query string.
    Returns the slug (e.g. 'wholesale-262') or None."""
    code, _f, body = fetch(base_url, sess)
    if code != 200 or not body:
        return None
    m = _GREENLIGHT_BUNDLE_RE.search(body)
    if not m:
        return None
    bundle_url = urljoin(base_url, m.group(1))
    code, _f, bjs = fetch(bundle_url, sess)
    if code != 200 or not bjs:
        return None
    bm = _GREENLIGHT_ID_RE.search(bjs)
    return bm.group(1) if bm else None


def fetch_greenlight_inventory(base_url, sess, dealership_id=None):
    """Greenlight bridge API. Returns a list of normalized vehicle dicts,
    or None if the API isn't reachable."""
    if not dealership_id:
        dealership_id = _greenlight_dealership_id(base_url, sess)
        if not dealership_id:
            return None
    api_url = urljoin(
        base_url,
        f'/bridge/inventory/inventory.php?dealership_id={dealership_id}'
        f'&sort_inventory=true')
    code, _f, body = fetch(api_url, sess)
    if code != 200 or not body:
        return None
    try:
        data = _json.loads(body)
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    vehicles = []
    for item in data:
        if not isinstance(item, dict):
            continue
        v = _normalize_greenlight_vehicle(item, base_url)
        if v:
            vehicles.append(v)
    return vehicles


def _normalize_greenlight_vehicle(item, base_url):
    vin = (item.get('vin') or '').strip().upper()

    def _int(k):
        raw = item.get(k)
        if raw in (None, '', '0'):
            return None
        try:
            return int(str(raw).replace(',', '').replace('$', '').strip())
        except (ValueError, TypeError):
            return None

    year_raw = item.get('year')
    year = int(year_raw) if year_raw and str(year_raw).isdigit() else None

    # image_urls is a CSV string with embedded double-quotes per record:
    #   "\"https://...jpg,https://...jpg\""
    # Per the React bundle's own parse: strip wrapping quotes, then split(',').
    photos = []
    raw_imgs = item.get('image_urls') or ''
    if isinstance(raw_imgs, str) and raw_imgs:
        clean = raw_imgs.strip().strip('"').replace('"', '')
        photos = [p.strip() for p in clean.split(',') if p.strip()]
    elif isinstance(raw_imgs, list):
        photos = [str(p) for p in raw_imgs if p]

    # VDP URL: prefer the API's prebuilt vdp_url slug; fall back to the React
    # convention `used-<vin>` when missing.
    vdp_slug = (item.get('vdp_url') or '').strip()
    if not vdp_slug and vin:
        vdp_slug = f'used-{vin}'
    url = urljoin(base_url, f'/vehicle-details/{vdp_slug}') if vdp_slug else None

    out = {
        'vin': vin,
        'year': year,
        'make': (item.get('make') or '').strip() or None,
        'model': (item.get('model') or '').strip() or None,
        'trim': (item.get('trim') or '').strip() or None,
        'ext_color': (item.get('exterior_color') or '').strip() or None,
        'int_color': (item.get('interior_color') or '').strip() or None,
        'body_style': (item.get('body_style') or '').strip() or None,
        'stock_number': (item.get('stockno') or '').strip() or None,
        'mileage': _int('mileage'),
        'price': _int('price') or _int('listing_price'),
        'url': url,
        'photo_url': photos[0] if photos else None,
        'photos': photos,
        'location': (item.get('location') or '').strip() or None,
    }
    if not (out.get('vin') or (out.get('make') and out.get('year'))):
        return None
    return out


# ── RideMotive platform extractor (Algolia-backed) ─────────────────────
# RideMotive (tacticalfleet.com and the 282K-vehicle network behind
# tfc.app.ridemotive.com) ships search-only Algolia credentials in the SSR
# HTML. One POST per location yields the entire dealer's inventory with
# 160+ structured fields. Locations are derived from VDP URL suffixes:
# tacticalfleet.com sitemap entries end in `-2358` (Charlotte) or `-2359`
# (Dallas) — that trailing integer IS the Algolia dealer_id filter.
# Falls back to discover_via_ridemotive (FlareSolverr render) if Algolia
# is unreachable for any reason.

_RIDEMOTIVE_ALGOLIA_APP = 'G58LKO3ETJ'
_RIDEMOTIVE_ALGOLIA_KEY = 'cc3dce06acb2d9fc715bc10c9a624d80'
_RIDEMOTIVE_INDEX = 'production-inventory-global_price_desc'
_RIDEMOTIVE_DEALER_RE = re.compile(r'/inventory/[^/<"\s]+-(\d{3,5})(?=[<"\s])')


def _ridemotive_dealer_ids(base_url, sess):
    """Discover the set of Algolia dealer_ids for this site by scanning the
    sitemap for VDP URLs and extracting the trailing -NNN integer. Returns
    a set of ints (usually 1-3 per site)."""
    ids = set()
    try:
        code, _f, body = fetch(urljoin(base_url, '/sitemap.xml'), sess)
        if code != 200 or not body:
            return ids
        for m in _RIDEMOTIVE_DEALER_RE.finditer(body):
            try:
                ids.add(int(m.group(1)))
            except ValueError:
                continue
    except Exception:
        pass
    return ids


def fetch_ridemotive_inventory(base_url, sess):
    """Pull all live inventory across this site's dealer_ids via Algolia.
    Returns a list of normalized vehicle dicts, or None if Algolia call fails.
    """
    dealer_ids = _ridemotive_dealer_ids(base_url, sess)
    if not dealer_ids:
        return None
    algolia_url = (f'https://{_RIDEMOTIVE_ALGOLIA_APP}-dsn.algolia.net'
                   f'/1/indexes/{_RIDEMOTIVE_INDEX}/query')
    headers = {
        'X-Algolia-Application-Id': _RIDEMOTIVE_ALGOLIA_APP,
        'X-Algolia-API-Key':       _RIDEMOTIVE_ALGOLIA_KEY,
        'Content-Type':            'application/json',
        'User-Agent':              sess.headers.get('User-Agent', 'Mozilla/5.0'),
    }
    vehicles = []
    seen_vins = set()
    for did in sorted(dealer_ids):
        # hitsPerPage=1000 returns the whole dealership in one shot; no site
        # in the network currently has >1000 listings.
        payload = {'params': f'hitsPerPage=1000&page=0&filters=dealer_id%3D{did}'}
        try:
            r = requests.post(algolia_url, headers=headers, json=payload,
                              timeout=20)
            j = r.json()
        except Exception as e:
            print(f'  [ridemotive] dealer {did} algolia error: {e}', flush=True)
            continue
        hits = j.get('hits') or []
        for h in hits:
            v = _normalize_ridemotive_vehicle(h, base_url, did)
            if not v:
                continue
            vin = v.get('vin') or ''
            if vin and vin in seen_vins:
                continue
            if vin:
                seen_vins.add(vin)
            vehicles.append(v)
    return vehicles


def _normalize_ridemotive_vehicle(hit, base_url, dealer_id):
    vin = (hit.get('vin') or '').strip().upper()

    def _int(v):
        if v in (None, '', '0'):
            return None
        try:
            return int(str(v).replace(',', '').replace('$', '').strip())
        except (ValueError, TypeError):
            return None

    year = _int(hit.get('make_year'))

    # VDP URL slug: mirror the site's own rule. Hyphen-join non-empty
    # fields, replace anything outside [A-Za-z0-9-] with underscore.
    parts = [hit.get(k) for k in
             ('car_condition', 'make_year', 'make', 'model', 'car_trim', 'vin')]
    raw = '-'.join(str(p) for p in parts if p)
    slug = re.sub(r'[^a-zA-Z0-9-]', '_', raw)
    # Site uses trailing -<dealer_id> on every VDP URL.
    url = urljoin(base_url, f'/inventory/{slug}-{dealer_id}')

    # images is a list of CDN keys; full URL is images.app.ridemotive.com/<key>
    imgs = hit.get('images') or []
    photos = [f'https://images.app.ridemotive.com/{k}' for k in imgs
              if isinstance(k, str) and k]

    out = {
        'vin': vin,
        'year': year,
        'make': (hit.get('make') or '').strip() or None,
        'model': (hit.get('model') or '').strip() or None,
        'trim': (hit.get('car_trim') or '').strip() or None,
        'ext_color': (hit.get('exterior_color') or hit.get('ext_color') or '').strip() or None,
        'int_color': (hit.get('interior_color') or hit.get('int_color') or '').strip() or None,
        'body_style': (hit.get('body_style') or hit.get('body') or '').strip() or None,
        'stock_number': (hit.get('stock_number') or '').strip() or None,
        'mileage': _int(hit.get('odometer')) or _int(hit.get('mileage')),
        'price': _int(hit.get('price')),
        'url': url,
        'photo_url': photos[0] if photos else None,
        'photos': photos,
        'location': (hit.get('dealership') or '').strip() or None,
    }
    if not (out.get('vin') or (out.get('make') and out.get('year'))):
        return None
    return out


# ── Dealer.com platform extractor ───────────────────────────────────────
# EW only sources pre-owned inventory — /new-inventory/ and /new/ VDPs are
# excluded at both the list-page and VDP-regex level so we don't waste a
# FlareSolverr fetch on new vehicles we can't wholesale.
DEALERCOM_LIST_PATHS = (
    '/used-inventory/index.htm',
    '/certified-inventory/index.htm',
    '/pre-owned-inventory/index.htm',
    '/certified-pre-owned-inventory/index.htm',
    '/featured-vehicles/index.htm',
)
DEALERCOM_VDP_RE = re.compile(
    r'href=["\']([^"\']*/(?:used|certified|preowned|pre-owned)/[^"\']+\.htm)["\']',
    re.I,
)


def discover_via_dealer_com(base_url, sess, max_pages_per_list=10, per_page=24, list_paths=None):
    """Dealer.com-specific VDP discovery.

    Sitemap on dealer.com only exposes list pages, not per-VDP URLs. This walks
    /new-inventory, /used-inventory, /certified-inventory, /pre-owned-inventory,
    and /featured-vehicles — paginating via ?start=N — and extracts VDP URLs
    matching /new/, /used/, /certified/, or /preowned/ paths ending in .htm.

    Fetches go through whatever tier the scanner set (usually FlareSolverr for
    dealer.com because Akamai blocks plain curl).
    """
    found = set()
    netloc = urlparse(base_url).netloc.lower().lstrip('www.')

    # DEALERCOM_LIST_PATHS_OVERRIDE_2026_05_29: per-dealer list-path override
    # lets a multi-location Dealer.com site (e.g. Lifted Trucks Hurst) crawl
    # only its location-scoped used-inventory listing instead of the site-wide one.
    for path in (list_paths or DEALERCOM_LIST_PATHS):
        prev_count = -1
        for page_idx in range(max_pages_per_list):
            start = page_idx * per_page
            url = urljoin(base_url, path)
            if start:
                url += f'?start={start}'
            code, _f, body = fetch(url, sess)
            if code != 200 or not body:
                break
            page_hits = set()
            for m in DEALERCOM_VDP_RE.finditer(body):
                link = urljoin(base_url, m.group(1)).split('#', 1)[0]
                if urlparse(link).netloc.lower().lstrip('www.') != netloc:
                    continue
                page_hits.add(link)
            if not page_hits:
                break  # empty page — past the last pagination slot
            new_count = len(found | page_hits)
            if new_count == prev_count:
                break  # this page added nothing — duplicate / cycled
            found |= page_hits
            prev_count = new_count
            if len(found) >= CRAWL_MAX_URLS:
                return sorted(found)[:CRAWL_MAX_URLS]
    return sorted(found)


# ── Dealer.com ws-inv-data getInventory JSON API ───────────────
# DDC_GETINVENTORY_2026_05_29: Dealer.com listing pages render inventory from a
# public POST API (no auth) at {base}/api/widget/ws-inv-data/getInventory. One
# POST returns up to 100 vehicles with full data; paginate via
# inventoryParameters.start=[offset]. Multi-location sites (e.g. Lifted Trucks)
# return the home rooftop PLUS nearby/transfer stores boosted by listing.boost
# .order; account_filter scopes to one physical rooftop. Replaces the slow,
# Akamai-throttled per-VDP crawl for dealer.com stores.
def _normalize_dealercom_getinv(it, base_url):
    """Convert one getInventory hit to our internal vehicle dict (AAN-shaped)."""
    cond_raw = (it.get('condition') or it.get('type') or '').strip().lower()
    if cond_raw in ('new', 'demo', 'loaner', 'courtesy'):
        return None  # EW sources pre-owned + certified only
    vin = (it.get('vin') or '').strip().upper()
    if not vin:
        return None
    miles = None
    for a in (it.get('attributes') or []):
        if isinstance(a, dict) and a.get('name') == 'odometer':
            digits = re.sub(r'[^0-9]', '', str(a.get('value') or ''))
            miles = int(digits) if digits else None
            break
    price = None
    pr = it.get('pricing') or {}
    cands = [pr.get('retailPrice')] + [d.get('value') for d in (pr.get('dprice') or []) if isinstance(d, dict)]
    for cand in cands:
        if cand:
            digits = re.sub(r'[^0-9]', '', str(cand))
            if digits:
                price = int(digits)
                break
    source_added_at = None
    di = (it.get('inventoryDate') or '').strip()
    if di:
        try:
            from datetime import datetime as _dt
            source_added_at = _dt.strptime(di, '%m/%d/%Y').replace(tzinfo=timezone.utc).isoformat()
        except Exception:
            pass
    imgs = it.get('images') or []
    photo = imgs[0].get('uri') if imgs and isinstance(imgs[0], dict) else None
    year = it.get('year')
    try:
        year = int(year)
    except (ValueError, TypeError):
        year = None
    return {
        'vin': vin, 'year': year,
        'make': (it.get('make') or '').strip() or None,
        'model': (it.get('model') or '').strip() or None,
        'trim': (it.get('trim') or '').strip() or None,
        'body_style': (it.get('bodyStyle') or '').strip() or None,
        'mileage': miles, 'price': price,
        'condition': 'cpo' if it.get('certified') else 'used',
        'stock_number': (it.get('stockNumber') or '').strip() or None,
        'url': urljoin(base_url, it.get('link')) if it.get('link') else None,
        'photo_url': photo, 'photos': [photo] if photo else [],
        'source_added_at': source_added_at,
        '_aan_sold': False, '_aan_pending': False, '_aan_coming_soon': False,
    }


def fetch_dealercom_getinventory(base_url, sess, cfg):
    """Dealer.com getInventory JSON-API fetch. cfg = {'body': <POST template>,
    'account_filter': <accountId or None>}. Returns a list of normalized used/cpo
    vehicle dicts (rooftop-scoped if account_filter set), or None on hard failure
    so the caller can decide whether to abort vs fall through."""
    import json as _j
    body0 = (cfg or {}).get('body')
    if not body0:
        return None
    acct = (cfg or {}).get('account_filter')
    _u = urlparse(base_url)
    _root = f'{_u.scheme}://{_u.netloc}'
    url = _root + '/api/widget/ws-inv-data/getInventory'
    headers = {'Content-Type': 'application/json', 'Referer': base_url,
               'Accept': '*/*'}
    out, offset, total, PAGE = [], 0, None, 100
    for _page in range(80):  # hard cap 8000 vehicles
        b = _j.loads(_j.dumps(body0))  # deep copy
        b.setdefault('preferences', {})['pageSize'] = str(PAGE)
        ip = dict(b.get('inventoryParameters') or {})
        ip['start'] = [str(offset)]
        b['inventoryParameters'] = ip
        try:
            r = requests.post(url, json=b, headers=headers, timeout=30)
            if r.status_code != 200:
                return None if not out else out
            data = r.json()
        except Exception:
            return None if not out else out
        inv = data.get('inventory') or []
        if total is None:
            total = (data.get('pageInfo') or {}).get('totalCount') or 0
        if not inv:
            break
        for it in inv:
            if acct and it.get('accountId') != acct:
                continue
            v = _normalize_dealercom_getinv(it, base_url)
            if v:
                out.append(v)
        offset += PAGE
        if offset >= (total or 0):
            break
    return out


# ── Cars Commerce listings API (newer DealerInspire / group sites) ────────
# CARSCOMMERCE_2026_05_29: POST {api}/api/v1/listings/{ccid}/search with an
# x-api-key. ccid is the per-account id; the response pools the whole group
# (boosted by the store's source_id), so facetFilters.source_id=[N] scopes to
# one physical rooftop. perPage caps ~100; paginate via page. used/cpo only.
def _normalize_carscommerce(it):
    t = (it.get('type') or '').strip().lower()
    if t in ('new', 'demo', 'loaner', 'courtesy'):
        return None
    vin = (it.get('vin') or '').strip().upper()
    if not vin:
        return None
    cond = 'cpo' if ('certif' in t or t == 'ctp') else 'used'
    pr = it.get('pricing') or {}
    price = pr.get('our_price') or pr.get('price') or None
    try:
        price = int(price) if price else None
    except (ValueError, TypeError):
        price = None
    miles = it.get('mileage')
    miles = int(miles) if isinstance(miles, (int, float)) and miles else None
    source_added_at = None
    di = (it.get('date_in_stock') or '').strip()
    if di:
        try:
            from datetime import datetime as _dt
            source_added_at = _dt.fromisoformat(di.replace('Z', '+00:00')).isoformat()
        except Exception:
            pass
    media = it.get('media') or {}
    imgs = media.get('images') if isinstance(media, dict) else None
    imgs = [i for i in (imgs or []) if isinstance(i, str)]
    year = it.get('year')
    try:
        year = int(year)
    except (ValueError, TypeError):
        year = None
    return {
        'vin': vin, 'year': year,
        'make': (it.get('make') or '').strip() or None,
        'model': (it.get('model') or '').strip() or None,
        'trim': (it.get('trim') or '').strip() or None,
        'body_style': None, 'mileage': miles, 'price': price, 'condition': cond,
        'stock_number': (it.get('stock') or '').strip() or None,
        'url': it.get('vdp_url') or None,
        'photo_url': (imgs[0] if imgs else None), 'photos': imgs,
        'source_added_at': source_added_at,
        '_aan_sold': False, '_aan_pending': False, '_aan_coming_soon': False,
    }


def _carscommerce_used_types(url, headers, src):
    """Probe the live Cars Commerce `type` facet and return every non-New
    label (Used / Pre-Owned / Certified / Certified Pre-Owned / CPO / ...),
    so the main query never silently drops cars on a store that uses a
    different used-label convention. Returns None on failure -> caller then
    fetches unfiltered and relies on _normalize_carscommerce to drop
    new/demo/loaner/courtesy. CARSCOMMERCE_TYPE_AUTO_2026_06_18."""
    probe = {'perPage': 1, 'page': 1, 'facets': ['type']}
    if src:
        probe['facetFilters'] = {'source_id': [str(src)]}
    try:
        r = requests.post(url, json=probe, headers=headers, timeout=20)
        if r.status_code != 200:
            return None
        facets = ((r.json().get('data') or {}).get('facets')) or []
    except Exception:
        return None
    vals = []
    for f in facets:
        if isinstance(f, dict) and f.get('name') == 'type':
            for v in (f.get('values') or []):
                k = (v.get('key') or '').strip()
                if k and 'new' not in k.lower():
                    vals.append(k)
    return vals or None


def fetch_carscommerce_inventory(cfg):
    """cfg = {ccid, api_key, source_id(optional rooftop filter), origin}. Returns
    list of normalized used/cpo dicts, or None on hard failure."""
    import json as _j
    ccid = (cfg or {}).get('ccid')
    key = (cfg or {}).get('api_key')
    src = (cfg or {}).get('source_id')
    origin = (cfg or {}).get('origin') or ''
    if not (ccid and key):
        return None
    url = f'https://websites-search.api.carscommerce.inc/api/v1/listings/{ccid}/search'
    headers = {'x-api-key': key, 'Content-Type': 'application/json', 'Accept': '*/*'}
    if origin:
        headers['Origin'] = origin
        headers['Referer'] = origin.rstrip('/') + '/'
    # CARSCOMMERCE_TYPE_AUTO_2026_06_18: the old hard-coded type allowlist
    # ('Used','Certified Used','CTP') silently dropped EVERY car on stores
    # that label used inventory 'Pre-Owned'/'Certified Pre-Owned' (the Hendrick
    # group rooftops, e.g. BMW McKinney #89, Mall of GA MINI #91) -> 0 vehicles
    # -> scan aborted. Probe the live `type` facet and include every non-New
    # label instead. The `type_slug` AND-constraint is dropped (its slug also
    # varies by convention and was excluding Pre-Owned). _normalize_carscommerce
    # remains the final guard (drops new/demo/loaner/courtesy regardless).
    used_types = _carscommerce_used_types(url, headers, src)
    base_body = {
        'perPage': 100,
        'facets': ['type_slug', 'year', 'make', 'model_slug', 'trim_slug',
                   'miles', 'body_type', 'low_price'],
        'filters': {'status': ['publish', 'modified', 'pend-sale']},
        'facetFilters': {},
        'requestedFields': ['vin', 'stock', 'type', 'year', 'make', 'model',
                            'trim', 'date_in_stock', 'mileage', 'vdp_url',
                            'source_id', 'pricing', 'media'],
    }
    if used_types:
        base_body['facetFilters']['type'] = used_types
    if src:
        base_body['facetFilters']['source_id'] = [str(src)]
    out, page, total = [], 1, None
    for _ in range(80):
        b = _j.loads(_j.dumps(base_body))
        b['page'] = page
        try:
            r = requests.post(url, json=b, headers=headers, timeout=30)
            if r.status_code != 200:
                return None if not out else out
            data = r.json()
        except Exception:
            return None if not out else out
        block = data.get('data') or {}
        L = block.get('listings') or []
        if total is None:
            total = block.get('total_vehicle_count') or 0
        if not L:
            break
        for it in L:
            v = _normalize_carscommerce(it)
            if v:
                out.append(v)
        if page * 100 >= (total or 0):
            break
        page += 1
    return out


# ── Jazel (jazelauto / "auto5" / jzl) WordPress+Elementor platform ───────
# JAZEL_DATAVEHICLE_2026_06_11: Jazel-built dealer sites (Tamiami Ford, et al)
# render their SRP server-side with each vehicle card carrying a complete,
# HTML-escaped JSON record in a `data-vehicle="{...}"` attribute. No DDC
# /ws-inv-data API, no dealer.com markers — fingerprint is the jazel/auto5/jzl
# bundle on a WordPress homepage. One plain GET per SRP page (12 cars/page)
# returns full data (vin/year/make/model/trim/price/mileage/color/condition),
# paginated via `<list>/srp-page-N/`. Replaces the universal per-VDP crawl
# that discovered ~781 generic URLs and hung 10+ min returning 0 vehicles.
_JAZEL_DATA_VEHICLE_RE = re.compile(r'data-vehicle="(\{.*?\})"\s', re.S)
JAZEL_USED_LIST_PATH = '/inventory/used-vehicles/'


def _normalize_jazel(o, base_url):
    """Convert one Jazel data-vehicle JSON object to our AAN-shaped dict.
    Skips 'new' condition (EW sources pre-owned + certified only)."""
    vin = (o.get('vin') or '').strip().upper()
    if not vin:
        return None
    cond_raw = (o.get('condition') or '').strip().lower()
    if cond_raw in ('new', 'demo', 'loaner', 'courtesy'):
        return None
    # `conditions` is a list like ["Gold Certified,Certified,Used"]; treat any
    # Certified marker as CPO, else used.
    conds = o.get('conditions')
    conds_s = (' '.join(conds) if isinstance(conds, list) else str(conds or '')).lower()
    condition = 'cpo' if 'certif' in conds_s else 'used'
    price = None
    pr = o.get('price')
    if pr is not None:
        digits = re.sub(r'[^0-9]', '', str(pr))
        price = int(digits) if digits else None
    miles = o.get('mileage')
    if miles is not None and not isinstance(miles, int):
        digits = re.sub(r'[^0-9]', '', str(miles))
        miles = int(digits) if digits else None
    year = o.get('year')
    try:
        year = int(year)
    except (ValueError, TypeError):
        year = None
    return {
        'vin': vin, 'year': year,
        'make': (o.get('make') or '').strip() or None,
        'model': (o.get('model') or '').strip() or None,
        'trim': (o.get('trim') or '').strip() or None,
        'body_style': None,
        'mileage': miles, 'price': price,
        'ext_color': (o.get('exterior_color') or '').strip() or None,
        'condition': condition,
        'stock_number': (o.get('stockNumber') or '').strip() or None,
        # The data-vehicle blob carries no VDP link, but Jazel VDP URLs are
        # /vehicle/<VIN>/<slug>/ and the slug is cosmetic — /vehicle/<VIN>/
        # resolves. Gives the upsert a stable URL dedup key + a working link.
        'url': urljoin(base_url, '/vehicle/%s/' % vin),
        'photo_url': None, 'photos': [],
        'source_added_at': None,
        '_aan_sold': False, '_aan_pending': False, '_aan_coming_soon': False,
    }


def fetch_jazel_inventory(base_url, sess, list_path=None, max_pages=80):
    """Jazel SRP data-vehicle fetch. Walks `<list_path>` + `srp-page-N/` and
    parses the inline `data-vehicle` JSON on each card. Returns a list of
    normalized used/cpo dicts, or None on a hard failure (page-1 fetch error)
    so the caller can decide abort-vs-fall-through. An empty list means the
    SRP loaded but had no used vehicles."""
    list_path = list_path or JAZEL_USED_LIST_PATH
    out, seen = [], set()
    for page in range(1, max_pages + 1):
        if page == 1:
            url = urljoin(base_url, list_path)
        else:
            url = urljoin(base_url, list_path.rstrip('/') + '/srp-page-%d/' % page)
        code, _f, body = fetch(url, sess)
        if code != 200 or not body:
            # Page-1 failure is a hard failure (let caller abort/escalate);
            # a later page just means we've walked off the end of pagination.
            if page == 1:
                return None
            break
        blobs = _JAZEL_DATA_VEHICLE_RE.findall(body)
        if not blobs:
            break  # past the last populated SRP page
        added = 0
        for b in blobs:
            try:
                o = _json.loads(_html.unescape(b))
            except Exception:
                continue
            v = _normalize_jazel(o, base_url)
            if not v or v['vin'] in seen:
                continue
            seen.add(v['vin'])
            out.append(v)
            added += 1
        if added == 0:
            break  # page rendered cards but all dupes — pagination cycled
        if len(out) >= CRAWL_MAX_URLS:
            break
    return out


# ── DealerInspire (Cox) platform ─────────────────────────────────────────
# DealerInspire WordPress sites expose a single inventory_sitemap endpoint
# that returns every VDP URL on the lot in one HTML response. Confirmed on
# ferrarifl.com: /dealer-inspire-inventory/inventory_sitemap → 156 unique
# /inventory/<slug>-<vin>/ URLs in a 47KB body. Slugs are prefixed
# `new-`, `pre-owned-`, or `certified-pre-owned-`. EW only sources used,
# so we filter to the latter two.
DEALER_INSPIRE_SITEMAP = '/dealer-inspire-inventory/inventory_sitemap'
DEALER_INSPIRE_VDP_RE = re.compile(
    r'https?://[^/\s"\'<>]+/inventory/(?:pre-owned|certified-pre-owned)[^"\'<>\s)]*',
    re.I,
)


def discover_via_dealer_inspire(base_url, sess):
    """DealerInspire-specific VDP discovery via the inventory_sitemap endpoint.

    One FlareSolverr fetch returns the full lot. Filters out new vehicles
    (slug prefix `new-`) — EW only sources pre-owned + certified-pre-owned.
    """
    netloc = urlparse(base_url).netloc.lower().lstrip('www.')
    url = urljoin(base_url, DEALER_INSPIRE_SITEMAP)
    code, _f, body = fetch(url, sess)
    if code != 200 or not body:
        return []
    found = set()
    for m in DEALER_INSPIRE_VDP_RE.finditer(body):
        link = m.group(0).split('#', 1)[0].rstrip(',.;')
        if urlparse(link).netloc.lower().lstrip('www.') != netloc:
            continue
        found.add(link)
    return sorted(found)[:CRAWL_MAX_URLS]


# ── DealerInspire Algolia fast path ──────────────────────────────────────
# DealerInspire sites embed an Algolia public search key in every page (the
# `mvnAlgSettings` JS object). Algolia's inventory index has every vehicle
# with full metadata (VIN, price, miles, photos, days_in_stock, etc.) and
# returns the entire lot in a single ~3 second request. Replaces 60-minute
# per-VDP scans for sites with 100+ vehicles.
_DI_ALGOLIA_RE = re.compile(
    r'mvnAlgSettings\s*=\s*(\{[^;]+?\})\s*;',
    re.S,
)


def _dealer_inspire_algolia_config(base_url, sess, cached_cfg=None):
    """Pull Algolia appId/apiKey/inventoryIndex from any DealerInspire page.
    Returns dict or None. The settings object is identical site-wide so the
    homepage works fine — no need to hit a heavy listing page.

    `cached_cfg`: previously-discovered cfg from `dealers.scrape_config.algolia`.
    If supplied and complete, returned as-is — FlareSolverr is skipped entirely.
    The Algolia keys are public/browser-callable and rotate rarely, so a cached
    copy stays valid for months. This is the path that makes the fast-path
    100% reliable post-discovery (FlareSolverr is non-deterministic against
    Cloudflare-fronted sites — same call returned 700KB then empty body 15min
    later on 2026-04-27).

    Fetches via plain FlareSolverr (no proxy, no tier honoring). The Algolia
    keys embedded in the HTML are public/browser-callable, so we don't need
    residential IP rotation just to read them. The DealerInspire-default tier
    is `flaresolverr_proxy`, which has been observed returning empty bodies
    on Ferrari Fort Lauderdale — using plain FlareSolverr here gets the JS
    challenge solved without paying the proxy reliability tax.
    """
    if cached_cfg and cached_cfg.get('app_id') and cached_cfg.get('api_key') and cached_cfg.get('index'):
        return {
            'app_id': cached_cfg['app_id'],
            'api_key': cached_cfg['api_key'],
            'index': cached_cfg['index'],
        }

    body = None
    try:
        # NOTE: spec called for a 10s timeout, but plain FlareSolverr on
        # Cloudflare-fronted sites (e.g. ferrarifl.com) consistently takes
        # 25-98s to solve the JS challenge — 10s would make the fast path
        # never succeed. 120s mirrors fetch_flaresolverr (90s) plus margin.
        # The maxTimeout in the payload is FlareSolverr's internal cap; the
        # outer requests timeout is the HTTP transport cap. Both must be
        # generous or the fast path bails before Cloudflare returns.
        resp = requests.post(
            dealer_fetchers.FLARESOLVERR_URL,
            json={
                'cmd': 'request.get',
                'url': base_url,
                'maxTimeout': 60000,
            },
            timeout=120,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if data.get('status') != 'ok':
            return None
        body = ((data.get('solution') or {}).get('response') or '')
    except Exception:
        return None
    if not body or len(body) < 200:
        return None
    m = _DI_ALGOLIA_RE.search(body)
    if not m:
        return None
    try:
        cfg = _json.loads(m.group(1))
    except Exception:
        return None
    if not (cfg.get('appId') and cfg.get('apiKeySearch') and cfg.get('inventoryIndex')):
        return None
    return {
        'app_id': cfg['appId'],
        'api_key': cfg['apiKeySearch'],
        'index': cfg['inventoryIndex'],
    }


def fetch_dealer_inspire_inventory(base_url, sess, cached_cfg=None):
    """DealerInspire fast-path inventory fetch via Algolia.

    Returns `(vehicles, cfg)` on success — cfg is the resolved Algolia
    config the caller can persist to `dealers.scrape_config.algolia` so
    future scans skip the FlareSolverr config-extraction step.
    Returns `(None, None)` if Algolia config can't be resolved or the
    Algolia API errors out.

    Filters out 'New' type — EW only sources pre-owned/certified-pre-owned.
    """
    cfg = _dealer_inspire_algolia_config(base_url, sess, cached_cfg=cached_cfg)
    if not cfg:
        return None, None

    # ROOFTOP_FILTER_2026_05_29: Hendrick (and other group) DI indexes pool a
    # huge shared 'Available for Transfer' nationwide pool on top of the
    # store's own lot. Without scoping, every EW dealer pulls the same ~9k
    # cars instead of its physical inventory. cached_cfg.rooftop_filter is an
    # Algolia facetFilters array (e.g. [["api_id:9022398"]] or
    # [["location:<exact addr>"]]) that scopes to one rooftop. Carry it onto
    # cfg so the persist step below doesn't clobber it.
    rooftop = (cached_cfg or {}).get('rooftop_filter')
    if rooftop:
        cfg['rooftop_filter'] = rooftop

    algolia_url = f"https://{cfg['app_id']}-dsn.algolia.net/1/indexes/{cfg['index']}/query"
    headers = {
        'X-Algolia-Application-Id': cfg['app_id'],
        'X-Algolia-API-Key': cfg['api_key'],
        'Content-Type': 'application/json',
    }
    _params = 'hitsPerPage=1000&page=0'
    if rooftop:
        import urllib.parse as _ul
        _params += '&facetFilters=' + _ul.quote(_json.dumps(rooftop))
    payload = _json.dumps({'params': _params})
    try:
        # Direct Algolia call — bypass FlareSolverr/proxy entirely. Algolia's
        # public search key is meant to be browser-callable, no Cloudflare.
        r = requests.post(algolia_url, headers=headers, data=payload, timeout=30)
        if r.status_code != 200:
            return None, None
        data = r.json()
    except Exception:
        return None, None

    hits = data.get('hits') or []
    vehicles = []
    for h in hits:
        v = _normalize_dealer_inspire_vehicle(h, base_url)
        if v:
            vehicles.append(v)
    return vehicles, cfg


def _normalize_dealer_inspire_vehicle(item, base_url):
    """Convert one Algolia hit to our internal vehicle dict (same shape as
    AAN's _normalize_aan_vehicle so _process_aan can swallow it directly)."""
    vtype = (item.get('type') or '').strip()
    # EW only sources pre-owned + certified — drop everything else.
    if vtype.lower() in ('new', 'demo', 'loaner', 'courtesy'):
        return None

    vin = (item.get('vin') or '').strip().upper()
    url = item.get('link') or None

    def _int_or_none(raw):
        if raw in (None, '', '0', 0):
            return None
        try:
            return int(str(raw).replace(',', '').replace('$', '').strip())
        except (ValueError, TypeError):
            return None

    year = item.get('year')
    if isinstance(year, str) and year.isdigit():
        year = int(year)
    elif not isinstance(year, int):
        year = None

    thumb = item.get('thumbnail') or None
    photos = [thumb] if thumb else []

    # Dealer-declared days-on-lot. Algolia's `date_in_stock` is MM/DD/YYYY.
    # We populate source_added_at directly so age buckets reflect truth from
    # the dealer's DMS, not our first_seen_at scan timestamp.
    source_added_at = None
    dis = item.get('date_in_stock') or ''
    if dis:
        try:
            from datetime import datetime as _dt
            source_added_at = _dt.strptime(dis.strip(), '%m/%d/%Y').replace(
                tzinfo=timezone.utc).isoformat()
        except Exception:
            pass

    out = {
        'vin': vin,
        'year': year,
        'make': (item.get('make') or '').strip() or None,
        'model': (item.get('model') or '').strip() or None,
        'trim': (item.get('trim') or '').strip() or None,
        'ext_color': (item.get('ext_color') or '').strip() or None,
        'int_color': (item.get('int_color') or '').strip() or None,
        'body_style': (item.get('body') or '').strip() or None,
        'stock_number': (item.get('stock') or item.get('stock_no') or '').strip() or None,
        'mileage': _int_or_none(item.get('miles')),
        'price': _int_or_none(item.get('our_price')),
        'msrp': _int_or_none(item.get('msrp')),
        'url': url,
        'photo_url': thumb,
        'photos': photos,
        'source_added_at': source_added_at,
        # CONDITION_2026_05_29: record used vs cpo (New already dropped above).
        'condition': ('cpo' if vtype.lower() in ('ctp','cpo','certified','certified pre-owned')
                      else ('used' if vtype else None)),
        # Reuse AAN flags so the downstream upsert path is unchanged.
        '_aan_sold': False,
        '_aan_pending': False,
        '_aan_coming_soon': False,
    }
    if not (out.get('vin') or out.get('make') or out.get('year')):
        return None
    return out


# ── URL discovery ────────────────────────────────────────────────────────
def discover_via_ridemotive(base_url, sess):
    """RideMotive sites (Tactical Fleet) hide all VDP URLs behind client-side
    React hydration. Sitemap.xml only carries the homepage + a single generic
    /inventory link, no per-VDP entries. The homepage exposes one inventory
    page per location at /cars/{slug}-inventory; rendering each via the
    active fetch tier (must be FlareSolverr) yields a hydrated DOM with
    /inventory/Used-{year}-{make}-{model}-{vin}-{stockid} hrefs.

    Caller is expected to have set _CURRENT_TIER['tier'] = 'flaresolverr'
    before this runs; otherwise the rendered hrefs won't be present.
    """
    found = set()
    code, _f, home = fetch(base_url, sess)
    if code != 200 or not home:
        return []
    # Find every /cars/{slug}-inventory link advertised on the homepage.
    location_paths = set(re.findall(r'href="(/cars/[a-z0-9-]+-inventory)"', home))
    if not location_paths:
        # Fallback: assume single-store site exposes /inventory.
        location_paths = {'/inventory'}
    for path in location_paths:
        url = urljoin(base_url, path)
        # FlareSolverr's request.get returns the DOM as soon as the page's
        # `load` event fires, which can race the SPA's data hydration. If
        # we get 0 VDP hrefs on first try, retry up to 2 more times — each
        # retry hits a fresh Chromium instance, giving the React app another
        # chance to populate the inventory grid before the snapshot.
        per_loc = set()
        for attempt in range(3):
            code, _f, body = fetch(url, sess)
            if code != 200 or not body:
                continue
            for m in re.finditer(r'href="(/inventory/[^"#?]+)"', body):
                per_loc.add(urljoin(base_url, m.group(1)))
            if per_loc:
                break
            time.sleep(2)
        found |= per_loc
    return list(found)


def discover_via_sitemap(base_url, sess):
    """Pull inventory URLs from sitemap(s). Returns list of URLs (deduped).
    Side effect: populates _SITEMAP_LASTMOD[url] with the <lastmod> value per URL
    so extract_vehicle can fall back to it if JSON-LD has no datePosted."""
    found = set()
    sitemap_urls = [
        urljoin(base_url, '/sitemap.xml'),
        urljoin(base_url, '/sitemap_index.xml'),
        urljoin(base_url, '/vehicles-sitemap.xml'),
        urljoin(base_url, '/inventory-sitemap.xml'),
    ]
    queue = list(sitemap_urls)
    visited = set()
    while queue and len(found) < CRAWL_MAX_URLS:
        sm_url = queue.pop(0)
        if sm_url in visited:
            continue
        visited.add(sm_url)
        code, _final, body = fetch(sm_url, sess)
        if code != 200 or not body:
            continue
        try:
            # Handle sitemap index (nested sitemaps) + urlset
            root = ET.fromstring(body)
            # Strip namespace for easier querying
            for el in root.iter():
                el.tag = el.tag.split('}', 1)[-1]
            if root.tag == 'sitemapindex':
                for loc in root.findall('.//loc'):
                    if loc.text:
                        queue.append(loc.text.strip())
            else:
                for url_el in root.findall('.//url'):
                    loc_el = url_el.find('loc')
                    if loc_el is None or not loc_el.text:
                        continue
                    u = loc_el.text.strip()
                    if not _looks_like_vehicle_url(u):
                        continue
                    found.add(u)
                    lastmod_el = url_el.find('lastmod')
                    if lastmod_el is not None and lastmod_el.text:
                        _SITEMAP_LASTMOD[u] = lastmod_el.text.strip()
        except ET.ParseError:
            continue
    return sorted(found)


# Per-scan cache of sitemap <lastmod> values — feeds extract_vehicle when the
# dealer's JSON-LD doesn't include a datePosted. Cleared at start of each scan
# via DealerScanner.run() → _SITEMAP_LASTMOD.clear().
_SITEMAP_LASTMOD = {}


def discover_via_crawl(base_url, sess, max_pages=15):
    """Fallback: crawl homepage + inventory list pages for vehicle detail URLs."""
    found = set()
    homepage = base_url.rstrip('/') + '/'
    to_visit = [homepage]
    visited = set()
    netloc = urlparse(base_url).netloc.lower().lstrip('www.')
    while to_visit and len(visited) < max_pages:
        url = to_visit.pop(0)
        if url in visited:
            continue
        visited.add(url)
        code, _f, body = fetch(url, sess)
        if code != 200 or not body:
            continue
        # Coarse href extraction — no bs4 dep
        for m in re.finditer(r'href=["\']([^"\']+)["\']', body):
            link = urljoin(url, m.group(1)).split('#', 1)[0]
            link_netloc = urlparse(link).netloc.lower().lstrip('www.')
            if link_netloc and link_netloc != netloc:
                continue
            if _is_vdp_url(link):
                found.add(link)
            elif _is_list_page(link) and link not in visited \
                    and link not in to_visit and len(to_visit) < max_pages:
                to_visit.append(link)
    return sorted(found)


def _path_of(url):
    return urlparse(url).path.lower().rstrip('/')


def _is_vdp_url(url):
    """A VDP URL contains a VDP hint AND has extra path content after it
    (so /inventory/ alone = list, /inventory/<slug>/ = VDP)."""
    u = url.lower()
    if any(x in u for x in NON_VEHICLE_HINTS):
        return False
    path = _path_of(url)
    # DIA is used-only by design — reject any new-car VDP path globally.
    # Dealer.com / Dealer Inspire already filter upstream; this catches DealerOn.
    if path.startswith('/new-') or '/new-' in path or '/new/' in path or '/new-vehicles/' in path:
        return False
    # Quick wins — unambiguous VDP patterns
    if '/vehicle-details/' in path or '/vdp/' in path:
        return True
    if '-vin-' in path or '?vin=' in u or '?stock=' in u:
        return True
    # /vehicle/<slug>/ or /car/<slug>/ — slug required
    for h in ('/vehicle/', '/car/', '/cars/', '/details/', '/detail/',
              '/used-vehicles/', '/new-vehicles/', '/certified-vehicles/',
              '/preowned/', '/pre-owned/'):
        if h in path:
            tail = path.split(h, 1)[1]
            if tail and len(tail) > 3:
                return True
    # /inventory/<slug>/ — slug must be substantive, not just '/'
    if '/inventory/' in path:
        tail = path.split('/inventory/', 1)[1]
        if tail and len(tail) > 5 and not tail.isdigit():
            return True
    # Trailing numeric stock id: /.../something-1234/
    m = re.search(r'-(\d{3,6})$', path)
    if m and '/' in path[1:]:
        return True
    # Trailing 17-char VIN — strongest possible VDP signal. Used by DealerOn
    # (URL pattern: /used-{city}-{year}-{make}-{model}-...-{17char-VIN}) and
    # by any future dealer that suffixes its VDPs with a VIN. Case-insensitive
    # because path was lower()'d above; VIN must include at least one letter
    # so we don't false-match a 17-digit number.
    m = re.search(r'-([a-hj-npr-z0-9]{17})$', path)
    if m and re.search(r'[a-hj-npr-z]', m.group(1)):
        return True
    return False


def _is_list_page(url):
    """Pages we should crawl deeper for VDP links, but never treat as a vehicle."""
    u = url.lower()
    if any(x in u for x in NON_VEHICLE_HINTS):
        return False
    if _is_vdp_url(url):
        return False
    path = _path_of(url)
    return any(h in path for h in LIST_HINTS)


def _looks_like_vehicle_url(url):
    """Back-compat alias used by the sitemap path."""
    return _is_vdp_url(url)


# ── Vehicle data extraction ──────────────────────────────────────────────
def extract_vehicle(url, html):
    """Extract {vin, year, make, model, trim, mileage, price, ext_color, photo_url, photos}
    from a vehicle detail page. Returns dict with url populated. Null fields allowed.
    Priority: JSON-LD Vehicle > URL slug > og:title / <h1> / <title> > regex fallbacks.
    """
    out = {'url': url}

    # 1) JSON-LD Vehicle/Car — highest signal
    jsonld = _extract_jsonld_vehicle(html)
    if jsonld:
        out.update(jsonld)

    # 1b) dealer.com inline-JSON fields (price, trim, mileage, msrp). Runs
    #     before URL-slug parser so the hex listing-id never lands in trim,
    #     and before any other price extractor so the $377 monthly-payment
    #     leak doesn't take priority over real internetPrice.
    dc = _extract_dealer_com_fields(html)
    for k, v in dc.items():
        if v is not None and not out.get(k):
            out[k] = v

    # 2) URL slug — deterministic when structured
    slug = _parse_ymm_from_url(url)
    for k, v in (slug or {}).items():
        if v and not out.get(k):
            out[k] = v

    # 3) og:title / <h1> / <title> for YMM + trim
    for candidate in (_meta(html, 'og:title'),
                      _first_match(H1_RE, html),
                      _first_match(TITLE_RE, html)):
        if not candidate:
            continue
        parsed = _parse_ymm_from_title(candidate)
        if not parsed:
            continue
        for k, v in parsed.items():
            if v and not out.get(k):
                out[k] = v

    # 3a2) Platform-embedded dates — dealer.com's "inventoryDate" field.
    #       Runs before photo-ts because this is the dealer's own declared
    #       date-on-lot, which is more authoritative than photo upload time.
    if not out.get('source_added_at'):
        ts = _extract_inventory_date_from_html(html)
        if ts:
            out['source_added_at'] = ts

    # 3b) Photo-filename timestamps — most reliable signal for dealers whose
    #      JSON-LD is stale-reset (TXT Charlie / WordPress auto-regen) or
    #      absent (Marino / AAN). Photo upload time ≈ vehicle-intake time.
    #      Free to parse — no image download needed.
    if not out.get('source_added_at'):
        photo_pool = list(out.get('photos') or [])
        if out.get('photo_url') and out['photo_url'] not in photo_pool:
            photo_pool.append(out['photo_url'])
        for m in re.finditer(r'https?://[^\s"\'<>]+\.(?:jpe?g|webp|png)',
                             (html or ''), re.I):
            u = m.group(0)
            if u not in photo_pool:
                photo_pool.append(u)
            if len(photo_pool) > 30:
                break
        ts = _extract_photo_timestamp(photo_pool)
        if ts:
            out['source_added_at'] = ts

    # 3c) Sitemap <lastmod> as fallback. Skip if lastmod == today's date —
    #      WordPress sitemaps regenerate on every cache invalidation, stamping
    #      every URL with `lastmod=today`. That bug stamped 104 TXT Charlie
    #      cars with today's date in scan 26 before photo-ts was deployed.
    if not out.get('source_added_at'):
        lm = _SITEMAP_LASTMOD.get(url)
        if lm and not lm.startswith(datetime.now(timezone.utc).date().isoformat()):
            out['source_added_at'] = lm

    # 3d) VDP page text fallback — "47 days on our lot", "In stock since Jan 15",
    #      "Listed 3 weeks ago", etc. Only runs when we still have no timestamp.
    if not out.get('source_added_at'):
        ts = _extract_days_on_lot_from_text(html)
        if ts:
            out['source_added_at'] = ts

    # 4) OpenGraph photo + HTML gallery fallback
    og_image = _meta(html, 'og:image')
    if og_image and not out.get('photo_url'):
        out['photo_url'] = og_image
    existing_photos = out.get('photos') if isinstance(out.get('photos'), list) else []
    if len(existing_photos) < 5:
        gallery = _extract_photos_from_html(html, url)
        if gallery:
            seen = set(existing_photos)
            merged = list(existing_photos)
            for p in gallery:
                if p not in seen:
                    merged.append(p)
                    seen.add(p)
                if len(merged) >= 10:
                    break
            out['photos'] = merged
            if not out.get('photo_url') and merged:
                out['photo_url'] = merged[0]

    # 5) VIN from URL / page text if JSON-LD missed it. Validate that the
    # capture actually looks like a VIN (must have a letter) — bare digit
    # runs of 17 chars are inline timestamps, not VINs. First scan VIN-ish
    # candidates and pick the first one that passes _is_valid_vin.
    if not out.get('vin'):
        for src in (url, html or ''):
            for m in VIN_RE.finditer(src):
                cand = m.group(1)
                if _is_valid_vin(cand):
                    out['vin'] = cand
                    break
            if out.get('vin'):
                break

    # 6) Year fallback
    if not out.get('year'):
        m = YEAR_RE.search(url)
        if m:
            out['year'] = int(m.group(1))

    # 6c) WP_VEHICLE_PRICE_TERM_2026_05_20 — TXT Charlie / Cars Dealer
    # taxonomy. Body class `price-<id>` → resolve via WP REST `name` field.
    # Runs only when JSON-LD + dealer.com inline JSON + URL slug all missed.
    # Falls through to PRICE_RE if API returns nothing.
    if not out.get('price'):
        v = _resolve_wp_price_from_html(url, html)
        if v:
            out['price'] = v

    # 7) Price regex fallback — ONLY if the number is plausibly a retail price.
    # Without the $2000 floor, "call for price" VDPs leak $377/mo payment
    # estimates into the price field. Structured extractors (AAN feed,
    # dealer.com inline JSON, JSON-LD offers.price) already cover the reliable
    # cases; this fallback is for the rare custom platforms only.
    if not out.get('price'):
        m = PRICE_RE.search(html or '')
        if m:
            try:
                v = int(m.group(1).replace(',', ''))
                if v >= 2000:
                    out['price'] = v
            except ValueError:
                pass

    # 8a) DealerOn HTML mileage — labelled `info__value title="N"` next to
    #      a `<span>Mileage</span>` label. Their JSON-LD omits
    #      mileageFromOdometer so this is the only signal on Encore-class
    #      sites.
    if not out.get('mileage'):
        v = _extract_dealeron_mileage(html)
        if v is not None:
            out['mileage'] = v

    # 8b) Mileage regex fallback
    if not out.get('mileage'):
        m = MILES_RE.search(html or '')
        if m:
            try:
                out['mileage'] = int(m.group(1).replace(',', ''))
            except ValueError:
                pass

    # 8c) DEALERON_COLOR_2026_05_20 — interior + exterior color from the
    # same info__label/info__value DOM the mileage extractor uses. JSON-LD
    # on DealerOn rooftops doesn't carry interior_color, and ext_color
    # JSON-LD coverage is patchy. Fallback fills both fields when missing.
    if not out.get('int_color'):
        c = _extract_dealeron_int_color(html)
        if c:
            out['int_color'] = c
    if not out.get('ext_color'):
        c = _extract_dealeron_ext_color(html)
        if c:
            out['ext_color'] = c

    # 8d) WP_VEHICLE_COLOR_2026_05_20 — WordPress dealer plugin body class
    # taxonomy (TXT Charlie + any Cars Dealer / Elementor rooftop). Falls
    # through DealerOn miss; safe to run on any HTML.
    if not out.get('int_color'):
        c = _extract_wp_vehicle_int_color(html)
        if c:
            out['int_color'] = c
    if not out.get('ext_color'):
        c = _extract_wp_vehicle_ext_color(html)
        if c:
            out['ext_color'] = c

    # Canonicalise make capitalisation
    if out.get('make'):
        out['make'] = _normalize_make(out['make'])
    if out.get('model'):
        out['model'] = _title_case(out['model'])
    if out.get('trim'):
        out['trim'] = _title_case(out['trim'])

    # Strip leading-model duplication from trim. Happens when JSON-LD
    # provides a multi-word model (e.g. "E 450", "Range Rover Sport", "4
    # Series") that the URL-slug fallback wasn't aware of, so the
    # URL-derived trim still contains some/all model words at the front.
    #
    # Two patterns to handle:
    #   (a) Full model overlap. JSON-LD model="E 450", URL trim="E 450
    #       4matic Cabriolet ...". Strip 2 leading trim tokens.
    #   (b) Partial model overlap. URL parser took ONE model token
    #       (e.g. "range") then JSON-LD overrode model with "Range Rover
    #       Sport"; URL trim still leads with "Rover Sport P400 ...". Need
    #       to strip the model SUFFIX that matches the trim prefix.
    #
    # Algorithm: find the largest k for which any contiguous k-token window
    # of model matches the first-k tokens of trim (case-insensitive). Strip
    # those k tokens from trim. Covers all overlap shapes:
    #   - "E 450" + "E 450 4matic ..."          → suffix==prefix==full model
    #   - "Range Rover Sport" + "Rover Sport P400 ..." → suffix overlap (2 of 3)
    #   - "S 63 Amg®" + "S 63 4 Door Sedan ..." → prefix overlap (2 of 3)
    #   - "4 Series" + "Series M440i ..."       → suffix overlap (1 of 2)
    # k=0 means no overlap, no strip.
    if out.get('model') and out.get('trim'):
        m_toks = out['model'].split()
        t_toks = out['trim'].split()
        m_low = [t.lower() for t in m_toks]
        t_low = [t.lower() for t in t_toks]
        # Largest k bounded by both list lengths; always leave at least one
        # trim token so we don't end up with an empty trim field.
        max_k = min(len(m_low), len(t_low) - 1)
        for k in range(max_k, 0, -1):
            t_prefix = t_low[:k]
            # Does any k-window of model equal trim's first k tokens?
            if any(m_low[i:i+k] == t_prefix
                   for i in range(len(m_low) - k + 1)):
                out['trim'] = ' '.join(t_toks[k:]) or None
                break

    # CONDITION_FROM_SLUG_2026_05_29: derive used/cpo from the VDP URL slug.
    # DealerOn (/used-..., /certified-pre-owned-...) and dealer.com crawl VDPs
    # encode condition in the path. 'certified' -> cpo, else used. Only EW-
    # sourced pre-owned reach here (new-car VDP paths filtered upstream).
    if not out.get('condition'):
        _ul = (url or '').lower()
        if 'certified' in _ul or 'cpo' in _ul:
            out['condition'] = 'cpo'
        elif 'used' in _ul or 'pre-owned' in _ul or 'preowned' in _ul:
            out['condition'] = 'used'

    # Filter: need at least ONE of year/make/model OR vin
    if not (out.get('vin') or out.get('make') or out.get('year')):
        return None
    return out


def _first_match(pattern, text):
    if not text:
        return None
    m = pattern.search(text)
    return m.group(1).strip() if m else None


def _parse_ymm_from_url(url):
    """Parse year/make/model/trim from a VDP URL slug.
    Handles patterns like /vehicle-details/2019-rolls-royce-phantom-6709/.

    DealerOn (encoreautos.com) URL-encodes intra-token spaces as `+` and
    suffixes the slug with the 17-char VIN, e.g.:
        /used-Sarasota-2025-Mercedes+Benz-Cle+300-4matic+cabriolet+w+line+package-W1KMK4HB6SF054997
    Normalizing `+` to `-` up front lets the existing hyphen-tokenizer see
    individual words, and the trailing-17-char-VIN strip rule below removes
    the VIN before it lands in `trim`. (2026-05-03 Encore onboarding.)
    """
    # DealerOn drops the slash from "W/Feature" leaving a single CamelCase
    # token like `WPremium`/`WNavigation`/`wAdvance`/`WM`/`WP1`. Without this
    # strip those survive lowercase + title-case as cosmetic glitches
    # (`Wpremium`, `Wm Sport`). Pattern: word-boundary + W (either case),
    # followed by a capital, followed by either a lowercase letter, a digit,
    # or another word boundary. The third condition matches `WM Sport`
    # (M then space) and the second matches `WP1` (P then 1) — but NOT VINs
    # (`W-B-A-...`, three-cap run with no boundary) and NOT `WRX` (R-X
    # contiguous caps). Done pre-lowercase so case info is still intact.
    path_raw = urlparse(url).path.rstrip('/')
    path_raw = re.sub(r'\b[Ww](?=[A-Z](?:[a-z\d]|\b))', '', path_raw)
    path = path_raw.lower()
    slug = path.rsplit('/', 1)[-1] if path else ''
    # Normalize DealerOn's `+`-as-space to a uniform `-` separator so the
    # tokenizer below produces proper word tokens for trim/make/model.
    slug = slug.replace('+', '-')
    if not slug or '-' not in slug:
        return None
    tokens = [t for t in slug.split('-') if t]
    # Year = first 4-digit token
    year = None
    for i, t in enumerate(tokens):
        if re.fullmatch(r'(19[89]\d|20[0-3]\d)', t):
            year = int(t)
            tokens = tokens[i+1:]
            break
    if year is None:
        return None
    # Strip trailing 17-char VIN (DealerOn URL pattern). Matches the
    # _is_vdp_url trailing-VIN rule. Letter-required so a 17-digit number
    # never matches.
    if tokens and re.fullmatch(r'[a-hj-npr-z0-9]{17}', tokens[-1]) \
            and re.search(r'[a-hj-npr-z]', tokens[-1]):
        tokens = tokens[:-1]
    # Stock# = trailing numeric token (short number)
    if tokens and re.fullmatch(r'[a-z]?\d{2,6}[a-z]?', tokens[-1]):
        tokens = tokens[:-1]
    if not tokens:
        return {'year': year}
    # Make = known-compound-2-token OR single token. Only emit when the
    # candidate is actually a known make; otherwise the slug is non-standard
    # (e.g. Bentley Denver puts model first, make mid-slug) and we should
    # let the title-tag parser fill year+make+model instead.
    make = None
    if len(tokens) >= 2:
        two = f'{tokens[0]}-{tokens[1]}'
        if two in MAKE_NORMALIZE or two in KNOWN_COMPOUND_MAKES:
            make = MAKE_NORMALIZE.get(two, _title_case(two.replace('-', ' ')))
            tokens = tokens[2:]
    if make is None and tokens:
        first = tokens[0]
        if first in _KNOWN_SINGLE_MAKES or first in MAKE_NORMALIZE:
            make = MAKE_NORMALIZE.get(first, _title_case(first))
            tokens = tokens[1:]
        else:
            # Unknown first token — slug shape isn't year-make-model-…
            # Skip make/model/trim emission so the title parser wins.
            return {'year': year}
    # Model = next token (take single word — most model names are one word)
    model = _title_case(tokens[0]) if tokens else None
    # Trim = everything after model
    trim = _title_case(' '.join(tokens[1:])) if len(tokens) > 1 else None
    out = {'year': year}
    if make:  out['make'] = make
    if model: out['model'] = model
    if trim:  out['trim'] = trim
    return out


def _parse_ymm_from_title(title):
    """Parse 'YYYY Make Model Trim | Dealer' or similar title strings."""
    if not title:
        return None
    # Strip site branding after | or – or —
    head = re.split(r'\s*[|\-–—]\s*', title, maxsplit=1)[0].strip()
    m = re.match(r'^(\d{4})\s+(.+)$', head)
    if not m:
        return None
    year = int(m.group(1))
    remainder = m.group(2).strip()
    # Drop trailing stock #
    remainder = re.sub(r'\s+[a-zA-Z]?\d{3,6}[a-zA-Z]?$', '', remainder).strip()
    if not remainder:
        return {'year': year}
    tokens = remainder.split()
    # Check for 2-token compound make
    make = None
    if len(tokens) >= 2:
        two = f'{tokens[0]} {tokens[1]}'.lower()
        if two in KNOWN_COMPOUND_MAKES:
            make = MAKE_NORMALIZE.get(two.replace(' ', '-'),
                                      _title_case(two))
            tokens = tokens[2:]
    if make is None and tokens:
        make = _normalize_make(tokens[0])
        tokens = tokens[1:]
    model = tokens[0] if tokens else None
    trim = ' '.join(tokens[1:]) if len(tokens) > 1 else None
    out = {'year': year}
    if make:  out['make'] = make
    if model: out['model'] = _title_case(model)
    if trim:  out['trim'] = _title_case(trim)
    return out


def _normalize_make(raw):
    if not raw:
        return raw
    key = raw.strip().lower().replace(' ', '-')
    if key in MAKE_NORMALIZE:
        return MAKE_NORMALIZE[key]
    return _title_case(raw.replace('-', ' '))


def _title_case(s):
    if not s:
        return s
    # Preserve known capitalised acronyms
    ACRONYMS = {'bmw', 'gmc', 'srt', 'amg', 'rs', 'rs3', 'rs5', 'rs6', 'rs7',
                'sti', 'wrx', 'gti', 'tdi', 'hemi', 'hse', 'v8', 'v6', 'v10', 'v12',
                'ev', 'phev', 'awd', 'fwd', 'rwd', '4wd', '4x4', 'ltz', 'ltd', 'lx',
                'sv', 'sl', 'se', 's', 'ex', 'lt', 'xlt', 'zl1', 'z06', 'zr1',
                'm3', 'm4', 'm5', 'm6', 'm7', 'm8'}
    def _cap(tok):
        low = tok.lower()
        if low in ACRONYMS:
            return low.upper()
        return tok.capitalize()
    return ' '.join(_cap(t) for t in str(s).split())


def _extract_jsonld_vehicle(html):
    """Find <script type='application/ld+json'> with a Vehicle / Car schema."""
    if not html:
        return None
    for m in re.finditer(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>([\s\S]*?)</script>', html):
        raw = m.group(1).strip()
        try:
            data = _json.loads(raw)
        except Exception:
            # Some dealers inline multiple concatenated objects; skip
            continue
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            graph = item.get('@graph') if isinstance(item.get('@graph'), list) else [item]
            for node in graph:
                if not isinstance(node, dict):
                    continue
                t = node.get('@type')
                if isinstance(t, list):
                    t = next((x for x in t if x), None)
                if t in ('Vehicle', 'Car', 'Product') or (
                        isinstance(t, str) and 'ehicle' in t):
                    parsed = _parse_vehicle_node(node)
                    if parsed:
                        return parsed
    return None


def _parse_vehicle_node(node):
    v = {}
    # Trust JSON-LD's VIN field even if non-17-char. Track-only specialty
    # vehicles (Ford GT MK IV, Lambo Essenza SCV12, Pagani, vintage etc.)
    # carry shorter dealer-assigned identifiers. Without this, the
    # vin-extraction falls back to scanning the entire HTML for any 17-char
    # token, which catches cross-referenced VINs from "related vehicles"
    # panels and merges the wrong row (Ford GT MK IV merged into Ferrari 400i
    # incident, 2026-04-27). Setting the dealer's VIN here means upsert_vehicle's
    # `_is_valid_vin` will sanitize non-17-char strings to '' and the row
    # dedups via URL — clean separation.
    vin = node.get('vehicleIdentificationNumber') or node.get('sku')
    if isinstance(vin, str) and vin.strip():
        v['vin'] = vin.upper().strip()
    y = node.get('vehicleModelDate') or node.get('modelDate') or node.get('productionDate')
    if y:
        try:
            v['year'] = int(str(y)[:4])
        except ValueError:
            pass
    brand = node.get('brand') or node.get('manufacturer')
    if isinstance(brand, dict):
        brand = brand.get('name')
    if brand:
        v['make'] = str(brand).strip()
    model = node.get('model')
    if isinstance(model, dict):
        model = model.get('name')
    if model:
        v['model'] = str(model).strip()
    trim = node.get('vehicleConfiguration') or node.get('trim')
    if trim:
        v['trim'] = str(trim).strip()
    color = node.get('color')
    if color:
        v['ext_color'] = str(color).strip()
    miles = node.get('mileageFromOdometer')
    if isinstance(miles, dict):
        miles = miles.get('value')
    if miles is not None:
        try:
            v['mileage'] = int(float(str(miles).replace(',', '')))
        except (ValueError, TypeError):
            pass
    offers = node.get('offers')
    if offers:
        if isinstance(offers, list):
            offers = offers[0] if offers else None
        if isinstance(offers, dict):
            price = offers.get('price')
            if price is not None:
                try:
                    v['price'] = int(float(str(price).replace(',', '')))
                except (ValueError, TypeError):
                    pass
    img = node.get('image')
    if isinstance(img, list):
        v['photos'] = [x for x in img if isinstance(x, str)]
        if v['photos']:
            v['photo_url'] = v['photos'][0]
    elif isinstance(img, str):
        v['photo_url'] = img
        v['photos'] = [img]
    # Source-added timestamp — dealer's own "date added to inventory" field.
    # Check JSON-LD's datePosted / datePublished / dateAdded in that order.
    # Returned as ISO string; upsert_vehicle parses to TIMESTAMPTZ.
    for k in ('datePosted', 'datePublished', 'dateAdded', 'dateCreated'):
        val = node.get(k)
        if val and isinstance(val, str) and len(val) >= 10:
            v['source_added_at'] = val
            break
    return v if v else None


# ── VDP page-text date extractor ────────────────────────────────────────
# Patterns in priority order — first match wins.
# Anchored to specific phrases to avoid matching "30 days warranty" etc.
_DAYS_ON_LOT_RE = re.compile(
    r'(?P<n>\d{1,4})\s*(?:days?|d)\s+'
    r'(?:on\s+(?:our\s+|the\s+)?lot|in\s+(?:our\s+)?(?:stock|inventory)|on\s+(?:our\s+)?market)',
    re.I,
)
_WEEKS_ON_LOT_RE = re.compile(
    r'(?P<n>\d{1,3})\s*(?:weeks?|wks?)\s+(?:on\s+(?:our\s+|the\s+)?lot|in\s+(?:our\s+)?(?:stock|inventory))',
    re.I,
)
_LISTED_DAYS_AGO_RE = re.compile(
    r'(?:listed|posted|added|arrived|in\s+stock)\s+(?P<n>\d{1,4})\s*(?:days?|d)\s+ago',
    re.I,
)
_LISTED_WEEKS_AGO_RE = re.compile(
    r'(?:listed|posted|added|arrived|in\s+stock)\s+(?P<n>\d{1,3})\s*(?:weeks?|wks?)\s+ago',
    re.I,
)
# "In stock since Jan 15, 2026" / "Added on 01/15/2026" / "Arrived 2026-01-15"
_DATE_AFTER_KW_RE = re.compile(
    r'(?:in[- ]stock\s+since|available\s+since|added\s+on|added|listed\s+on|listed|posted\s+on|posted'
    r'|arrived\s+on|arrived|stock\s+date|date\s+added|date\s+listed|date\s+posted)'
    r'[\s:]*[:\-]?\s*'
    r'(?P<d>'
      r'\d{1,2}[/-]\d{1,2}[/-]\d{2,4}'                           # 01/15/2026 or 1-15-26
      r'|\d{4}-\d{2}-\d{2}'                                       # 2026-01-15
      r'|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2},?\s+\d{2,4}'  # Jan 15, 2026
      r'|\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{2,4}'    # 15 Jan 2026
    r')',
    re.I,
)

_MONTH_MAP = {
    'jan': 1, 'january': 1, 'feb': 2, 'february': 2, 'mar': 3, 'march': 3,
    'apr': 4, 'april': 4, 'may': 5, 'jun': 6, 'june': 6,
    'jul': 7, 'july': 7, 'aug': 8, 'august': 8, 'sep': 9, 'sept': 9, 'september': 9,
    'oct': 10, 'october': 10, 'nov': 11, 'november': 11, 'dec': 12, 'december': 12,
}


def _parse_human_date(s):
    """Parse common US date formats into 'YYYY-MM-DD' or None.
    Handles: 01/15/2026 · 1-15-26 · 2026-01-15 · Jan 15, 2026 · 15 Jan 2026"""
    if not s:
        return None
    s = s.strip().lower()
    # ISO
    m = re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})$', s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f'{y:04d}-{mo:02d}-{d:02d}'
    # MM/DD/YY or MM/DD/YYYY or MM-DD-YY
    m = re.match(r'^(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})$', s)
    if m:
        mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000 if y < 70 else 1900
        if 1 <= mo <= 12 and 1 <= d <= 31 and 1990 <= y <= 2100:
            return f'{y:04d}-{mo:02d}-{d:02d}'
    # Month name first: Jan 15, 2026
    m = re.match(r'^([a-z]+)\s+(\d{1,2}),?\s+(\d{2,4})$', s)
    if m:
        mo = _MONTH_MAP.get(m.group(1))
        if mo:
            d, y = int(m.group(2)), int(m.group(3))
            if y < 100:
                y += 2000 if y < 70 else 1900
            if 1 <= d <= 31 and 1990 <= y <= 2100:
                return f'{y:04d}-{mo:02d}-{d:02d}'
    # Day first: 15 Jan 2026
    m = re.match(r'^(\d{1,2})\s+([a-z]+)\s+(\d{2,4})$', s)
    if m:
        mo = _MONTH_MAP.get(m.group(2))
        if mo:
            d, y = int(m.group(1)), int(m.group(3))
            if y < 100:
                y += 2000 if y < 70 else 1900
            if 1 <= d <= 31 and 1990 <= y <= 2100:
                return f'{y:04d}-{mo:02d}-{d:02d}'
    return None


def _strip_html_for_text(html):
    """Strip tags + collapse whitespace. Used only for date-mining so we don't
    parse the full document — rough is fine."""
    if not html:
        return ''
    txt = re.sub(r'<script[\s\S]*?</script>', ' ', html, flags=re.I)
    txt = re.sub(r'<style[\s\S]*?</style>', ' ', txt, flags=re.I)
    txt = re.sub(r'<[^>]+>', ' ', txt)
    txt = re.sub(r'\s+', ' ', txt)
    return txt


# ── dealer.com embedded-JSON extractor ────────────────────────────────
# dealer.com embeds VDP data as inline JSON throughout the page (NOT in
# schema.org JSON-LD). All authoritative fields live there: internetPrice,
# askingPrice, trim, inventoryDate, vin, mileage. Without this, the URL-slug
# fallback grabs the hex listing-id as trim ("706c0904ac18269702.htm"), and
# something on the page yields garbage prices like $377. These regexes pull
# the named fields directly — no false positives.
_INVENTORY_DATE_RE = re.compile(
    r'"inventoryDate"\s*:\s*"(\d{1,2})\\?/(\d{1,2})\\?/(\d{4})"', re.I)
_DC_INTERNET_PRICE_RE = re.compile(r'"internetPrice"\s*:\s*"(\d{3,8})"')
_DC_ASKING_PRICE_RE   = re.compile(r'"askingPrice"\s*:\s*"(\d{3,8})"')
_DC_FINAL_PRICE_RE    = re.compile(r'"finalPrice"\s*:\s*"(\d{3,8})"')
_DC_MSRP_RE           = re.compile(r'"msrp"\s*:\s*"(\d{3,8})"')
_DC_TRIM_RE           = re.compile(r'"trim"\s*:\s*"([^"]{1,80})"')
_DC_MILEAGE_RE        = re.compile(r'"odometer"\s*:\s*"(\d{1,7})"|"mileage"\s*:\s*"(\d{1,7})"')


def _extract_inventory_date_from_html(html):
    """Returns ISO date 'YYYY-MM-DD' or None. Handles dealer.com's
    inventoryDate field; works for every franchise on the platform."""
    if not html:
        return None
    m = _INVENTORY_DATE_RE.search(html)
    if not m:
        return None
    try:
        mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31 and 1990 <= y <= 2100:
            return f'{y:04d}-{mo:02d}-{d:02d}'
    except ValueError:
        pass
    return None


def _extract_dealer_com_fields(html):
    """Pull the authoritative dealer.com VDP fields out of the inline JSON
    payload. Returns dict with any of {price, trim, mileage, msrp} that
    were found — caller merges into the vehicle dict, preferring these over
    the URL-slug fallback (which grabs hex listing-ids as trim)."""
    if not html:
        return {}
    out = {}
    # Price priority: internetPrice > askingPrice > finalPrice. Skip values
    # under 5000 (those are often monthly payment estimates that leak through).
    for rx in (_DC_INTERNET_PRICE_RE, _DC_ASKING_PRICE_RE, _DC_FINAL_PRICE_RE):
        m = rx.search(html)
        if not m:
            continue
        try:
            v = int(m.group(1))
            if v >= 5000:
                out['price'] = v
                break
        except ValueError:
            pass
    m = _DC_MSRP_RE.search(html)
    if m:
        try:
            v = int(m.group(1))
            if v >= 5000:
                out['msrp'] = v
        except ValueError:
            pass
    m = _DC_TRIM_RE.search(html)
    if m:
        t = m.group(1).strip()
        # Reject obvious junk (hex hashes, file extensions, empty)
        if t and not t.endswith('.htm') and not re.match(r'^[0-9a-f]{16,}$', t, re.I):
            out['trim'] = t
    m = _DC_MILEAGE_RE.search(html)
    if m:
        try:
            v = int(m.group(1) or m.group(2))
            if 0 <= v <= 999999:
                out['mileage'] = v
        except (ValueError, TypeError):
            pass
    return out


# ── DealerOn HTML mileage extractor ────────────────────────────────────
# DealerOn's JSON-LD doesn't carry mileageFromOdometer; the visible markup
# renders it as `<span class="info__label">Mileage</span><span
# class="info__value" title="32,947">32,947</span>`. The MILES_RE fallback
# wants the number adjacent to "miles"/"mi" so it never fires on this DOM.
_DEALERON_MILEAGE_RE = re.compile(
    r'>Mileage</span>\s*<span[^>]*?title="([\d,]+)"', re.I)


def _extract_dealeron_mileage(html):
    if not html:
        return None
    m = _DEALERON_MILEAGE_RE.search(html)
    if not m:
        return None
    try:
        v = int(m.group(1).replace(',', ''))
        if 0 < v <= 999999:
            return v
    except ValueError:
        pass
    return None


# ── DealerOn HTML color extractors — DEALERON_COLOR_2026_05_20 ─────────
# Same DealerOn DOM pattern as mileage: `<span class="info__label">
# Interior Color</span><span class="info__value info__value--color"
# title="Ivory White/Dark Oyster">`. JSON-LD on DealerOn sites doesn't
# carry interior color reliably, so this is the only path for those
# rooftops. Confirmed working on Encore (encoreautos.com) 2026-05-20.
_DEALERON_INT_COLOR_RE = re.compile(
    r'>Interior Color</span>\s*<span[^>]*?title="([^"]+)"', re.I)
_DEALERON_EXT_COLOR_RE = re.compile(
    r'>Exterior Color</span>\s*<span[^>]*?title="([^"]+)"', re.I)


def _extract_dealeron_int_color(html):
    if not html:
        return None
    m = _DEALERON_INT_COLOR_RE.search(html)
    if not m:
        return None
    s = m.group(1).strip()
    return s or None


def _extract_dealeron_ext_color(html):
    if not html:
        return None
    m = _DEALERON_EXT_COLOR_RE.search(html)
    if not m:
        return None
    s = m.group(1).strip()
    return s or None


# WP_VEHICLE_COLOR_2026_05_20 — WordPress dealer plugins (Elementor /
# Cars Dealer family) emit taxonomy slugs into the <body class="...">
# attribute, e.g. `interior_color-nero-ade interior_color-yellow-black
# exterior_color-giallo-inti-pearl`. Multiple slugs per car (primary +
# accent); first one is canonical. Confirmed on TXT Charlie 2026-05-20.
_WP_VEHICLE_INT_COLOR_RE = re.compile(
    r'\b(?:interior_color|interiorcolor)-([a-z0-9-]+)', re.I)
_WP_VEHICLE_EXT_COLOR_RE = re.compile(
    r'\b(?:exterior_color|exteriorcolor)-([a-z0-9-]+)', re.I)

# WP_VEHICLE_PRICE_TERM_2026_05_20 — TXT Charlie / Cars Dealer / Elementor
# WordPress sites encode price as a taxonomy term referenced by ID in the
# body class (e.g., `price-11992`). The visible $74,999 in the DOM is
# JS-hydrated from term metadata; static HTML has only the term ID. Resolve
# via /wp-json/wp/v2/price/<id> where term `name` is the numeric price string.
# Some sites also expose `total-price-<id>` (vehicle price + dealer fee);
# we use `price` for parity with how other dealers report asking price.
_WP_VEHICLE_PRICE_TERM_RE = re.compile(
    r'\bprice-([0-9]{2,8})\b')


def _wp_color_slug_to_display(slug):
    """nero-ade -> Nero Ade · yellow-black -> Yellow Black."""
    if not slug:
        return None
    parts = [p for p in slug.split('-') if p]
    return ' '.join(p.capitalize() for p in parts) if parts else None


def _extract_wp_vehicle_int_color(html):
    if not html:
        return None
    m = _WP_VEHICLE_INT_COLOR_RE.search(html)
    return _wp_color_slug_to_display(m.group(1)) if m else None


def _extract_wp_vehicle_ext_color(html):
    if not html:
        return None
    m = _WP_VEHICLE_EXT_COLOR_RE.search(html)
    return _wp_color_slug_to_display(m.group(1)) if m else None


def _resolve_wp_price_from_html(url, html):
    """Resolve WP price taxonomy term ID → numeric dollar value.
    Returns int or None. Fires only when the body class contains the
    `price-<term_id>` token, so it's effectively a no-op on non-WP sites.
    One GET per VDP; ~50-150ms typical. Failures swallowed (returns None).
    """
    if not html or not url:
        return None
    m = _WP_VEHICLE_PRICE_TERM_RE.search(html)
    if not m:
        return None
    term_id = m.group(1)
    try:
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        api = f"{base}/wp-json/wp/v2/price/{term_id}?_fields=name"
        r = requests.get(api, timeout=8,
                         headers={'User-Agent': 'Mozilla/5.0 EW-Scanner'})
        if r.status_code != 200:
            return None
        name = (r.json() or {}).get('name')
        if not name:
            return None
        val = int(str(name).replace(',', '').replace('$', '').strip())
        if val >= 2000:
            return val
    except Exception:
        return None
    return None


# ── Photo-filename timestamp extractor ─────────────────────────────────
# Dealers stamp upload timestamps into image filenames in two common ways.
# We mine these because (a) EXIF is usually stripped by image optimizers and
# (b) the upload time is a much better proxy for "vehicle intake" than any
# other public signal we've seen.
_PHOTO_UNIX_TS_RE = re.compile(r'[-_](?P<ts>1[0-9]{9}|2[0-4][0-9]{8})(?:[.-]|$)')
_PHOTO_YMD_HMS_RE = re.compile(r'(?<!\d)(?P<d>(?:19|20)\d{12})(?!\d)')   # YYYYMMDDHHMMSS
_PHOTO_YMD_RE     = re.compile(r'(?<!\d)(?P<d>(?:19|20)\d{6})(?!\d)')    # YYYYMMDD


def _extract_photo_timestamp(photo_urls):
    """Recover the earliest 'photo upload' timestamp across all photo URLs.
    Patterns (tested against every URL):
      1. Unix epoch: `-1533753420.jpg` → 2018-08-08      [Marino / AAN]
      2. YYYYMMDDHHMMSS: `-20260421214046.jpg` → Apr 21  [TXT Charlie / WordPress]
      3. YYYYMMDD: `-20260421.jpg` → Apr 21              [some dealerinspire themes]
    Returns ISO date string (YYYY-MM-DD) of the earliest parse, or None.
    We pick the EARLIEST because dealers re-upload photos for touch-ups but
    the first upload is closest to 'when they took the car in'."""
    if not photo_urls:
        return None
    candidates = []
    now_ts = datetime.now(timezone.utc).timestamp()
    floor_ts = datetime(2005, 1, 1, tzinfo=timezone.utc).timestamp()
    for url in photo_urls:
        if not url or not isinstance(url, str):
            continue
        # 1) Unix epoch
        for m in _PHOTO_UNIX_TS_RE.finditer(url):
            try:
                ts = int(m.group('ts'))
                if floor_ts <= ts <= now_ts:
                    candidates.append(datetime.fromtimestamp(ts, tz=timezone.utc))
            except (ValueError, OSError):
                pass
        # 2) YYYYMMDDHHMMSS
        for m in _PHOTO_YMD_HMS_RE.finditer(url):
            s = m.group('d')
            try:
                dt = datetime.strptime(s, '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
                if floor_ts <= dt.timestamp() <= now_ts + 86400:
                    candidates.append(dt)
            except ValueError:
                pass
        # 3) Plain YYYYMMDD — only consider if the two above didn't find anything
        #    (avoid double-counting when a YYYYMMDDHHMMSS also contains YYYYMMDD).
        if not _PHOTO_UNIX_TS_RE.search(url) and not _PHOTO_YMD_HMS_RE.search(url):
            for m in _PHOTO_YMD_RE.finditer(url):
                s = m.group('d')
                try:
                    dt = datetime.strptime(s, '%Y%m%d').replace(tzinfo=timezone.utc)
                    if floor_ts <= dt.timestamp() <= now_ts + 86400:
                        candidates.append(dt)
                except ValueError:
                    pass
    if not candidates:
        return None
    return min(candidates).date().isoformat()


def _extract_days_on_lot_from_text(html):
    """Try to find a dealer-declared listing age in the visible VDP text.
    Returns ISO timestamp string (YYYY-MM-DD) or None. Order of preference:
      1. Explicit 'X days on (our) lot / in stock' → NOW() - X days
      2. 'X weeks on lot' → NOW() - X*7 days
      3. 'Listed X days ago' / 'Posted X weeks ago' → same math
      4. 'In stock since <date>' / 'Added on <date>' / 'Stock date: <date>'
    Returns None on ambiguity — we'd rather fall through to first_seen_at
    than stamp a wrong date."""
    if not html:
        return None
    text = _strip_html_for_text(html)
    today = datetime.now(timezone.utc).date()

    m = _DAYS_ON_LOT_RE.search(text)
    if m:
        try:
            n = int(m.group('n'))
            if 0 <= n <= 3000:  # 8+ years sanity cap
                d = today - timedelta(days=n)
                return d.isoformat()
        except ValueError:
            pass

    m = _WEEKS_ON_LOT_RE.search(text)
    if m:
        try:
            n = int(m.group('n'))
            if 0 <= n <= 400:
                d = today - timedelta(days=n * 7)
                return d.isoformat()
        except ValueError:
            pass

    m = _LISTED_DAYS_AGO_RE.search(text)
    if m:
        try:
            n = int(m.group('n'))
            if 0 <= n <= 3000:
                d = today - timedelta(days=n)
                return d.isoformat()
        except ValueError:
            pass

    m = _LISTED_WEEKS_AGO_RE.search(text)
    if m:
        try:
            n = int(m.group('n'))
            if 0 <= n <= 400:
                d = today - timedelta(days=n * 7)
                return d.isoformat()
        except ValueError:
            pass

    m = _DATE_AFTER_KW_RE.search(text)
    if m:
        parsed = _parse_human_date(m.group('d'))
        if parsed:
            return parsed

    return None


def _meta(html, prop):
    if not html:
        return None
    m = re.search(
        r'<meta[^>]*(?:property|name)=["\']' + re.escape(prop) + r'["\'][^>]*content=["\']([^"\']+)["\']',
        html, re.I)
    return m.group(1) if m else None


def _extract_photos_from_html(html, base_url):
    """Pull vehicle photos from <img> tags. Handles lazy-load (data-src / data-lazy-src).
    Filters to same-origin JPEG/WEBP to skip branding PNGs and external trackers."""
    if not html:
        return []
    netloc = urlparse(base_url).netloc.lower().lstrip('www.')
    seen = set()
    out = []
    for m in re.finditer(r'<img\b[^>]*>', html, re.I):
        tag = m.group(0)
        # Prefer data-src (lazy load); fall back to src; skip data: URIs.
        src = None
        for attr in ('data-lazy-src', 'data-src', 'src'):
            sm = re.search(rf'{attr}=["\']([^"\']+)["\']', tag, re.I)
            if sm:
                candidate = sm.group(1).strip()
                if candidate and not candidate.startswith('data:'):
                    src = candidate
                    break
        if not src:
            continue
        absolute = urljoin(base_url, src).split('#', 1)[0]
        if urlparse(absolute).netloc.lower().lstrip('www.') != netloc:
            continue
        ext = absolute.split('?', 1)[0].lower().rsplit('.', 1)[-1]
        if ext not in ('jpg', 'jpeg', 'webp'):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        out.append(absolute)
        if len(out) >= 15:
            break
    return out


# ── Sold detection ───────────────────────────────────────────────────────
def probe_sold_signals(url, sess):
    """Returns a list of signal dicts: [{type, detail, confidence}].
    Two-tier probe: try the active fetch tier first (free), fall back to
    direct_proxy (residential IP, ~$0.001) if the server IP gets a transient
    error OR a 599 redirect-to-internal (e.g., Hostinger/LiteSpeed cache poison
    that hides a real 404 behind a cached 302). Without this, TXT Charlie's
    genuinely-sold VDPs came back as 599 → no sold signal fired."""
    signals = []
    if not url:
        return signals
    code, final_url, body = fetch(url, sess)
    # Retry through residential proxy if our server IP can't see the truth.
    if code is None or code == 599 or (code and 500 <= code <= 599):
        try:
            code2, final_url2, body2 = dealer_fetchers.fetch_direct_proxy(url, sess)
            if code2 is not None and code2 != 599:
                code, final_url, body = code2, final_url2, body2
        except Exception:
            pass
    if code in (404, 410):
        signals.append({'type': 'url_404', 'detail': f'HTTP {code}', 'confidence': 0.75})
        return signals
    if code is None:
        # Still transient even through proxy — let next scan retry
        return signals
    if final_url and final_url != url:
        # Redirect — check if final looks like sold/inventory/404 landing page.
        # Many WordPress dealers (TXT Charlie / Hostinger) redirect sold VDPs
        # to a custom 404 page that returns HTTP 200 — a "soft 404." We treat
        # those redirects as strong sold signal (0.75) since the dealer
        # explicitly removed the listing.
        fp = urlparse(final_url).path.lower()
        if any(k in fp for k in ('page-404', '/404', 'not-found', 'notfound',
                                  'inventory-removed', 'no-longer-available')):
            signals.append({'type': 'url_404', 'detail': f'soft-404 redirect → {final_url}',
                            'confidence': 0.75})
            return signals
        if any(k in fp for k in ('sold', 'unavailable', 'inventory', 'search')):
            signals.append({'type': 'url_redirect_sold',
                            'detail': f'{url} -> {final_url}',
                            'confidence': 0.55})
    if body and SOLD_TEXT_RE.search(body):
        match = SOLD_TEXT_RE.search(body).group(0).lower()
        if 'pending' in match or 'contract' in match or 'reserved' in match:
            signals.append({'type': 'text_pending', 'detail': match, 'confidence': 0.55})
        else:
            signals.append({'type': 'text_sold', 'detail': match, 'confidence': 0.75})
    return signals


def sold_confidence(signals):
    """Combine per-signal confidences into a 0..1 score, dampening redundant signals."""
    if not signals:
        return 0.0
    # additive w/ diminishing returns: 1 - prod(1 - c_i)
    prod = 1.0
    for s in signals:
        c = max(0.0, min(1.0, float(s.get('confidence', 0.0))))
        prod *= (1.0 - c)
    return round(1.0 - prod, 2)


# ── Upsert ───────────────────────────────────────────────────────────────
def upsert_vehicle(cur, dealer_id, scan_id, veh):
    """Insert or merge a vehicle row; returns (inventory_id, is_new, price_drop_amount).

    Lookup ladder — prevents duplicate rows when one scan has VIN and the next
    doesn't (or vice versa). Bug this guards against: TXT Charlie scan 33 got
    partial HTML for 51 cars → no VIN extracted → dedup key fell back to URL →
    INSERT inserted new orphan rows shadowing the real-VIN rows that already
    existed with the same URL.
        1. If incoming VIN set, try VIN match first (strongest key)
        2. If no VIN match (or no VIN incoming), try URL match — this catches
           cases where our row HAS a VIN but incoming doesn't (or when VIN
           from one scan differs from another by case/formatting)
    """
    vin = (veh.get('vin') or '').strip().upper()
    url = veh.get('url') or ''
    # Pre-1981 vehicles (vintage Ferrari 308 GTSi `035851`, 1957 Cadillac
    # `00000005762072039`, etc.) carry placeholder all-digit "VINs" that
    # violate the `vin_has_letter_when_present` CHECK constraint and would
    # abort the whole scan transaction. Strip non-VIN strings so the row
    # still inserts (URL dedup handles uniqueness for VIN-less rows).
    if vin and not _is_valid_vin(vin):
        vin = ''
    if not (vin or url):
        return (None, False, None)

    # Lookup must include price_drop_amount + price_drop_at + last_price so
    # the smart-merge logic below can read existing values and preserve them
    # when nothing changed. Without these columns, `existing_drop` resolves
    # to None on every upsert and the UPDATE wipes every drop on every scan.
    row = None
    if vin:
        cur.execute(
            "SELECT id, price, status, first_seen_at, missing_scans, "
            "price_drop_amount, price_drop_at, last_price "
            "FROM dealer_inventory WHERE dealer_id=%s AND UPPER(vin)=%s",
            (dealer_id, vin)
        )
        row = cur.fetchone()
    if not row and url:
        cur.execute(
            "SELECT id, price, status, first_seen_at, missing_scans, "
            "price_drop_amount, price_drop_at, last_price "
            "FROM dealer_inventory WHERE dealer_id=%s AND url=%s "
            "ORDER BY (vin <> '' AND vin IS NOT NULL) DESC, id ASC LIMIT 1",
            (dealer_id, url)
        )
        row = cur.fetchone()

    photos_json = _json.dumps(veh['photos']) if isinstance(veh.get('photos'), list) else None
    raw_json = _json.dumps(veh, default=str)

    if row:
        # UPDATE — COALESCE smart merge: don't overwrite existing fields with NULL
        old_price = row.get('price')
        new_price = veh.get('price')
        existing_drop = row.get('price_drop_amount')
        existing_drop_at = row.get('price_drop_at')
        # Symmetric price-move sanity gate (2026-04-27). Reject impossibly
        # large price moves in EITHER direction. Without this, a cross-merge
        # bug (Ford GT MK IV merging into Ferrari 400i, 2026-04-27) made the
        # 400i's $229k price jump to $1,999,900 (773% rise) and the upsert
        # accepted it. A real ask price doesn't move >25% AND >$100k in a
        # single scan-over-scan delta. If it does, the new price is suspect —
        # most likely cross-contamination, parser error, or stale data —
        # so we keep the existing price untouched until the next scan
        # produces a sane reading.
        if (new_price is not None and old_price is not None
                and new_price > 0 and old_price > 0):
            move = abs(new_price - old_price)
            move_pct = move * 100.0 / max(old_price, new_price)
            if move_pct > 25.0 and move > 100000:
                # Reject this scan's price; preserve existing
                new_price = old_price
        # Only rotate last_price when the ask actually changed — otherwise we
        # lose the "price before the drop" meaning (old bug: every scan
        # overwrote it with current price).
        price_changed = (new_price is not None and old_price is not None
                         and new_price != old_price)
        last_price = old_price if price_changed else row.get('last_price')
        price_drop = existing_drop
        price_drop_at = existing_drop_at
        # Sticky price-drop semantics (2026-04-26): once a drop is recorded,
        # it persists for the lifetime of the row. Status changes (sold,
        # missing) do NOT clear it; price rises do NOT clear it. The dashboard
        # treats price drops as a running tally of dealer pricing activity.
        #
        # Sanity gate: only record drops that look like real price moves —
        # a parser error or stale CMS placeholder once produced "$1.9M drops"
        # on a $19,999 row when the prior scrape misread the price as the
        # MSRP. Reject anything outside the plausible band: either price
        # below $1k, drop more than 25% of the higher price, or drop > $100k.
        if (price_changed and new_price < old_price
                and old_price >= 1000 and new_price >= 1000):
            this_drop = old_price - new_price
            drop_pct = this_drop * 100.0 / old_price
            if drop_pct <= 25.0 and this_drop <= 100000:
                # Replace existing drop only if this new drop is larger — that
                # way price_drop_amount tracks the biggest peak-to-trough swing
                # the car has ever shown, not just the latest scan-over-scan delta.
                if existing_drop is None or this_drop > existing_drop:
                    price_drop = this_drop
                    price_drop_at = now_utc()
        # Else (price rose / unchanged / out of sanity band): keep existing drop.

        cur.execute('''
            UPDATE dealer_inventory SET
                vin          = COALESCE(NULLIF(vin,''), %s),
                year         = COALESCE(year, %s),
                make         = COALESCE(make, %s),
                model        = COALESCE(model, %s),
                trim         = COALESCE(trim, %s),
                ext_color    = COALESCE(ext_color, %s),
                mileage      = COALESCE(%s, mileage),
                price        = COALESCE(%s, price),
                last_price   = %s,
                -- No COALESCE on price_drop* — NULL means "no active drop
                -- right now" (price rose back above its prior low). Scanner
                -- logic above sets these to None when the car is no longer
                -- actually discounted.
                price_drop_amount = %s,
                price_drop_at = %s,
                last_price_change_at = CASE
                    WHEN %s IS NOT NULL AND %s IS DISTINCT FROM price THEN NOW()
                    ELSE last_price_change_at END,
                url          = COALESCE(%s, url),
                photo_url    = COALESCE(%s, photo_url),
                photos       = COALESCE(%s::jsonb, photos),
                raw          = %s::jsonb,
                source_added_at = COALESCE(source_added_at, %s::timestamptz),
                last_seen_at = NOW(),
                missing_scans = 0,
                -- PHANTOM_SOLD_FIX_2026_05_26: a row reappearing in the
                -- current scan is alive by definition. Flip back to active
                -- and clear stale sold-evidence, regardless of prior status.
                -- This handles upstream misfire cascades (e.g. 5/12 batch
                -- of 19 Bentley Denver units wrongly transitioned to sold
                -- while still on the live feed).
                status         = 'active',
                sold_at        = NULL,
                sold_confidence= NULL,
                sold_signals   = NULL,
                updated_at   = NOW()
            WHERE id = %s
        ''', (
            vin or None,
            veh.get('year'), veh.get('make'), veh.get('model'),
            veh.get('trim'), veh.get('ext_color'),
            veh.get('mileage'), new_price,
            last_price, price_drop, price_drop_at,
            new_price, new_price,
            url or None, veh.get('photo_url'), photos_json,
            raw_json, veh.get('source_added_at'),
            row['id'],
        ))
        cur.execute('''INSERT INTO dealer_inventory_history
                         (dealer_id, inventory_id, vin, url, price, mileage, scan_id)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)''',
                    (dealer_id, row['id'], vin or None, url, new_price,
                     veh.get('mileage'), scan_id))
        return (row['id'], False, price_drop)
    else:
        # INSERT
        cur.execute('''
            INSERT INTO dealer_inventory
                (dealer_id, vin, year, make, model, trim, ext_color, mileage,
                 price, url, photo_url, photos, raw, source_added_at, condition)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::timestamptz, %s)
            RETURNING id
        ''', (
            dealer_id, vin or '', veh.get('year'), veh.get('make'),
            veh.get('model'), veh.get('trim'), veh.get('ext_color'),
            veh.get('mileage'), veh.get('price'), url, veh.get('photo_url'),
            photos_json, raw_json, veh.get('source_added_at'),
            (veh.get('condition') or veh.get('car_condition') or None),
        ))
        inv_id = cur.fetchone()['id']
        cur.execute('''INSERT INTO dealer_inventory_history
                         (dealer_id, inventory_id, vin, url, price, mileage, scan_id)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)''',
                    (dealer_id, inv_id, vin or None, url, veh.get('price'),
                     veh.get('mileage'), scan_id))
        # YMMT_MATCH_2026_05_26: tag the new row with its canonical catalog
        # row. Failure is non-fatal — keeps scrape moving even if Anthropic
        # or the catalog is unreachable. A nightly drift-catcher fills gaps.
        try:
            from ymmt_match import resolve_ymmt
            _r = resolve_ymmt(veh.get('year'), veh.get('make'),
                              veh.get('model'), veh.get('trim'),
                              db_conn=cur.connection)
            cur.execute("""UPDATE dealer_inventory
                              SET ymmt_id=%s, ymmt_resolved_at=NOW(),
                                  ymmt_confidence=%s
                            WHERE id=%s""",
                        (_r.get('ymmt_id'),
                         float(_r.get('confidence') or 0), inv_id))
        except Exception as _yerr:
            print(f'[ymmt_match] scraper tag failed inv_id={inv_id}: {_yerr}',
                  flush=True)
        return (inv_id, True, None)




# -- DealerOn / Dealer-eProcess sitemap inventory (CLOUDFLARE_ESCALATE_2026_06_11) --
# Some DealerOn / Dealer-eProcess rooftops (e.g. Chatham Parkway Lexus, dealer
# 102) sit behind a Cloudflare "managed challenge" that FlareSolverr 3.4.6
# CANNOT solve (it times out after 60s, returns code=None / flaresolverr_bad)
# and that residential proxies are IP-banned from. Every HTML page and every
# JSON/data API (/apis/widget/.../getInventory, /api/vhcliaa/..., /resrc/
# searchabledata) returns HTTP 403. The ONE family of endpoints Cloudflare
# lets through un-challenged is the XML sitemaps under /resrc/xmlsitemap/.
#
# The per-rooftop inventory sitemap (/resrc/xmlsitemap/sitemap-inventory-search/
# <city>-<st>-<zip>/) lists every VDP and -- for most units -- embeds a
# <video:video> block whose <video:title> carries "Used <year> <make> <model>
# <trim> <VIN>" and whose <video:thumbnail_loc> carries the hero photo. The VDP
# URL path itself reliably encodes condition/year/make/model for 100% of units.
#
# This yields vin (where the title is populated)/year/make/model/trim/photo/url
# for the full pre-owned lot with NO browser and NO FlareSolverr. price and
# mileage are NOT exposed on any un-challenged endpoint on these sites, so they
# come back NULL -- the upsert stores the row on (vin|url) regardless, and a
# later valuation pass can backfill from VIN. See agent-A2 report 2026-06-11.
_DEALERON_SITEMAP_INDEX = '/resrc/xmlsitemap/xml-sitemaps/'
_SM_LOC = '{http://www.sitemaps.org/schemas/sitemap/0.9}loc'
_SM_URL = '{http://www.sitemaps.org/schemas/sitemap/0.9}url'
_VID = '{http://www.google.com/schemas/sitemap-video/1.1}video'
_VID_TITLE = '{http://www.google.com/schemas/sitemap-video/1.1}title'
_VID_THUMB = '{http://www.google.com/schemas/sitemap-video/1.1}thumbnail_loc'
# /auto/<condition>-<year>-<make>-<model...>-<city>_<st>-<st>/<id>/  -- make is a
# single token; model is everything up to the trailing "-<city>-<2-letter-st>/".
_DEALERON_VDP_RE = re.compile(
    r'/auto/(?P<cond>[a-z]+)-(?P<year>\d{4})-(?P<make>[a-z0-9]+)-'
    r'(?P<model>[a-z0-9\-]+?)-[a-z0-9_]+-[a-z]{2}/\d+/?$', re.I)
_VIN_RE = re.compile(r'\b([A-HJ-NPR-Z0-9]{17})\b')


def _dealeron_zip_from_url(base_url):
    """Pull a 5-digit rooftop zip from the dealer's stored URL query (cy=NNNNN)
    if present -- lets us target the dealer's own rooftop sitemap on multi-store
    domains. Returns the zip string or None."""
    m = re.search(r'[?&]cy=(\d{5})', base_url or '')
    return m.group(1) if m else None


def _parse_dealeron_vdp(loc, title_text, thumb):
    """Build a vehicle dict from one sitemap <url> entry. Returns None for
    non-inventory or new-car URLs."""
    m = _DEALERON_VDP_RE.search(loc)
    if not m:
        return None
    cond = (m.group('cond') or '').lower()
    # Only pre-owned side. 'new' VDPs share the same URL shape.
    if cond == 'new':
        return None
    year = int(m.group('year'))
    make = m.group('make').replace('-', ' ').strip().title()
    model = m.group('model').replace('-', ' ').strip().upper()
    veh = {
        'year': year,
        'make': make,
        'model': model,
        'url': loc,
        'condition': 'certified' if cond in ('certified', 'cpo') else 'used',
    }
    title_text = (title_text or '').strip()
    if title_text:
        vm = _VIN_RE.search(title_text)
        if vm:
            veh['vin'] = vm.group(1).upper()
        # Trim = text after "<year> <make> <model> " and before the VIN.
        # e.g. "Used 2022 Lexus LX 600 F SPORT JTJMB7CX7N4005853"
        t = title_text
        if vm:
            t = t[:vm.start()].strip()
        t = re.sub(r'^(?:used|certified|pre[- ]?owned|new)\s+', '', t, flags=re.I)
        t = re.sub(r'^\d{4}\s+', '', t)
        t = re.sub(r'^' + re.escape(make) + r'\s+', '', t, flags=re.I)
        for mw in model.split():
            t = re.sub(r'^' + re.escape(mw) + r'\s+', '', t, flags=re.I)
        t = t.strip()
        if t:
            veh['trim'] = t[:60]
    if thumb:
        veh['photo_url'] = thumb.strip()
    return veh


def fetch_dealeron_sitemap_inventory(base_url, sess, home_zip=None):
    """Pull pre-owned inventory for a DealerOn / Dealer-eProcess rooftop from its
    un-challenged XML sitemaps (no FlareSolverr; works through Cloudflare).

    Returns a list of vehicle dicts (vin?/year/make/model/trim?/photo?/url), or
    None if the sitemap index is unreachable (hard failure -> caller preserves
    existing inventory rather than wiping it)."""
    base = base_url.rstrip('/')
    idx_url = base + _DEALERON_SITEMAP_INDEX
    # /resrc/ endpoints answer to a direct GET; force the direct tier so a
    # dealer pinned to preferred_tier='flaresolverr' (because the HTML is
    # CF-walled) doesn't route the sitemap through FS and trip the same
    # unsolvable challenge.
    code, _f, body = fetch(idx_url, sess, tier='direct')
    if code != 200 or not body or '<sitemapindex' not in body:
        print('  [dealeron] sitemap index ' + idx_url + ' -> code=' + str(code) +
              ' len=' + str(len(body or '')) + ' (no index) -- aborting handler',
              flush=True)
        return None
    try:
        root = ET.fromstring(body.encode('utf-8') if isinstance(body, str) else body)
    except Exception as e:
        print('  [dealeron] sitemap index parse error: ' + str(e), flush=True)
        return None
    inv_sitemaps = []
    for loc in root.iter(_SM_LOC):
        u = (loc.text or '').strip()
        if 'sitemap-inventory-search/' in u:
            # only the per-rooftop ones (.../sitemap-inventory-search/<rooftop>/),
            # not the bare .../sitemap-inventory-search/ index sub-map.
            tail = u.rstrip('/').rsplit('/sitemap-inventory-search/', 1)[-1]
            if tail and '/' not in tail:
                inv_sitemaps.append(u)
    if not inv_sitemaps:
        print('  [dealeron] no per-rooftop inventory sitemaps in index', flush=True)
        return None
    # Prefer the rooftop whose zip matches the dealer's own URL (cy=NNNNN);
    # otherwise take the first (DealerOn lists the home rooftop first).
    home_zip = home_zip or _dealeron_zip_from_url(base_url)
    chosen = None
    if home_zip:
        for u in inv_sitemaps:
            if u.rstrip('/').endswith(home_zip) or ('-' + home_zip + '/') in u:
                chosen = [u]
                break
    if not chosen:
        chosen = [inv_sitemaps[0]]
    print('  [dealeron] inventory sitemaps found=' + str(len(inv_sitemaps)) +
          ' home_zip=' + (home_zip or '-') + ' using=' +
          chosen[0].rsplit('/sitemap-inventory-search/', 1)[-1], flush=True)

    vehicles = []
    seen = set()
    for sm_url in chosen:
        code, _f, sbody = fetch(sm_url, sess, tier='direct')
        if code != 200 or not sbody:
            print('  [dealeron] inventory sitemap ' + sm_url + ' -> code=' +
                  str(code) + ' skip', flush=True)
            continue
        try:
            sroot = ET.fromstring(sbody.encode('utf-8') if isinstance(sbody, str) else sbody)
        except Exception as e:
            print('  [dealeron] inventory sitemap parse error: ' + str(e), flush=True)
            continue
        for url_el in sroot.iter(_SM_URL):
            loc_el = url_el.find(_SM_LOC)
            if loc_el is None or not loc_el.text:
                continue
            loc = loc_el.text.strip()
            if '/auto/' not in loc:
                continue
            title_text = ''
            thumb = ''
            vid = url_el.find(_VID)
            if vid is not None:
                t_el = vid.find(_VID_TITLE)
                if t_el is not None and t_el.text:
                    title_text = _html.unescape(t_el.text)
                th_el = vid.find(_VID_THUMB)
                if th_el is not None and th_el.text:
                    thumb = th_el.text.strip()
            veh = _parse_dealeron_vdp(loc, title_text, thumb)
            if not veh:
                continue
            key = veh.get('vin') or veh.get('url')
            if key in seen:
                continue
            seen.add(key)
            vehicles.append(veh)
    n_vin = sum(1 for v in vehicles if v.get('vin'))
    print('  [dealeron] parsed ' + str(len(vehicles)) + ' pre-owned VDPs (' +
          str(n_vin) + ' with VIN, ' + str(len(vehicles) - n_vin) +
          ' VIN-less) -- price/mileage not exposed (CF-walled), stored NULL',
          flush=True)
    return vehicles

# ── Scanner ──────────────────────────────────────────────────────────────
class DealerScanner:
    def __init__(self, dealer_row):
        self.dealer = dict(dealer_row)
        self.dealer_id = self.dealer['id']
        self.base_url = self.dealer['url'].rstrip('/')
        self.sess = _session()

    @classmethod
    def from_dealer_id(cls, dealer_id):
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute('SELECT * FROM dealers WHERE id = %s', (dealer_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError(f'Dealer {dealer_id} not found')
            return cls(row)

    @classmethod
    def create_and_scan(cls, url, name, notes=None):
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute('''INSERT INTO dealers (name, url, notes)
                           VALUES (%s, %s, %s)
                           ON CONFLICT (url) DO UPDATE SET name = EXCLUDED.name
                           RETURNING *''',
                        (name, url.rstrip('/'), notes))
            row = cur.fetchone()
            conn.commit()
        return cls(row).run()

    # ─── run ───
    def run(self):
        started = time.time()
        # Per-scan sitemap-lastmod cache — cleared so stale values from a
        # previous dealer's scan don't bleed into this one.
        _SITEMAP_LASTMOD.clear()
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute('''INSERT INTO dealer_scans (dealer_id, status)
                           VALUES (%s, 'running') RETURNING id''', (self.dealer_id,))
            scan_id = cur.fetchone()['id']
            conn.commit()

        stats = {'scan_id': scan_id, 'vehicles_found': 0, 'new_count': 0,
                 'sold_count': 0, 'missing_count': 0, 'price_drop_count': 0,
                 'colors_detected': 0,
                 'platform_detected': None, 'status': 'running', 'error': None,
                 'tier': 'direct'}
        try:
            # 0. DealerOn / Dealer-eProcess sitemap+JSON-LD fast-path
            #    (CLOUDFLARE_ESCALATE_2026_06_11). Fires BEFORE the platform
            #    fingerprint so it works even when the stored `platform` column
            #    is a generic value (Chatham Parkway Lexus dealer 102 is stored
            #    as 'ai-generated', which is in _STORED_KNOWN and would otherwise
            #    skip the homepage-403 rescue entirely). Gated purely on the
            #    dealer's scrape_config so no other rooftop is affected.
            _sc = self.dealer.get('scrape_config') or {}
            if isinstance(_sc, dict) and (_sc.get('platform') == 'dealeron') \
                    and (_sc.get('strategy') == 'sitemap+jsonld'):
                stats['platform_detected'] = 'dealeron'
                self._scan_dealeron_jsonld(scan_id, _sc, stats, started)
                return stats

            # 1. Platform fingerprint.
            # If the dealer has a known stored platform from onboarding, skip
            # the homepage probe entirely. The probe was the wedge point on
            # 2026-05-12 when FlareSolverr got stuck on Manheim Imports'
            # .lds-ring loading spinner (a permanent UI element FS misread
            # as a Cloudflare challenge). One wedged probe blocked the whole
            # scan for 4+ hours. All platform fast-paths below either gate
            # on `self.dealer.get('platform')` (dealerinspire/ect/greenlight)
            # or accept a pre-set `platform` local (aan/ridemotive/etc), so
            # re-fingerprinting via homepage HTML is dead weight here.
            stored_platform = (self.dealer.get('platform') or '').strip().lower()
            _STORED_KNOWN = {
                'aan', 'wordpress', 'shopify', 'autorevo', 'autodealercms',
                'frazer', 'dealercenter', 'dealer.com', 'dealerinspire',
                'greenlight', 'ridemotive', 'dealer-eprocess', 'vinsolutions',
                'cdk', 'homenet', 'ect', 'dealeron', 'autotrader-embed',
                'ai-generated', 'jazel',
            }
            if stored_platform in _STORED_KNOWN:
                platform = stored_platform
                method = ''
                _CURRENT_TIER['tier'] = 'direct'
                body = ''
            else:
                _CURRENT_TIER['tier'] = 'direct'
                code, _f, body = fetch(self.base_url, self.sess)
                # CLOUDFLARE_ESCALATE_2026_06_11: a 403/503/401 on the homepage
                # is the Cloudflare "Just a moment..." interstitial (or an
                # Akamai/WAF block). The committed 9a0a663 escalation jumped to
                # FlareSolverr, but (a) it only fired when flaresolverr_healthy()
                # and (b) it treated FS's OWN challenge-solve failure (code=None,
                # body='flaresolverr_bad:...') as a usable page. On hardened CF
                # rooftops (Chatham Parkway Lexus, dealer 102) FlareSolverr 3.4.6
                # times out solving the managed challenge, so neither direct nor
                # FS yields HTML. Before aborting blocked, try the DealerOn /
                # Dealer-eProcess sitemap rescue, whose /resrc/ XML endpoints
                # Cloudflare serves un-challenged.
                cf_blocked = (code in (403, 503, 401)) or (not body) \
                    or (isinstance(body, str) and (
                        'Just a moment' in body or body.startswith('flaresolverr')))
                if cf_blocked and dealer_fetchers.flaresolverr_healthy():
                    # Homepage is blocked — try FlareSolverr for the whole scan.
                    _CURRENT_TIER['tier'] = 'flaresolverr'
                    code, _f, body = fetch(self.base_url, self.sess)
                    cf_blocked = (code != 200) or (not body) \
                        or (isinstance(body, str) and (
                            'Just a moment' in body or body.startswith('flaresolverr')))
                if cf_blocked:
                    # Last resort before giving up: DealerOn/eProcess sitemap.
                    # Reset tier to direct — the /resrc/ sitemap is the ONE thing
                    # CF lets through, and routing it via FS would re-trip the
                    # unsolvable challenge.
                    _CURRENT_TIER['tier'] = 'direct'
                    stats['tier'] = 'direct'
                    # AUTO_DISCOVER_DEALERON_2026_06_11: CF-walled DealerOn ->
                    # full per-VDP JSON-LD path (price+mileage), which
                    # auto-discovers its rooftop sitemap from the un-challenged
                    # /resrc/ index. Self-finalizes (sets status ok/blocked).
                    # Locks in Chatham-quality for any NEW CF DealerOn dealer
                    # with zero hand-config (was: sitemap-only, no price/miles).
                    self._scan_dealeron_jsonld(
                        scan_id, (self.dealer.get('scrape_config') or {}),
                        stats, started)
                    return stats
                platform, method = detect_platform(body or '')

            # Cars Commerce listings fast-path (CARSCOMMERCE_2026_05_29).
            # Runs BEFORE the Algolia path: on newer DealerInspire/group sites
            # the Algolia index is the shared nationwide pool, while Cars
            # Commerce (scoped by source_id) is the store's real rooftop lot.
            if self.dealer.get('platform') == 'dealerinspire' \
                    and (self.dealer.get('scrape_config') or {}).get('carscommerce'):
                stats['platform_detected'] = 'dealerinspire'
                cc = fetch_carscommerce_inventory(self.dealer['scrape_config']['carscommerce'])
                if cc is not None:
                    if not cc:
                        prior = self._zero_vehicle_abort_check()
                        if prior:
                            stats['status'] = 'blocked'
                            stats['error'] = ('carscommerce returned 0 but '
                                              f'{prior} active — source_id/api_key may '
                                              'be stale, scan aborted, inventory preserved')
                            self._update_dealer('dealerinspire', 'carscommerce', scan_id,
                                                stats['error'][:200], stats['tier'])
                            self._finalize(scan_id, stats, started)
                            return stats
                    print(f'  carscommerce returned {len(cc)} vehicles', flush=True)
                    self._process_aan(scan_id, cc, stats)
                    stats['colors_detected'] = self._detect_colors()
                    self._update_dealer('dealerinspire', 'carscommerce', scan_id, 'ok', stats['tier'])
                    stats['status'] = 'ok'
                    self._finalize(scan_id, stats, started)
                    return stats
                print('  carscommerce path failed — falling through', flush=True)

            # DealerInspire Algolia fast-path — runs before any other
            # branching. Gated on the dealer's STORED platform column
            # (set during initial onboarding/discovery), not the live
            # homepage fingerprint, because the static homepage HTML
            # sometimes omits the 'dealerinspire' literal even though
            # the listing pages embed Algolia config. If Algolia answers,
            # we're done in ~3 seconds and skip the entire scrape_config /
            # universal-VDP machinery below.
            if self.dealer.get('platform') == 'dealerinspire':
                stats['platform_detected'] = 'dealerinspire'
                # Pull cached Algolia keys from scrape_config.algolia so the
                # FlareSolverr config-extraction step is skipped on every scan
                # after the first successful discovery. The keys are public
                # browser tokens that rotate rarely; caching makes this path
                # 100% reliable instead of dependent on FlareSolverr's flaky
                # behavior against Cloudflare-fronted sites.
                cached_algolia = (self.dealer.get('scrape_config') or {}).get('algolia') \
                    if isinstance(self.dealer.get('scrape_config'), dict) else None
                if cached_algolia:
                    print(f'  using cached algolia config (app_id={cached_algolia.get("app_id")})', flush=True)
                di, resolved_cfg = fetch_dealer_inspire_inventory(
                    self.base_url, self.sess, cached_cfg=cached_algolia)
                if di is not None:
                    if not di:
                        prior = self._zero_vehicle_abort_check()
                        if prior:
                            stats['status'] = 'blocked'
                            stats['error'] = (
                                f'dealerinspire-algolia returned 0 vehicles but '
                                f'{prior} were active — keys may have rotated or '
                                f'index drained, scan aborted, inventory preserved'
                            )
                            self._update_dealer('dealerinspire', 'algolia', scan_id,
                                                stats['error'][:200], stats['tier'])
                            self._finalize(scan_id, stats, started)
                            return stats
                    print(f'  dealerinspire algolia returned {len(di)} vehicles', flush=True)
                    # Persist newly-discovered cfg to dealers.scrape_config.algolia
                    # so subsequent scans skip FlareSolverr entirely. Idempotent
                    # — re-writes the same JSON if the cfg hadn't changed, which
                    # is fine and cheaper than diffing.
                    if resolved_cfg and resolved_cfg != cached_algolia:
                        self._persist_algolia_cfg(resolved_cfg)
                        print(f'  cached algolia config to scrape_config (app_id={resolved_cfg.get("app_id")})', flush=True)
                    self._process_aan(scan_id, di, stats)
                    stats['colors_detected'] = self._detect_colors()
                    self._update_dealer('dealerinspire', 'algolia', scan_id, 'ok', stats['tier'])
                    stats['status'] = 'ok'
                    self._finalize(scan_id, stats, started)
                    return stats
                # Algolia failed (config not in homepage HTML, API down, or —
                # increasingly — a 403 because DealerInspire migrated the store
                # onto Cars Commerce and DISABLED the old Algolia account).
                # CARSCOMMERCE_AUTOFALLBACK_2026_06_18: before giving up to the
                # universal path, try to auto-migrate this dealer to Cars
                # Commerce — pull its SEARCH_SERVICE ccid/apiKey off the SRP,
                # derive the rooftop source_id from the algolia rooftop_filter
                # api_id, validate the count, persist, and use it. Makes the
                # whole DealerInspire fleet self-heal on migration instead of
                # silently aborting until someone notices.
                cc_cfg = self._autodiscover_carscommerce()
                if cc_cfg:
                    cc = fetch_carscommerce_inventory(cc_cfg)
                    if cc:
                        # Group-feed guard. Baseline = the LARGER of the current
                        # active count and the recent historical max scan size;
                        # the live active count alone is unsafe because a prior
                        # partial/degraded scan can shrink it (Ferrari #4: a
                        # half-dead algolia run cut 169->49, which made an
                        # active-only gate reject the CORRECT 179-car carscommerce
                        # result). Only refuse when the result dwarfs that
                        # baseline — a real unscoped group pool is thousands.
                        # GATE_FIX_2026_06_19.
                        prior = self._zero_vehicle_abort_check() or 0
                        baseline = max(prior, self._recent_max_scan_count())
                        if len(cc) > max(1000, baseline * 4):
                            print(f'  [cc-autofallback] carscommerce returned {len(cc)} '
                                  f'vs baseline {baseline} — looks like an unscoped '
                                  f'group feed, NOT migrating; falling through', flush=True)
                        else:
                            print(f'  [cc-autofallback] algolia dead -> carscommerce '
                                  f'returned {len(cc)} vehicles; migrating dealer '
                                  f'{self.dealer_id} to carscommerce', flush=True)
                            self._persist_carscommerce_cfg(cc_cfg)
                            self._process_aan(scan_id, cc, stats)
                            stats['colors_detected'] = self._detect_colors()
                            self._update_dealer('dealerinspire', 'carscommerce',
                                                scan_id, 'ok', stats['tier'])
                            stats['status'] = 'ok'
                            self._finalize(scan_id, stats, started)
                            return stats
                # fall through to the legacy universal path so the dealer
                # still gets a scan attempt.
                print('  dealerinspire algolia path failed — falling back to universal', flush=True)

            # ECT (Exotic Car Trader) marketplace — Webflow + Bubble.io. Walks
            # listings sitemap newest-first, parses Product JSON-LD per VDP,
            # filters to listings created in the last 90 days. See dealer_ect.py.
            if self.dealer.get('platform') == 'ect':
                stats['platform_detected'] = 'ect'
                from dealer_ect import fetch_ect_inventory
                ect_v = fetch_ect_inventory(self.base_url, self.sess, max_age_days=90)
                # ECT is a purpose-built handler — if it can't get inventory,
                # the universal path WILL find ~17 unrelated URLs via generic
                # crawl and wipe everything as "missing". Always abort here;
                # never fall through. (2026-05-02 incident: handler returned
                # None on CF block, fall-through universal-crawl wiped 451.)
                if ect_v is None or not ect_v:
                    prior = self._zero_vehicle_abort_check()
                    if prior:
                        stats['status'] = 'blocked'
                        reason = ('handler hard failure (CF/rate-limit)'
                                  if ect_v is None else 'handler returned 0 vehicles')
                        stats['error'] = (
                            f'ect {reason} but {prior} active rows exist — '
                            f'scan aborted, inventory preserved'
                        )
                    else:
                        stats['status'] = 'error'
                        stats['error'] = ('ect handler failed and no prior '
                                          'inventory to protect')
                    self._update_dealer('ect', 'sitemap_jsonld', scan_id,
                                        stats['error'][:200], stats['tier'])
                    self._finalize(scan_id, stats, started)
                    return stats
                print(f'  ect sitemap returned {len(ect_v)} vehicles (last 90 days)', flush=True)
                self._process_aan(scan_id, ect_v, stats)
                stats['colors_detected'] = self._detect_colors()
                self._update_dealer('ect', 'sitemap_jsonld', scan_id, 'ok', stats['tier'])
                stats['status'] = 'ok'
                self._finalize(scan_id, stats, started)
                return stats

            # If the dealer has a stored scrape_config, prefer the generic
            # config-driven path - UNLESS a known native platform already
            # owns the routing. NATIVE_CFG_ROUTING_2026_05_29: dealer.com
            # (dealercom_list_paths) and other native platforms consume their
            # own scrape_config keys in their own branch; don't hijack those
            # into the AI-generated extractor (which only understands algolia/
            # extraction configs and would silently return ~0 for them).
            if self.dealer.get('scrape_config') and stored_platform in ('', 'custom', 'ai-generated'):
                platform = 'ai-generated'
                method = 'config-driven'
            # Auto-spawn AI discovery when no fingerprint matched ('custom').
            # Skipped if a discovery already failed today (don't burn $4 every
            # hour on a stuck dealer).
            elif platform == 'jsonvehicle':
                import json as _jv_json
                _cfg = _build_jsonvehicle_config(self.base_url)
                try:
                    with get_conn() as cn, cn.cursor() as cu:
                        cu.execute("UPDATE dealers SET scrape_config=%s::jsonb, scrape_config_version=COALESCE(scrape_config_version,0)+1, scrape_config_at=now(), platform='ai-generated', scrape_method='config-driven' WHERE id=%s", (_jv_json.dumps(_cfg), self.dealer_id))
                        cn.commit()
                    self.dealer['scrape_config'] = _cfg
                    print('  jsonvehicle platform - auto-built config-driven config', flush=True)
                except Exception as e:
                    print('  jsonvehicle auto-config failed: ' + str(e), flush=True)
                platform = 'ai-generated'
                method = 'config-driven'
            elif platform == 'custom':
                already_tried = self._discovery_tried_today()
                if not already_tried:
                    print(f'  no platform fingerprint matched — spawning AI discovery agent', flush=True)
                    if self._auto_discover():
                        # Reload dealer row to pick up the new scrape_config
                        with get_conn() as cn, cn.cursor() as cu:
                            cu.execute('SELECT * FROM dealers WHERE id=%s', (self.dealer_id,))
                            self.dealer = dict(cu.fetchone())
                        if self.dealer.get('scrape_config'):
                            platform = 'ai-generated'
                            method = 'config-driven'
                            print(f'  AI discovery succeeded — proceeding with config-driven extraction', flush=True)
                        else:
                            print(f'  AI discovery returned no config — falling back to universal path', flush=True)
                else:
                    print(f'  skipping AI discovery (already attempted today)', flush=True)
            stats['platform_detected'] = platform
            # Choose the default tier for this platform (may escalate below).
            # Dealer-level `preferred_tier` overrides the platform default — used
            # for hosts with IP-reputation issues (e.g., TXT Charlie's Hostinger
            # LiteSpeed cache poisons our Contabo IP, serving 302→127.0.0.1 on
            # random VDPs; pinning to direct_proxy routes via residential IP).
            picked = (self.dealer.get('preferred_tier') or '').strip() \
                     or dealer_fetchers.tier_for_platform(platform)
            if picked != _CURRENT_TIER['tier']:
                _CURRENT_TIER['tier'] = picked
            stats['tier'] = _CURRENT_TIER['tier']

            # 2. Platform-specific fast paths first

            # Greenlight Automotive Solutions — JSON bridge API. One curl
            # returns full inventory, no FlareSolverr. The greenlight fingerprint
            # lives inside the JS bundle, not the homepage HTML, so live-detect
            # can't see it — gate on the dealer's STORED platform (mirrors the
            # dealerinspire-algolia fast path's logic).
            if self.dealer.get('platform') == 'greenlight':
                platform = 'greenlight'  # so _update_dealer preserves it
                cached_did = None
                cfg = self.dealer.get('scrape_config') or {}
                if isinstance(cfg, dict):
                    cached_did = cfg.get('dealership_id')
                gl = fetch_greenlight_inventory(self.base_url, self.sess,
                                                dealership_id=cached_did)
                if gl is not None:
                    if not gl:
                        prior = self._zero_vehicle_abort_check()
                        if prior:
                            stats['status'] = 'blocked'
                            stats['error'] = (
                                f'greenlight bridge returned 0 vehicles but '
                                f'{prior} were active — dealership_id may have '
                                f'rotated, scan aborted, inventory preserved'
                            )
                            self._update_dealer(platform, 'api', scan_id,
                                                stats['error'][:200], stats['tier'])
                            self._finalize(scan_id, stats, started)
                            return stats
                    self._process_aan(scan_id, gl, stats)
                    stats['colors_detected'] = self._detect_colors()
                    self._update_dealer(platform, 'api', scan_id, 'ok', stats['tier'])
                    stats['status'] = 'ok'
                    self._finalize(scan_id, stats, started)
                    return stats

            # RideMotive (Tactical Fleet, tfc.app.ridemotive.com network) —
            # public Algolia search-only credentials embedded in SSR HTML.
            # POST to Algolia, filter by dealer_id (discovered from sitemap
            # VDP URL suffixes), get the full inventory in one round-trip.
            # Order: Algolia first; on failure fall through to the legacy
            # FlareSolverr-render discovery path inside discover_via_ridemotive.
            if platform == 'ridemotive':
                rm = fetch_ridemotive_inventory(self.base_url, self.sess)
                if rm is not None:
                    if not rm:
                        prior = self._zero_vehicle_abort_check()
                        if prior:
                            stats['status'] = 'blocked'
                            stats['error'] = (
                                f'ridemotive algolia returned 0 vehicles but '
                                f'{prior} were active — credentials may have '
                                f'rotated, scan aborted, inventory preserved'
                            )
                            self._update_dealer(platform, 'algolia', scan_id,
                                                stats['error'][:200], stats['tier'])
                            self._finalize(scan_id, stats, started)
                            return stats
                    self._process_aan(scan_id, rm, stats)
                    stats['colors_detected'] = self._detect_colors()
                    self._update_dealer(platform, 'algolia', scan_id, 'ok', stats['tier'])
                    stats['status'] = 'ok'
                    self._finalize(scan_id, stats, started)
                    return stats
                # Algolia missed (no dealer_ids in sitemap, or network) —
                # fall through to FlareSolverr-render discovery below.

            if platform == 'aan':
                aan = fetch_aan_inventory(self.base_url, self.sess)
                if aan is not None:
                    if not aan:
                        prior = self._zero_vehicle_abort_check()
                        if prior:
                            stats['status'] = 'blocked'
                            stats['error'] = (
                                f'aan /api/cars returned 0 vehicles but {prior} were '
                                f'active — feed likely empty or auth-blocked, '
                                f'scan aborted, inventory preserved'
                            )
                            self._update_dealer(platform, 'api', scan_id,
                                                stats['error'][:200], stats['tier'])
                            self._finalize(scan_id, stats, started)
                            return stats
                    self._process_aan(scan_id, aan, stats)
                    stats['colors_detected'] = self._detect_colors()
                    self._update_dealer(platform, 'api', scan_id, 'ok', stats['tier'])
                    stats['status'] = 'ok'
                    self._finalize(scan_id, stats, started)
                    return stats
                # AAN detected but /api/cars didn't cooperate — fall through to
                # universal extraction on the same tier.

            # NOTE: dealerinspire Algolia fast path lives above (gated on
            # the dealer's stored platform). It runs before scrape_config /
            # ai-generated branching so a single HTTP call replaces the
            # 100+ FlareSolverr VDP fetches that the universal path needs.

            # Dealer.com getInventory JSON-API fast path (DDC_GETINVENTORY_2026_05_29).
            # Gated on stored platform + scrape_config.dealercom_getinventory so it
            # only fires for dealers explicitly configured with a captured body.
            if self.dealer.get('platform') == 'dealer.com' \
                    and (self.dealer.get('scrape_config') or {}).get('dealercom_getinventory'):
                dc = fetch_dealercom_getinventory(
                    self.base_url, self.sess,
                    self.dealer['scrape_config']['dealercom_getinventory'])
                if dc is not None:
                    if not dc:
                        prior = self._zero_vehicle_abort_check()
                        if prior:
                            stats['status'] = 'blocked'
                            stats['error'] = ('dealer.com getInventory returned 0 but '
                                              f'{prior} active — body/account_filter may '
                                              'be stale, scan aborted, inventory preserved')
                            self._update_dealer('dealer.com', 'getinventory-api', scan_id,
                                                stats['error'][:200], stats['tier'])
                            self._finalize(scan_id, stats, started)
                            return stats
                    print(f'  dealer.com getInventory returned {len(dc)} vehicles', flush=True)
                    self._process_aan(scan_id, dc, stats)
                    stats['colors_detected'] = self._detect_colors()
                    self._update_dealer('dealer.com', 'getinventory-api', scan_id, 'ok', stats['tier'])
                    stats['status'] = 'ok'
                    self._finalize(scan_id, stats, started)
                    return stats
                print('  dealer.com getInventory path failed — falling through to crawl', flush=True)

            # Jazel SRP data-vehicle fast-path (JAZEL_DATAVEHICLE_2026_06_11).
            # Gated on stored platform='jazel'. One GET per SRP page parses the
            # inline data-vehicle JSON — replaces the universal ~781-URL crawl
            # that hung 10+ min returning 0 vehicles (Tamiami Ford, dealer 98).
            # Optional scrape_config.jazel_list_path overrides the used-vehicles
            # listing path for non-standard rooftops.
            if self.dealer.get('platform') == 'jazel':
                jz = fetch_jazel_inventory(
                    self.base_url, self.sess,
                    list_path=(self.dealer.get('scrape_config') or {}).get('jazel_list_path'))
                if jz is not None:
                    if not jz:
                        prior = self._zero_vehicle_abort_check()
                        if prior:
                            stats['status'] = 'blocked'
                            stats['error'] = ('jazel SRP returned 0 vehicles but '
                                              f'{prior} active — SRP markup/list path '
                                              'may have changed, scan aborted, '
                                              'inventory preserved')
                            self._update_dealer('jazel', 'srp-data-vehicle', scan_id,
                                                stats['error'][:200], stats['tier'])
                            self._finalize(scan_id, stats, started)
                            return stats
                    print(f'  jazel SRP returned {len(jz)} vehicles', flush=True)
                    self._process_aan(scan_id, jz, stats)
                    stats['colors_detected'] = self._detect_colors()
                    self._update_dealer('jazel', 'srp-data-vehicle', scan_id, 'ok', stats['tier'])
                    stats['status'] = 'ok'
                    self._finalize(scan_id, stats, started)
                    return stats
                print('  jazel SRP path failed (page-1 fetch) — falling through to crawl', flush=True)

            # AI-generated config-driven extraction (Ferrari, novel platforms).
            # Reads dealers.scrape_config JSONB produced by discover_dealer.py
            # and runs config_driven_extractor.fetch_inventory against it.
            if platform == 'ai-generated':
                cfg = self.dealer.get('scrape_config')
                if cfg:
                    try:
                        from config_driven_extractor import fetch_inventory as _cfg_fetch
                        vehs = _cfg_fetch(cfg, self.base_url, sess=self.sess)
                        if vehs:
                            print(f'  config-driven extractor returned {len(vehs)} vehicles', flush=True)
                            self._process_aan(scan_id, vehs, stats)  # reuse same upsert flow (no AAN-specific flags)
                            stats['colors_detected'] = self._detect_colors()
                            self._update_dealer(platform, 'config-driven', scan_id, 'ok', stats['tier'])
                            stats['status'] = 'ok'
                            self._finalize(scan_id, stats, started)
                            return stats
                        else:
                            print(f'  config-driven extractor returned 0 vehicles — config may need re-discovery', flush=True)
                    except Exception as e:
                        print(f'  config-driven extractor error: {e}', flush=True)
                        traceback.print_exc()
                # Fall through to universal path if config missing or extraction empty.

            # 3. Universal path: discover + per-VDP extract, with one escalation if
            #    the first pass gets suspiciously low results or high fetch-fail rate.
            def _scan_pass():
                # Platform-specific discovery first (dealer.com uses /new/, /used/
                # VDP URL patterns; sitemap only lists category index pages).
                if platform == 'dealer.com':
                    urls = discover_via_dealer_com(self.base_url, self.sess,
                        list_paths=(self.dealer.get('scrape_config') or {}).get('dealercom_list_paths'))
                    if len(urls) < 5:
                        urls = list(set(urls) | set(
                            discover_via_sitemap(self.base_url, self.sess)))
                elif platform == 'dealerinspire':
                    urls = discover_via_dealer_inspire(self.base_url, self.sess)
                    if len(urls) < 5:
                        urls = list(set(urls) | set(
                            discover_via_sitemap(self.base_url, self.sess)))
                elif platform == 'ridemotive':
                    urls = discover_via_ridemotive(self.base_url, self.sess)
                else:
                    # Per-dealer skip_sitemap bypasses doomed sitemap probes
                    # for sites whose WAF rate-blocks /sitemap_index.xml
                    # (Ferrari of Washington / Sucuri — 2026-05-13 hang root cause).
                    # Their /inventory/ page works fine; the crawl path finds
                    # all VDPs without ever touching the WAF's tripwire.
                    if self.dealer.get('skip_sitemap'):
                        print(f'  skipping sitemap discovery (dealers.skip_sitemap=TRUE)', flush=True)
                        urls = list(discover_via_crawl(self.base_url, self.sess))
                    else:
                        urls = discover_via_sitemap(self.base_url, self.sess)
                        if len(urls) < 5:
                            urls = list(set(urls) | set(
                                discover_via_crawl(self.base_url, self.sess)))
                urls = urls[:CRAWL_MAX_URLS]
                # Live progress: stamp urls_total now (after discovery) and
                # bump urls_fetched every 5 VDPs so the dashboard can show
                # a real progress bar while the scan runs. Cheap UPDATE,
                # negligible vs the FlareSolverr fetch latency dominating.
                try:
                    with get_conn() as _pconn, _pconn.cursor() as _pcur:
                        _pcur.execute(
                            "UPDATE dealer_scans SET urls_total = %s, urls_fetched = 0 WHERE id = %s",
                            (len(urls), scan_id))
                        _pconn.commit()
                except Exception:
                    pass  # progress tracking is best-effort, never fail the scan
                fails = 0
                vehs = []
                _pass_started = time.time()
                for idx, u in enumerate(urls):
                    # CRAWL_WALLCLOCK_2026_06_11: bail out of a runaway crawl
                    # pass so a slow site can't wedge the queue for hours. We
                    # return what we have; the zero/fetch-fail guards below
                    # still protect existing inventory from a bad partial.
                    if time.time() - _pass_started > CRAWL_PASS_DEADLINE_SEC:
                        print(f'  crawl pass hit {CRAWL_PASS_DEADLINE_SEC}s wall-clock '
                              f'deadline at {idx}/{len(urls)} URLs — stopping pass',
                              flush=True)
                        break
                    c, _ff, vbody = fetch(u, self.sess)
                    # Per-URL retry on a fresh session+IP. DataImpulse rotates
                    # exit nodes per fresh TCP connection — using a new Session
                    # forces a new connection, dodging a poisoned-cache IP that
                    # the previous Session got stuck on. Only retry on transient
                    # failures (None, 599, 5xx) — don't waste a roundtrip on
                    # genuine 404/200-with-empty.
                    if (c is None or c == 599 or (c and 500 <= c <= 599)) \
                            and _CURRENT_TIER['tier'] in ('direct_proxy', 'flaresolverr_proxy'):
                        try:
                            fresh = _session()
                            c, _ff, vbody = fetch(u, fresh)
                        except Exception:
                            pass
                    if c != 200 or not vbody:
                        fails += 1
                    else:
                        veh = extract_vehicle(u, vbody)
                        if veh:
                            vehs.append(veh)
                    # Progress checkpoint every 5 VDPs (or on the last one)
                    if (idx + 1) % 5 == 0 or (idx + 1) == len(urls):
                        try:
                            with get_conn() as _pconn, _pconn.cursor() as _pcur:
                                _pcur.execute(
                                    "UPDATE dealer_scans SET urls_fetched = %s WHERE id = %s",
                                    (idx + 1, scan_id))
                                _pconn.commit()
                        except Exception:
                            pass
                return urls, vehs, fails

            urls, vehicles, fetch_fail = _scan_pass()
            total_urls = len(urls)

            # Auto-escalate through EVERY remaining tier if this tier found
            # essentially nothing. `total_urls < 10` covers the case where the
            # sitemap itself is being blocked / 302'd (TXT Charlie's LiteSpeed
            # cache poison) — we never got URLs to fetch. We keep climbing:
            # direct → flaresolverr → direct_proxy → flaresolverr_proxy.
            # The Hostinger cache poison is IP-specific; routing via DataImpulse
            # gets us a clean 200 OK where our server IP gets 302→127.0.0.1.
            # Threshold lowered from 0.40 → 0.20 because IP-cache poisoning
            # often partials: TXT Charlie scan 26 had 29% per-VDP fail rate
            # (49/168), missed escalating, falsely marked 51 cars missing.
            def _needs_escalation():
                return (
                    len(vehicles) == 0 or
                    (total_urls >= 20 and fetch_fail / max(total_urls, 1) > 0.20)
                )
            while _needs_escalation():
                nxt = dealer_fetchers.next_tier(_CURRENT_TIER['tier'])
                if not nxt:
                    break
                # flaresolverr-family tiers require the container healthy
                if 'flaresolverr' in nxt and not dealer_fetchers.flaresolverr_healthy():
                    break
                _CURRENT_TIER['tier'] = nxt
                stats['tier'] = nxt
                urls, vehicles, fetch_fail = _scan_pass()
                total_urls = len(urls)

            stats['vehicles_found'] = len(vehicles)

            # Abort guard: if still too many fetch fails after escalation, bail
            # without reconciling — we'd wrongly mark healthy cars as missing.
            if total_urls >= 20 and fetch_fail / total_urls > 0.40:
                stats['status'] = 'blocked'
                stats['error'] = f'{fetch_fail}/{total_urls} URLs failed via {stats["tier"]} — scan aborted, inventory preserved'
                self._update_dealer(platform, method, scan_id, stats['error'][:200], stats['tier'])
                self._finalize(scan_id, stats, started)
                return stats

            # Baseline sanity guard: zero vehicles found but dealer was healthy
            # yesterday = entry point (sitemap/homepage) is broken. Skip reconcile.
            # Caught TXT Charlie scan 13 (2026-04-22) that flipped 155 cars to
            # missing after a 4-second empty-sitemap fetch slipped past the
            # fetch-failure guard (which requires total_urls>=20 to trigger).
            if stats['vehicles_found'] == 0:
                with get_conn() as conn, conn.cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*) AS n FROM dealer_inventory "
                        "WHERE dealer_id=%s AND status='active'",
                        (self.dealer_id,)
                    )
                    row = cur.fetchone()
                    prior_active = (row['n'] if isinstance(row, dict) else row[0]) if row else 0
                if prior_active >= 20:
                    stats['status'] = 'blocked'
                    stats['error'] = (
                        f'zero vehicles found but {prior_active} were active — '
                        f'entry point likely broken, scan aborted, inventory preserved'
                    )
                    self._update_dealer(platform, method, scan_id, stats['error'][:200], stats['tier'])
                    self._finalize(scan_id, stats, started)
                    return stats

            # 4. Upsert + reconcile + color detect
            scanned_vins, scanned_urls = set(), set()
            with get_conn() as conn, conn.cursor() as cur:
                for v in vehicles:
                    inv_id, is_new, drop = upsert_vehicle(cur, self.dealer_id, scan_id, v)
                    if inv_id is None:
                        continue
                    if is_new:
                        stats['new_count'] += 1
                    if drop:
                        stats['price_drop_count'] += 1
                    vin = (v.get('vin') or '').upper().strip()
                    if vin:
                        scanned_vins.add(vin)
                    if v.get('url'):
                        scanned_urls.add(v['url'])
                conn.commit()

            stats.update(self._reconcile(scan_id, scanned_vins, scanned_urls))
            stats['colors_detected'] = self._detect_colors()
            self._update_dealer(platform, method, scan_id, 'ok', stats['tier'])
            stats['status'] = 'ok'

        except Exception as e:
            stats['status'] = 'error'
            stats['error'] = f'{type(e).__name__}: {e}'
            self._update_dealer(None, None, scan_id, 'error:' + stats['error'][:200],
                                stats.get('tier'))

        self._finalize(scan_id, stats, started)
        return stats

    # ─── Shared zero-vehicle abort guard for early-return platform paths ───
    def _zero_vehicle_abort_check(self):
        """Return prior_active count if reconciling against zero vehicles would
        wipe a healthy dealer (>=20 active rows); else None.

        Used by the AAN, DealerInspire-Algolia, and ECT early-return paths,
        which all hand a vehicle list straight to `_process_aan` and skip the
        line ~2070 universal sanity guard. Caught by the 2026-05-01 ECT
        incident: Webflow served the sitemap fine but every VDP returned no
        JSON-LD Product block (rate-limit / CF challenge), handler returned [],
        451 active rows got swept. This guard turns that into status=blocked
        with inventory preserved instead.
        """
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM dealer_inventory "
                "WHERE dealer_id=%s AND status='active'",
                (self.dealer_id,))
            row = cur.fetchone()
            prior = (row['n'] if isinstance(row, dict) else row[0]) if row else 0
        return prior if prior >= 20 else None

    # ─── DealerOn / Dealer-eProcess sitemap + per-VDP JSON-LD ───
    #     (CLOUDFLARE_ESCALATE_2026_06_11)
    def _dealeron_discover_sitemap(self):
        """AUTO_DISCOVER_DEALERON_2026_06_11: return the per-rooftop DealerOn
        inventory-search sitemap URL from the un-challenged /resrc/ index, or
        None. Lets a NEW Cloudflare-walled DealerOn rooftop onboard at full
        JSON-LD quality with zero hand-config."""
        idx_url = self.base_url.rstrip('/') + _DEALERON_SITEMAP_INDEX
        code, _f, body = fetch(idx_url, self.sess, tier='direct')
        if code != 200 or not body or '<sitemapindex' not in body:
            return None
        try:
            root = ET.fromstring(body.encode('utf-8') if isinstance(body, str) else body)
        except Exception:
            return None
        inv = []
        for loc in root.iter(_SM_LOC):
            u = (loc.text or '').strip()
            if 'sitemap-inventory-search/' in u:
                tail = u.rstrip('/').rsplit('/sitemap-inventory-search/', 1)[-1]
                if tail and '/' not in tail:
                    inv.append(u)
        if not inv:
            return None
        hz = _dealeron_zip_from_url(self.base_url)
        if hz:
            for u in inv:
                if u.rstrip('/').endswith(hz) or ('-' + hz + '/') in u:
                    return u
        return inv[0]

    def _scan_dealeron_jsonld(self, scan_id, sc, stats, started):
        """Pre-owned scan for a Cloudflare-walled DealerOn rooftop.

        Discovery: configured /resrc/ inventory sitemap(s) — fetched DIRECT
        (Cloudflare serves /resrc/ un-challenged). Filter VDP URLs to the
        configured pre-owned regex.
        Data: each VDP fetched via a WARM FlareSolverr session (one session
        mints cf_clearance once, then reuses it — first solve ~110s, warm
        pages much faster). Per-VDP JSON-LD parsed by _extract_jsonld_vehicle
        (vin/year/make/model/trim/price/mileage/color/photos). URL-slug
        fallback (_parse_dealeron_vdp) backfills year/make/model if a VDP
        yields no JSON-LD.
        """
        inv_src = sc.get('inventory_source') or {}
        sm_urls = inv_src.get('sitemap_urls') or []
        vdp_re = inv_src.get('vdp_url_regex')
        # AUTO_DISCOVER_DEALERON_2026_06_11: a NEW CF-walled DealerOn rooftop
        # reaches here (via the CF-escalation) with no configured sitemap.
        # Derive the rooftop inventory sitemap from the index and default the
        # pre-owned VDP pattern so full-quality JSON-LD needs zero hand-config.
        if not sm_urls:
            try:
                _disc = self._dealeron_discover_sitemap()
            except Exception as _de:
                _disc = None
                print('  [dealeron-jsonld] auto-discover err: ' + str(_de), flush=True)
            if _disc:
                sm_urls = [_disc]
                print('  [dealeron-jsonld] auto-discovered sitemap: ' + _disc, flush=True)
        if not vdp_re:
            vdp_re = r"/auto/(preowned|used|certified)[^\"']*?/\d+/?$"
        max_to_ms = int(inv_src.get('flaresolverr_max_timeout_ms')
                        or dealer_fetchers.FLARESOLVERR_TIMEOUT_MS)
        try:
            vdp_pat = re.compile(vdp_re, re.I) if vdp_re else None
        except re.error as e:
            print('  [dealeron-jsonld] bad vdp_url_regex: ' + str(e), flush=True)
            vdp_pat = None

        # 1. Discover VDP URLs from the configured sitemap(s) — direct fetch.
        vdp_urls = []
        seen_u = set()
        for sm_url in sm_urls:
            code, _f, sbody = fetch(sm_url, self.sess, tier='direct')
            if code != 200 or not sbody:
                print('  [dealeron-jsonld] sitemap ' + sm_url + ' -> code=' +
                      str(code) + ' len=' + str(len(sbody or '')) + ' skip',
                      flush=True)
                continue
            try:
                sroot = ET.fromstring(
                    sbody.encode('utf-8') if isinstance(sbody, str) else sbody)
            except Exception as e:
                print('  [dealeron-jsonld] sitemap parse error: ' + str(e),
                      flush=True)
                continue
            for loc_el in sroot.iter(_SM_LOC):
                loc = (loc_el.text or '').strip()
                if not loc or '/auto/' not in loc:
                    continue
                if vdp_pat and not vdp_pat.search(loc):
                    continue
                if loc in seen_u:
                    continue
                seen_u.add(loc)
                vdp_urls.append(loc)
        print('  [dealeron-jsonld] ' + str(len(vdp_urls)) +
              ' pre-owned VDP urls discovered (maxTimeout=' + str(max_to_ms) +
              'ms)', flush=True)

        if not vdp_urls:
            # Discovery itself failed — preserve any prior inventory.
            prior = self._zero_vehicle_abort_check()
            stats['status'] = 'blocked'
            stats['error'] = ('dealeron sitemap discovery found 0 pre-owned VDPs'
                              + (' (%s prior rows preserved)' % prior if prior else ''))
            self._update_dealer('dealeron', None, scan_id, stats['error'][:200])
            self._finalize(scan_id, stats, started)
            return

        # 2. Per-VDP JSON-LD via a warm FlareSolverr session.
        if not dealer_fetchers.flaresolverr_healthy():
            stats['status'] = 'blocked'
            stats['error'] = 'flaresolverr not healthy; cannot fetch CF-walled VDPs'
            self._update_dealer('dealeron', None, scan_id, stats['error'][:200])
            self._finalize(scan_id, stats, started)
            return

        stats['tier'] = 'flaresolverr'
        session_id = 'dealeron-%s' % self.dealer_id
        have_session = dealer_fetchers.flaresolverr_session_create(session_id)
        if not have_session:
            print('  [dealeron-jsonld] session create failed — sessionless fetch',
                  flush=True)
            session_id = None

        vehicles = []
        timings = []
        n_ok = n_jsonld = n_slug = n_fail = 0
        try:
            for i, vurl in enumerate(vdp_urls, 1):
                t0 = time.time()
                code, _f, body = dealer_fetchers.fetch_flaresolverr_session(
                    vurl, session_id=session_id, max_timeout_ms=max_to_ms)
                dt = time.time() - t0
                timings.append(dt)
                if code != 200 or not body:
                    n_fail += 1
                    print('  [dealeron-jsonld] %d/%d FAIL code=%s %.1fs %s'
                          % (i, len(vdp_urls), code, dt, vurl), flush=True)
                    continue
                veh = _extract_jsonld_vehicle(body)
                if veh:
                    n_jsonld += 1
                else:
                    # JSON-LD missing — fall back to URL-slug for y/m/m.
                    veh = _parse_dealeron_vdp(vurl, '', '')
                    if veh:
                        n_slug += 1
                if not veh:
                    n_fail += 1
                    print('  [dealeron-jsonld] %d/%d no-data %.1fs %s'
                          % (i, len(vdp_urls), dt, vurl), flush=True)
                    continue
                veh.setdefault('url', vurl)
                veh['condition'] = veh.get('condition') or 'used'
                vehicles.append(veh)
                n_ok += 1
                print('  [dealeron-jsonld] %d/%d ok %.1fs vin=%s price=%s miles=%s'
                      % (i, len(vdp_urls), dt, veh.get('vin', '-'),
                         veh.get('price', '-'), veh.get('mileage', '-')),
                      flush=True)
        finally:
            if session_id:
                dealer_fetchers.flaresolverr_session_destroy(session_id)

        if timings:
            print('  [dealeron-jsonld] timing: first=%.1fs warm_median=%.1fs '
                  'total=%.1fs (%d ok / %d jsonld / %d slug / %d fail)'
                  % (timings[0], sorted(timings[1:])[len(timings[1:]) // 2]
                     if len(timings) > 1 else timings[0],
                     sum(timings), n_ok, n_jsonld, n_slug, n_fail), flush=True)

        if not vehicles:
            prior = self._zero_vehicle_abort_check()
            stats['status'] = 'blocked'
            stats['error'] = ('dealeron per-VDP fetch yielded 0 vehicles'
                              + (' (%s prior rows preserved)' % prior if prior else ''))
            self._update_dealer('dealeron', 'sitemap+jsonld', scan_id,
                                stats['error'][:200], stats['tier'])
            self._finalize(scan_id, stats, started)
            return

        # 3. Upsert via the AAN path (full reconcile marks vanished VINs sold).
        self._process_aan(scan_id, vehicles, stats)
        stats['colors_detected'] = self._detect_colors()
        self._update_dealer('dealeron', 'sitemap+jsonld', scan_id, 'ok',
                            stats['tier'])
        stats['status'] = 'ok'
        self._finalize(scan_id, stats, started)
        return

    # ─── AAN: feed-driven upsert + direct sold marking ───
    def _process_aan(self, scan_id, aan_vehicles, stats):
        """The AAN feed tells us which vehicles are sold / pending directly.
        Trust those flags. Upsert active ones, and mark sold VINs with
        sold_confidence=1.0 immediately — no multi-signal inference needed."""
        scanned_vins, scanned_urls = set(), set()
        sold_vins = set()
        # Diagnostic counters — surface why kept-input may not match
        # vehicles_found in stats. Reasons:
        #   marked_sold     — listing had _aan_sold flag, routed to sold path
        #   no_keys         — upsert_vehicle returned None (no VIN AND no URL,
        #                     or only invalid all-digit "VIN")
        #   inserted/updated — upsert succeeded; rows actually touched
        #   url_only_match  — upsert succeeded but VIN was empty/invalid; row
        #                     deduped by URL and won't appear in scanned_vins
        # The 2026-05-04 investigation showed the kept→stored "30% loss" was
        # an artifact of stats['vehicles_found'] = len(scanned_vins) ignoring
        # 159 VIN-less rows that were correctly stored — see commit message.
        reasons = {'marked_sold': 0, 'no_keys': 0,
                   'inserted': 0, 'updated': 0, 'url_only_match': 0}
        for v in aan_vehicles:
            vin = (v.get('vin') or '').upper().strip()
            if v.get('_aan_sold'):
                reasons['marked_sold'] += 1
                if vin:
                    sold_vins.add(vin)
        with get_conn() as conn, conn.cursor() as cur:
            for v in aan_vehicles:
                vin = (v.get('vin') or '').upper().strip()
                if v.get('_aan_sold'):
                    continue  # already counted above; sold rows handled below
                # Strip the AAN-specific flags before calling the shared upsert
                clean = {k: val for k, val in v.items() if not k.startswith('_aan_')}
                inv_id, is_new, drop = upsert_vehicle(cur, self.dealer_id, scan_id, clean)
                if inv_id is None:
                    reasons['no_keys'] += 1
                    continue
                if is_new:
                    stats['new_count'] += 1
                    reasons['inserted'] += 1
                else:
                    reasons['updated'] += 1
                if drop:
                    stats['price_drop_count'] += 1
                if vin:
                    scanned_vins.add(vin)
                else:
                    reasons['url_only_match'] += 1
                if v.get('url'):
                    scanned_urls.add(v['url'])

            # Mark any existing VIN that the feed says is sold
            for vin in sold_vins:
                # Some AAN dealers (e.g. Marshall Goldman, multi-location chains)
                # leave stale 'Sold' records alongside the same VIN's current
                # active listing in /api/cars. If we see this VIN as active in
                # the same scan, trust active — the dealer is currently selling
                # it. Otherwise we'd insta-mark a freshly-imported active car
                # as sold from the dealer's own historical pollution.
                if vin in scanned_vins:
                    continue
                cur.execute('''SELECT id FROM dealer_inventory
                               WHERE dealer_id=%s AND vin=%s''',
                            (self.dealer_id, vin))
                row = cur.fetchone()
                if not row:
                    continue
                sig = [{'type': 'aan_feed_sold', 'detail': 'API reports Sold',
                        'confidence': 1.0}]
                cur.execute('''UPDATE dealer_inventory
                               SET status='sold', sold_at=COALESCE(sold_at, NOW()),
                                   sold_confidence=1.0,
                                   sold_signals=%s::jsonb, updated_at=NOW()
                               WHERE id=%s''',
                            (_json.dumps(sig), row['id']))
                cur.execute('''INSERT INTO dealer_sold_signals
                                (dealer_id, inventory_id, vin, signal_type,
                                 signal_detail, confidence, scan_id)
                               VALUES (%s, %s, %s, %s, %s, %s, %s)''',
                            (self.dealer_id, row['id'], vin, 'aan_feed_sold',
                             'API reports Sold', 1.0, scan_id))
                stats['sold_count'] += 1
            conn.commit()

        # Standard reconcile catches any VIN missing from the feed entirely
        extra = self._reconcile(scan_id, scanned_vins, scanned_urls)
        stats['sold_count'] += extra.get('sold_count', 0)
        stats['missing_count'] = extra.get('missing_count', 0)
        # vehicles_found counts every row actually upserted, NOT just unique
        # VINs. Pre-2026-05-04 this was len(scanned_vins) which excluded the
        # ~35% of vintage ECT listings without a 17-char VIN (deduped by URL
        # only) and looked like a 30% silent data loss in scan reports.
        stats['vehicles_found'] = reasons['inserted'] + reasons['updated']
        # Log the rejection / disposition breakdown so any future kept-vs-
        # stored gap is immediately visible in /var/log/ew-dealer-scans.log.
        print(f'  [process_aan] dealer={self.dealer_id} input={len(aan_vehicles)} '
              f'inserted={reasons["inserted"]} updated={reasons["updated"]} '
              f'url_only_match={reasons["url_only_match"]} '
              f'marked_sold={reasons["marked_sold"]} '
              f'no_keys={reasons["no_keys"]} '
              f'unique_vins={len(scanned_vins)} unique_urls={len(scanned_urls)}',
              flush=True)

    # ─── reconcile missing / sold ───
    def _reconcile(self, scan_id, scanned_vins, scanned_urls):
        out = {'sold_count': 0, 'missing_count': 0}
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute('''
                SELECT id, vin, url, status, missing_scans
                FROM dealer_inventory
                WHERE dealer_id = %s AND status IN ('active','missing')
            ''', (self.dealer_id,))
            rows = cur.fetchall()
            for r in rows:
                vin = (r['vin'] or '').upper()
                url = r['url'] or ''
                key_seen = (vin and vin in scanned_vins) or (url and url in scanned_urls)
                if key_seen:
                    continue  # handled by upsert_vehicle (resets missing_scans)
                new_missing = (r['missing_scans'] or 0) + 1
                signals = [{'type': 'missing_from_scan',
                            'detail': f'consecutive={new_missing}',
                            'confidence': min(0.8, 0.25 * new_missing)}]
                if new_missing >= MISSING_SCANS_BEFORE_PROBE and url:
                    signals.extend(probe_sold_signals(url, self.sess))

                # Persist signals
                for s in signals:
                    cur.execute('''INSERT INTO dealer_sold_signals
                                    (dealer_id, inventory_id, vin, signal_type,
                                     signal_detail, confidence, scan_id)
                                   VALUES (%s, %s, %s, %s, %s, %s, %s)''',
                                (self.dealer_id, r['id'], vin or None,
                                 s['type'], s.get('detail'), s['confidence'], scan_id))

                score = sold_confidence(signals)
                if score >= SOLD_CONFIDENCE_THRESHOLD:
                    cur.execute('''UPDATE dealer_inventory
                                   SET status='sold', sold_at=NOW(),
                                       sold_confidence=%s,
                                       sold_signals=%s::jsonb,
                                       missing_scans=%s, updated_at=NOW()
                                   WHERE id=%s''',
                                (score, _json.dumps(signals), new_missing, r['id']))
                    out['sold_count'] += 1
                else:
                    cur.execute('''UPDATE dealer_inventory
                                   SET status='missing', missing_scans=%s,
                                       sold_confidence=%s,
                                       sold_signals=%s::jsonb, updated_at=NOW()
                                   WHERE id=%s''',
                                (new_missing, score, _json.dumps(signals), r['id']))
                    out['missing_count'] += 1
            conn.commit()
        return out

    def _detect_colors(self):
        """Fill ext_color for up to COLORS_PER_SCAN rows per run using Gemini Flash.
        Lazy-imports the existing helper from app.py to avoid a circular import."""
        try:
            from app import extract_color_from_file
        except Exception as e:
            print(f'[dealer_scanner] color detect unavailable: {e}', flush=True)
            return 0

        # updated_at ASC picks rows we haven't touched in a while, so successive
        # scans cycle through different vehicles instead of hammering the same
        # 60 that already failed once.
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute('''SELECT id, photo_url, photos FROM dealer_inventory
                           WHERE dealer_id = %s AND status = 'active'
                             AND (ext_color IS NULL OR ext_color = '')
                             AND photo_url IS NOT NULL AND photo_url <> ''
                           ORDER BY updated_at ASC
                           LIMIT %s''',
                        (self.dealer_id, COLORS_PER_SCAN))
            targets = cur.fetchall()

        count = 0
        for r in targets:
            # Try primary photo, then up to 2 fallback photos from the photos JSONB.
            urls = [r['photo_url']]
            extra = r.get('photos')
            if isinstance(extra, list):
                for u in extra[:4]:
                    if isinstance(u, str) and u and u not in urls:
                        urls.append(u)
                    if len(urls) >= 3:
                        break
            color = None
            for u in urls[:3]:
                try:
                    resp = self.sess.get(u, timeout=REQUEST_TIMEOUT)
                    if resp.status_code != 200 or not resp.content:
                        continue
                    mime = resp.headers.get('Content-Type', 'image/jpeg').split(';')[0]
                    ok_call, raw = _call_with_timeout(
                        extract_color_from_file, GEMINI_CALL_TIMEOUT_SEC,
                        resp.content, mime)
                    if not ok_call:
                        print(f'[dealer_scanner] gemini timed out after {GEMINI_CALL_TIMEOUT_SEC}s on {u[-60:]}',
                              flush=True)
                        continue
                    if _is_valid_color(raw):
                        color = raw
                except Exception as e:
                    print(f'[dealer_scanner] color fetch failed {u}: {e}', flush=True)
                    continue
                if color:
                    break
            # Touch updated_at either way so next scan tries a different row.
            with get_conn() as conn, conn.cursor() as cur:
                if color:
                    cur.execute('UPDATE dealer_inventory SET ext_color=%s, updated_at=NOW() WHERE id=%s',
                                (color, r['id']))
                    count += 1
                else:
                    cur.execute('UPDATE dealer_inventory SET updated_at=NOW() WHERE id=%s',
                                (r['id'],))
                conn.commit()
        return count

    def _discovery_tried_today(self):
        """Did we already attempt AI discovery on this dealer in the last 24h?
        Avoids re-burning Opus credits on a stuck dealer every scan cycle."""
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute('''
                SELECT COUNT(*) AS n FROM dealer_discovery_runs
                WHERE dealer_id=%s AND started_at > NOW() - INTERVAL '24 hours'
            ''', (self.dealer_id,))
            row = cur.fetchone()
            return (row['n'] if row else 0) > 0

    def _auto_discover(self):
        """Spawn discover_dealer.py for this dealer in-process. Returns True
        if a config was successfully written."""
        try:
            import discover_dealer
            ok = discover_dealer.run_discovery(self.dealer_id, force=False)
            return bool(ok)
        except Exception as e:
            print(f'  AI discovery exception: {e}', flush=True)
            traceback.print_exc()
            return False

    def _persist_algolia_cfg(self, cfg):
        """Merge the resolved Algolia config into dealers.scrape_config under
        the `algolia` key. Uses Postgres jsonb concat (`||`) so any existing
        keys (e.g. the AI-discovery Next.js scrape config for Ferrari) are
        preserved. COALESCE handles the NULL scrape_config case — `NULL || x`
        returns NULL in jsonb arithmetic, which would silently drop the cfg.
        """
        payload = _json.dumps({'algolia': cfg})
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute('''UPDATE dealers
                            SET scrape_config = COALESCE(scrape_config, '{}'::jsonb) || %s::jsonb,
                                updated_at = NOW()
                           WHERE id = %s''',
                        (payload, self.dealer_id))
            conn.commit()
        # Update the in-memory dealer dict so the rest of the scan sees the
        # merged config (matters if anything downstream re-reads scrape_config).
        existing = self.dealer.get('scrape_config') or {}
        if isinstance(existing, dict):
            existing['algolia'] = cfg
            self.dealer['scrape_config'] = existing

    def _carscommerce_source_id_from_rooftop(self):
        """The Algolia rooftop_filter's api_id IS the Cars Commerce source_id
        for the same physical rooftop (verified: BMW McKinney algolia
        api_id 9093124 == cc source_id 9093124). Pull it from
        scrape_config.algolia.rooftop_filter so a shared group ccid can be
        scoped to this one store. Returns str or None.
        CARSCOMMERCE_AUTOFALLBACK_2026_06_18."""
        sc = self.dealer.get('scrape_config') or {}
        if not isinstance(sc, dict):
            return None
        rf = (sc.get('algolia') or {}).get('rooftop_filter')
        if not rf:
            return None
        m = re.search(r'api_id:(\d{3,9})', _json.dumps(rf))
        return m.group(1) if m else None

    def _autodiscover_carscommerce(self):
        """Pull the Cars Commerce SEARCH_SERVICE config (ccid + apiKey) off a
        JS-hydrated listing page and build a fetch_carscommerce_inventory cfg,
        scoped by the rooftop source_id when the dealer is on a shared group
        ccid. Used as the Algolia-failure fallback so a migrated DealerInspire
        store self-heals. Returns a cfg dict or None.
        CARSCOMMERCE_AUTOFALLBACK_2026_06_18."""
        srp = self.base_url.rstrip('/') + '/used-vehicles/'
        # DealerInspire hydrates inventory (and the SEARCH_SERVICE config block)
        # client-side, so a single FlareSolverr fetch frequently returns a
        # partial pre-hydration render with no config (it cost Ferrari #4 a
        # ~30-min universal grind on 06-19). Retry until the carscommerce
        # SEARCH_SERVICE marker appears; keep the largest body seen.
        # AUTODISCOVER_RETRY_2026_06_19.
        body = ''
        for _attempt in range(3):
            h = ''
            try:
                resp = requests.post(
                    dealer_fetchers.FLARESOLVERR_URL,
                    json={'cmd': 'request.get', 'url': srp, 'maxTimeout': 85000},
                    timeout=130,
                )
                if resp.status_code == 200 and resp.json().get('status') == 'ok':
                    h = ((resp.json().get('solution') or {}).get('response') or '')
            except Exception:
                h = ''
            if len(h) > len(body):
                body = h
            if 'websites-search' in body:
                break
        if not body:
            return None
        # SEARCH_SERVICE = {"apiUrl":"https://websites-search.api.carscommerce.inc",
        #                   "ccid":"NNN","enabled":"1","apiKey":"KEY", ...}
        m = re.search(
            r'websites-search\.api\.carscommerce\.inc"\s*,\s*"ccid"\s*:\s*"(\d{3,9})"'
            r'.{0,80}?"apiKey"\s*:\s*"([0-9A-Za-z]{20,48})"',
            body, re.S)
        if not m:
            return None
        cfg = {'ccid': m.group(1), 'api_key': m.group(2),
               'origin': self.base_url.rstrip('/')}
        src = self._carscommerce_source_id_from_rooftop()
        if src:
            cfg['source_id'] = src
        return cfg

    def _recent_max_scan_count(self):
        """Largest vehicles_found across this dealer's recent OK scans — a
        corruption-resistant baseline for the auto-fallback group-feed gate
        (the live active count can be wrong if a prior partial scan shrank
        it). GATE_FIX_2026_06_19."""
        try:
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute('''SELECT COALESCE(MAX(vehicles_found), 0) AS mx
                                 FROM dealer_scans
                                WHERE dealer_id = %s AND status = 'ok'
                                  AND started_at > NOW() - INTERVAL '30 days' ''',
                            (self.dealer_id,))
                row = cur.fetchone()
                if row is None:
                    return 0
                return (row.get('mx') if isinstance(row, dict) else row[0]) or 0
        except Exception:
            return 0

    def _persist_carscommerce_cfg(self, cfg):
        """Persist the auto-discovered Cars Commerce config under
        scrape_config.carscommerce and flip scrape_method, so every future
        scan uses the fast carscommerce path directly (it runs before the now
        dead Algolia path). CARSCOMMERCE_AUTOFALLBACK_2026_06_18."""
        payload = _json.dumps({'carscommerce': cfg})
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute('''UPDATE dealers
                            SET scrape_config = COALESCE(scrape_config, '{}'::jsonb) || %s::jsonb,
                                scrape_method = 'carscommerce',
                                scrape_config_at = NOW(),
                                updated_at = NOW()
                           WHERE id = %s''',
                        (payload, self.dealer_id))
            conn.commit()
        existing = self.dealer.get('scrape_config') or {}
        if isinstance(existing, dict):
            existing['carscommerce'] = cfg
            self.dealer['scrape_config'] = existing

    def _update_dealer(self, platform, method, scan_id, status, tier=None):
        # scrape_method combines the extractor (api/jsonld/sitemap) with the fetch
        # tier (direct/flaresolverr/...). Keeps both visible on the dealer card.
        combined = None
        if method and tier and tier != 'direct':
            combined = f'{method}+{tier}'
        elif method:
            combined = method
        elif tier:
            combined = tier
        # Never demote a configured handler-platform (ect, etc.) to 'custom'.
        # The universal fall-through path passes the auto-detected platform
        # which is 'custom' when no HTML fingerprint matched — but the dealer
        # may already be pinned to a custom-handler platform like 'ect'. If we
        # let 'custom' overwrite, the next scan skips the handler block and
        # falls through to universal-crawl, which will wipe inventory.
        # (2026-05-02 incident: scan #141's universal fall-through overwrote
        # dealer 7's platform from 'ect' to 'custom', causing scan #142 to
        # also fall through and wipe 451 rows a second time.)
        platform_to_set = None if platform == 'custom' else platform
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute('''UPDATE dealers SET
                            platform = COALESCE(%s, platform),
                            scrape_method = COALESCE(%s, scrape_method),
                            last_scan_at = NOW(),
                            last_scan_status = %s,
                            last_scan_id = %s,
                            updated_at = NOW()
                           WHERE id = %s''',
                        (platform_to_set, combined, status, scan_id, self.dealer_id))
            conn.commit()

    def _finalize(self, scan_id, stats, started):
        duration_ms = int((time.time() - started) * 1000)
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute('''UPDATE dealer_scans SET
                            finished_at = NOW(),
                            status = %s,
                            vehicles_found = %s,
                            new_count = %s,
                            sold_count = %s,
                            missing_count = %s,
                            price_drop_count = %s,
                            colors_detected = %s,
                            platform_detected = %s,
                            error_message = %s,
                            duration_ms = %s
                           WHERE id = %s''',
                        (stats['status'], stats['vehicles_found'],
                         stats['new_count'], stats['sold_count'],
                         stats['missing_count'], stats['price_drop_count'],
                         stats.get('colors_detected', 0),
                         stats['platform_detected'], stats.get('error'),
                         duration_ms, scan_id))
            conn.commit()


# ── CLI ──────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description='EW Dealer inventory scanner')
    ap.add_argument('--dealer-id', type=int, help='Scan an existing dealer by id')
    ap.add_argument('--url', help='Add + scan a new dealer by URL')
    ap.add_argument('--name', help='Dealer name (required with --url)')
    ap.add_argument('--notes', help='Optional notes')
    args = ap.parse_args()

    if args.dealer_id:
        scanner = DealerScanner.from_dealer_id(args.dealer_id)
        stats = scanner.run()
    elif args.url and args.name:
        stats = DealerScanner.create_and_scan(args.url, args.name, args.notes)
    else:
        ap.print_help()
        sys.exit(2)

    print(_json.dumps(stats, indent=2, default=str))


if __name__ == '__main__':
    main()
