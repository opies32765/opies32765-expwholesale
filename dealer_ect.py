"""ECT (Exotic Car Trader) inventory fetcher.

ECT is a Webflow-fronted Bubble.io marketplace. As of ~2026-05-01 the per-VDP
Product data is no longer rendered in a static `<script type="application/ld+json">`
block — it's built and injected at runtime by inline JavaScript:

    var price = parseInt("$18,999".substring(1)...);
    var structuredData = { "@context": "https://schema.org", "@type": "Product",
        "name": "1992 ...", "image": "...", "offers": { "price": price, ... } };
    jsonldScript.textContent = JSON.stringify(structuredData);
    document.head.appendChild(jsonldScript);

The data is still all there as a JS literal — we just have to extract it ourselves.
The literal is mostly valid JSON; the only non-JSON token is the `price` variable
reference, which we substitute back to an int before parsing.

For per-listing creation timestamp: the `og:image` meta tag still points at
`listingcontent.exoticcartrader.com/<UnixMillis>x<rowid>/...` even though the
JSON-LD `image` now points at the Webflow CDN (which has no embedded timestamp).

We walk the listings sitemap newest→oldest, fetch each VDP, and stop when we
have seen N consecutive listings older than `max_age_days`. Returns vehicle
dicts in the same shape `_normalize_aan_vehicle` produces, so `_process_aan`
can ingest the result without per-platform branching downstream.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup

ECT_SITEMAP = "https://www.exoticcartrader.com/sitemap/listings.xml"
# Fallback sitemap — alphabetic ordering by URL slug rather than chronological,
# but contains every active /listing/ URL on the site (~8.3K). Used when the
# primary sitemap times out / 5xx's; we sort by trailing lot-number desc
# afterwards to approximate newest-first walking, then rely on per-VDP
# source_added_at filtering for the cutoff. (2026-05-04 fix: prior version
# would abort the entire scan on a single Read-timed-out, swept inventory
# was preserved by the abort circuit but the next morning's scan also
# transient-failed and the user got no fresh data for days.)
ECT_CMS_SITEMAP = "https://www.exoticcartrader.com/cms-sitemap.xml"

# Sitemap fetch retry schedule — 3 attempts, 60s per-attempt timeout, with
# delays between retries. ~150s of grace before falling back to cms-sitemap.
SITEMAP_RETRY_DELAYS = (5, 15, 30)
SITEMAP_PER_ATTEMPT_TIMEOUT = 60

# Trailing numeric lot ID for sorting cms-sitemap URLs newest-first when we
# fall back. Modern slug-form URLs end in `-260456575`-style numbers that
# increase monotonically with listing creation; legacy VIN-form URLs lack a
# trailing number and are pushed to the end (mostly older inventory).
_URL_LOT_TAIL_RE = re.compile(r"-(\d{6,})$")

# Stop walking after this many consecutive older-than-cutoff listings — the
# sitemap is chronologically ordered but not guaranteed strict, so we don't
# break on the first old one.
CONSECUTIVE_OLD_STOP = 40

# Hard upper bound on per-scan VDP fetches — defense in depth.
MAX_VDP_FETCHES = 1500

_IMG_TS_RE = re.compile(r"listingcontent\.exoticcartrader\.com/(\d{13})x")
_VIN_URL_RE = re.compile(r"/listing/([A-HJ-NPR-Z0-9]{17})(?:[/?#]|$)")
_VIN_TEXT_RE = re.compile(r"VIN\s+([A-HJ-NPR-Z0-9]{17})\b")
_LOT_RE = re.compile(r"Lot\s*#(\d+)", re.I)
_LOC_RE = re.compile(r"<loc>\s*([^<]+?)\s*</loc>")
_NAME_YEAR_RE = re.compile(r"^(\d{4})\s+(.+)$")
# JS-injected Product literal (post-2026-05-01 site change)
_JS_STRUCTURED_DATA_RE = re.compile(
    r"var\s+structuredData\s*=\s*(\{.*?\});", re.DOTALL)
_JS_PRICE_RE = re.compile(r'var\s+price\s*=\s*parseInt\("\$([\d,]+)"')
_JS_PRICE_FIELD_RE = re.compile(r'"price"\s*:\s*price\b')
# og:image meta carries the old listingcontent.exoticcartrader.com URL with
# embedded UnixMillis timestamp, even though structuredData.image points at
# the Webflow CDN (no timestamp).
_OG_IMAGE_RE = re.compile(
    r'<meta\s+(?:property|name)=["\']og:image["\']\s+content=["\']([^"\']+)["\']',
    re.IGNORECASE)
# URL slug trailing digits = ECT lot number for slug-form URLs like
# /listing/1992-am-general-hummer-260252713
_URL_LOT_RE = re.compile(r"-(\d{6,})(?:[/?#]|$)")

_MULTI_WORD_MAKES = (
    "Aston Martin", "Alfa Romeo", "Land Rover", "Range Rover",
    "Mercedes-Benz", "Mercedes Benz", "Rolls-Royce", "Rolls Royce",
    "AC Cars", "AM General", "Ruf",
)


def _parse_name(name: str):
    """'1989 Land Rover Defender 130' → (1989, 'Land Rover', 'Defender 130')."""
    s = (name or "").strip()
    if not s:
        return None, None, None
    m = _NAME_YEAR_RE.match(s)
    if not m:
        return None, None, None
    try:
        year = int(m.group(1))
    except ValueError:
        year = None
    rest = m.group(2).strip()
    rl = rest.lower()
    make, remainder = None, rest
    for mm in _MULTI_WORD_MAKES:
        if rl.startswith(mm.lower()):
            make = mm
            remainder = rest[len(mm):].strip()
            break
    if make is None:
        parts = rest.split(None, 1)
        make = parts[0] if parts else None
        remainder = parts[1] if len(parts) > 1 else ""
    model = remainder.strip() or None
    return year, make, model


def _find_product_jsonld(soup: BeautifulSoup) -> Optional[dict]:
    """Locate the @type=Product block among possibly-multiple JSON-LD scripts.

    Returns the first Product object found in any static <script type=
    "application/ld+json"> tag. Used by sites that still ship server-side
    structured data — kept for backwards compatibility with any non-migrated
    ECT VDPs and for any future Product-shipping site.
    """
    for s in soup.select('script[type="application/ld+json"]'):
        txt = s.get_text() or ""
        if not txt.strip():
            continue
        try:
            d = json.loads(txt)
        except Exception:
            continue
        if isinstance(d, dict) and d.get("@type") == "Product":
            return d
        if isinstance(d, list):
            for item in d:
                if isinstance(item, dict) and item.get("@type") == "Product":
                    return item
    return None


def _slice_js_object(html: str, var_name: str) -> Optional[str]:
    """Brace-balanced extraction of a JS object literal.

    Returns the {...} string for `var <var_name> = {...}` or None. Uses depth
    counting (with simple string-state tracking so braces inside double-quoted
    strings don't confuse the counter) instead of a lazy regex, because the
    structuredData object is followed by an unrelated `window.__X = {...};`
    line whose terminating `};` would falsely terminate a lazy match.
    """
    pat = re.compile(rf"var\s+{re.escape(var_name)}\s*=\s*", re.IGNORECASE)
    m = pat.search(html)
    if not m:
        return None
    start = html.find("{", m.end())
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(html)):
        c = html[i]
        if escape:
            escape = False
            continue
        if c == "\\":
            escape = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return html[start:i + 1]
    return None


def _find_product_js_literal(html: str) -> Optional[dict]:
    """Extract `var structuredData = {...}` JS literal as a Product dict.

    Substitutes the `price` JS variable reference with the int parsed from the
    preceding `var price = parseInt("$X,XXX"...)` declaration so the literal
    becomes valid JSON. Returns None if either piece is missing.
    """
    literal = _slice_js_object(html, "structuredData")
    if not literal:
        return None
    price_int = None
    pm = _JS_PRICE_RE.search(html)
    if pm:
        try:
            price_int = int(pm.group(1).replace(",", ""))
        except ValueError:
            price_int = None
    # Sub `"price": price` → `"price": <int>` (or 0 if we couldn't find one;
    # caller-side price coercion will treat 0 as missing)
    literal = _JS_PRICE_FIELD_RE.sub(f'"price": {price_int or 0}', literal)
    try:
        d = json.loads(literal)
    except Exception:
        return None
    if isinstance(d, dict) and d.get("@type") == "Product":
        return d
    return None


def _extract_og_image(html: str) -> Optional[str]:
    """The og:image meta still points at the old listingcontent CDN URL with
    the embedded UnixMillis timestamp, even after the 2026-05-01 site change
    moved structuredData.image to the (timestamp-less) Webflow CDN."""
    m = _OG_IMAGE_RE.search(html)
    return m.group(1) if m else None


def _ect_extract_one(vdp_url: str, sess) -> Optional[dict]:
    """Fetch a single VDP and turn it into an AAN-shaped vehicle dict.

    Returns None if the page can't be parsed or has no Product data — which
    is also what happens for sold/removed listings on this site.

    Tries static JSON-LD first (legacy / non-migrated VDPs), then the JS
    `var structuredData = {...}` literal injected at runtime (post-2026-05-01
    Webflow migration). For source_added_at, prefers the og:image meta URL
    because it still carries the listingcontent.exoticcartrader.com path with
    embedded UnixMillis timestamp, even though structuredData.image now points
    at the Webflow CDN (which has no timestamp).
    """
    try:
        r = sess.get(vdp_url, timeout=15)
    except Exception:
        return None
    if r.status_code != 200 or not r.text:
        return None

    html = r.text
    soup = BeautifulSoup(html, "html.parser")

    # Try static JSON-LD first; fall back to JS-injected literal.
    product = _find_product_jsonld(soup) or _find_product_js_literal(html)
    if not product:
        return None

    # source_added_at: prefer og:image (still has listingcontent timestamp),
    # fall back to the structuredData image (only carries timestamp on
    # legacy non-migrated VDPs).
    og_img = _extract_og_image(html)
    sd_img = product.get("image")
    if isinstance(sd_img, list):
        sd_img = sd_img[0] if sd_img else None
    sd_img = sd_img if isinstance(sd_img, str) else None

    source_added_at = None
    for candidate in (og_img, sd_img):
        if not candidate:
            continue
        m = _IMG_TS_RE.search(candidate)
        if m:
            ts = int(m.group(1)) / 1000.0
            source_added_at = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            break

    # Display photo: prefer the structuredData image (Webflow CDN, smaller +
    # the dealer's chosen hero shot), fall back to og:image. Skip the webflow
    # placeholder ("passenger-white.webp") that ECT shows for cars added
    # before real inventory photos are uploaded — better to store NULL than
    # render a misleading generic icon downstream.
    def _is_placeholder(u):
        if not u:
            return True
        ul = u.lower()
        return ("passenger-white" in ul or "placeholder" in ul)
    img = sd_img if not _is_placeholder(sd_img) else (og_img if not _is_placeholder(og_img) else None)

    # Year / make / model from `name`
    year, make, model = _parse_name(product.get("name") or "")

    # Price: structuredData.offers.price is the source of truth on legacy
    # VDPs and on JS-injected VDPs (we substituted the JS variable upstream).
    offers = product.get("offers") or {}
    price = offers.get("price")
    try:
        price = int(float(price)) if price not in (None, "", 0, "0") else None
    except (ValueError, TypeError):
        price = None

    # VIN — prefer the URL pattern (17-char trailing slug), else extract
    # from meta description ("... VIN ABCDEFGHJKLMNPRTU| Lot #...").
    vin = None
    um = _VIN_URL_RE.search(vdp_url)
    if um:
        vin = um.group(1)
    else:
        # Slug-form URLs: try meta description, then og:description (newer
        # VDPs sometimes only have og:description, not the legacy `name=
        # "description"` tag).
        meta = soup.find("meta", attrs={"name": "description"})
        desc_text = (meta.get("content") if meta else "") or ""
        if not desc_text:
            ogd = soup.find("meta", attrs={"property": "og:description"})
            desc_text = (ogd.get("content") if ogd else "") or ""
        tm = _VIN_TEXT_RE.search(desc_text)
        if tm:
            vin = tm.group(1)

    # Stock # = ECT lot number. Try (a) meta description "Lot #N", then
    # (b) trailing digits on the URL slug ("/listing/...-260252713"). The
    # latter is the only source on JS-injected VDPs that lack a meta desc.
    stock = None
    meta = soup.find("meta", attrs={"name": "description"})
    if meta:
        lm = _LOT_RE.search(meta.get("content") or "")
        if lm:
            stock = lm.group(1)
    if not stock:
        um = _URL_LOT_RE.search(vdp_url)
        if um:
            stock = um.group(1)

    # Canonical URL — ECT has a /listing/<slug-or-VIN> canonical even when
    # the sitemap returns /beta/listing/...; prefer canonical for storage.
    canon = soup.find("link", rel="canonical")
    canon_href = canon.get("href") if canon else None
    url = canon_href or vdp_url

    return {
        "vin": (vin or "").strip().upper(),
        "year": year,
        "make": make,
        "model": model,
        "trim": None,
        "ext_color": None,
        "int_color": None,
        "body_style": None,
        "stock_number": stock,
        "mileage": None,
        "price": price,
        "url": url,
        "photo_url": img,
        "photos": [img] if img else [],
        "source_added_at": source_added_at,
    }


def _fetch_sitemap_with_retry(sess, sitemap_url, log):
    """Fetch a sitemap URL with retry+backoff. Returns response.text on success
    or None on terminal failure. Logs each attempt clearly so cron logs make
    the failure mode obvious."""
    attempts = len(SITEMAP_RETRY_DELAYS)
    last_err = None
    for n in range(1, attempts + 1):
        try:
            r = sess.get(sitemap_url, timeout=SITEMAP_PER_ATTEMPT_TIMEOUT)
            if r.status_code == 200 and r.text:
                if n > 1:
                    log(f"  [ect] sitemap attempt {n}/{attempts} succeeded "
                        f"({sitemap_url})", flush=True)
                return r.text
            last_err = f"HTTP {r.status_code}"
        except Exception as exc:
            last_err = str(exc) or type(exc).__name__
        if n < attempts:
            delay = SITEMAP_RETRY_DELAYS[n - 1]
            log(f"  [ect] sitemap attempt {n}/{attempts} failed: {last_err}, "
                f"retrying in {delay}s ({sitemap_url})", flush=True)
            time.sleep(delay)
        else:
            log(f"  [ect] sitemap attempt {n}/{attempts} failed: {last_err} "
                f"({sitemap_url}) — giving up on this source", flush=True)
    return None


def _load_ect_urls(sess, sitemap_url, log):
    """Load and order ECT listing URLs newest-first.

    Tries the primary listings.xml (chronological, walked in reverse). On
    terminal failure tries cms-sitemap.xml (alphabetic — we re-sort by
    trailing lot-number desc to approximate chronological order). Returns
    (urls_list, source_label) or (None, None) on total failure.
    """
    # Primary: chronological sitemap, reverse for newest-first.
    body = _fetch_sitemap_with_retry(sess, sitemap_url, log)
    if body:
        urls = _LOC_RE.findall(body)
        if urls:
            urls.reverse()
            return urls, "primary"
        log("  [ect] primary sitemap had zero <loc> entries", flush=True)

    # Fallback: cms-sitemap.xml, sort by lot-number desc.
    log(f"  [ect] falling back to cms-sitemap ({ECT_CMS_SITEMAP})", flush=True)
    body = _fetch_sitemap_with_retry(sess, ECT_CMS_SITEMAP, log)
    if not body:
        return None, None
    raw = _LOC_RE.findall(body)
    # Restrict to /listing/ URLs (cms-sitemap also includes builder pages,
    # CMS landing pages, etc. that we don't want to fetch as VDPs).
    listing_urls = [u for u in raw if "/listing/" in u]
    if len(listing_urls) < 50:
        log(f"  [ect] cms-sitemap fallback: only {len(listing_urls)} listing "
            f"URLs found (need >=50) — treating as failure", flush=True)
        return None, None

    def _sort_key(u):
        m = _URL_LOT_TAIL_RE.search(u)
        # Slug-form URLs sort by lot-number desc (negative = larger first).
        # VIN-form URLs (no trailing lot number) get pushed to the end.
        return (0, -int(m.group(1))) if m else (1, 0)

    listing_urls.sort(key=_sort_key)
    return listing_urls, "fallback"


def fetch_ect_inventory(base_url, sess, *, max_age_days=90, max_vehicles=None,
                        sitemap_url=ECT_SITEMAP, log=print) -> Optional[list]:
    """Walk the ECT listings sitemap from newest to oldest, return vehicle
    dicts for listings created within `max_age_days`.

    Returns None only when BOTH the primary sitemap and the cms-sitemap
    fallback are unreachable / unparsable. Returns [] if sitemap was readable
    but yielded no in-window listings (treat as ok).
    """
    urls, source = _load_ect_urls(sess, sitemap_url, log)
    if not urls:
        log("  [ect] both primary sitemap and cms-sitemap fallback failed — "
            "returning None so scanner abort circuit preserves inventory",
            flush=True)
        return None
    log(f"  [ect] sitemap: {len(urls)} URLs total ({source}), "
        f"walking newest-first", flush=True)

    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    cap = min(max_vehicles or MAX_VDP_FETCHES, MAX_VDP_FETCHES)
    # Fallback ordering (sort-by-lot-number) is approximately chronological
    # but not strictly so — bump the consecutive-old stop on the fallback
    # path to avoid prematurely terminating if a few old listings cluster.
    consecutive_old_stop = (CONSECUTIVE_OLD_STOP if source == "primary"
                            else CONSECUTIVE_OLD_STOP * 3)

    vehicles = []
    consecutive_old = 0
    fetched = 0
    too_old = 0
    no_product = 0
    progress_every = 50
    # Circuit breaker thresholds: if we burn through the warm-up window and
    # the JSON-LD Product block is missing on most VDPs, the site is serving
    # CF challenges / empty shells / rate-limit pages. Returning [] from this
    # state would let the scanner reconcile against an empty list and wipe
    # active inventory (2026-05-01 incident: 1500 fetched, 1500 no_product,
    # kept 0 → 451 active rows swept). Returning None signals hard failure
    # so the early-return path's zero-vehicle guard preserves inventory.
    NO_PRODUCT_WARMUP = 30
    NO_PRODUCT_RATIO_ABORT = 0.6

    for vdp_url in urls:
        if fetched >= cap:
            log(f"  [ect] hit fetch cap {cap}, stopping", flush=True)
            break
        v = _ect_extract_one(vdp_url, sess)
        fetched += 1
        if v is None:
            no_product += 1
            if fetched >= NO_PRODUCT_WARMUP \
                    and no_product / fetched > NO_PRODUCT_RATIO_ABORT:
                log(f"  [ect] aborting: {no_product}/{fetched} VDPs missing "
                    f"JSON-LD Product (ratio {no_product/fetched:.0%} > "
                    f"{NO_PRODUCT_RATIO_ABORT:.0%}) — likely CF challenge "
                    f"or rate-limit, signalling hard failure", flush=True)
                return None
            continue

        added = v.get("source_added_at")
        is_old = False
        if added:
            try:
                added_dt = datetime.fromisoformat(added.replace("Z", "+00:00")) \
                    if isinstance(added, str) else added
                is_old = added_dt < cutoff
            except Exception:
                pass

        if is_old:
            too_old += 1
            consecutive_old += 1
            if consecutive_old >= consecutive_old_stop:
                log(f"  [ect] {consecutive_old_stop} consecutive listings older "
                    f"than {max_age_days}d — stopping at sitemap pos "
                    f"{len(urls) - urls.index(vdp_url)}", flush=True)
                break
            continue

        consecutive_old = 0
        vehicles.append(v)
        if fetched % progress_every == 0:
            log(f"  [ect] fetched {fetched}, kept {len(vehicles)}, "
                f"too_old {too_old}, no_product {no_product}", flush=True)

    log(f"  [ect] done — fetched {fetched}, kept {len(vehicles)}, "
        f"too_old {too_old}, no_product {no_product}", flush=True)
    # Post-loop circuit breaker: if we burned through fetches and kept zero
    # while no_product was the dominant reason, signal hard failure rather
    # than handing back []. Distinguishes "site is broken" from the legit
    # "every recent listing is older than the cutoff window" case (where
    # too_old would dominate and kept could legitimately be 0).
    if vehicles == [] and fetched >= NO_PRODUCT_WARMUP \
            and no_product / max(fetched, 1) > 0.5:
        log(f"  [ect] post-scan abort: kept 0 with no_product-dominant "
            f"({no_product}/{fetched}), returning None", flush=True)
        return None
    return vehicles
