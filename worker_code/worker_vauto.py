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


def lookup(page, ctx, vin, miles, t):
    """Full vAuto pipeline. Returns dict with books + Carfax/AutoCheck PDFs + saved URL."""
    print(f"[+{time.time()-t:5.1f}s] [vauto] start")
    page.goto(VAUTO_HOME, wait_until="domcontentloaded", timeout=30000)
    if not any(h in page.url for h in SUCCESS_HOSTS):
        if not auto_login(page, ctx):
            return {"error": "auto_login_failed"}
    print(f"[+{time.time()-t:5.1f}s] [vauto] logged in")
    page = next((pg for pg in ctx.pages if any(h in pg.url for h in SUCCESS_HOSTS)), page)

    page.goto(VAUTO_APPRAISAL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_function("() => document.querySelector('profit-time-guided-appraisal')?.shadowRoot != null", timeout=15000)
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

    t0 = time.time(); last = {}
    keys = ("rbook","black_book","mmr","kbb","kbb_com","jd_power")
    while time.time() - t0 < HYDRATE_TIMEOUT:
        s = page.evaluate("() => window.__vauto.readSummary()") or {}
        last = s
        if sum(1 for k in keys if s.get(k)) == 6: break
        time.sleep(1)
    print(f"[+{time.time()-t:5.1f}s] [vauto] hydration done in {time.time()-t0:.1f}s")

    title = page.evaluate("() => window.__vauto.titleStatus()")

    # Carfax PNG (dashboard renders as <img>, can't use PDF here)
    carfax = REPORTS_DIR / f"carfax_{vin}.png"
    try:
        page.evaluate("() => window.__vauto.clickCarfaxTrigger()")
        time.sleep(1.5)
        with ctx.expect_page(timeout=15000) as ni:
            page.evaluate("() => window.__vauto.clickCarfaxPopover()")
        cf = ni.value
        cf.wait_for_load_state("load", timeout=30000); time.sleep(2)
        cf.screenshot(path=str(carfax), full_page=True)
        cf.close()
        print(f"[+{time.time()-t:5.1f}s] [vauto] carfax PNG saved")
    except Exception as e:
        print(f"[+{time.time()-t:5.1f}s] [vauto] carfax FAIL: {e}")
        carfax = None

    autocheck = REPORTS_DIR / f"autocheck_{vin}.png"
    try:
        with ctx.expect_page(timeout=15000) as ni:
            page.evaluate("() => window.__vauto.clickAutoCheck()")
        ac = ni.value
        ac.wait_for_load_state("load", timeout=30000); time.sleep(2)
        ac.screenshot(path=str(autocheck), full_page=True)
        ac.close()
        print(f"[+{time.time()-t:5.1f}s] [vauto] autocheck PNG saved")
    except Exception as e:
        print(f"[+{time.time()-t:5.1f}s] [vauto] autocheck FAIL: {e}")
        autocheck = None

    # Save appraisal
    saved_ok = False
    try:
        if page.evaluate("() => window.__vauto.clickActions()") == "clicked":
            time.sleep(1.5)
            r = page.evaluate("() => window.__vauto.clickSave()")
            saved_ok = "saved" in (r or "")
            # Give vAuto's backend time to commit the save before we go look it up
            time.sleep(8)
    except Exception: pass
    print(f"[+{time.time()-t:5.1f}s] [vauto] save: {saved_ok} (waited 8s for commit)")

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
                time.sleep(8 if attempt == 1 else 5)
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
