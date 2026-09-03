#!/usr/bin/env python3
"""edge_weekly_pull.py — weekly EDGE Pipeline comps refresh for auction_comps.

Marker: EDGE_WEEKLY_PULL_2026_09_02.  Runs ON C1 (62.146.226.100) ONLY.

WHY THIS EXISTS
---------------
EDGE Pipeline retains only ~6 weeks of post-sale / no-sale reports.  A week we
do not pull is gone permanently.  This job runs every Saturday morning, pulls
whatever sale dates we do not already hold, and hands them to load_comps.py.

APPEND-ONLY.  This script NEVER deletes, expires or rewrites an auction_comps
row.  Its own DB connection is opened READ ONLY (psycopg2 set_session
readonly=True), so it is structurally incapable of removing history; the only
writer is load_comps.py's idempotent upsert.  That is the whole point of the
table: EW keeps what EDGE throws away.

  ============================================================================
  ONE-TIME HUMAN SETUP — the cookie jar
  ============================================================================
  This runs under the operator's REAL EDGE dealer account.  There is no API
  key; the session is a browser cookie set that a human must export.

  1. Log in to https://www.edgepipeline.com in Chrome as the dealer account.
  2. Export the cookies for that site with any cookie-export extension
     ("Cookie-Editor" / "EditThisCookie" -> Export -> JSON), or as a Netscape
     `cookies.txt`.  Save the file onto C1, e.g. /root/edge_cookies.json.
  3. Convert it into the session jar (this makes NO network request):

         python3 /opt/expwholesale/edge_weekly_pull.py \
             --import-cookies /root/edge_cookies.json

     -> writes /opt/expwholesale/state/edge_session.json (mode 0600) in the
     same schema cookie_jar.CookieJar already uses for vAuto
     (captured_at / cookies[] / headers{}).  `state/` is gitignored, so the
     jar never rides the nightly GitHub push.
  4. Delete the raw export (`shred -u /root/edge_cookies.json`).
  5. Validate WITHOUT downloading anything:

         python3 /opt/expwholesale/edge_weekly_pull.py --dry-run

     Dry run fetches only the list pages, checks session health, and prints
     exactly what a real run would download.  If the session is dead it says
     so and exits non-zero — it never silently reports "nothing new".
  6. When the jar goes stale (EDGE logged you out), repeat steps 1-3.  The
     job is a no-op-with-a-loud-failure until you do; it will not quietly
     skip a week.
  7. Install the timer (units live beside this file):

         cp /opt/expwholesale/ops/systemd/ew-edge-weekly-pull.{service,timer} \
            /etc/systemd/system/
         systemctl daemon-reload
         systemctl enable --now ew-edge-weekly-pull.timer   # operator does this

  Tune EDGE_SESSION_MAX_AGE_SEC once the real cookie lifetime is known; the
  default (30d) is deliberately generous so a weekly timer does not trip the
  age gate on its own.  The authoritative check is the LIVE probe, not the age.
  ============================================================================

USAGE
    edge_weekly_pull.py                 # normal weekly run
    edge_weekly_pull.py --dry-run       # list pages only, download nothing
    edge_weekly_pull.py --import-cookies FILE   # offline: build the jar
    edge_weekly_pull.py --slug aaayatb --slug orlandoaa   # subset
    edge_weekly_pull.py --no-ingest     # download only, skip load_comps

EXIT CODES
    0  success (including "nothing new", but only when the session verified OK)
    1  one or more slugs / downloads / the ingest failed
    2  session missing, stale or expired  (LOUD — the main failure mode)
    3  session looked alive but ZERO csv links were found across ALL slugs
       (the other silent-no-op signature)
"""
from __future__ import annotations

import argparse
import contextlib
import glob
import io
import json
import logging
import os
import random
import re
import sys
import time
import urllib.parse
from datetime import date, datetime, timedelta

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cookie_jar import CookieJar          # noqa: E402  (EW's existing jar)
import load_comps                          # noqa: E402  (the ingester; unmodified)

# ── Configuration ──────────────────────────────────────────────────────────

BASE = os.environ.get("EDGE_BASE_URL", "https://www.edgepipeline.com")

# The 13 Florida slugs.  These are the CANONICAL auction_slug values stored in
# auction_comps — NOT necessarily the URL segment EDGE uses (see below).
SLUGS = [
    "orlandolongwoodaafl",
    "southfloridaaa",
    "anaaorlando",
    "aaayatb",
    "aaayam",
    "jacksonvilleaa",
    "speedwayaa",
    "orlandoaa",
    "daxtampafl2",
    "aaayafm",
    "aaayasa",
    "aaapensacola",
    "vemoaag",
    # ATLANTA_HIGHLINE_2026_09_03 — added for high-line depth.  Measured on one
    # sale each: AutoNation ATL 26.6%% high-line / 43 cars >=$30k on 316 units,
    # Dealers AA ATL 34.6%% / 12 on 130.  AAA Atlanta returned ONE car on 09-02,
    # which is an unrepresentative day, not a dead auction (38 sale dates).
    "anaaatlanta",
    "vipauctions",
    "aaaatlanta",
]

