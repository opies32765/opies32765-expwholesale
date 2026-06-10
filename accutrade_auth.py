#!/usr/bin/env python3
"""accutrade_auth.py - headless minter for the AccuTrade sirius_key (API token).

Replicates the browser's Auth0 universal-login + PKCE chain, fully server-side:

  1. PKCE verifier/challenge (S256)
  2. GET  {auth0}/authorize?...&login_hint=USER  -> 302 -> /u/login/password?state=S1
  3. POST {auth0}/u/login/password {state:S1, username, password}
         -> 302 -> /authorize/resume -> {redirect_uri}?code=CODE
  4. POST {auth0}/oauth/token {client_id, code_verifier, grant_type, code, redirect_uri}
         -> Auth0 access_token (JWT)
  5. POST {lyra}/api/auth/login/ {access_token} -> profile.sirius_key  (the API token)

Credentials live in /etc/ew-accutrade.json (root, 0600). Secrets are never logged.
The sirius_key is a stable per-user key, so it is cached and only re-minted on demand
or when older than --max-age.
"""
import os
import sys
import json
import base64
import hashlib
import secrets
import time
import re
import subprocess
from urllib.parse import urlparse, parse_qs
import requests

SIRIUS = "https://sirius-api-production.accu-trade.com"
HERCULES = "https://hercules-api-production.accu-trade.com"

# Pooled accu-trade.com cookies (incl Akamai _abck/bm_sz) live in vauto_session,
# pushed by the worker's cookie export exactly like vAuto's 'oscarpas' label.
DB_URL = os.environ.get("DATABASE_URL",
                        "postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale")
COOKIE_LABEL = os.environ.get("ACCUTRADE_COOKIE_LABEL", "accutrade")


def load_pool_cookies(label=None):
    """Load pooled accu-trade.com cookies from vauto_session.label. Accepts
    either a {name: value} dict or a [{name,value,...}] list. Returns
    {name: value} (empty if none). Never raises."""
    label = label or COOKIE_LABEL
    try:
        import psycopg2
        conn = psycopg2.connect(DB_URL)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT cookies FROM vauto_session WHERE label=%s", (label,))
                row = cur.fetchone()
        finally:
            conn.close()
        if not row or not row[0]:
            return {}
        raw = row[0]
        if isinstance(raw, str):
            raw = json.loads(raw)
        if isinstance(raw, dict):
            return {k: v for k, v in raw.items() if isinstance(v, str)}
        out = {}
        for c in raw or []:
            if c.get("name"):
                out[c["name"]] = c.get("value", "")
        return out
    except Exception:
        return {}

CFG_PATH = os.environ.get("ACCUTRADE_CFG", "/etc/ew-accutrade.json")
CACHE_PATH = os.environ.get("ACCUTRADE_KEY_CACHE", "/run/ew-accutrade-key.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")
APP_ORIGIN = "https://appraiser3.accu-trade.com"


def _cfg():
    with open(CFG_PATH, encoding="utf-8-sig") as f:
        return json.load(f)


def _b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _pkce():
    v = _b64url(secrets.token_bytes(32))
    c = _b64url(hashlib.sha256(v.encode()).digest())
    return v, c


def mint(debug=False):
    cfg = _cfg()
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    verifier, challenge = _pkce()
    authz = {
        "client_id": cfg["client_id"],
        "scope": "openid profile email offline_access",
        "redirect_uri": cfg["redirect_uri"],
        "login_hint": cfg["username"],
        "response_type": "code",
        "response_mode": "query",
        "state": _b64url(secrets.token_bytes(32)),
        "nonce": _b64url(secrets.token_bytes(32)),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "auth0Client": cfg["auth0client"],
    }
    # Step 2: /authorize -> login page (collect Auth0 transaction cookies)
    r = s.get(cfg["auth0"] + "/authorize", params=authz, allow_redirects=True, timeout=30)
    if debug:
        print("  [authorize] %s -> %s (%d)" % (authz["state"][:6], r.url, r.status_code), file=sys.stderr)
    login_state = parse_qs(urlparse(r.url).query).get("state", [None])[0]
    if not login_state:
        m = (re.search(r'name="state"[^>]*value="([^"]+)"', r.text)
             or re.search(r'"state"\s*:\s*"([^"]+)"', r.text))
        login_state = m.group(1) if m else None
    if not login_state:
        raise RuntimeError("no Auth0 login state (status=%s url=%s)" % (r.status_code, r.url))

    # Step 3: POST credentials
    r2 = s.post(cfg["auth0"] + "/u/login/password",
                data={"state": login_state, "username": cfg["username"], "password": cfg["password"]},
                headers={"Content-Type": "application/x-www-form-urlencoded",
                         "Referer": r.url, "Origin": cfg["auth0"]},
                allow_redirects=True, timeout=30)
    code = None
    for resp in [r2] + list(r2.history):
        q = parse_qs(urlparse(resp.url).query)
        if "code" in q:
            code = q["code"][0]
            break
    if debug:
        chain = " -> ".join("%d" % h.status_code for h in (list(r2.history) + [r2]))
        print("  [login] chain %s final=%s code=%s" % (chain, r2.url, bool(code)), file=sys.stderr)
    if not code:
        raise RuntimeError("login yielded no auth code (status=%s url=%s) - bad creds or Auth0 challenge"
                           % (r2.status_code, r2.url))

    # Step 4: exchange code -> Auth0 access_token
    tok = s.post(cfg["auth0"] + "/oauth/token",
                 json={"client_id": cfg["client_id"], "code_verifier": verifier,
                       "grant_type": "authorization_code", "code": code,
                       "redirect_uri": cfg["redirect_uri"]},
                 headers={"Origin": APP_ORIGIN, "Referer": APP_ORIGIN + "/"}, timeout=30)
    tok.raise_for_status()
    tj = tok.json()
    access_token = tj["access_token"]
    id_token = tj.get("id_token", "")

    # Step 5: lyra exchange -> sirius_key. The HAR body is a dummy {"access_token":"foobar"},
    # so the real auth is a header (Bearer) the HAR export stripped. Self-discover it.
    base_hdr = {"Origin": APP_ORIGIN, "Referer": APP_ORIGIN + "/", "Content-Type": "application/json"}
    strategies = [
        ("bearer-access+foobar", {"Authorization": "Bearer " + access_token}, {"access_token": "foobar"}),
        ("bearer-id+foobar",     {"Authorization": "Bearer " + id_token},     {"access_token": "foobar"}),
        ("bearer-access+jwt",    {"Authorization": "Bearer " + access_token}, {"access_token": access_token}),
        ("body-id",              {},                                          {"access_token": id_token}),
        ("bearer-id+jwt-id",     {"Authorization": "Bearer " + id_token},     {"access_token": id_token}),
        ("body-access",          {},                                          {"access_token": access_token}),
    ]
    last = None
    for name, hdr, body in strategies:
        h = dict(base_hdr)
        h.update(hdr)
        pr = s.post(cfg["lyra"] + "/api/auth/login/", json=body, headers=h, timeout=30)
        last = pr
        if debug:
            print("  [lyra:%s] -> %d" % (name, pr.status_code), file=sys.stderr)
        if pr.status_code == 200:
            try:
                prof = pr.json().get("profile") or {}
            except Exception:
                prof = {}
            if prof.get("sirius_key"):
                if debug:
                    print("  [lyra] WINNER strategy = %s" % name, file=sys.stderr)
                return prof["sirius_key"]
    raise RuntimeError("lyra login failed all strategies (last status=%s)"
                       % (last.status_code if last is not None else "n/a"))


