"""vAuto lookup module — exports lookup(page, ctx, vin, miles, t)."""
import os, re, time
from pathlib import Path


def _parse_dollars(s):
    """Convert vAuto dollar text to int. '$50,775' -> 50775. '$0'/'—'/None -> None."""
    if not s or s in ("$0", "—", "--", "-", ""):
        return None
    try:
        digits = re.sub(r"[^0-9]", "", str(s))
        return int(digits) if digits else None
    except Exception:
        return None


REPORTS_DIR = Path(r"C:\worker\vauto_reports")
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

VAUTO_USERNAME = os.environ.get("VAUTO_USERNAME", "OscarPas")
VAUTO_PASSWORD = os.environ.get("VAUTO_PASSWORD", "Sedecremlun34$")
VAUTO_HOME = "https://www2.vauto.com/"
VAUTO_APPRAISAL = "https://provision.vauto.app.coxautoinc.com/Va/Appraisal/Default.aspx?new=true"
APPRAISAL_LIST = "https://provision.vauto.app.coxautoinc.com/Va/Appraisal/List.aspx?uq=1"
SUCCESS_HOSTS = ("provision.vauto.app.coxautoinc.com", "vauto.app.coxautoinc.com")
HYDRATE_TIMEOUT = 60

# ── Worker-side cookie pusher (added 2026-05-14) ──────────────────────────────
# Every successful lookup() captures the live Cox cookies from the Playwright
# context and POSTs the BFF-format payload to /api/vauto/refresh_session on C1.
# Mirrors verifier's cookie_export.py — gives 10+ producers feeding the pool
# so the verifier stops being a single point of failure.
EW_SERVER = os.environ.get("EW_SERVER", "https://experience-wholesale.net").rstrip("/")
EW_REFRESH_SECRET = os.environ.get(
    "EW_VAUTO_REFRESH_SECRET",
    "72bb9c82c4fb8d72220cdff8292afb7d1e8cc73bd073c67a5d4c3b4e1ed0a420")
SESSION_APPRAISAL_ID = os.environ.get(
    "EW_VAUTO_SESSION_APPRAISAL_ID",
    "qWNKSOaUPCW6x4lPKnM8iojBTMhHy415I2iIv9GiCZ4=")
PLATFORM_USER_ID = os.environ.get(
    "EW_PLATFORM_USER_ID", "871ccb54-8ee2-4b06-884c-763673204ae9")
ENTITY_ID = os.environ.get(
    "EW_ENTITY_ID", "jwaCvVdjsSFLY6C4O3LS63o-dJrUWByBui-rLqfI30Y=")
_WANTED_DOMAINS = ("coxautoinc.com", "vauto.com", "vauto.app.coxautoinc.com",
                   "okta", "megazord")
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")


def _push_vauto_session(ctx):
    """Snapshot ctx.cookies() and POST a BFF-shaped payload to refresh_session.
    Skips silently if vAutoAuth absent (Cox session not yet established) or
    if anything else fails (never wants to break a bid)."""
    try:
        all_cookies = ctx.cookies()
    except Exception as _e:
        print(f"[vauto] cookie snapshot failed: {type(_e).__name__}: {_e}")
        return
    bff_cookies = []
    have_vauto_auth = False
    for _c in all_cookies:
        _d = (_c.get("domain") or "").lstrip(".").lower()
        if not any(_dom in _d for _dom in _WANTED_DOMAINS):
            continue
        if _c.get("name") == "vAutoAuth":
            have_vauto_auth = True
        bff_cookies.append({
            "name":     _c.get("name"),
            "value":    _c.get("value"),
            "domain":   _c.get("domain", ""),
            "path":     _c.get("path", "/"),
            "secure":   bool(_c.get("secure", False)),
            "httpOnly": bool(_c.get("httpOnly", False)),
            "sameSite": _c.get("sameSite", "Lax"),
            "expires":  _c.get("expires", -1),
        })
    if not have_vauto_auth or len(bff_cookies) < 10:
        return  # not a real session yet
    payload = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "cookies": bff_cookies,
        "headers": {
            "platformuserid":    PLATFORM_USER_ID,
            "appraisalentityid": ENTITY_ID,
            "currententityid":   ENTITY_ID,
            "accept":             "application/json",
            "content-type":       "application/json",
            "referer":            "https://provision.vauto.app.coxautoinc.com/",
            "user-agent":         _BROWSER_UA,
            "sec-ch-ua-mobile":   "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-ch-ua":          '"Chromium";v="147", "Not.A/Brand";v="8"',
        },
        "session_appraisal_id": SESSION_APPRAISAL_ID,
    }
    try:
        import json as _json
        from urllib import request as _ureq
        _data = _json.dumps(payload).encode("utf-8")
        _req = _ureq.Request(
            f"{EW_SERVER}/api/vauto/refresh_session",
            data=_data,
            headers={
                "Content-Type": "application/json",
                "X-Auth": EW_REFRESH_SECRET,
                "User-Agent": _BROWSER_UA,
            },
            method="POST",
        )
        with _ureq.urlopen(_req, timeout=8) as _resp:
            if _resp.status == 200:
                print(f"[vauto] session pushed ({len(bff_cookies)} cookies, vAutoAuth)")
            else:
                print(f"[vauto] session push HTTP {_resp.status}")
    except Exception as _e:
        print(f"[vauto] session push skipped: {type(_e).__name__}: {str(_e)[:120]}")