KINDS = ("postsale", "nosale")            # the two list pages per slug

APP_DIR = os.path.dirname(os.path.abspath(__file__))
IN_DIR = os.environ.get("EDGE_COMPS_IN", "/opt/expwholesale/edge_comps_in")
LOG_PATH = os.environ.get("EDGE_PULL_LOG",
                          "/opt/expwholesale/logs/edge_weekly_pull.log")
SESSION_PATH = os.environ.get("EDGE_SESSION_PATH",
                              "/opt/expwholesale/state/edge_session.json")
# Generous on purpose: a weekly timer must not trip an age gate written for a
# 12h vAuto session.  The live probe is what actually decides.
SESSION_MAX_AGE_SEC = int(os.environ.get("EDGE_SESSION_MAX_AGE_SEC",
                                         30 * 24 * 3600))

# Pacing.  This runs under the operator's real dealer account; hammering EDGE
# risks the account.  ~4-6s between every HTTP request, 13 slugs x 2 list
# pages = 26 requests before a single CSV is touched.
REQ_DELAY_SEC = float(os.environ.get("EDGE_REQ_DELAY_SEC", "4.0"))
REQ_JITTER_SEC = float(os.environ.get("EDGE_REQ_JITTER_SEC", "2.0"))
REQ_TIMEOUT_SEC = float(os.environ.get("EDGE_REQ_TIMEOUT_SEC", "60"))
MAX_RETRIES = int(os.environ.get("EDGE_MAX_RETRIES", "2"))   # 5xx/timeout only
MAX_DOWNLOADS = int(os.environ.get("EDGE_MAX_DOWNLOADS", "200"))  # safety valve

# A report that came back with a header but no data rows may simply not have
# been published yet.  Re-check those for this many days, then let them be.
EMPTY_RECHECK_DAYS = int(os.environ.get("EDGE_EMPTY_RECHECK_DAYS", "21"))

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36")

# kind -> the outcome load_comps writes for it.
KIND_OUTCOME = {"postsale": "sold", "nosale": "no_sale"}

log = logging.getLogger("edge_weekly_pull")


# ── Exceptions ─────────────────────────────────────────────────────────────

class SessionExpired(RuntimeError):
    """EDGE bounced us to a login page / 401 / 403.  Never continue."""


class FetchError(RuntimeError):
    pass


# ── Href parsing ───────────────────────────────────────────────────────────
#
# CRITICAL: the CSV slug segment is NOT uniform.  Orlando Longwood exports as
# `orlandolongwoodaafl-all`, Pensacola as plain `aaapensacola`.  We take the
# href VERBATIM off the list page and never reconstruct a URL from a date —
# that produced 20 silent 404s.

HREF_ATTR_RE = re.compile(r"""href\s*=\s*(?:"([^"]+)"|'([^']+)'|([^\s>]+))""",
                          re.I)
CSV_PATH_RE = re.compile(
    r"/components/report/(postsale|nosale)/csv/([^/?#\"'\s]+)/(\d{4})/(\d{1,2})/(\d{1,2})",
    re.I)

# A password box or a login/signin landing URL means the session is dead.
LOGIN_INPUT_RE = re.compile(r"""<input[^>]+type\s*=\s*["']?password""", re.I)
LOGIN_URL_RE = re.compile(r"/(login|signin|sign[-_]in|logon)\b", re.I)


def parse_csv_links(html: str, page_url: str) -> list[dict]:
    """Pull every postsale/nosale CSV link out of a list page.

    The href is used VERBATIM (urljoin'd against the page, since EDGE may emit
    relative or absolute hrefs).  `kind` comes from the href itself, not from
    which page we happen to be on — list pages cross-link.
    Returns dicts: {kind, segment, y, m, d, date_str, url}.
    """
    out: dict[str, dict] = {}

    def _consider(raw_href: str) -> None:
        if not raw_href:
            return
        resolved = urllib.parse.urljoin(page_url, raw_href.strip())
        m = CSV_PATH_RE.search(urllib.parse.urlsplit(resolved).path)
        if not m:
            return
        kind, segment, y, mo, d = m.groups()
        rec = {
            "kind": kind.lower(),
            "segment": segment,
            "y": int(y), "m": int(mo), "d": int(d),
            "date_str": f"{int(y):04d}-{int(mo):02d}-{int(d):02d}",
            "stamp": f"{int(y):04d}{int(mo):02d}{int(d):02d}",
            "url": resolved,
        }
        out.setdefault(resolved, rec)

    for m in HREF_ATTR_RE.finditer(html or ""):
        _consider(m.group(1) or m.group(2) or m.group(3))

    if not out and html:
        # Fallback: the link may live in an onclick/data-* attribute rather
        # than an href.  Still taken verbatim FROM THE PAGE, never rebuilt.
        for m in CSV_PATH_RE.finditer(html):
            _consider(m.group(0))
        if out:
            log.warning("  href-attribute scan found nothing; recovered %d "
                        "csv link(s) via raw-page scan on %s", len(out), page_url)

    return sorted(out.values(), key=lambda r: (r["kind"], r["date_str"]))


