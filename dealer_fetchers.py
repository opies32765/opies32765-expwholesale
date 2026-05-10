"""
Fetch tiers for the dealer scanner.

Tiered in cost/speed order. Scanner picks a tier per platform and can
escalate if a tier yields nothing.

  Tier 1 — fetch_direct           | $0    | fastest, works ~67% of dealers
  Tier 2 — fetch_flaresolverr     | $0    | real headless Chromium, handles Akamai / Cloudflare
  Tier 3 — fetch_flaresolverr_proxy | ~$0.001/pg | tier 2 through DataImpulse residential IP
  Tier 4 — fetch_direct_proxy     | ~$0.001/pg | fast + residential IP, for dealers gated only on IP

Each fetcher returns (status_code, final_url, body_text).
status_code = None when the fetch raised a transport error.
status_code = 599 is our synthetic "redirected to an internal address" code.

FlareSolverr must be running at FLARESOLVERR_URL (default http://127.0.0.1:8191).
DataImpulse creds default to the same values used by CarHub's 3proxy on C1.
"""
import json
import os
import time
from urllib.parse import urlparse

import requests

FLARESOLVERR_URL = os.environ.get('FLARESOLVERR_URL', 'http://127.0.0.1:8191/v1')
FLARESOLVERR_TIMEOUT_MS = int(os.environ.get('FLARESOLVERR_TIMEOUT_MS', '60000'))

DATAIMPULSE_HOST = os.environ.get('DATAIMPULSE_HOST', 'gw.dataimpulse.com')
DATAIMPULSE_PORT = int(os.environ.get('DATAIMPULSE_PORT', '10000'))
DATAIMPULSE_USER = os.environ.get('DATAIMPULSE_USER', '6e88fc8bf9340b01470e')
DATAIMPULSE_PASS = os.environ.get('DATAIMPULSE_PASS', '42928cf323560da3')

REQUEST_TIMEOUT = int(os.environ.get('DEALER_HTTP_TIMEOUT', '20'))


