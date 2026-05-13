"""Live SOLD-status check for dealer listing pages.

Hits each candidate's detail_url with a browser-like UA and inspects the
HTML for sold markers. Used by the opportunity pipeline to filter cars
the dealer scanner hasn't caught yet as sold (e.g. dealer marks SOLD
mid-day, our morning scan said active).

Common sold patterns found in the wild:
  - CSS classes: car_single_summary_sold, *-sold, is-sold, badge-sold,
    sold-banner, sold-tag, vehicle-sold, status-sold
  - Big-font text inside a styled element: "SOLD", "Sold"
  - Text patterns: "no longer available", "sale pending"
"""
from __future__ import annotations
import re
import requests

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/130.0 Safari/537.36')
HEADERS = {
    'User-Agent': UA,
    'Accept': 'text/html,application/xhtml+xml',
    'Accept-Language': 'en-US,en;q=0.9',
}

# CSS class patterns — case-insensitive substring inside any class attr
CSS_SOLD_PATTERNS = [
    r'_sold(?:\b|[\s"\'])',     # car_single_summary_sold, status_sold etc.
    r'\bsold-(?:banner|tag|badge|overlay|ribbon|label|status)',
    r'\bis-sold\b',
    r'\bvehicle-sold\b',
    r'\bbadge-sold\b',
]
CSS_RE = re.compile(
    r'class=["\'][^"\']*(?:' + '|'.join(CSS_SOLD_PATTERNS) + r')[^"\']*["\']',
    re.IGNORECASE,
)

# Bold SOLD text in obvious display-element context
SOLD_TEXT_RE = re.compile(
    r'>\s*(?:SOLD|Sold)\s*</(?:span|div|h[1-6]|strong|p|b)\b',
)

# Other phrases
SALE_PENDING_RE = re.compile(
    r'\b(?:sale\s+pending|sale\s+is\s+pending|no\s+longer\s+available|'
    r'out\s+of\s+stock|currently\s+unavailable)\b',
    re.IGNORECASE,
)


def check_sold(url: str, timeout: int = 15) -> dict:
    """Fetch a dealer listing page and decide if it's sold.

    Returns:
        {ok: bool, sold: bool, reason: str, http: int, url: str}

    If we can't fetch the page (timeout, 4xx/5xx), returns ok=False —
    caller should treat it as "unknown" and not exclude the candidate.
    """
    out = {'ok': False, 'sold': False, 'reason': '', 'http': None, 'url': url}
    if not url:
        out['reason'] = 'no url'
        return out
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout,
                         allow_redirects=True)
        out['http'] = r.status_code
        if r.status_code == 404:
            out['ok'] = True
            out['sold'] = True
            out['reason'] = 'http_404'
            return out
        if not r.ok:
            out['reason'] = f'http_{r.status_code}'
            return out
        html = r.text
        if CSS_RE.search(html):
            out['ok'] = True
            out['sold'] = True
            out['reason'] = 'css_sold_class'
            return out
        if SOLD_TEXT_RE.search(html):
            out['ok'] = True
            out['sold'] = True
            out['reason'] = 'inline_sold_text'
            return out
        if SALE_PENDING_RE.search(html):
            out['ok'] = True
            out['sold'] = True
            out['reason'] = 'sale_pending_text'
            return out
        out['ok'] = True
        out['sold'] = False
        out['reason'] = 'active'
        return out
    except requests.RequestException as e:
        out['reason'] = f'network:{type(e).__name__}'
        return out