def slug_for_segment(segment: str, configured_slug: str) -> str:
    """Map an EDGE URL segment back to our canonical auction_slug.

    `orlandolongwoodaafl-all` -> `orlandolongwoodaafl`; `aaapensacola` stays.
    If the segment does not reduce to the slug whose list page we asked for,
    that is an anomaly: shout, and keep the CONFIGURED slug so the DB lineage
    for that auction never forks.
    """
    reduced = segment[:-4] if segment.lower().endswith("-all") else segment
    if reduced.lower() != configured_slug.lower():
        log.warning("  segment/slug mismatch: href segment %r does not reduce "
                    "to configured slug %r — filing under %r (url still uses "
                    "the href verbatim)", segment, configured_slug,
                    configured_slug)
        return configured_slug
    return configured_slug


def filename_for(kind: str, segment: str, slug: str, stamp: str) -> str:
    """Name the CSV so load_comps.FNAME parses it back to `slug`.

    Keeps EDGE's own segment when it reduces cleanly (matches the files the
    manual backfill produced, e.g. `..._orlandolongwoodaafl-all_...`).
    """
    reduced = segment[:-4] if segment.lower().endswith("-all") else segment
    seg = segment if reduced.lower() == slug.lower() else slug
    name = f"edgepipeline_{kind}_{seg}_{stamp}.csv"
    # Belt and braces: the ingester must agree with us about slug+outcome.
    m = load_comps.FNAME.search(name)
    if not m or m.group(2) != slug or load_comps.OUTCOME[m.group(1)] != KIND_OUTCOME[kind]:
        raise RuntimeError(f"refusing to write {name!r}: load_comps would not "
                           f"parse it back to slug={slug} kind={kind}")
    return name


# ── HTTP ───────────────────────────────────────────────────────────────────

_last_request_at = [0.0]
_html_csv_streak = [0]
# HTML where CSV was expected: 1-2 can be a "report not published" page;
# this many in a row means the session, not the date, is the problem.
HTML_CSV_STREAK_ABORT = int(os.environ.get("EDGE_HTML_CSV_STREAK_ABORT", "3"))


def _pace() -> None:
    """Sleep so consecutive requests are >= REQ_DELAY_SEC (+jitter) apart."""
    wait = REQ_DELAY_SEC + random.uniform(0, REQ_JITTER_SEC)
    elapsed = time.monotonic() - _last_request_at[0]
    if _last_request_at[0] and elapsed < wait:
        time.sleep(wait - elapsed)
    _last_request_at[0] = time.monotonic()


def _check_auth(resp: requests.Response) -> None:
    """Raise SessionExpired on anything that smells like a logged-out reply."""
    if resp.status_code in (401, 403):
        raise SessionExpired(f"HTTP {resp.status_code} from {resp.url}")
    if LOGIN_URL_RE.search(urllib.parse.urlsplit(resp.url).path or ""):
        raise SessionExpired(f"redirected to a login URL: {resp.url}")
    ctype = (resp.headers.get("content-type") or "").lower()
    if "html" in ctype or not ctype:
        body = resp.text[:200000]
        if LOGIN_INPUT_RE.search(body):
            raise SessionExpired(f"login form (password input) served at {resp.url}")