def _cache_write(key):
    try:
        old = os.umask(0o077)
        try:
            with open(CACHE_PATH, "w") as f:
                json.dump({"sirius_key": key, "ts": int(time.time())}, f)
        finally:
            os.umask(old)
    except Exception:
        pass


def get_key(force=False, max_age=43200):
    if not force:
        try:
            with open(CACHE_PATH) as f:
                c = json.load(f)
            if c.get("sirius_key") and (time.time() - c.get("ts", 0)) < max_age:
                return c["sirius_key"]
        except Exception:
            pass
    key = mint()
    _cache_write(key)
    return key


def api_headers(key):
    # AccuTrade's sirius/hercules APIs 401 unless the FULL browser header set is present
    # (proven: token-only or partial header sets get rejected).
    return {
        "token": key,
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.9",
        "cache-control": "no-cache",
        "pragma": "no-cache",
        "priority": "u=1, i",
        "dnt": "1",
        "origin": APP_ORIGIN,
        "referer": APP_ORIGIN + "/",
        "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": UA,
    }


def api_get(key, path, base=SIRIUS, timeout=30, cookies=None):
    """GET an AccuTrade data endpoint via curl (--http2) to pass the CDN's
    HTTP/2/TLS fingerprint check that 401s urllib3/httpx. Sends pooled
    accu-trade.com cookies (Akamai _abck/bm_sz) so the call rides the real
    browser's bot-manager trust. Returns (status, body_text)."""
    h = api_headers(key)
    if cookies is None:
        cookies = load_pool_cookies()
    sentinel = "\n__HTTPCODE__"
    args = ["curl", "-s", "--http2", "--compressed", "-m", str(timeout),
            "-w", sentinel + "%{http_code}", base + path]
    for k, v in h.items():
        args += ["-H", "%s: %s" % (k, v)]
    if cookies:
        args += ["-b", "; ".join("%s=%s" % (k, v) for k, v in cookies.items())]
    out = subprocess.run(args, capture_output=True, text=True, timeout=timeout + 5)
    body, _, code = out.stdout.rpartition("__HTTPCODE__")
    body = body[:-1] if body.endswith("\n") else body
    try:
        return int(code.strip()), body
    except ValueError:
        return 0, out.stdout


def api_get_json(key, path, base=SIRIUS, timeout=30):
    st, body = api_get(key, path, base=base, timeout=timeout)
    if st != 200:
        raise RuntimeError("AccuTrade %s -> HTTP %s" % (path, st))
    return json.loads(body)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="ignore cache, mint fresh")
    ap.add_argument("--debug", action="store_true", help="trace the auth chain to stderr")
    ap.add_argument("--test", action="store_true", help="call a sirius endpoint to verify the key")
    args = ap.parse_args()
    try:
        if args.force or args.debug:
            key = mint(debug=args.debug)
            _cache_write(key)
        else:
            key = get_key()
    except Exception as e:
        print("MINT FAILED: %s" % e)
        sys.exit(1)
    print("sirius_key OK: %s... len=%d" % (key[:8], len(key)))
    if args.test:
        for ep in ("/perseusurl", "/accuprice/appraisal/41696714"):
            st, body = api_get(key, ep)
            print("API(curl) %-35s -> HTTP %s (%d bytes)" % (ep, st, len(body)))