JS_HELPERS = r"""
window.__vauto = (function() {
  function findByLabel(labelText) {
    const app = document.querySelector('profit-time-guided-appraisal');
    if (!app || !app.shadowRoot) return null;
    const hosts = app.shadowRoot.querySelectorAll('vauto-appraisal-formatted-input');
    for (const host of hosts) {
      if (!host.shadowRoot) continue;
      const lab = host.shadowRoot.querySelector('label.ids-form-label');
      if (!lab) continue;
      const txt = (lab.textContent || '').trim().replace(/\*$/, '').trim();
      if (txt === labelText) return host.shadowRoot.querySelector('input#formatted-input-field');
    }
    return null;
  }
  function setValue(input, value) {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    setter.call(input, value);
    input.dispatchEvent(new Event('input',  { bubbles: true, composed: true }));
    input.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
    input.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, composed: true }));
  }
  function clickGo() {
    const app = document.querySelector('profit-time-guided-appraisal');
    if (!app || !app.shadowRoot) return false;
    const btn = app.shadowRoot.querySelector('#vehicle-info-go');
    if (!btn) return false;
    btn.click();
    return true;
  }
  function readSummary() {
    const app = document.querySelector('profit-time-guided-appraisal');
    if (!app || !app.shadowRoot) return null;
    const root = app.shadowRoot;
    const ids = {
      rbook: 'appraisal-summary-row-rbook-button',
      black_book: 'appraisal-summary-row-black-book-button',
      mmr: 'appraisal-summary-row-mmr-button',
      kbb: 'appraisal-summary-row-kbb-button',
      kbb_com: 'appraisal-summary-row-kbb-com-button',
      jd_power: 'appraisal-summary-row-j-d--power-button',
    };
    const out = {};
    for (const [k, id] of Object.entries(ids)) {
      const el = root.querySelector('[aria-labelledby="' + id + '"]');
      out[k] = el ? (el.textContent || '').trim() : null;
    }
    out._year = (findByLabel('Year') || {}).value || null;
    return out;
  }
  function dismissDuplicate() {
    const app = document.querySelector('profit-time-guided-appraisal');
    if (!app || !app.shadowRoot) return 'no_app';
    const modals = app.shadowRoot.querySelectorAll('ids-modal:not([hidden]), [role="dialog"]:not([hidden]), .modal:not([hidden])');
    if (modals.length === 0) return 'none';
    for (const m of modals) {
      const btns = m.querySelectorAll('button, ids-button');
      for (const b of btns) {
        if ((b.textContent || '').trim().toLowerCase() === 'ignore') { b.click(); return 'ignored'; }
      }
    }
    return 'modal_no_ignore';
  }
  function clickCarfaxTrigger() {
    const app = document.querySelector('profit-time-guided-appraisal');
    if (!app || !app.shadowRoot) return false;
    const btn = app.shadowRoot.querySelector('vauto-appraisal-carfax-select-list button.carfax');
    if (!btn) return false; btn.click(); return true;
  }
  function clickCarfaxPopover() {
    const app = document.querySelector('profit-time-guided-appraisal');
    if (!app || !app.shadowRoot) return false;
    const buttons = app.shadowRoot.querySelectorAll('vauto-appraisal-carfax-select-list button');
    for (const b of buttons) {
      if ((b.textContent || '').includes('Click to view CARFAX')) { b.click(); return true; }
    }
    return false;
  }
  function clickAutoCheck() {
    const app = document.querySelector('profit-time-guided-appraisal');
    if (!app || !app.shadowRoot) return false;
    const btn = app.shadowRoot.querySelector('#autocheck-btn');
    if (!btn) return false; btn.click(); return true;
  }
  function titleStatus() {
    const app = document.querySelector('profit-time-guided-appraisal');
    if (!app || !app.shadowRoot) return null;
    const btn = app.shadowRoot.querySelector('vauto-appraisal-carfax-select-list button.carfax');
    if (!btn) return null;
    const c = btn.className || '';
    if (c.includes('cleantitle') || c.includes('oneowner')) return 'clean';
    if (c.includes('accident') || c.includes('warning')) return 'accident';
    if (c.includes('salvage')) return 'salvage';
    if (c.includes('branded')) return 'branded';
    if (c.includes('rebuilt')) return 'rebuilt';
    if (c.includes('recall')) return 'recall';
    return 'unknown';
  }
  function clickActions() {
    const app = document.querySelector('profit-time-guided-appraisal');
    if (!app || !app.shadowRoot) return 'no_app';
    const btns = app.shadowRoot.querySelectorAll('button.ids-btn');
    for (const b of btns) {
      const span = b.querySelector('span');
      const txt = span ? span.textContent.trim() : b.textContent.trim();
      if (txt === 'Actions') { b.click(); return 'clicked'; }
    }
    return 'not_found';
  }
  function clickSave() {
    const app = document.querySelector('profit-time-guided-appraisal');
    if (!app || !app.shadowRoot) return 'no_app';
    const root = app.shadowRoot;
    const byId = root.querySelector('#qa-id-action-list-option-Save');
    if (byId) { byId.click(); return 'saved_by_id'; }
    const items = root.querySelectorAll('.ids-listbox-item, .ids-listbox-action');
    for (const item of items) {
      if ((item.textContent || '').trim() === 'Save') { item.click(); return 'saved_by_class'; }
    }
    return 'save_not_found';
  }
  return { findByLabel, setValue, clickGo, readSummary, dismissDuplicate,
           clickCarfaxTrigger, clickCarfaxPopover, clickAutoCheck,
           titleStatus, clickActions, clickSave };
})();
"""


