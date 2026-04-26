"""
Generic dealer extractor driven by a JSON scrape_config.

The config is produced by discover_dealer.py (AI) or written by hand. This
module reads the config and runs extraction — no per-dealer Python code.

Supports four inventory_source.type values:
  - api_json     : fetch URL → JSON → JSONPath to vehicle list
  - next_data    : fetch HTML → find <script id="__NEXT_DATA__"> → JSON → JSONPath
  - html_listing : fetch HTML → CSS selector for vehicle cards
  - sitemap      : fetch sitemap.xml → list of VDP URLs → fetch each → extract

Field extraction uses simple JSONPath ($.foo.bar[*].baz) for JSON sources,
and CSS-selector → text/attr extraction for HTML sources.

Pagination supports page_param (most common) and offset; "next_link" requires
sitemap-style following and is handled per-call.
"""
import json
import re
import sys
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "DNT": "1",
}


# ─── Tiny JSONPath ────────────────────────────────────────────────────────
# Supports: $.foo.bar, $.foo[0].bar, $.foo[*].bar, foo.bar (no $ prefix).
_PATH_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+|\*)\]")

def _jsonpath(data, path):
    """Return a list of matches (always a list, even single match).
    For [*], returns multiple. For unknown keys, returns []."""
    if path in ("$", "", None):
        return [data]
    p = path.lstrip("$").lstrip(".")
    tokens = []
    for m in _PATH_TOKEN.finditer(p):
        if m.group(1):
            tokens.append(("key", m.group(1)))
        else:
            idx = m.group(2)
            tokens.append(("idx", "*" if idx == "*" else int(idx)))
    cur = [data]
    for tok_type, tok in tokens:
        nxt = []
        for c in cur:
            if tok_type == "key":
                if isinstance(c, dict) and tok in c:
                    nxt.append(c[tok])
            else:  # idx
                if isinstance(c, list):
                    if tok == "*":
                        nxt.extend(c)
                    elif 0 <= tok < len(c):
                        nxt.append(c[tok])
        cur = nxt
        if not cur:
            return []
    return cur


def _first(values):
    """Return the first non-None value from a JSONPath result list."""
    for v in values:
        if v is not None:
            return v
    return None


# ─── Field coercion ───────────────────────────────────────────────────────
def _coerce_int(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).replace(",", "").replace("$", "").replace(" ", "").strip()
    m = re.match(r"-?\d+", s)
    return int(m.group(0)) if m else None

def _coerce_text(v):
    if v is None:
        return None
    if isinstance(v, list):
        return " ".join(str(x) for x in v if x).strip() or None
    return str(v).strip() or None

def _coerce_url(v, base_url=None):
    s = _coerce_text(v)
    if not s:
        return None
    if s.startswith(("http://", "https://")):
        return s
    if base_url:
        return urljoin(base_url, s)
    return s

def _coerce_list(v):
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v if x]
    return [str(v)]


_FIELD_COERCERS = {
    "vin":             _coerce_text,
    "year":            _coerce_int,
    "make":            _coerce_text,
    "model":           _coerce_text,
    "trim":            _coerce_text,
    "price":           _coerce_int,
    "miles":           _coerce_int,
    "stock_number":    _coerce_text,
    "exterior_color":  _coerce_text,
    "interior_color":  _coerce_text,
    "url":             None,  # special — needs base_url
    "photos":          _coerce_list,
}


# ─── Source fetchers ──────────────────────────────────────────────────────
def _fetch(url, sess=None, headers=None, method="GET", body=None):
    s = sess or requests.Session()
    h = {**DEFAULT_HEADERS, **(headers or {})}
    r = s.request(method, url, headers=h, data=body, timeout=25, allow_redirects=True)
    return r

def _extract_next_data(html):
    """Find <script id="__NEXT_DATA__">...</script> and return the parsed JSON."""
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag or not tag.string:
        return None
    try:
        return json.loads(tag.string)
    except Exception:
        return None


# ─── Field extraction (per source kind) ──────────────────────────────────
def _extract_fields_from_json(record, fields, base_url):
    """Apply field-map JSONPath extraction to one JSON record.
    Field value can be:
      - str path: "vin" or "$.props.foo"
      - dict {path, type, default}
      - dict {literal: "Ferrari"} — hardcoded value (e.g., make for single-brand sites)
    """
    out = {}
    for fname, fpath in (fields or {}).items():
        if not fpath:
            continue
        # Literal value — no path extraction
        if isinstance(fpath, dict) and "literal" in fpath:
            out[fname] = fpath["literal"]
            continue
        if isinstance(fpath, dict):
            path = fpath.get("path") or fpath.get("selector")
            kind = fpath.get("type")
            default = fpath.get("default")
        else:
            path = fpath
            kind = None
            default = None
        vals = _jsonpath(record, path)
        if not vals:
            out[fname] = default
            continue
        if fname == "photos":
            out[fname] = _coerce_list(vals if "[*]" in (path or "") else _first(vals))
        elif fname == "url":
            out[fname] = _coerce_url(_first(vals), base_url=base_url)
        else:
            coerce = _FIELD_COERCERS.get(fname, _coerce_text)
            if coerce:
                out[fname] = coerce(_first(vals))
            else:
                out[fname] = _first(vals)
    return out