def fetch(session: requests.Session, url: str, referer: str | None = None,
          expect_csv: bool = False) -> requests.Response:
    """Paced GET with bounded retries.

    Retries ONLY on 5xx / timeout / connection error.  A 404 on a verbatim
    href is real — no retry, it gets logged and counted as a failure.
    401/403/login-redirect aborts the whole run immediately: continuing would
    just hammer EDGE with a dead session.
    """
    headers = {"Referer": referer} if referer else {}
    last: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        _pace()
        try:
            resp = session.get(url, headers=headers, timeout=REQ_TIMEOUT_SEC,
                               allow_redirects=True)
        except requests.RequestException as e:
            last = e
            log.warning("  request error (%s/%s) on %s: %s",
                        attempt + 1, MAX_RETRIES + 1, url, e)
            time.sleep(5 * (attempt + 1))
            continue

        _check_auth(resp)                          # raises SessionExpired

        if resp.status_code >= 500:
            last = FetchError(f"HTTP {resp.status_code}")
            log.warning("  HTTP %s (%s/%s) on %s", resp.status_code,
                        attempt + 1, MAX_RETRIES + 1, url)
            time.sleep(5 * (attempt + 1))
            continue
        if resp.status_code != 200:
            raise FetchError(f"HTTP {resp.status_code} on {url}")

        if expect_csv:
            head = resp.text.lstrip()[:400]
            ctype = (resp.headers.get("content-type") or "").lower()
            if head.startswith("<") or "html" in ctype:
                # A 200 that is actually HTML on a CSV endpoint is either an
                # EDGE "no report" page (one bad date) or the login wall (a
                # dead session).  _check_auth above already caught the obvious
                # login markers, so treat the FIRST few as per-date failures
                # and only call the session dead once they pile up — a wrong
                # "session dead" diagnosis is itself a failure mode.
                _html_csv_streak[0] += 1
                if _html_csv_streak[0] >= HTML_CSV_STREAK_ABORT:
                    raise SessionExpired(
                        f"{_html_csv_streak[0]} consecutive CSV endpoints "
                        f"returned HTML ({ctype or 'no content-type'}); latest "
                        f"{resp.url} — session is almost certainly dead")
                raise FetchError(
                    f"CSV endpoint returned HTML ({ctype or 'no content-type'}) "
                    f"at {resp.url} — report may not be published")
            _html_csv_streak[0] = 0
        return resp
    raise FetchError(f"gave up on {url}: {last}")


def build_session(jar: CookieJar) -> requests.Session:
    s = requests.Session()
    for c in jar.data.get("cookies", []):
        name, value = c.get("name"), c.get("value")
        if not name:
            continue
        kw = {}
        if c.get("domain"):
            kw["domain"] = c["domain"]
        if c.get("path"):
            kw["path"] = c["path"]
        s.cookies.set(name, value, **kw)
    headers = {k: v for k, v in (jar.get_headers() or {}).items()
               if k.lower() in ("user-agent", "accept-language", "sec-ch-ua",
                                "sec-ch-ua-mobile", "sec-ch-ua-platform")}
    headers.setdefault("User-Agent", DEFAULT_UA)
    headers.setdefault("Accept",
                       "text/html,application/xhtml+xml,application/xml;q=0.9,"
                       "text/csv,*/*;q=0.8")
    headers.setdefault("Accept-Language", "en-US,en;q=0.9")
    s.headers.update(headers)
    return s


# ── Session health — checked FIRST, fails closed ───────────────────────────

def get_jar() -> CookieJar:
    if not os.path.exists(SESSION_PATH):
        raise SessionExpired(
            f"no EDGE session file at {SESSION_PATH}. Export cookies from a "
            f"logged-in browser and run: {sys.argv[0]} --import-cookies FILE "
            f"(see the README block at the top of this script).")
    jar = CookieJar(SESSION_PATH)
    jar.load()
    return jar


def check_session_health(jar: CookieJar, session: requests.Session,
                         probe_slug: str) -> tuple[str, str]:
    """Three fail-closed layers, run BEFORE anything else.

    (a) jar age vs EDGE_SESSION_MAX_AGE_SEC
    (b) cookie `expires` timestamps already in the past
    (c) a LIVE probe of the first slug's postsale list page — 401/403, a
        login redirect or a password form aborts the run.
    The probe response is returned so the caller reuses it instead of paying
    for a duplicate request.
    """
    age = jar.age_seconds()
    log.info("session: %s captured_at=%s age=%.1fh",
             SESSION_PATH, jar.captured_at(), age / 3600.0)
    if age > SESSION_MAX_AGE_SEC:
        raise SessionExpired(
            f"cookie jar is {age/3600:.1f}h old (max "
            f"{SESSION_MAX_AGE_SEC/3600:.1f}h) — re-export EDGE cookies and "
            f"run --import-cookies.")

    now = time.time()
    cookies = jar.data.get("cookies", [])
    dated = [c for c in cookies
             if isinstance(c.get("expires"), (int, float)) and c["expires"] > 0]
    expired = [c for c in dated if c["expires"] < now]
    if dated and len(expired) == len(dated):
        raise SessionExpired(
            f"every dated cookie in the jar has expired ({len(expired)}/"
            f"{len(dated)}) — re-export EDGE cookies.")
    if expired:
        log.warning("session: %d/%d dated cookies already expired (%s) — "
                    "continuing to the live probe",
                    len(expired), len(dated),
                    ", ".join(sorted(c["name"] for c in expired)[:6]))
    if not cookies:
        raise SessionExpired("cookie jar contains no cookies at all.")

    url = list_url(probe_slug, "postsale")
    log.info("session: live probe -> %s", url)
    resp = fetch(session, url, referer=BASE + "/")     # raises SessionExpired
    log.info("session: probe OK (HTTP %s, %d bytes, final url %s)",
             resp.status_code, len(resp.content), resp.url)
    return url, resp.text