def auto_login(page, ctx, max_seconds=60):
    t0 = time.time(); last = ""
    while time.time() - t0 < max_seconds:
        for pg in ctx.pages:
            try:
                if any(h in pg.url for h in SUCCESS_HOSTS): return True
            except Exception: pass
        try:
            uf = page.query_selector('input[type="email"], input[name="username"]')
            if uf and uf.is_visible() and last != "user":
                uf.fill(VAUTO_USERNAME)
                btn = page.query_selector('button[type="submit"], button:has-text("Next")')
                (btn.click() if btn else uf.press("Enter"))
                last = "user"; time.sleep(2); continue
        except Exception: pass
        try:
            pw = page.query_selector('input[type="password"]')
            if pw and pw.is_visible() and last != "pass":
                pw.fill(VAUTO_PASSWORD)
                btn = page.query_selector('button[type="submit"], button:has-text("Sign in")')
                (btn.click() if btn else pw.press("Enter"))
                last = "pass"; time.sleep(3); continue
        except Exception: pass
        time.sleep(1)
    return False


def _ensure_appraisal_ready(page, ctx, t):
    """WARM_PAGE_2026_06_17: reuse an already-loaded BLANK vAuto appraisal page so we skip the
    ~15s HOME+APPRAISAL re-navigation every bid. Returns (page, reused). BULLETPROOF: on ANY
    doubt it falls through to the full cold re-establish (today's proven flow), so worst case
    == today, best case (the common case, after bid #1) saves ~15s."""
    import time as _tm
    try:
        for _p in list(ctx.pages):
            try:
                _u = _p.url or ''
            except Exception:
                continue
            if not (any(h in _u for h in SUCCESS_HOSTS) and 'ppraisal' in _u):
                continue
            try:
                _p.add_script_tag(content=JS_HELPERS)
                _state = _p.evaluate("""() => {
                    try {
                        const host = document.querySelector('profit-time-guided-appraisal');
                        if (!host || !host.shadowRoot) return 'no_component';
                        const v = window.__vauto.findByLabel('VIN');
                        if (!v) return 'no_vin_field';
                        return ((v.value || '').trim()) ? 'stale' : 'blank';
                    } catch (e) { return 'err'; }
                }""")
            except Exception:
                _state = 'err'
            if _state == 'blank':
                print(f"[+{_tm.time()-t:5.1f}s] [vauto] REUSED warm appraisal page (skip re-nav)")
                return _p, True
            # component-up-but-stale / no-field / err -> fall through to safe cold re-establish
    except Exception:
        pass
    # COLD re-establish == today's proven flow (the bulletproof fallback)
    try:
        page.goto(VAUTO_HOME, wait_until="domcontentloaded", timeout=30000)
        if not any(h in page.url for h in SUCCESS_HOSTS):
            if not auto_login(page, ctx):
                return None, False
        page = next((pg for pg in ctx.pages if any(h in pg.url for h in SUCCESS_HOSTS)), page)
        page.goto(VAUTO_APPRAISAL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_function("() => document.querySelector('profit-time-guided-appraisal')?.shadowRoot != null", timeout=15000)
        print(f"[+{_tm.time()-t:5.1f}s] [vauto] re-established appraisal page (cold)")
        return page, False
    except Exception as _e:
        print(f"[+{_tm.time()-t:5.1f}s] [vauto] re-establish err: {_e}")
        return None, False


def lookup(page, ctx, vin, miles, t, bid_id=None):
    """Full vAuto pipeline. Returns dict with books + Carfax/AutoCheck PDFs + saved URL."""
    print(f"[+{time.time()-t:5.1f}s] [vauto] start")
    page, _reused = _ensure_appraisal_ready(page, ctx, t)
    if page is None:
        return {"error": "auto_login_failed"}
    print(f"[+{time.time()-t:5.1f}s] [vauto] appraisal page ready (reused={_reused})")
    # VAUTO_API_MODE_2026_06_16: session established; push pool + fire the server BFF
    # API enrich (books+Carfax+AutoCheck+gate) now that the pooled session is FRESH,
    # then skip the slow browser hydration/Carfax/AutoCheck/save (~30-40s).
    try:
        _push_vauto_session(ctx)
        if bid_id:
            try:
                import requests as _rqv2
                _rqv2.post(EW_SERVER + "/api/vauto/carfax_api_enrich",
                           json={"bid_id": bid_id, "vin": vin}, timeout=8)
            except Exception:
                pass
        print(f"[+{time.time()-t:5.1f}s] [vauto] API_MODE: session pushed + enrich fired (fresh); skipping browser pull")
    except Exception as _apie:
        print(f"[+{time.time()-t:5.1f}s] [vauto] API_MODE push err {_apie}")
    _api_appr_url = None
    try:
        _api_appr_url = _apimode_save_appraisal(page, ctx, vin, miles, bid_id, t)
    except Exception as _save_e:
        print("[vauto] api_mode appraisal-save outer err %s" % _save_e)
    return {"api_mode": True, "appraisal_url": _api_appr_url, "books": {}, "raw": {}, "title": None,
            "carfax_screenshot": None, "autocheck_screenshot": None}
    page.add_script_tag(content=JS_HELPERS)
    page.wait_for_function("() => window.__vauto.findByLabel('VIN') != null", timeout=15000)
    page.wait_for_function("() => window.__vauto.findByLabel('Odometer') != null", timeout=15000)
    page.evaluate(f"""() => {{
        window.__vauto.setValue(window.__vauto.findByLabel('VIN'), '{vin}');
        window.__vauto.setValue(window.__vauto.findByLabel('Odometer'), '{miles}');
    }}""")
    if not page.evaluate("() => window.__vauto.clickGo()"):
        return {"error": "go_button_not_clicked"}
    print(f"[+{time.time()-t:5.1f}s] [vauto] form submitted")
    time.sleep(2)
    for _ in range(10):
        r = page.evaluate("() => window.__vauto.dismissDuplicate()")
        if r == "ignored": time.sleep(3); break
        if r == "none": break
        time.sleep(0.5)

    # CARFAX_FIRST_2026_06_16 (operator directive): book-hydration + title MOVED
    # below the Carfax/AutoCheck pull so Carfax is captured + OCR-pushed the
    # INSTANT the appraisal exists (the only trim source the 9B waits for), not
    # ~10s later after all 6 books hydrate. Brief settle lets the appraisal page
    # render the Carfax button first; full hydration runs below before save.
    last = {}
    time.sleep(3)

    # 2026-05-08: Carfax/AutoCheck expect_page timeouts bumped from 15000 to
    # 45000 ms — intermittently >15s on heavy-image bids (Rolls/Bentley).
    # Carfax + AutoCheck — open BOTH new tabs back-to-back, let the browser
    # load them in parallel, then wait+screenshot each. Sync Playwright single
    # thread but the actual page loads run concurrently in the browser, so
    # total wait shrinks from cf_load+ac_load to ~max(cf_load, ac_load).
    carfax = REPORTS_DIR / f"carfax_{vin}.png"
    autocheck = REPORTS_DIR / f"autocheck_{vin}.png"
    cf_tab = ac_tab = None

    # Carfax needs a 2-step click (trigger then popover)
    try:
        page.evaluate("() => window.__vauto.clickCarfaxTrigger()")
        time.sleep(1.5)
        with ctx.expect_page(timeout=45000) as ni:
            page.evaluate("() => window.__vauto.clickCarfaxPopover()")
        cf_tab = ni.value
    except Exception as e:
        print(f"[+{time.time()-t:5.1f}s] [vauto] carfax open FAIL: {e}")
        carfax = None

    # AutoCheck — single click, fire IMMEDIATELY so it loads alongside Carfax
    try:
        with ctx.expect_page(timeout=45000) as ni:
            page.evaluate("() => window.__vauto.clickAutoCheck()")
        ac_tab = ni.value
    except Exception as e:
        print(f"[+{time.time()-t:5.1f}s] [vauto] autocheck open FAIL: {e}")
        autocheck = None

    # Both tabs now loading in parallel in the browser. Wait sequentially in
    # Python — the second wait_for_load_state usually returns instantly because
    # the page has been loading the whole time we were waiting on the first.
    if cf_tab is not None:
        try:
            cf_tab.wait_for_load_state("load", timeout=30000); time.sleep(2)
            cf_tab.screenshot(path=str(carfax), full_page=True)
            cf_tab.close()
            print(f"[+{time.time()-t:5.1f}s] [vauto] carfax PNG saved")
            # CARFAX_EARLY_PUSH_2026_06_16: upload the Carfax PNG + trigger the
            # server OCR NOW (not at end-of-leg) so AccuTrade trim-select gets the
            # Carfax trim fast -- critical when iPacket is absent (iPacket djapi
            # already early-exits the trim wait; Carfax must too). Best-effort;
            # never blocks the vAuto leg.
            if bid_id:
                try:
                    import requests as _rq
                    with open(str(carfax), "rb") as _cf:
                        _up = _rq.post(EW_SERVER + "/api/vauto/upload_report",
                                       files={"file": (os.path.basename(str(carfax)), _cf, "image/png")},
                                       timeout=20)
                    _fn = _up.json().get("filename") if _up.status_code == 200 else None
                    if _fn:
                        _rq.post(EW_SERVER + "/api/vauto/carfax_early",
                                 json={"bid_id": bid_id, "vin": vin,
                                       "carfax_screenshot": "/vauto_reports/" + _fn},
                                 timeout=25)
                        print(f"[+{time.time()-t:5.1f}s] [vauto] carfax pushed early (bid {bid_id})")
                except Exception as _ce:
                    print(f"[+{time.time()-t:5.1f}s] [vauto] carfax early-push err {_ce}")
        except Exception as e:
            print(f"[+{time.time()-t:5.1f}s] [vauto] carfax screenshot FAIL: {e}")
            carfax = None

    if ac_tab is not None:
        try:
            ac_tab.wait_for_load_state("load", timeout=30000); time.sleep(2)
            ac_tab.screenshot(path=str(autocheck), full_page=True)
            ac_tab.close()
            print(f"[+{time.time()-t:5.1f}s] [vauto] autocheck PNG saved")
        except Exception as e:
            print(f"[+{time.time()-t:5.1f}s] [vauto] autocheck screenshot FAIL: {e}")
            autocheck = None

    # CARFAX_FIRST_2026_06_16: hydrate the 6 books NOW (Carfax already captured +
    # pushed above), before saving the appraisal.
    t0 = time.time()
    keys = ("rbook","black_book","mmr","kbb","kbb_com","jd_power")
    _prev_n = -1; _settled_at = None
    while time.time() - t0 < HYDRATE_TIMEOUT:
        s = page.evaluate("() => window.__vauto.readSummary()") or {}
        last = s
        _n = sum(1 for k in keys if s.get(k))
        if _n == 6: break
        if _n > 0:
            if _n != _prev_n:
                _prev_n = _n; _settled_at = time.time()
            elif time.time() - _settled_at >= 3.0:
                break
        time.sleep(0.4)
    print(f"[+{time.time()-t:5.1f}s] [vauto] hydration done in {time.time()-t0:.1f}s")

    title = page.evaluate("() => window.__vauto.titleStatus()")

    # Save appraisal
    saved_ok = False
    try:
        if page.evaluate("() => window.__vauto.clickActions()") == "clicked":
            time.sleep(1.5)
            r = page.evaluate("() => window.__vauto.clickSave()")
            saved_ok = "saved" in (r or "")
            # Give vAuto's backend time to commit the save before we go look it up
            time.sleep(4)  # was 8s — empirically 4s is enough for the index to update
    except Exception: pass
    print(f"[+{time.time()-t:5.1f}s] [vauto] save: {saved_ok} (waited 4s for commit)")

    # Capture saved permalink from list page (Beelink's exact pattern)
    appraisal_url = None
    decoded_year = last.get("_year")
    # Build a "{year} {make}" prefix for disambiguation if multiple rows match VIN
    label_prefix = ""
    if decoded_year:
        # Make isn't directly in summary; just use year as a weak prefix.
        label_prefix = str(decoded_year).strip()
    try:
        page.goto(APPRAISAL_LIST, wait_until="domcontentloaded", timeout=20000); time.sleep(3)
        # Find the Quick Search input — vAuto's ExtJS doesn't use a real placeholder
        # attribute, so we walk up from the "Go" button to find the sibling input.
        qs_frame = None
        qs_handle = None
        find_input_js = r"""
            (() => {
                // 1) Try placeholder route (rare on ExtJS but try first)
                let q = document.querySelector(
                    'input[placeholder*="Quick" i], input[type=search], input[name*="quickSearch" i]');
                if (q && q.offsetParent !== null) return q;
                // 2) Walk up from "Go" button — the stable text near the input
                const btns = [...document.querySelectorAll('button, a, input[type=submit], input[type=button]')];
                const go = btns.find(b => ((b.textContent || b.value || '').trim().toLowerCase()) === 'go'
                                          && b.offsetParent !== null);
                if (!go) return null;
                let p = go;
                for (let h = 0; h < 8 && p; h++) {
                    const inp = p.querySelector('input[type=text], input:not([type])');
                    if (inp && inp.offsetParent !== null) return inp;
                    p = p.parentElement;
                }
                // 3) First visible text input on the page
                const all = [...document.querySelectorAll('input')];
                return all.find(i => i.offsetParent !== null
                                      && (i.type === 'text' || i.type === 'search' || !i.type)) || null;
            })()
        """
        deadline = time.time() + 30
        while time.time() < deadline and qs_handle is None:
            for f in page.frames:
                try:
                    handle = f.evaluate_handle(find_input_js)
                    if handle and handle.evaluate("el => !!el && el.offsetParent !== null"):
                        qs_handle = handle
                        qs_frame = f
                        break
                except Exception:
                    continue
            if qs_handle is None:
                time.sleep(1)

        if qs_handle is None:
            print(f"[+{time.time()-t:5.1f}s] [vauto] permalink: Quick Search not found in any frame; frames={len(page.frames)}, url={page.url[:90]}")
            try: page.screenshot(path=r"C:\worker\vauto_list_no_input.png", full_page=True)
            except Exception: pass
        else:
            print(f"[+{time.time()-t:5.1f}s] [vauto] permalink: found Quick Search in frame {qs_frame.url[:80]}")
            # Drive the input via the handle: focus, clear, fill, press Enter via JS
            qs_handle.evaluate("""el => {
                el.focus();
                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                setter.call(el, '');
                el.dispatchEvent(new Event('input', {bubbles: true}));
            }""")
            qs_handle.evaluate("""(el, vin) => {
                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                setter.call(el, vin);
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.dispatchEvent(new KeyboardEvent('keydown', {bubbles: true, key: 'Enter', keyCode: 13, which: 13}));
                el.dispatchEvent(new KeyboardEvent('keypress', {bubbles: true, key: 'Enter', keyCode: 13, which: 13}));
                el.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true, key: 'Enter', keyCode: 13, which: 13}));
            }""", vin)
            time.sleep(0.4)
            # Also click the "Go" button next to it for reliability
            qs_frame.evaluate(r"""() => {
                const btns = [...document.querySelectorAll('button, a, input[type=submit], input[type=button]')];
                const go = btns.find(b => ((b.textContent || b.value || '').trim().toLowerCase()) === 'go'
                                          && b.offsetParent !== null);
                if (go) go.click();
            }""")
            # Retry the row lookup up to 3 times — vAuto can be slow to index a freshly-saved appraisal
            for attempt in range(1, 4):
                time.sleep(3)  # was 8/5/5 — first attempt usually finds it; tighter loop saves ~10s on hits
                # Search inside the same frame that has Quick Search
                result = qs_frame.evaluate(r"""(expected) => {
                const want = (expected || '').toLowerCase();
                const titles = [...document.querySelectorAll('a.AppraisalVehicleTitle')]
                                .filter(a => a.offsetParent !== null);
                if (titles.length === 0) return {err: 'no_titles'};
                let target = null;
                if (want) {
                    target = titles.find(a =>
                        (a.textContent || '').trim().toLowerCase().startsWith(want));
                }
                if (!target) target = titles[0];
                const href = target.href || '';
                if (href.indexOf('Appraisal/Default.aspx?Id=') !== -1) {
                    return {action: 'href', href: href, total: titles.length};
                }
                target.click();
                return {action: 'clicked', total: titles.length,
                        text: (target.textContent || '').trim().slice(0, 60)};
            }""", label_prefix) or {}
                if result.get('action') == 'href' and result.get('href'):
                    appraisal_url = result['href']
                    print(f"[+{time.time()-t:5.1f}s] [vauto] permalink (href, attempt {attempt}): {appraisal_url[:80]}")
                    break
                elif result.get('action') == 'clicked':
                    deadline = time.time() + 8
                    while time.time() < deadline:
                        # The clicked link may have navigated either page or frame
                        for url_src in (page.url, qs_frame.url):
                            if 'Appraisal/Default.aspx?Id=' in url_src:
                                appraisal_url = url_src
                                break
                        if appraisal_url: break
                        time.sleep(0.5)
                    if appraisal_url:
                        print(f"[+{time.time()-t:5.1f}s] [vauto] permalink (clicked, attempt {attempt}): {appraisal_url[:80]}")
                        break
                    print(f"[+{time.time()-t:5.1f}s] [vauto] permalink attempt {attempt}: clicked but no nav, retrying")
                else:
                    print(f"[+{time.time()-t:5.1f}s] [vauto] permalink attempt {attempt}: {result.get('err','no-titles')}, retrying")
    except Exception as e:
        print(f"[+{time.time()-t:5.1f}s] [vauto] permalink FAIL: {e}")

    # Push fresh Cox cookies to C1 pool (best-effort, never blocks bid)
    _push_vauto_session(ctx)

    return {
        "rbook":      _parse_dollars(last.get("rbook")),
        "black_book": _parse_dollars(last.get("black_book")),
        "mmr":        _parse_dollars(last.get("mmr")),
        "kbb":        _parse_dollars(last.get("kbb")),
        "kbb_com":    _parse_dollars(last.get("kbb_com")),
        "jd_power":   _parse_dollars(last.get("jd_power")),
        "decoded_year": last.get("_year"),
        "title_status": title,
        "carfax_screenshot": str(carfax) if carfax else None,
        "autocheck_screenshot": str(autocheck) if autocheck else None,
        "appraisal_url": appraisal_url,
        "raw": last,  # keep raw text for debug
    }


def _apimode_save_appraisal(page, ctx, vin, miles, bid_id, t):
    """API_MODE saved-appraisal (2026-06-16). The appraisal page (?new=true) is
    loaded, the pooled session pushed, and the fast server BFF enrich fired. Now
    enter VIN+miles, SAVE the appraisal, capture the saved permalink, and POST it
    to /api/vauto/url_capture_result (server then sets vauto_lookups.appraisal_url
    + kicks the direct rBook/Manheim BFF enrichment -> Saved-vAuto link + rBook/MMR
    cards). Runs in parallel with AccuTrade/iPacket; best-effort + bounded; never
    raises. Returns the captured URL or None."""
    import time as _t, requests as _rq
    appraisal_url = None
    _summary = {}
    _rbook_val = None
    _rbook_debug = ''
    _rbook_shot = ''
    _summary_shot = ''
    try:
        page.add_script_tag(content=JS_HELPERS)
        page.wait_for_function("() => window.__vauto.findByLabel('VIN') != null", timeout=15000)
        page.wait_for_function("() => window.__vauto.findByLabel('Odometer') != null", timeout=15000)
        page.evaluate("""(d) => {
            window.__vauto.setValue(window.__vauto.findByLabel('VIN'), d.vin);
            window.__vauto.setValue(window.__vauto.findByLabel('Odometer'), d.miles);
        }""", {"vin": vin, "miles": str(miles)})
        if not page.evaluate("() => window.__vauto.clickGo()"):
            print("[+%5.1fs] [vauto] api_mode save: go not clicked" % (_t.time() - t))
            return None
        _t.sleep(2)
        for _ in range(10):
            r = page.evaluate("() => window.__vauto.dismissDuplicate()")
            if r == "ignored":
                _t.sleep(3); break
            if r == "none":
                break
            _t.sleep(1)
        # (Summary-9B pre-save capture removed 2026-06-17: the vAuto Summary panel never
        # hydrates in fast api_mode, so it wasted ~25s/bid and always failed. The 5
        # non-rBook books come from the priceGuides API; rBook stays comp-median.)
        # POST_SAVE_NAV_2026_06_17: clicking Save makes vAuto AUTO-NAVIGATE to the saved
        # appraisal detail page (Default.aspx?Id=<persisted>). That URL *is* the permalink --
        # just wait for the nav + read page.url. (The PUT-listener never matched, and the old
        # Quick-Search goto to List.aspx RACED this very navigation -> "interrupted by another
        # navigation to Default.aspx" -> bids 3449/3450 lost the link. This is simpler + the
        # page lands on the Books detail page, which the rBook scrape below also needs.)
        try:
            if page.evaluate("() => window.__vauto.clickActions()") == "clicked":
                _t.sleep(1.5)
                page.evaluate("() => window.__vauto.clickSave()")
        except Exception:
            pass
        for _ in range(8):  # was 22; nav-capture misses + Quick Search gets the link -- just let nav settle
            _t.sleep(1)
            _hit = ''
            try:
                for _fr in page.frames:          # the appraisal loads in an IFRAME, not main
                    try:
                        _fu = _fr.url or ''
                    except Exception:
                        _fu = ''
                    if 'Appraisal/Default.aspx?Id=' in _fu:
                        _hit = _fu
                        break
                    # WARM-SAFE permalink: the "Appraisal Saved" banner has a "View Appraisal"
                    # link whose href IS the saved permalink. Read it (no click/nav) instead of
                    # the Quick-Search, which navigates to the LIST and leaves the page there --
                    # defeating warm-page reuse. Reading it keeps the page on the blank form = WARM.
                    try:
                        _va = _fr.evaluate(r"""() => {
                            const a = [...document.querySelectorAll('a')].find(x =>
                                /view\s*appraisal/i.test((x.textContent || '').trim())
                                && /Default\.aspx\?Id=/i.test(x.href || ''));
                            return a ? a.href : '';
                        }""")
                        if _va and 'Default.aspx?Id=' in _va:
                            _hit = _va
                            break
                    except Exception:
                        pass
            except Exception:
                pass
            if _hit:
                appraisal_url = _hit
                break
        if appraisal_url:
            print("[+%5.1fs] [vauto] api_mode permalink via post-save nav: %s" % (_t.time() - t, appraisal_url[:72]))
        else:
            print("[+%5.1fs] [vauto] api_mode no post-save nav URL -> Quick Search fallback" % (_t.time() - t))
        print("[+%5.1fs] [vauto] api_mode appraisal saved; capturing permalink" % (_t.time() - t))
        # (post-Save rBook DOM-scrape + full-page screenshot removed 2026-06-17: post-Save
        # the form resets to blank so it read nothing + cost ~8s + a 200KB shot.)
        # FALLBACK: Quick-Search-by-VIN on the list page (only if the PUT capture missed)
        if not appraisal_url:
            page.goto(APPRAISAL_LIST, wait_until="domcontentloaded", timeout=20000)
            _t.sleep(3)
        find_input_js = r"""
            (() => {
                let q = document.querySelector(
                    'input[placeholder*="Quick" i], input[type=search], input[name*="quickSearch" i]');
                if (q && q.offsetParent !== null) return q;
                const btns = [...document.querySelectorAll('button, a, input[type=submit], input[type=button]')];
                const go = btns.find(b => ((b.textContent || b.value || '').trim().toLowerCase()) === 'go'
                                          && b.offsetParent !== null);
                if (!go) return null;
                let p = go;
                for (let h = 0; h < 8 && p; h++) {
                    const inp = p.querySelector('input[type=text], input:not([type])');
                    if (inp && inp.offsetParent !== null) return inp;
                    p = p.parentElement;
                }
                const all = [...document.querySelectorAll('input')];
                return all.find(i => i.offsetParent !== null
                                      && (i.type === 'text' || i.type === 'search' || !i.type)) || null;
            })()
        """
        qs_frame = None
        qs_handle = None
        deadline = _t.time() + 30
        while _t.time() < deadline and qs_handle is None and not appraisal_url:
            for f in page.frames:
                try:
                    handle = f.evaluate_handle(find_input_js)
                    if handle and handle.evaluate("el => !!el && el.offsetParent !== null"):
                        qs_handle = handle
                        qs_frame = f
                        break
                except Exception:
                    continue
            if qs_handle is None:
                _t.sleep(1)
        if qs_handle is None:
            print("[+%5.1fs] [vauto] api_mode permalink: Quick Search not found" % (_t.time() - t))
        else:
            qs_handle.evaluate("""el => {
                el.focus();
                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                setter.call(el, '');
                el.dispatchEvent(new Event('input', {bubbles: true}));
            }""")
            qs_handle.evaluate("""(el, vin) => {
                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                setter.call(el, vin);
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.dispatchEvent(new KeyboardEvent('keydown', {bubbles: true, key: 'Enter', keyCode: 13, which: 13}));
                el.dispatchEvent(new KeyboardEvent('keypress', {bubbles: true, key: 'Enter', keyCode: 13, which: 13}));
                el.dispatchEvent(new KeyboardEvent('keyup', {bubbles: true, key: 'Enter', keyCode: 13, which: 13}));
            }""", vin)
            _t.sleep(0.4)
            qs_frame.evaluate(r"""() => {
                const btns = [...document.querySelectorAll('button, a, input[type=submit], input[type=button]')];
                const go = btns.find(b => ((b.textContent || b.value || '').trim().toLowerCase()) === 'go'
                                          && b.offsetParent !== null);
                if (go) go.click();
            }""")
            for attempt in range(1, 4):
                _t.sleep(3)
                result = qs_frame.evaluate(r"""(expected) => {
                const want = (expected || '').toLowerCase();
                const titles = [...document.querySelectorAll('a.AppraisalVehicleTitle')]
                                .filter(a => a.offsetParent !== null);
                if (titles.length === 0) return {err: 'no_titles'};
                let target = null;
                if (want) {
                    target = titles.find(a =>
                        (a.textContent || '').trim().toLowerCase().startsWith(want));
                }
                if (!target) target = titles[0];
                const href = target.href || '';
                if (href.indexOf('Appraisal/Default.aspx?Id=') !== -1) {
                    return {action: 'href', href: href, total: titles.length};
                }
                target.click();
                return {action: 'clicked', total: titles.length};
            }""", "") or {}
                if result.get('action') == 'href' and result.get('href'):
                    appraisal_url = result['href']
                    break
                elif result.get('action') == 'clicked':
                    d2 = _t.time() + 8
                    while _t.time() < d2:
                        for url_src in (page.url, qs_frame.url):
                            if 'Appraisal/Default.aspx?Id=' in url_src:
                                appraisal_url = url_src
                                break
                        if appraisal_url:
                            break
                        _t.sleep(0.5)
                    if appraisal_url:
                        break
    except Exception as e:
        print("[+%5.1fs] [vauto] api_mode appraisal-save FAIL: %s" % (_t.time() - t, e))
    if (appraisal_url or _rbook_val or _rbook_shot or _summary_shot) and bid_id:
        try:
            _rq.post(EW_SERVER + "/api/vauto/url_capture_result",
                     json={"bid_id": bid_id, "vin": vin, "appraisal_url": appraisal_url, "books": _summary,
                           "rbook_exact": _rbook_val, "rbook_debug": _rbook_debug, "rbook_shot": _rbook_shot,
                           "summary_shot": _summary_shot}, timeout=30)
            print("[+%5.1fs] [vauto] api_mode url/rbook POSTED: url=%s rbook_dom=%s summary=%s" % (_t.time() - t, (appraisal_url or '-')[:55], _rbook_val, bool(_summary_shot)))
        except Exception as _pe:
            print("[+%5.1fs] [vauto] api_mode url post err: %s" % (_t.time() - t, _pe))
    else:
        print("[+%5.1fs] [vauto] api_mode: no appraisal_url captured" % (_t.time() - t))
    # (re-warm-at-end removed 2026-06-17: it added ~15-45s to every vAuto leg with no benefit on
    # a sporadic workload -- page idle-expires before the next bid -- and could tip a slow bid
    # over the 180s worker watchdog. vAuto leg back to the lean ~38s cleaned path.)
    return appraisal_url