def _extract_fields_from_html(card, fields, base_url):
    """Apply CSS selector extraction to one BeautifulSoup card element."""
    out = {}
    for fname, fpath in (fields or {}).items():
        if not fpath:
            continue
        if isinstance(fpath, dict):
            sel = fpath.get("selector") or fpath.get("path")
            attr = fpath.get("attr")
        else:
            sel = fpath
            attr = None
            # auto-detect "img@src" syntax
            m = re.match(r"^(.+)@(\w+)$", sel)
            if m:
                sel, attr = m.group(1), m.group(2)
        if not sel:
            continue
        if fname == "photos":
            els = card.select(sel)
            urls = [el.get(attr or "src") or el.get_text(strip=True) for el in els]
            out[fname] = [_coerce_url(u, base_url) for u in urls if u]
            continue
        el = card.select_one(sel)
        if not el:
            out[fname] = None
            continue
        if attr:
            raw = el.get(attr)
        else:
            raw = el.get_text(" ", strip=True)
        if fname == "url":
            out[fname] = _coerce_url(raw, base_url=base_url)
        else:
            coerce = _FIELD_COERCERS.get(fname, _coerce_text)
            out[fname] = coerce(raw) if coerce else raw
    return out


# ─── Main entrypoint ──────────────────────────────────────────────────────
def fetch_inventory(config, dealer_url, sess=None, max_vehicles=None):
    """Run a config against a dealer URL. Returns list of vehicle dicts."""
    if not config:
        return []
    src = config.get("inventory_source") or {}
    src_type = src.get("type")
    url_template = src.get("url_template", "")
    pagination = src.get("pagination") or {"type": "none"}
    extraction = config.get("extraction") or {}
    list_path = extraction.get("list_path", "")
    fields = extraction.get("fields") or {}

    sess = sess or requests.Session()
    base_for_urls = dealer_url
    pages_to_fetch = _enumerate_pages(url_template, pagination, dealer_url)

    vehicles = []
    for page_url in pages_to_fetch:
        try:
            r = _fetch(page_url, sess=sess,
                       headers=src.get("headers"),
                       method=src.get("method", "GET"),
                       body=src.get("body_template"))
        except Exception as e:
            print(f"  fetch error {page_url}: {e}", file=sys.stderr)
            continue
        if r.status_code != 200:
            continue

        if src_type == "api_json":
            try:
                data = r.json()
            except Exception:
                continue
            records = _jsonpath(data, list_path)
            for rec in records:
                vehicles.append(_extract_fields_from_json(rec, fields, base_for_urls))
        elif src_type == "next_data":
            data = _extract_next_data(r.text)
            if not data:
                continue
            records = _jsonpath(data, list_path)
            for rec in records:
                vehicles.append(_extract_fields_from_json(rec, fields, base_for_urls))
        elif src_type == "html_listing":
            soup = BeautifulSoup(r.text, "html.parser")
            cards = soup.select(list_path)
            for card in cards:
                vehicles.append(_extract_fields_from_html(card, fields, base_for_urls))
        elif src_type == "sitemap":
            # list_path here isn't used; we treat the response as XML
            soup = BeautifulSoup(r.text, "xml")
            urls = [loc.get_text(strip=True) for loc in soup.find_all("loc")]
            for vdp_url in urls[:200]:  # safety cap
                try:
                    vr = _fetch(vdp_url, sess=sess)
                    if vr.status_code == 200:
                        # treat the VDP as a single-record HTML extraction
                        soup_v = BeautifulSoup(vr.text, "html.parser")
                        vehicles.append(_extract_fields_from_html(soup_v, fields, base_for_urls))
                except Exception:
                    continue

        if max_vehicles and len(vehicles) >= max_vehicles:
            break

    # ─── Normalise extractor keys → DB column names ──────────────────────
    # The config calls fields by their human-friendly names (miles,
    # exterior_color, interior_color). The DealerScanner upsert path uses
    # the actual DB column names. Map them once here so every config can
    # use the readable names without each caller re-renaming.
    KEY_MAP = {
        'miles':           'mileage',
        'exterior_color':  'ext_color',
        'interior_color':  'int_color',
    }
    normalized = []
    for v in vehicles:
        if not v:
            continue
        nv = {}
        for k, val in v.items():
            nv[KEY_MAP.get(k, k)] = val
        # photos[] is a list; the upsert also wants a single primary photo_url.
        # Prefer config-supplied photo_url if present; otherwise lift the first
        # entry from photos[].
        if not nv.get('photo_url'):
            ph = nv.get('photos')
            if isinstance(ph, list) and ph:
                nv['photo_url'] = ph[0]
        normalized.append(nv)
    return normalized


def _enumerate_pages(url_template, pagination, dealer_url):
    """Yield up to N URLs based on pagination config."""
    base = dealer_url.rstrip("/")
    if "{base}" in url_template:
        url_template = url_template.replace("{base}", base)
    if not url_template.startswith("http"):
        url_template = urljoin(base + "/", url_template.lstrip("/"))

    ptype = pagination.get("type", "none")
    if ptype == "none" or "{page}" not in url_template and "{offset}" not in url_template:
        return [url_template]
    if ptype == "page_param":
        start = pagination.get("start", 1)
        max_p = pagination.get("max_pages", 30)
        return [url_template.replace("{page}", str(start + i)) for i in range(max_p)]
    if ptype == "offset":
        start = pagination.get("start", 0)
        step = pagination.get("step", 20)
        max_p = pagination.get("max_pages", 30)
        return [url_template.replace("{offset}", str(start + i * step)) for i in range(max_p)]
    return [url_template]


# ─── CLI for ad-hoc testing ──────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--config-file", required=True)
    ap.add_argument("--dealer-url", required=True)
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()
    cfg = json.load(open(args.config_file))
    veh = fetch_inventory(cfg, args.dealer_url, max_vehicles=args.limit)
    print(f"Extracted {len(veh)} vehicles. First 3:")
    for v in veh[:3]:
        print(json.dumps(v, indent=2, default=str))