def list_url(slug: str, kind: str) -> str:
    return f"{BASE}/components/report/{kind}/list/{slug}"


# ── What do we already have? ───────────────────────────────────────────────

def db_dsn() -> str:
    dsn = os.environ.get("DATABASE_URL") or load_comps.DSN
    if not dsn:
        raise RuntimeError("DATABASE_URL not found (env or "
                           "/etc/default/expwholesale-mcp)")
    return dsn


def db_have() -> set[tuple[str, str, str]]:
    """{(auction_slug, outcome, 'YYYY-MM-DD')} already in auction_comps.

    Opened READ ONLY — this script cannot modify or delete history.
    """
    import psycopg2
    conn = psycopg2.connect(db_dsn(), connect_timeout=15)
    try:
        conn.set_session(readonly=True, autocommit=True)
        cur = conn.cursor()
        cur.execute("SELECT pg_is_in_recovery()")
        if cur.fetchone()[0]:
            raise SystemExitStandby()
        cur.execute("SELECT auction_slug, outcome, sale_date "
                    "FROM auction_comps GROUP BY 1,2,3")
        return {(s, o, d.isoformat()) for s, o, d in cur.fetchall()}
    finally:
        conn.close()


class SystemExitStandby(RuntimeError):
    """This host is a PG standby (i.e. C2). Never pull from here."""


def data_rows(path: str) -> int:
    """Count CSV data rows (header excluded); -1 if unreadable."""
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            import csv as _csv
            rdr = _csv.reader(fh)
            n = sum(1 for _ in rdr)
        return max(0, n - 1)
    except Exception:
        return -1


def local_index() -> dict[tuple[str, str, str], str]:
    """{(slug, outcome, 'YYYY-MM-DD'): path} for CSVs already on disk.

    Keyed with load_comps' OWN regex so our idea of slug+outcome can never
    drift from the ingester's.

    pre_sale files are EXCLUDED on purpose.  This job only pulls postsale and
    nosale.  A pre_sale row is upgraded to sold/no_sale by a later post-sale
    file, at which point its (slug,'pre_sale',date) key disappears from the DB
    — so a presale CSV left in the directory would look like a permanent
    orphan and be re-ingested every Saturday, and load_comps' upsert would
    overwrite the sold row's announcements/source_file with stale pre-sale
    values each time.
    """
    idx: dict[tuple[str, str, str], str] = {}
    for path in sorted(glob.glob(os.path.join(IN_DIR, "*.csv"))):
        m = load_comps.FNAME.search(os.path.basename(path))
        if not m:
            continue
        kind, slug, y, mo, d = m.groups()
        outcome = load_comps.OUTCOME[kind]
        if outcome not in KIND_OUTCOME.values():      # skip pre_sale
            continue
        idx[(slug, outcome, f"{y}-{mo}-{d}")] = path
    return idx


# ── Planning (pure — no network, no DB) ────────────────────────────────────

def plan(links: list[dict], have_db: set, have_local: dict,
         today: date | None = None) -> tuple[list[dict], list[str]]:
    """Decide what to download and what to (re-)ingest.

    A link is MISSING when (slug, outcome, sale_date) is absent from
    auction_comps AND there is no usable local CSV for it.  A local CSV with
    zero data rows and a sale date inside EMPTY_RECHECK_DAYS is treated as
    "report not published yet" and re-downloaded.

    Orphans — local CSVs whose (slug, outcome, date) is absent from the DB —
    are queued for ingest even if we download nothing, so a crash between
    download and ingest cannot strand a file forever.
    """
    today = today or date.today()
    to_download: list[dict] = []
    to_ingest: list[str] = []
    seen_keys: set[tuple[str, str, str]] = set()

    for rec in links:
        key = (rec["slug"], KIND_OUTCOME[rec["kind"]], rec["date_str"])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        if key in have_db:
            rec["skip"] = "in-db"
            continue
        path = have_local.get(key)
        if path:
            rows = data_rows(path)
            if rows > 0:
                rec["skip"] = "local-file-pending-ingest"
                to_ingest.append(path)
                continue
            try:
                sd = datetime.strptime(rec["date_str"], "%Y-%m-%d").date()
            except ValueError:
                sd = today
            if (today - sd).days > EMPTY_RECHECK_DAYS:
                rec["skip"] = f"local-file-empty-and-older-than-{EMPTY_RECHECK_DAYS}d"
                continue
            rec["refetch_empty"] = True
        to_download.append(rec)

    # Orphan sweep: anything on disk the DB has never seen.  Guarded again on
    # outcome so a pre_sale file can never re-ingest over an upgraded row.
    for key, path in sorted(have_local.items()):
        if key in have_db or key[1] not in KIND_OUTCOME.values():
            continue
        if path in to_ingest:
            continue
        if data_rows(path) > 0:
            to_ingest.append(path)

    # Oldest first: if MAX_DOWNLOADS ever bites, protect the dates closest to
    # rolling off EDGE's ~6-week retention window.
    to_download.sort(key=lambda r: (r["date_str"], r["slug"], r["kind"]))
    return to_download, sorted(set(to_ingest))


