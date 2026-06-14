# thin_save.py — the hybrid's saved-appraisal step. Drives ProVision headless on C1
# with the vauto_session cookies, using the SAME proven window.__vauto helper the
# browser workers use daily (worker_code/worker_vauto.py): set VIN+Odometer (composed
# shadow-DOM events) -> Go -> dismiss duplicate -> wait for the 6 books to hydrate ->
# Actions -> Save, and capture the persisted appraisalId from the PUT /api/appraisal
# the page fires. Returns the real "Saved vAuto" URL for the user. The browser's own
# JS assembles the full valuation grid, so the save is always valid.
#
#   venv/bin/python thin_save.py <VIN> <MILES>      # prints JSON {ok, appraisal_url, ...}
import sys, json, time
sys.path.insert(0, '/opt/expwholesale')
import psycopg2.extras
import shadow_worker as sw
from playwright.sync_api import sync_playwright

CHROME = "/root/.cache/ms-playwright/chromium_headless_shell-1217/chrome-headless-shell-linux64/chrome-headless-shell"
PROVISION = "https://provision.vauto.app.coxautoinc.com"
NEW_APPRAISAL = PROVISION + "/Va/Appraisal/Default.aspx?new=true"
HYDRATE_TIMEOUT = 60

# Proven shadow-DOM helper, lifted verbatim from worker_code/worker_vauto.py.
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
    out._odometer = (findByLabel('Odometer') || {}).value || null;
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
           clickActions, clickSave };
})();
"""


def _cookies(sess):
    return [{"name": k, "value": str(v), "domain": ".vauto.app.coxautoinc.com", "path": "/"}
            for k, v in sess['cookies'].items()]


def thin_save(vin, miles, sess, want_shot=None, verbose=False):
    out = {"ok": False, "appraisal_url": None, "appraisal_id": None, "puts": [],
           "summary": None, "odometer_readback": None}

    def log(*a):
        if verbose:
            print(*a, flush=True)

    with sync_playwright() as p:
        br = p.chromium.launch(headless=True, executable_path=CHROME, args=['--no-sandbox'])
        try:
            ctx = br.new_context(viewport={'width': 1500, 'height': 1100})
            ctx.add_cookies(_cookies(sess))
            pg = ctx.new_page()

            def on_resp(resp):
                try:
                    rq = resp.request
                    if rq.method in ("PUT", "POST") and rq.url.rstrip('/').endswith('/api/appraisal'):
                        apid = None
                        try:
                            apid = json.loads(rq.post_data or '{}').get('appraisalId')
                        except Exception:
                            pass
                        out["puts"].append({"m": rq.method, "status": resp.status, "appraisalId": apid})
                except Exception:
                    pass
            pg.on("response", on_resp)

            pg.goto(NEW_APPRAISAL, wait_until="domcontentloaded", timeout=60000)
            pg.wait_for_function(
                "() => document.querySelector('profit-time-guided-appraisal')?.shadowRoot != null",
                timeout=55000)  # ProVision app boot is occasionally slow; was 30s (caused transient TimeoutError)
            pg.add_script_tag(content=JS_HELPERS)
            pg.wait_for_function("() => window.__vauto.findByLabel('VIN') != null", timeout=40000)
            pg.wait_for_function("() => window.__vauto.findByLabel('Odometer') != null", timeout=40000)

            # Set VIN + Odometer BEFORE Go so the books value at the right miles.
            pg.evaluate("""() => {
                window.__vauto.setValue(window.__vauto.findByLabel('VIN'), '%s');
                window.__vauto.setValue(window.__vauto.findByLabel('Odometer'), '%s');
            }""" % (vin, miles))
            out["odometer_readback"] = pg.evaluate("() => (window.__vauto.findByLabel('Odometer')||{}).value")
            log("VIN+Odometer set (odo=%r)" % out["odometer_readback"])

            if not pg.evaluate("() => window.__vauto.clickGo()"):
                log("Go button not found"); return out
            log("clicked Go")
            pg.wait_for_timeout(2000)

            for _ in range(10):
                r = pg.evaluate("() => window.__vauto.dismissDuplicate()")
                if r == "ignored":
                    log("dismissed duplicate"); pg.wait_for_timeout(3000); break
                if r == "none":
                    break
                pg.wait_for_timeout(500)

            # Wait for the 6 books to hydrate (confirms valuation at the set miles).
            t0 = time.time(); keys = ("rbook", "black_book", "mmr", "kbb", "kbb_com", "jd_power")
            while time.time() - t0 < HYDRATE_TIMEOUT:
                s = pg.evaluate("() => window.__vauto.readSummary()") or {}
                out["summary"] = s
                if sum(1 for k in keys if s.get(k)) == 6:
                    break
                pg.wait_for_timeout(400)
            log("hydrated:", json.dumps(out["summary"]))

            # Actions -> Save
            if pg.evaluate("() => window.__vauto.clickActions()") == "clicked":
                pg.wait_for_timeout(1000)
                log("save:", pg.evaluate("() => window.__vauto.clickSave()"))

            apid = None
            for _ in range(40):
                ok = [x for x in out["puts"] if x["m"] == "PUT" and x["status"] in (200, 201, 204) and x["appraisalId"]]
                if ok:
                    apid = ok[-1]["appraisalId"]; break
                pg.wait_for_timeout(500)

            if want_shot:
                try:
                    pg.screenshot(path=want_shot, full_page=False)
                except Exception:
                    pass

            if apid:
                out["ok"] = True
                out["appraisal_id"] = apid
                out["appraisal_url"] = "%s/Va/Appraisal/Default.aspx?Id=%s&AppraisalStatus=Saved" % (PROVISION, apid)
            return out
        finally:
            br.close()


def main():
    vin, miles = sys.argv[1], int(sys.argv[2])
    conn = sw.db(); cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT label FROM vauto_session ORDER BY refreshed_at DESC LIMIT 1")
    sess = sw.load_session(conn, cur.fetchone()['label'])
    t0 = time.time()
    res = thin_save(vin, miles, sess, want_shot="/tmp/thin_save_result.png", verbose=True)
    res["secs"] = round(time.time() - t0, 1)
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