# ── Tier 1: direct ──────────────────────────────────────────────────────
def fetch_direct(url, sess, method='GET'):
    """Plain HTTP. Guards against redirects to internal addresses."""
    try:
        r = sess.request(method, url, timeout=REQUEST_TIMEOUT, allow_redirects=False)
        if r.is_redirect:
            loc = (r.headers.get('Location') or '').lower()
            if _looks_internal(loc):
                return 599, url, 'redirect_to_internal'
            r = sess.request(method, url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        return r.status_code, r.url, (r.text if method == 'GET' else '')
    except requests.RequestException as e:
        return None, None, f'error:{type(e).__name__}'


# ── Tier 2: FlareSolverr ────────────────────────────────────────────────
def fetch_flaresolverr(url, sess=None, method='GET'):
    """Route through a local FlareSolverr container. Handles Cloudflare/Akamai
    by executing the JS challenge in a real headless Chromium."""
    payload = {
        'cmd': 'request.get' if method == 'GET' else 'request.post',
        'url': url,
        'maxTimeout': FLARESOLVERR_TIMEOUT_MS,
    }
    try:
        resp = requests.post(FLARESOLVERR_URL, json=payload, timeout=90)
        data = resp.json()
    except Exception as e:
        return None, None, f'flaresolverr_error:{type(e).__name__}:{e}'
    if data.get('status') != 'ok':
        return None, None, f'flaresolverr_bad:{data.get("message","unknown")}'
    sol = data.get('solution', {}) or {}
    body = sol.get('response', '') or ''
    if not body or len(body) < 200:
        return None, sol.get('url', url), 'flaresolverr_empty'
    # FlareSolverr returns a body wrapped in a <pre> for some servers — unwrap
    if body.startswith('<html><head></head><body><pre'):
        # Strip the <pre> wrapper — leaves raw JSON/XML
        import re
        m = re.search(r'<pre[^>]*>([\s\S]*?)</pre>', body)
        if m:
            body = m.group(1)
    return sol.get('status', 200), sol.get('url', url), body


# ── Tier 3: FlareSolverr + residential proxy ────────────────────────────
def fetch_flaresolverr_proxy(url, sess=None, method='GET'):
    """FlareSolverr routed through a residential proxy (DataImpulse).
    Most expensive tier — only for sites that check IP reputation AND JS fingerprint."""
    payload = {
        'cmd': 'request.get' if method == 'GET' else 'request.post',
        'url': url,
        'maxTimeout': FLARESOLVERR_TIMEOUT_MS,
        'proxy': {
            'url': f'http://{DATAIMPULSE_HOST}:{DATAIMPULSE_PORT}',
            'username': DATAIMPULSE_USER,
            'password': DATAIMPULSE_PASS,
        },
    }
    try:
        resp = requests.post(FLARESOLVERR_URL, json=payload, timeout=120)
        data = resp.json()
    except Exception as e:
        return None, None, f'flaresolverr_proxy_error:{type(e).__name__}'
    if data.get('status') != 'ok':
        return None, None, f'flaresolverr_proxy_bad:{data.get("message","unknown")}'
    sol = data.get('solution', {}) or {}
    return sol.get('status', 200), sol.get('url', url), sol.get('response', '') or ''


# ── Tier 4: direct HTTP via residential proxy ───────────────────────────
def fetch_direct_proxy(url, sess, method='GET'):
    """Plain HTTP, but through a DataImpulse residential IP. Cheap + fast when
    the only gate is IP reputation (no JS challenge)."""
    proxies = {
        'http':  f'http://{DATAIMPULSE_USER}:{DATAIMPULSE_PASS}@{DATAIMPULSE_HOST}:{DATAIMPULSE_PORT}',
        'https': f'http://{DATAIMPULSE_USER}:{DATAIMPULSE_PASS}@{DATAIMPULSE_HOST}:{DATAIMPULSE_PORT}',
    }
    try:
        r = sess.request(method, url, timeout=REQUEST_TIMEOUT,
                         allow_redirects=False, proxies=proxies)
        if r.is_redirect:
            loc = (r.headers.get('Location') or '').lower()
            if _looks_internal(loc):
                return 599, url, 'redirect_to_internal'
            r = sess.request(method, url, timeout=REQUEST_TIMEOUT,
                             allow_redirects=True, proxies=proxies)
        return r.status_code, r.url, (r.text if method == 'GET' else '')
    except requests.RequestException as e:
        return None, None, f'error:{type(e).__name__}'


# ── Router ──────────────────────────────────────────────────────────────
# Escalation chain (cheapest/fastest → most expensive/slowest):
#   direct              → our Contabo IP, plain HTTP
#   flaresolverr        → Contabo IP, headless Chromium (defeats JS challenges)
#   flaresolverr_proxy  → residential IP via FlareSolverr (defeats IP-reputation + JS)
#   direct_proxy        → residential IP, plain HTTP (defeats IP-reputation ONLY,
#                         but ~10× faster than flaresolverr_proxy — ideal when the
#                         site is merely cache-poisoning our server IP, e.g.,
#                         TXT Charlie's Hostinger/LiteSpeed 302 → 127.0.0.1)
TIER_ORDER = ('direct', 'flaresolverr', 'direct_proxy', 'flaresolverr_proxy')

_FETCHERS = {
    'direct': fetch_direct,
    'flaresolverr': fetch_flaresolverr,
    'flaresolverr_proxy': fetch_flaresolverr_proxy,
    'direct_proxy': fetch_direct_proxy,
}

# Default tier per detected platform. Unknown / custom / WordPress / Shopify
# etc. stay on 'direct'. Known-protected platforms jump ahead.
PLATFORM_TIER = {
    'aan':               'direct',   # has JSON API — direct is fine
    'wordpress':         'direct',
    'shopify':           'direct',
    'autorevo':          'direct',
    'autodealercms':     'direct',
    'frazer':            'direct',
    'dealercenter':      'direct',
    'custom':            'direct',
    'unknown':           'direct',
    'dealer.com':        'flaresolverr',
    'dealerinspire':     'flaresolverr',
    'greenlight':        'direct',
    'ridemotive':        'flaresolverr',
    'dealer-eprocess':   'flaresolverr',
    'vinsolutions':      'flaresolverr',
    'cdk':               'flaresolverr',
    'homenet':           'flaresolverr_proxy',
}


def fetch(url, sess, method='GET', tier='direct'):
    fn = _FETCHERS.get(tier, fetch_direct)
    return fn(url, sess, method=method)


def tier_for_platform(platform):
    return PLATFORM_TIER.get((platform or '').lower(), 'direct')


def next_tier(current):
    """Returns the next tier to escalate to, or None if we're out of tiers."""
    try:
        i = TIER_ORDER.index(current)
    except ValueError:
        return TIER_ORDER[0]
    if i + 1 >= len(TIER_ORDER):
        return None
    return TIER_ORDER[i + 1]


def _looks_internal(loc):
    loc = (loc or '').lower()
    return (
        '127.0.0.1' in loc or
        'localhost' in loc or
        loc.startswith('http://10.') or
        loc.startswith('http://192.168.') or
        loc.startswith('http://172.16.') or
        loc.startswith('http://172.17.') or
        loc.startswith('http://172.18.') or
        loc.startswith('http://172.19.') or
        loc.startswith('http://172.2')
    )


def flaresolverr_healthy():
    """Quick ping to confirm FlareSolverr is reachable before escalating."""
    try:
        url = FLARESOLVERR_URL.replace('/v1', '/health')
        r = requests.get(url, timeout=3)
        return r.status_code == 200
    except Exception:
        return False