# ── Download + ingest ──────────────────────────────────────────────────────

def download(session: requests.Session, rec: dict, referer: str) -> str | None:
    """Fetch one CSV to IN_DIR atomically (.part -> rename). Returns path."""
    name = filename_for(rec["kind"], rec["segment"], rec["slug"], rec["stamp"])
    dest = os.path.join(IN_DIR, name)
    tmp = dest + ".part"
    resp = fetch(session, rec["url"], referer=referer, expect_csv=True)
    with open(tmp, "wb") as fh:
        fh.write(resp.content)
    rows = data_rows(tmp)
    if rows < 0:
        os.unlink(tmp)
        raise FetchError(f"unparseable CSV from {rec['url']}")
    os.replace(tmp, dest)
    log.info("    saved %-62s %5d rows  (%s)", name, rows, rec["url"])
    return dest if rows > 0 else None


def ingest(paths: list[str]) -> None:
    """Hand the files to the EXISTING load_comps ingester, unmodified.

    load_comps upserts only (ON CONFLICT DO UPDATE); nothing here deletes.
    """
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        load_comps.main(paths)
    for line in buf.getvalue().splitlines():
        if line.strip():
            log.info("load_comps| %s", line.rstrip())


# ── Cookie import (offline; no network) ────────────────────────────────────

def import_cookies(src: str, dest: str = SESSION_PATH) -> None:
    cookies: list[dict] = []
    raw = open(src, encoding="utf-8-sig", errors="replace").read()
    parsed = None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, list):
        items = parsed
    elif isinstance(parsed, dict):
        items = parsed.get("cookies") or []
        if not items and all(isinstance(v, str) for v in parsed.values()):
            items = [{"name": k, "value": v} for k, v in parsed.items()]
    else:
        items = None

    if items is not None:
        for c in items:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            exp = c.get("expirationDate", c.get("expires"))
            try:
                exp = float(exp) if exp not in (None, "", -1) else -1
            except (TypeError, ValueError):
                exp = -1
            cookies.append({
                "name": c["name"], "value": c.get("value", ""),
                "domain": c.get("domain") or ".edgepipeline.com",
                "path": c.get("path") or "/",
                "expires": exp,
                "secure": bool(c.get("secure")),
                "httpOnly": bool(c.get("httpOnly", c.get("httponly"))),
                "sameSite": c.get("sameSite") or "Lax",
            })
    else:
        # Netscape cookies.txt
        from http.cookiejar import MozillaCookieJar
        mj = MozillaCookieJar(src)
        mj.load(ignore_discard=True, ignore_expires=True)
        for c in mj:
            cookies.append({
                "name": c.name, "value": c.value,
                "domain": c.domain, "path": c.path,
                "expires": float(c.expires) if c.expires else -1,
                "secure": bool(c.secure), "httpOnly": False,
                "sameSite": "Lax",
            })

    if not cookies:
        raise SystemExit(f"no cookies found in {src}")

    payload = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "cookies": cookies,
        "headers": {
            "user-agent": os.environ.get("EDGE_USER_AGENT", DEFAULT_UA),
            "accept-language": "en-US,en;q=0.9",
            "referer": BASE + "/",
        },
        "source": os.path.abspath(src),
    }
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, dest)
    names = sorted(c["name"] for c in cookies)
    print(f"wrote {dest} with {len(cookies)} cookies "
          f"({', '.join(names[:8])}{'...' if len(names) > 8 else ''})")
    print("now validate with:  python3 %s --dry-run" % sys.argv[0])


# ── Logging ────────────────────────────────────────────────────────────────

def setup_logging(verbose: bool = False) -> None:
    """File log always; stdout copy too (the systemd unit deliberately does
    NOT redirect stdout into the same file, or every line would land twice —
    stdout goes to the journal instead)."""
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.handlers[:] = [fh, sh]


