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
import json as _json
import os
import re
import sys
import time
from datetime import datetime, timezone
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
SOLD_CONFIDENCE_THRESHOLD = 0.6
MISSING_SCANS_BEFORE_PROBE = 2
COLORS_PER_SCAN = int(os.environ.get('DEALER_COLORS_PER_SCAN', '10'))  # small burst on scan; continuous worker handles the rest

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
VIN_RE = re.compile(r'\b([A-HJ-NPR-Z0-9]{17})\b')
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
    s.headers.update({
        'User-Agent': USER_AGENT,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
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
    if 'ddc-' in h or 'dealer.com' in h:
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
    if 'wp-content' in h or 'wp-includes' in h:
        return ('wordpress', 'jsonld+html')
    if 'vinsolutions' in h:
        return ('vinsolutions', 'jsonld')
    return ('custom', 'sitemap+jsonld')


# ── AAN platform — JSON API extractor ───────────────────────────────────
def fetch_aan_inventory(base_url, sess):
    """AAN dealers expose the full live inventory at /api/cars as a JSON array.
    One call returns everything — VIN, YMM, trim, colors, miles, price, photos,
    sold/pending flags. No per-VDP fetching needed.

    Returns a list of normalized vehicle dicts, or None if the API is unreachable.
    """
    code, _f, body = fetch(urljoin(base_url, '/api/cars'), sess)
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
        v = _normalize_aan_vehicle(item, base_url)
        if v:
            vehicles.append(v)
    return vehicles


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

    photos = []
    img = item.get('image_link')
    if img:
        photos.append(img)

    out = {
        'vin': vin,
        'year': year,
        'make': (item.get('make') or '').strip() or None,
        'model': (item.get('model') or '').strip() or None,
        'trim': (item.get('trim') or '').strip() or None,
        'ext_color': (item.get('ext_color') or '').strip() or None,
        'int_color': (item.get('int_color') or '').strip() or None,
        'body_style': (item.get('body') or '').strip() or None,
        'stock_number': (item.get('stockno') or '').strip() or None,
        'mileage': _int('mileage'),
        'price': _int('price'),
        'url': url,
        'photo_url': img,
        'photos': photos,
        # Direct signals from the API — sold/pending flags we can trust.
        '_aan_sold': sold_raw == 'sold',
        '_aan_pending': pending,
        '_aan_coming_soon': coming_soon,
    }
    if not (out.get('vin') or out.get('make') or out.get('year')):
        return None
    return out


# ── URL discovery ────────────────────────────────────────────────────────
def discover_via_sitemap(base_url, sess):
    """Pull inventory URLs from sitemap(s). Returns list of URLs (deduped)."""
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
                for loc in root.findall('.//loc'):
                    if loc.text:
                        u = loc.text.strip()
                        if _looks_like_vehicle_url(u):
                            found.add(u)
        except ET.ParseError:
            continue
    return sorted(found)


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

    # 5) VIN from URL / page text if JSON-LD missed it
    if not out.get('vin'):
        m = VIN_RE.search(url) or VIN_RE.search(html or '')
        if m:
            out['vin'] = m.group(1)

    # 6) Year fallback
    if not out.get('year'):
        m = YEAR_RE.search(url)
        if m:
            out['year'] = int(m.group(1))

    # 7) Price regex fallback
    if not out.get('price'):
        m = PRICE_RE.search(html or '')
        if m:
            try:
                out['price'] = int(m.group(1).replace(',', ''))
            except ValueError:
                pass

    # 8) Mileage regex fallback
    if not out.get('mileage'):
        m = MILES_RE.search(html or '')
        if m:
            try:
                out['mileage'] = int(m.group(1).replace(',', ''))
            except ValueError:
                pass

    # Canonicalise make capitalisation
    if out.get('make'):
        out['make'] = _normalize_make(out['make'])
    if out.get('model'):
        out['model'] = _title_case(out['model'])
    if out.get('trim'):
        out['trim'] = _title_case(out['trim'])

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
    Handles patterns like /vehicle-details/2019-rolls-royce-phantom-6709/."""
    path = urlparse(url).path.rstrip('/').lower()
    slug = path.rsplit('/', 1)[-1] if path else ''
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
    # Stock# = trailing numeric token (short number)
    if tokens and re.fullmatch(r'[a-z]?\d{2,6}[a-z]?', tokens[-1]):
        tokens = tokens[:-1]
    if not tokens:
        return {'year': year}
    # Make = known-compound-2-token OR single token
    make = None
    if len(tokens) >= 2:
        two = f'{tokens[0]}-{tokens[1]}'
        if two in MAKE_NORMALIZE or two in KNOWN_COMPOUND_MAKES:
            make = MAKE_NORMALIZE.get(two, _title_case(two.replace('-', ' ')))
            tokens = tokens[2:]
    if make is None and tokens:
        first = tokens[0]
        make = MAKE_NORMALIZE.get(first, _title_case(first))
        tokens = tokens[1:]
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
    vin = node.get('vehicleIdentificationNumber') or node.get('sku')
    if isinstance(vin, str) and len(vin) == 17:
        v['vin'] = vin.upper()
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
    return v if v else None


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
    """Returns a list of signal dicts: [{type, detail, confidence}]."""
    signals = []
    if not url:
        return signals
    code, final_url, body = fetch(url, sess)
    if code in (404, 410):
        signals.append({'type': 'url_404', 'detail': f'HTTP {code}', 'confidence': 0.75})
        return signals
    if code is None:
        # transient error — don't infer sold, let next scan retry
        return signals
    if final_url and final_url != url:
        # redirect — check if final looks like sold/inventory landing page
        fp = urlparse(final_url).path.lower()
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
    """Insert or merge a vehicle row; returns (inventory_id, is_new, price_drop_amount)."""
    vin = (veh.get('vin') or '').strip().upper()
    url = veh.get('url') or ''
    key_val = vin if vin else url
    if not key_val:
        return (None, False, None)

    # Look up existing row
    cur.execute('''
        SELECT id, price, status, first_seen_at, missing_scans
        FROM dealer_inventory
        WHERE dealer_id = %s AND COALESCE(NULLIF(vin, ''), url) = %s
    ''', (dealer_id, key_val))
    row = cur.fetchone()

    photos_json = _json.dumps(veh['photos']) if isinstance(veh.get('photos'), list) else None
    raw_json = _json.dumps(veh, default=str)

    if row:
        # UPDATE — COALESCE smart merge: don't overwrite existing fields with NULL
        old_price = row.get('price')
        new_price = veh.get('price')
        price_drop = None
        price_drop_at = None
        last_price = old_price
        if (new_price is not None and old_price is not None
                and new_price < old_price):
            price_drop = old_price - new_price
            price_drop_at = now_utc()

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
                price_drop_amount = COALESCE(%s, price_drop_amount),
                price_drop_at = COALESCE(%s, price_drop_at),
                last_price_change_at = CASE
                    WHEN %s IS NOT NULL AND %s IS DISTINCT FROM price THEN NOW()
                    ELSE last_price_change_at END,
                url          = COALESCE(%s, url),
                photo_url    = COALESCE(%s, photo_url),
                photos       = COALESCE(%s::jsonb, photos),
                raw          = %s::jsonb,
                last_seen_at = NOW(),
                missing_scans = 0,
                status       = CASE WHEN status IN ('missing','active') THEN 'active' ELSE status END,
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
            raw_json, row['id'],
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
                 price, url, photo_url, photos, raw)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb)
            RETURNING id
        ''', (
            dealer_id, vin or '', veh.get('year'), veh.get('make'),
            veh.get('model'), veh.get('trim'), veh.get('ext_color'),
            veh.get('mileage'), veh.get('price'), url, veh.get('photo_url'),
            photos_json, raw_json,
        ))
        inv_id = cur.fetchone()['id']
        cur.execute('''INSERT INTO dealer_inventory_history
                         (dealer_id, inventory_id, vin, url, price, mileage, scan_id)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)''',
                    (dealer_id, inv_id, vin or None, url, veh.get('price'),
                     veh.get('mileage'), scan_id))
        return (inv_id, True, None)


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
            # 1. Platform fingerprint (homepage always fetched on 'direct' first)
            _CURRENT_TIER['tier'] = 'direct'
            code, _f, body = fetch(self.base_url, self.sess)
            if code in (403, 503, 401) and dealer_fetchers.flaresolverr_healthy():
                # Homepage is blocked — jump straight to FlareSolverr for the whole scan
                _CURRENT_TIER['tier'] = 'flaresolverr'
                code, _f, body = fetch(self.base_url, self.sess)
            if code in (403, 503, 401) or not body:
                stats['status'] = 'blocked'
                stats['error'] = f'HTTP {code} on homepage even via {_CURRENT_TIER["tier"]}'
                self._update_dealer(None, None, scan_id, stats['error'][:200])
                self._finalize(scan_id, stats, started)
                return stats
            platform, method = detect_platform(body or '')
            stats['platform_detected'] = platform
            # Choose the default tier for this platform (may escalate below)
            picked = dealer_fetchers.tier_for_platform(platform)
            if picked != _CURRENT_TIER['tier']:
                _CURRENT_TIER['tier'] = picked
            stats['tier'] = _CURRENT_TIER['tier']

            # 2. Platform-specific fast paths first
            if platform == 'aan':
                aan = fetch_aan_inventory(self.base_url, self.sess)
                if aan is not None:
                    self._process_aan(scan_id, aan, stats)
                    stats['colors_detected'] = self._detect_colors()
                    self._update_dealer(platform, 'api', scan_id, 'ok', stats['tier'])
                    stats['status'] = 'ok'
                    self._finalize(scan_id, stats, started)
                    return stats
                # AAN detected but /api/cars didn't cooperate — fall through to
                # universal extraction on the same tier.

            # 3. Universal path: discover + per-VDP extract, with one escalation if
            #    the first pass gets suspiciously low results or high fetch-fail rate.
            def _scan_pass():
                urls = discover_via_sitemap(self.base_url, self.sess)
                if len(urls) < 5:
                    urls = list(set(urls) | set(discover_via_crawl(self.base_url, self.sess)))
                urls = urls[:CRAWL_MAX_URLS]
                fails = 0
                vehs = []
                for u in urls:
                    c, _ff, vbody = fetch(u, self.sess)
                    if c != 200 or not vbody:
                        fails += 1
                        continue
                    veh = extract_vehicle(u, vbody)
                    if veh:
                        vehs.append(veh)
                return urls, vehs, fails

            urls, vehicles, fetch_fail = _scan_pass()
            total_urls = len(urls)

            # Auto-escalate if this tier found essentially nothing
            should_escalate = (
                (total_urls >= 10 and len(vehicles) == 0) or
                (total_urls >= 20 and fetch_fail / max(total_urls, 1) > 0.40)
            )
            if should_escalate and dealer_fetchers.flaresolverr_healthy():
                nxt = dealer_fetchers.next_tier(_CURRENT_TIER['tier'])
                if nxt:
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

    # ─── AAN: feed-driven upsert + direct sold marking ───
    def _process_aan(self, scan_id, aan_vehicles, stats):
        """The AAN feed tells us which vehicles are sold / pending directly.
        Trust those flags. Upsert active ones, and mark sold VINs with
        sold_confidence=1.0 immediately — no multi-signal inference needed."""
        scanned_vins, scanned_urls = set(), set()
        sold_vins = set()
        with get_conn() as conn, conn.cursor() as cur:
            for v in aan_vehicles:
                vin = (v.get('vin') or '').upper().strip()
                if v.get('_aan_sold'):
                    if vin:
                        sold_vins.add(vin)
                    continue
                # Strip the AAN-specific flags before calling the shared upsert
                clean = {k: val for k, val in v.items() if not k.startswith('_aan_')}
                inv_id, is_new, drop = upsert_vehicle(cur, self.dealer_id, scan_id, clean)
                if inv_id is None:
                    continue
                if is_new:
                    stats['new_count'] += 1
                if drop:
                    stats['price_drop_count'] += 1
                if vin:
                    scanned_vins.add(vin)
                if v.get('url'):
                    scanned_urls.add(v['url'])

            # Mark any existing VIN that the feed says is sold
            for vin in sold_vins:
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
        stats['vehicles_found'] = len(scanned_vins)

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
                    raw = extract_color_from_file(resp.content, mime)
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
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute('''UPDATE dealers SET
                            platform = COALESCE(%s, platform),
                            scrape_method = COALESCE(%s, scrape_method),
                            last_scan_at = NOW(),
                            last_scan_status = %s,
                            last_scan_id = %s,
                            updated_at = NOW()
                           WHERE id = %s''',
                        (platform, combined, status, scan_id, self.dealer_id))
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
