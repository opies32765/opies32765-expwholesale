"""ECT (Exotic Car Trader) inventory fetcher.

ECT is a Webflow-fronted Bubble.io marketplace. Per-VDP data lives in static
JSON-LD `Product` blocks; per-listing creation timestamp lives encoded in the
primary image URL (`listingcontent.exoticcartrader.com/<UnixMillis>x<rowid>/...`).

We walk the listings sitemap newest→oldest, fetch each VDP, and stop when we
have seen N consecutive listings older than `max_age_days`. Returns vehicle
dicts in the same shape `_normalize_aan_vehicle` produces, so `_process_aan`
can ingest the result without per-platform branching downstream.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup

ECT_SITEMAP = "https://www.exoticcartrader.com/sitemap/listings.xml"

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

_MULTI_WORD_MAKES = (
    "Aston Martin", "Alfa Romeo", "Land Rover", "Range Rover",
    "Mercedes-Benz", "Mercedes Benz", "Rolls-Royce", "Rolls Royce",
    "AC Cars", "Ruf",
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
    """Locate the @type=Product block among possibly-multiple JSON-LD scripts."""
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


def _ect_extract_one(vdp_url: str, sess) -> Optional[dict]:
    """Fetch a single VDP and turn it into an AAN-shaped vehicle dict.

    Returns None if the page can't be parsed or has no Product JSON-LD —
    which is also what happens for sold/removed listings on this site.
    """
    try:
        r = sess.get(vdp_url, timeout=15)
    except Exception:
        return None
    if r.status_code != 200 or not r.text:
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    product = _find_product_jsonld(soup)
    if not product:
        return None

    # Image timestamp — bubble.io ID prefix encodes upload time as Unix ms
    img = product.get("image")
    if isinstance(img, list):
        img = img[0] if img else None
    img = img if isinstance(img, str) else None
    source_added_at = None
    m = _IMG_TS_RE.search(img or "")
    if m:
        ts = int(m.group(1)) / 1000.0
        source_added_at = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

    # Year / make / model from `name`
    year, make, model = _parse_name(product.get("name") or "")

    # Price from offers
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
        # Newer ECT URLs are slug-based; fall back to meta description.
        meta = soup.find("meta", attrs={"name": "description"})
        if meta:
            tm = _VIN_TEXT_RE.search(meta.get("content") or "")
            if tm:
                vin = tm.group(1)

    # Stock # = ECT lot number from meta description
    stock = None
    meta = soup.find("meta", attrs={"name": "description"})
    if meta:
        lm = _LOT_RE.search(meta.get("content") or "")
        if lm:
            stock = lm.group(1)

    # Canonical URL — ECT has a /listing/<VIN> canonical even when the
    # sitemap returns /beta/listing/<VIN>; prefer canonical for storage.
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


def fetch_ect_inventory(base_url, sess, *, max_age_days=90, max_vehicles=None,
                        sitemap_url=ECT_SITEMAP, log=print) -> Optional[list]:
    """Walk the ECT listings sitemap from newest to oldest, return vehicle
    dicts for listings created within `max_age_days`.

    Returns None on hard failure (sitemap unreachable). Returns [] if
    sitemap was readable but yielded no in-window listings (treat as ok).
    """
    try:
        r = sess.get(sitemap_url, timeout=30)
    except Exception as exc:
        log(f"  [ect] sitemap fetch failed: {exc}", flush=True)
        return None
    if r.status_code != 200 or not r.text:
        log(f"  [ect] sitemap status={r.status_code}", flush=True)
        return None

    urls = _LOC_RE.findall(r.text)
    if not urls:
        log("  [ect] sitemap had zero <loc> entries", flush=True)
        return None
    urls.reverse()  # newest first
    log(f"  [ect] sitemap: {len(urls)} URLs total, walking newest-first", flush=True)

    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    cap = min(max_vehicles or MAX_VDP_FETCHES, MAX_VDP_FETCHES)

    vehicles = []
    consecutive_old = 0
    fetched = 0
    too_old = 0
    no_product = 0
    progress_every = 50

    for vdp_url in urls:
        if fetched >= cap:
            log(f"  [ect] hit fetch cap {cap}, stopping", flush=True)
            break
        v = _ect_extract_one(vdp_url, sess)
        fetched += 1
        if v is None:
            no_product += 1
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
            if consecutive_old >= CONSECUTIVE_OLD_STOP:
                log(f"  [ect] {CONSECUTIVE_OLD_STOP} consecutive listings older than "
                    f"{max_age_days}d — stopping at sitemap pos "
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
    return vehicles