ALERT_PHONE = "4074309675"  # EDGE_PULL_SMS_ALERT_2026_09_03 — same ops number as ew_backup_watch.py

def _envfile(path="/etc/default/expwholesale-mcp"):
    """EDGE_PULL_SMS_ALERT_2026_09_03: read creds from the FILE, never os.environ.

    A cron/systemd job does not inherit the unit's Environment=, and Twilio then
    fails silently — which is the exact failure mode this alert exists to catch.
    """
    out = {}
    try:
        for ln in open(path):
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return out


def alert_sms(text):
    """Best-effort SMS to the ops phone. Same number as ew_backup_watch.py."""
    try:
        env = _envfile()
        sid = env.get("TWILIO_ACCOUNT_SID")
        tok = env.get("TWILIO_AUTH_TOKEN")
        frm = env.get("TWILIO_PHONE")
        to = os.environ.get("EW_BW_PHONE") or env.get("EW_BW_PHONE") or ALERT_PHONE
        if not (sid and tok and frm and to):
            log.warning("sms alert: missing twilio creds or destination")
            return False
        if not to.startswith("+"):
            to = "+1" + to
        from twilio.rest import Client
        Client(sid, tok).messages.create(to=to, from_=frm, body=text[:1500])
        log.info("sms alert sent to %s", to[:-4] + "XXXX")
        return True
    except Exception as e:
        log.warning("sms alert failed: %s", e)
        return False

def alert(text: str) -> None:
    """Best-effort Telegram ping, matching /usr/local/bin/vauto_cookie_alert.py.

    No-ops silently when the token/chat are not configured. Operator ops
    channel only — this never contains customer or enrichment data.
    """
    # EDGE_PULL_SMS_ALERT_2026_09_03: SMS FIRST and unconditionally. Telegram
    # below returns early when unconfigured, and a missed week is permanently
    # unrecoverable (EDGE deletes at 6 weeks), so the channel that gets read
    # must not sit behind another channel's config check.
    alert_sms(text)

    env = dict(os.environ)
    if os.path.exists("/etc/ew_failover.env"):
        try:
            for line in open("/etc/ew_failover.env"):
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.strip().split("=", 1)
                    env.setdefault(k, v.strip().strip("'\""))
        except Exception:
            pass
    tok, chat = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return
    try:
        import urllib.request
        data = urllib.parse.urlencode({"chat_id": chat, "text": text,
                                       "disable_web_page_preview": "true"}).encode()
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            data=data, timeout=8).read()
    except Exception as e:
        log.warning("telegram alert failed: %s", e)


# ── Main ───────────────────────────────────────────────────────────────────

def run(args) -> int:
    t0 = time.time()
    log.info("=" * 78)
    log.info("EDGE weekly pull starting (dry_run=%s, slugs=%d, delay=%.1f-%.1fs)",
             args.dry_run, len(args.slugs), REQ_DELAY_SEC,
             REQ_DELAY_SEC + REQ_JITTER_SEC)
    os.makedirs(IN_DIR, exist_ok=True)

    # 1. What do we already hold?  (READ ONLY; also refuses to run on a standby)
    try:
        have_db = db_have()
    except SystemExitStandby:
        log.warning("this host is a Postgres STANDBY (pg_is_in_recovery) — "
                    "EDGE pulls run on C1 only. Exiting without doing anything.")
        return 0
    have_local = local_index()
    log.info("auction_comps holds %d distinct (slug, outcome, sale_date) keys; "
             "%d CSVs already on disk in %s",
             len(have_db), len(have_local), IN_DIR)

    # 2. SESSION HEALTH FIRST — a dead session must never look like "no news".
    jar = get_jar()
    session = build_session(jar)
    probe_url, probe_html = check_session_health(jar, session, args.slugs[0])

    # 3. List pages.
    all_links: list[dict] = []
    failures: list[str] = []
    total_hrefs = 0
    for slug in args.slugs:
        for kind in KINDS:
            url = list_url(slug, kind)
            try:
                html = probe_html if url == probe_url else \
                    fetch(session, url, referer=BASE + "/").text
            except SessionExpired:
                raise
            except Exception as e:
                log.error("  LIST FAIL %-22s %-8s %s", slug, kind, e)
                failures.append(f"list {slug}/{kind}: {e}")
                continue
            links = [r for r in parse_csv_links(html, url)
                     if r["kind"] in KIND_OUTCOME]
            total_hrefs += len(links)
            for r in links:
                r["slug"] = slug_for_segment(r["segment"], slug)
                r["list_url"] = url
            log.info("  %-22s %-8s %2d csv link(s)%s", slug, kind, len(links),
                     "" if links else "   <- none advertised")
            all_links.extend(links)

    # The other silent-no-op signature: session "fine", zero links anywhere.
    if total_hrefs == 0:
        msg = ("ZERO csv links found across ALL %d slugs. Either every list "
               "page changed shape or the session is dead in a way the probe "
               "missed. NOT treating this as 'nothing new'." % len(args.slugs))
        log.error(msg)
        alert("EW EDGE weekly pull FAILED: " + msg)
        return 3

    # 4. Plan.
    to_download, to_ingest = plan(all_links, have_db, have_local)
    log.info("plan: %d csv link(s) advertised, %d already held, %d to download,"
             " %d local file(s) queued for ingest",
             len(all_links), len(all_links) - len(to_download),
             len(to_download), len(to_ingest))
    for rec in to_download:
        log.info("  WANT %-22s %-8s %s%s", rec["slug"], rec["kind"],
                 rec["date_str"],
                 "  (re-fetch: local file was empty)"
                 if rec.get("refetch_empty") else "")

    if args.dry_run:
        log.info("DRY RUN — nothing downloaded, nothing ingested. "
                 "Session verified OK.")
        return 1 if failures else 0

    if len(to_download) > MAX_DOWNLOADS:
        log.warning("capping downloads at EDGE_MAX_DOWNLOADS=%d (wanted %d); "
                    "the remainder will be picked up next run",
                    MAX_DOWNLOADS, len(to_download))
        to_download = to_download[:MAX_DOWNLOADS]

    # 5. Download, paced.
    fetched = 0
    for rec in to_download:
        try:
            path = download(session, rec, referer=rec["list_url"])
            fetched += 1
            if path:
                to_ingest.append(path)
        except SessionExpired:
            raise
        except Exception as e:
            log.error("  DOWNLOAD FAIL %-22s %-8s %s: %s",
                      rec["slug"], rec["kind"], rec["date_str"], e)
            failures.append(f"download {rec['slug']}/{rec['kind']}/"
                            f"{rec['date_str']}: {e}")

    # 6. Ingest via the existing loader (upsert only — APPEND-ONLY).
    to_ingest = sorted(set(to_ingest))
    if to_ingest and not args.no_ingest:
        log.info("ingesting %d file(s) via load_comps", len(to_ingest))
        try:
            ingest(to_ingest)
        except Exception as e:
            log.exception("INGEST FAILED: %s", e)
            failures.append(f"ingest: {e}")
    elif args.no_ingest:
        log.info("--no-ingest: %d file(s) left for load_comps", len(to_ingest))
    else:
        log.info("nothing new to ingest")

    # 7. Summary.
    dt = time.time() - t0
    log.info("SUMMARY: downloaded=%d ingested_files=%d list_pages=%d "
             "failures=%d elapsed=%.1fs",
             fetched, 0 if args.no_ingest else len(to_ingest),
             len(args.slugs) * len(KINDS), len(failures), dt)
    if failures:
        for f in failures:
            log.error("  FAILURE: %s", f)
        alert(f"EW EDGE weekly pull finished with {len(failures)} failure(s): "
              + "; ".join(failures[:5]))
        return 1
    log.info("OK")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch list pages only; download and ingest nothing")
    ap.add_argument("--import-cookies", metavar="FILE",
                    help="build the session jar from a browser cookie export "
                         "(JSON or Netscape cookies.txt). Makes no network "
                         "request.")
    ap.add_argument("--slug", action="append", dest="slug_filter",
                    help="restrict to one slug (repeatable)")
    ap.add_argument("--no-ingest", action="store_true",
                    help="download only; do not run load_comps")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    if args.import_cookies:
        import_cookies(args.import_cookies)
        return 0

    setup_logging(args.verbose)
    unknown = [s for s in (args.slug_filter or []) if s not in SLUGS]
    if unknown:
        log.error("unknown slug(s): %s", ", ".join(unknown))
        return 1
    args.slugs = args.slug_filter or SLUGS

    try:
        return run(args)
    except SessionExpired as e:
        log.error("EDGE SESSION DEAD: %s", e)
        log.error("This run pulled NOTHING. EDGE keeps only ~6 weeks of "
                  "reports, so refresh the cookie jar TODAY: export cookies "
                  "from a logged-in browser, then run "
                  "`python3 %s --import-cookies FILE` and re-run this job.",
                  os.path.abspath(__file__))
        alert("EW EDGE weekly pull ABORTED — session dead: %s. Re-export "
              "edgepipeline cookies (only ~6 weeks of reports are retained)."
              % e)
        return 2
    except KeyboardInterrupt:
        log.error("interrupted")
        return 1
    except Exception as e:
        log.exception("UNHANDLED: %s", e)
        alert(f"EW EDGE weekly pull crashed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
