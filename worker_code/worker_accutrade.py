"""AccuTrade lookup module."""
import os, time
from pathlib import Path

try:
    import requests as http_requests
except Exception:
    http_requests = None

REPORTS_DIR = Path(r"C:\worker\accutrade_reports")
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

EMAIL = os.environ.get("ACCUTRADE_EMAIL", "opies32765@gmail.com")
PASSWORD = os.environ.get("ACCUTRADE_PASSWORD", "Sedecremlun35$")
EW_SERVER = os.environ.get("EW_SERVER", "https://experience-wholesale.net")
ACCUTRADE_URL = "https://appraiser3.accu-trade.com"
LOGIN_MARKERS = ("auth0.accu-trade.com", "/u/login", "/auth/login")
SUCCESS_PATHS = ("/dashboard", "/appraisal", "/vehicle", "/home", "/index", "/performance-center")


def _ask_overseer(vin, bid_id, choices, timeout=65):
    """Ask EW's AI overseer which trim choice to click. Returns dict or None.

    timeout bumped 15 -> 65 (2026-05-18) so the worker doesn't drop the
    HTTP call before C1's evidence-first overseer (allowlisted canary
    bids) finishes waiting for vauto + iPacket to complete. Server-side
    cap is ~55s; this gives the HTTP round-trip 10s of headroom.

    Non-canary bids get an instant response (LLM cache hit or LLM call
    in <1s) — the bump has zero behavior change on the common path."""
    if not http_requests or not choices:
        return None
    try:
        r = http_requests.post(
            f"{EW_SERVER}/api/accutrade/trim_select",
            json={"vin": vin, "bid_id": bid_id, "choices": choices},
            timeout=timeout,
        )
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"[accutrade] overseer call failed: {e}")
    return None


def is_logged_in(url):
    if "appraiser3.accu-trade.com" not in url: return False
    if any(m in url for m in LOGIN_MARKERS): return False
    return any(p in url for p in SUCCESS_PATHS)


def auto_login(page, ctx, max_seconds=60):
    t0 = time.time(); last = ""
    while time.time() - t0 < max_seconds:
        for pg in ctx.pages:
            try:
                if is_logged_in(pg.url): return True
            except Exception: pass
        try:
            uf = page.query_selector('input[type="email"], input[name="email"], input[name="username"], input[id="username"]')
            if uf and uf.is_visible() and last != "user":
                uf.fill(EMAIL)
                btn = page.query_selector('button[type="submit"], button:has-text("Continue"), button:has-text("Next")')
                (btn.click() if btn else uf.press("Enter"))
                last = "user"; time.sleep(2); continue
        except Exception: pass
        try:
            pw = page.query_selector('input[type="password"]')
            if pw and pw.is_visible() and last != "pass":
                pw.fill(PASSWORD)
                btn = page.query_selector('button[type="submit"], button:has-text("Sign in"), button:has-text("Log in")')
                (btn.click() if btn else pw.press("Enter"))
                last = "pass"; time.sleep(3); continue
        except Exception: pass
        time.sleep(1)
    return False


def lookup(page, ctx, vin, miles, t, trim=None, bid_id=None):
    print(f"[+{time.time()-t:5.1f}s] [accutrade] start")
    page.goto(ACCUTRADE_URL, wait_until="domcontentloaded", timeout=30000)
    if not is_logged_in(page.url):
        if not auto_login(page, ctx):
            return {"error": "auto_login_failed"}
    print(f"[+{time.time()-t:5.1f}s] [accutrade] logged in")
    page = next((pg for pg in ctx.pages if is_logged_in(pg.url)), page)

    # Skip the redundant re-goto when we're already on the dashboard.
    # auto_login leaves us logged in but on /home or wherever; the second
    # goto was costing ~3s + 2s sleep for no reason.
    if not is_logged_in(page.url):
        page.goto(ACCUTRADE_URL, wait_until="domcontentloaded", timeout=20000)
        time.sleep(1)
    page.evaluate(r"""() => {
        const btns = document.querySelectorAll('button, a, [role="button"]');
        for (const b of btns) {
            const txt = (b.textContent || '').trim();
            const aria = (b.getAttribute('aria-label') || '').toLowerCase();
            const cls = (b.className || '').toLowerCase();
            if (txt === '+' || txt === '＋' ||
                aria.includes('add') || aria.includes('create') || aria.includes('new') ||
                cls.includes('add-button') || cls.includes('fab') || cls.includes('create')) {
                b.click(); return 'clicked';
            }
        }
        return 'not_found';
    }""")
    time.sleep(0.5)  # was 1.5s
    page.evaluate(r"""() => {
        const items = document.querySelectorAll('a, button, li, [role="menuitem"], [role="option"], span, div');
        for (const item of items) {
            const txt = (item.textContent || '').trim().toLowerCase();
            if (txt === 'dealer acquisition' || txt.startsWith('dealer acquisition')) {
                item.click(); return 'clicked';
            }
        }
        return 'not_found';
    }""")
    time.sleep(1)  # was 2s

    deadline = time.time() + 20; has_vin = False
    while time.time() < deadline:
        has_vin = page.evaluate("""() => {
            const ins = document.querySelectorAll('input');
            for (const i of ins) {
                const ph = (i.placeholder || '').toLowerCase();
                if (ph.includes('vin') || (i.getAttribute('aria-label')||'').toLowerCase().includes('vin')) return true;
            }
            return false;
        }""")
        if has_vin: break
        time.sleep(0.3)  # was 1s — vin input usually appears within a couple ticks
    if not has_vin: return {"error": "vin_input_not_found"}

    page.evaluate(r"""(vin) => {
        const inputs = document.querySelectorAll('input');
        for (const i of inputs) {
            const ph = (i.placeholder || '').toLowerCase();
            if (ph.includes('vin') || (i.getAttribute('aria-label')||'').toLowerCase().includes('vin')) {
                i.focus();
                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                setter.call(i, vin);
                i.dispatchEvent(new Event('input', {bubbles: true}));
                i.dispatchEvent(new Event('change', {bubbles: true}));
                return 'filled';
            }
        }
        return 'not_found';
    }""", vin)
    time.sleep(0.5)  # was 1s
    page.evaluate(r"""() => {
        const all = document.querySelectorAll('*');
        for (const el of all) {
            let direct = '';
            for (const cn of el.childNodes) if (cn.nodeType === 3) direct += cn.textContent.trim();
            if (direct.toLowerCase() === 'search') { el.click(); return 'clicked'; }
        }
    }""")
    time.sleep(1)  # was 3s

    # 2026-05-11: "fast" + "clicked_existing" paths removed. Both bypassed the
    # trim modal where the AI overseer runs — meaning AccuTrade silently
    # served back the WRONG trim from a prior appraisal (King Ranch when the
    # real truck is XL, Turbo S when it's Carrera S, etc.). We now ALWAYS try
    # to reach the trim modal so the overseer (or fuzzy hint) picks.
    selected_trim_text = None
    trim_select_source = None
    if True:
        time.sleep(1.5)

        # Helper that scrapes visible trim choices from the "Start a New
        # Appraisal" modal. Returns [] when no modal is showing.
        # 2026-05-11: clone each choice and strip mat-icon / svg / known
        # Material-Icons glyph names before grabbing textContent — otherwise
        # "GT3 COUPE 4.0L 6 CYL" came through as
        # "GT3 COUPE 4.0L 6 CYLkeyboard_arrow_right" (chevron font ligature).
        _scrape_choices_js = r"""() => {
            let nodes = document.querySelectorAll('new-appraisal-trim-choice');
            if (!nodes.length) nodes = document.querySelectorAll('.new-appraisal-trim-choice');
            const out = [];
            nodes.forEach((c, i) => {
                if (!c.offsetParent) return;
                const clone = c.cloneNode(true);
                clone.querySelectorAll('mat-icon, .mat-icon, .material-icons, svg, i.material-icons-outlined').forEach(e => e.remove());
                let txt = (clone.textContent || '').trim().replace(/\s+/g, ' ');
                // Belt-and-suspenders: strip trailing Material-Icons glyph names that
                // some Angular builds render as plain text via ::before pseudo-elements.
                txt = txt.replace(/\s*(?:keyboard_arrow_right|keyboard_arrow_left|chevron_right|chevron_left|arrow_forward|arrow_back|arrow_drop_down|expand_more|more_vert)\s*$/i, '').trim();
                if (txt) out.push({index: out.length, dom_index: i, text: txt});
            });
            return out;
        }"""

        choices = page.evaluate(_scrape_choices_js) or []

        # If we didn't land on the trim modal (AccuTrade auto-redirected to an
        # existing appraisal), force back into "Start a New Appraisal" so the
        # overseer can pick. Click any "Start a New Appraisal" / "Add" / "+"
        # button, re-enter VIN, search, then re-scrape. This handles the case
        # of re-pulls for VINs that already have appraisals on AccuTrade.
        if not choices:
            print(f"[+{time.time()-t:5.1f}s] [accutrade] no modal — forcing fresh appraisal flow")
            try:
                page.evaluate(r"""() => {
                    const buttons = document.querySelectorAll('button, a, [role="button"]');
                    for (const b of buttons) {
                        const txt = (b.textContent || '').trim().toLowerCase();
                        if (txt === '+' || txt === 'start a new appraisal' ||
                            txt.indexOf('start a new appraisal') >= 0 ||
                            txt.indexOf('new appraisal') >= 0) {
                            b.click(); return 'clicked';
                        }
                    }
                    return 'not_found';
                }""")
                time.sleep(1.5)
                # Re-enter the VIN in the now-open modal
                page.evaluate(r"""(vin) => {
                    const inputs = document.querySelectorAll('input');
                    for (const i of inputs) {
                        const ph = (i.placeholder || '').toLowerCase();
                        const al = (i.getAttribute('aria-label') || '').toLowerCase();
                        if (ph.includes('vin') || al.includes('vin')) {
                            i.focus();
                            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                            setter.call(i, vin);
                            i.dispatchEvent(new Event('input', {bubbles: true}));
                            i.dispatchEvent(new Event('change', {bubbles: true}));
                            return 'filled';
                        }
                    }
                    return 'not_found';
                }""", vin)
                time.sleep(0.5)
                page.evaluate(r"""() => {
                    const all = document.querySelectorAll('*');
                    for (const el of all) {
                        let direct = '';
                        for (const cn of el.childNodes) if (cn.nodeType === 3) direct += cn.textContent.trim();
                        if (direct.toLowerCase() === 'search') { el.click(); return 'clicked'; }
                    }
                }""")
                time.sleep(1.5)
                choices = page.evaluate(_scrape_choices_js) or []
            except Exception as _force_err:
                print(f"[+{time.time()-t:5.1f}s] [accutrade] force-fresh failed: {_force_err}")

        if not choices:
            # SHADOW_LOCATOR_FALLBACK_2026_05_28: Playwright locator-based scrape
            # for shadow-rendered modals (Bentley Continental, possibly other
            # exotics). page.evaluate(querySelectorAll) runs in main-document
            # scope and cannot pierce open shadow roots. page.locator(...) Python
            # API auto-pierces. ADDITIVE-ONLY: only fires when the legacy JS
            # scrape returned empty above, so mainstream cars unaffected.
            try:
                _pw_locs = []
                _pw_selectors = (
                    "new-appraisal-trim-choice",
                    ".new-appraisal-trim-choice",
                    "article.select-container",
                    "[role='option']",
                )
                for _sel in _pw_selectors:
                    try:
                        _all = page.locator(_sel).all()
                    except Exception:
                        continue
                    for _i, _loc in enumerate(_all):
                        try:
                            if not _loc.is_visible(timeout=300):
                                continue
                            _txt = (_loc.text_content(timeout=600) or "").strip()
                        except Exception:
                            continue
                        import re as _re_local
                        _txt = _re_local.sub(r"\s+", " ", _txt)
                        _txt = _re_local.sub(
                            r"\s*(?:keyboard_arrow_right|keyboard_arrow_left|chevron_right|chevron_left|arrow_forward|arrow_back|arrow_drop_down|expand_more|more_vert)\s*$",
                            "", _txt, flags=_re_local.IGNORECASE,
                        ).strip()
                        if not _txt or len(_txt) > 200:
                            continue
                        _pw_locs.append({
                            "index": len(_pw_locs),
                            "dom_index": _i,
                            "text": _txt,
                            "_selector_for_click": _sel,
                            "_used_locator": True,
                        })
                    if _pw_locs:
                        break
                if _pw_locs:
                    print(f"[+{time.time()-t:5.1f}s] [accutrade] SHADOW_LOCATOR_FALLBACK picked up {len(_pw_locs)} trim choice(s) via Playwright locator")
                    choices = _pw_locs
            except Exception as _shl_err:
                print(f"[+{time.time()-t:5.1f}s] [accutrade] SHADOW_LOCATOR_FALLBACK err: {_shl_err}")

        if not choices:
            # Genuinely no modal even after JS + locator scrape — bail.
            pass
        else:
                chosen_index = None
                # 1) AI overseer (preferred)
                overseer = _ask_overseer(vin, bid_id, choices)
                if overseer and overseer.get('index') is not None:
                    chosen_index = int(overseer['index'])
                    selected_trim_text = overseer.get('text') or (choices[chosen_index]['text'] if chosen_index < len(choices) else None)
                    trim_select_source = overseer.get('source') or 'llm'
                    print(f"[+{time.time()-t:5.1f}s] [accutrade] overseer picked [{chosen_index}] '{selected_trim_text}' src={trim_select_source} conf={overseer.get('confidence')}")
                # 2) Fallback fuzzy match on seller trim hint
                if chosen_index is None and trim:
                    h = trim.lower()
                    for c in choices:
                        if h in c['text'].lower():
                            chosen_index = c['index']
                            selected_trim_text = c['text']
                            trim_select_source = 'fuzzy_hint'
                            break
                # 3) Last resort — first visible choice (legacy behavior)
                if chosen_index is None:
                    chosen_index = 0
                    selected_trim_text = choices[0]['text']
                    trim_select_source = 'first_visible'
                    print(f"[+{time.time()-t:5.1f}s] [accutrade] NO overseer/hint — defaulting to [0] '{selected_trim_text}'")

                # SHADOW_LOCATOR_FALLBACK_2026_05_28 click path. When the
                # choices came from the Playwright locator fallback, click via
                # locator (shadow-DOM-piercing). Otherwise use the legacy JS
                # click that runs in main-document scope (unchanged for
                # mainstream cars).
                _chosen_meta = choices[chosen_index] if 0 <= chosen_index < len(choices) else None
                if _chosen_meta and _chosen_meta.get("_used_locator"):
                    _sel = _chosen_meta.get("_selector_for_click")
                    _dom_i = int(_chosen_meta.get("dom_index", chosen_index))
                    try:
                        _target = page.locator(_sel).nth(_dom_i)
                        _target.scroll_into_view_if_needed(timeout=2000)
                        _target.click(timeout=5000)
                        print(f"[+{time.time()-t:5.1f}s] [accutrade] locator-click ok sel={_sel} idx={_dom_i}")
                    except Exception as _lc_err:
                        print(f"[+{time.time()-t:5.1f}s] [accutrade] locator-click err: {_lc_err}")
                else:
                    page.evaluate(r"""(targetDomIndex) => {
                        function fc(el) { try { el.click(); } catch(e) {}
                            try { el.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, view:window})); } catch(e) {} }
                        let nodes = document.querySelectorAll('new-appraisal-trim-choice');
                        if (!nodes.length) nodes = document.querySelectorAll('.new-appraisal-trim-choice');
                        const visible = [];
                        nodes.forEach(c => { if (c.offsetParent) visible.push(c); });
                        const best = visible[targetDomIndex] || visible[0];
                        if (!best) return 'no_target';
                        fc(best);
                        const inner = best.querySelector('.new-appraisal-trim-choice, .text');
                        if (inner) fc(inner);
                        return 'clicked_trim';
                    }""", chosen_index)
        time.sleep(1)  # was 3s
        deadline = time.time() + 30
        while time.time() < deadline:
            if "/appraisal/" in page.url and "/new" not in page.url:
                break
            time.sleep(0.3)  # was 1s — tighter polling lands on the new URL faster

    # GUIDEBOOK_RECOVER_2026_06_01: under render contention the freshly-created
    # appraisal sometimes routes to the READ-ONLY Black-Book *guidebook* page
    # (/appraisal/<id>/valuation/guidebooks/<vin>?guidebook=black-book) instead
    # of the editable *scorecard* (/appraisal/<id>?backUrl=...). The guidebook
    # DOM has NO odometer input at all (only a location-picker + 3 country
    # radios), so the real miles can never be typed -> AccuTrade returns
    # age-based DEFAULT-mileage values and the value-change detector below
    # false-fails (pre==post). Proven on bid 2371 (Acura MDX Type S): first run
    # landed on an 838KB guidebook DOM with 0 fc='odometer' inputs and typed
    # nothing; the AccuTrade-ALONE retry landed on the scorecard, typed 36381,
    # and committed $42,600. The old readiness loop above ACCEPTS the guidebook
    # URL because it only checks "/appraisal/" in url. Detect it and navigate to
    # the scorecard, then wait for the editable odometer to MOUNT (positive
    # signal, not URL), up to 3x. This makes the first (contended) run reach the
    # same editable state the alone-retry already reaches.
    def _is_guidebook(u):
        return bool(u) and ("/valuation/guidebooks/" in u or "guidebook=" in u)

    _gb_widen_sel = ("input[type='number'], input[inputmode='numeric'], "
                     "input.mat-input-element, .cdk-overlay-container input, "
                     "input[formcontrolname*='dometer' i], input[formcontrolname*='ileage' i], "
                     "input[formcontrolname='odometer']")

    def _odometer_mounted():
        # True only when a NON-location-picker editable odometer-ish input is
        # present + visible (the scorecard's `fc='odometer'` field).
        try:
            cand = page.locator(_gb_widen_sel)
            n = cand.count()
        except Exception:
            n = 0
        for _i in range(n):
            try:
                _c = cand.nth(_i)
                if not _c.is_visible(timeout=200):
                    continue
                _cls = (_c.get_attribute('class', timeout=300) or '')
                _ph = (_c.get_attribute('placeholder', timeout=300) or '')
                if 'location-picker' in _cls or 'Location' in _ph:
                    continue
                return True
            except Exception:
                continue
        # Also accept the collapsed odometer-dock-list presence on the
        # scorecard (it expands to the input) as a weaker positive.
        try:
            return page.locator("odometer-dock-list").first.count() > 0
        except Exception:
            return False

    import re as _re_gb
    _gb_recovered = False
    for _gb_try in range(3):
        if not _is_guidebook(page.url):
            break
        _m = _re_gb.search(r"/appraisal/(\d+)", page.url)
        if not _m:
            break
        _aid = _m.group(1)
        _sc_url = f"{ACCUTRADE_URL}/appraisal/{_aid}?backUrl=%2Freport%2Factive"
        print(f"[+{time.time()-t:5.1f}s] [accutrade] GUIDEBOOK_RECOVER: on read-only guidebook "
              f"(url={page.url[:90]}) — navigating to scorecard (try {_gb_try+1}/3)")
        try:
            page.goto(_sc_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as _gbnav:
            print(f"[+{time.time()-t:5.1f}s] [accutrade] GUIDEBOOK_RECOVER nav err: {_gbnav}")
        # Wait up to 20s for the editable odometer to MOUNT on the scorecard.
        _gb_deadline = time.time() + 20
        while time.time() < _gb_deadline:
            if not _is_guidebook(page.url) and _odometer_mounted():
                _gb_recovered = True
                break
            time.sleep(0.4)
        if _gb_recovered:
            print(f"[+{time.time()-t:5.1f}s] [accutrade] GUIDEBOOK_RECOVER ok — scorecard odometer mounted "
                  f"(url={page.url[:90]})")
            break
    if _is_guidebook(page.url) and not _gb_recovered:
        print(f"[+{time.time()-t:5.1f}s] [accutrade] GUIDEBOOK_RECOVER FAILED — still on guidebook after 3 tries; "
              f"odometer never mounted (url={page.url[:90]})")

    # PRE_VALUES_BEFORE_DISPATCH_2026_05_28: capture price snapshot BEFORE
    # the JS dispatch. Original v2 detector captured AFTER, which fires
    # false-fail when AccuTrade commits synchronously on input/blur — by the
    # time pre_values is read, the page already reflects the typed mileage,
    # so post==pre and "no change detected" becomes a false negative.
    # Two flakes today (bid 2208 GLC vm-worker-11, bid 2215 Maserati
    # vm-worker-2) both resolved on different-worker retry — classic
    # synchronous-recalc race symptom. This snapshot is taken before any
    # input event fires so any recalc Cox does post-dispatch is detectable.
    _pre_values_pre_dispatch = page.evaluate(r"""() => {
        const map = [
            ['Instant Offer','guaranteed_offer'],
            ['Target Auction','trade_in'],
            ['Target Retail','trade_market'],
            ['Wholesale / Average','market_avg'],
            ['Wholesale/Average','market_avg'],
            ['Wholesale Average','market_avg']
        ];
        const r = {guaranteed_offer:null, trade_in:null, trade_market:null, market_avg:null};
        const text = document.body.innerText || '';
        for (const [label, field] of map) {
            if (r[field] !== null) continue;
            const idx = text.indexOf(label);
            if (idx < 0) continue;
            const win = text.substring(idx + label.length, idx + label.length + 80);
            if (/^\s*\r?\n?\s*N\/A\b/i.test(win)) { r[field] = null; continue; }
            const m = win.match(/\$\s*([\d,]+)(?!\d)/);
            if (m) {
                const n = parseInt(m[1].replace(/,/g, ''));
                if (n > 100 && n < 10000000) r[field] = n;
            }
        }
        return r;
    }""") or {}

    # ANGULAR_FILL_2026_05_28: OS-keystroke commit via Playwright locator,
    # BEFORE the legacy JS dispatch. Empirical validation from bid 2208's
    # forensic HTML showed `ng-pristine` + `ng-untouched` + `ng-valid` on
    # the odometer input AT THE MOMENT OF FAILURE — Angular's reactive
    # form never saw the JS-dispatched input/change/blur events because
    # they're `trusted: false`. Cox's CDK input handler only updates the
    # reactive form model from TRUSTED keystrokes. Playwright's
    # locator.type() sends OS-level keystroke events that arrive trusted,
    # which Angular catches → model updates → recalc fires.
    # JS dispatch below is left in place as harmless belt-and-suspenders.
    try:
        _ang_target_miles = str(int(miles))

        # DOCK_LIST_EXPAND_2026_05_29: editable odometer input lives in the
        # COLLAPSED "Odometer" dock-list row; click to expand before it mounts.
        _dock_committed = False
        _committed_loc = None  # RETYPE_ROUNDS_2026_05_31: stash winning input
        try:
            _dock = page.locator("odometer-dock-list").first
            if _dock.count() > 0:
                _dock.scroll_into_view_if_needed(timeout=1500)
                # Click the row container / chevron to expand it.
                try:
                    _dock.locator(".container").first.click(timeout=2000)
                except Exception:
                    _dock.click(timeout=2000)
                print(f"[+{time.time()-t:5.1f}s] [accutrade] DOCK_LIST_EXPAND: clicked odometer-dock-list to expand")
                # DOCK_INPUT_WIDEN_2026_05_29: input mounts in a body-level
                # cdk-overlay, not under the dock-list — search document-wide.
                _dock_input = None
                _dl_deadline = time.time() + 25  # MOUNT_WAIT_25_2026_06_01: was 4s; odometer input mounts ~30-55s under render contention -> 1st run committed, no retry
                _widen_sel = ("input[type='number'], input[inputmode='numeric'], "
                              "input.mat-input-element, .cdk-overlay-container input, "
                              "input[formcontrolname*='dometer' i], input[formcontrolname*='ileage' i]")
                while time.time() < _dl_deadline:
                    _cand = page.locator(_widen_sel)
                    try:
                        _n = _cand.count()
                    except Exception:
                        _n = 0
                    # Pick first VISIBLE candidate that isn't the location picker.
                    for _ci in range(_n):
                        try:
                            _c = _cand.nth(_ci)
                            if not _c.is_visible(timeout=200):
                                continue
                            _cls = (_c.get_attribute('class', timeout=300) or '')
                            _ph = (_c.get_attribute('placeholder', timeout=300) or '')
                            if 'location-picker' in _cls or 'Location' in _ph:
                                continue
                            _dock_input = _c
                            break
                        except Exception:
                            continue
                    if _dock_input is not None:
                        break
                    time.sleep(0.3)
                # Always dump the full input inventory for forensics.
                try:
                    _inv = page.evaluate(
                        "() => { const out=[]; const walk=(r)=>{ "
                        "(r.querySelectorAll('input')||[]).forEach(el=>{ const cs=getComputedStyle(el); "
                        "out.push({id:el.id,ph:el.placeholder,aria:el.getAttribute('aria-label'),"
                        "type:el.type,fc:el.getAttribute('formcontrolname'),cls:(el.className||'').slice(0,40),"
                        "vis:(el.offsetParent!==null)}); }); "
                        "(r.querySelectorAll('*')||[]).forEach(el=>{ if(el.shadowRoot) walk(el.shadowRoot); }); }; "
                        "walk(document); return out; }"
                    )
                    print(f"[+{time.time()-t:5.1f}s] [accutrade] DOCK_LIST_EXPAND input-inventory: {_inv}")
                except Exception:
                    pass
                if _dock_input is not None:
                    _dock_input.scroll_into_view_if_needed(timeout=1500)
                    _dock_input.click(timeout=2000)
                    _dock_input.press("Control+A", timeout=1000)
                    _dock_input.press("Delete", timeout=1000)
                    _dock_input.type(_ang_target_miles, delay=40, timeout=4000)
                    _dock_input.press("Tab", timeout=1500)
                    _dock_committed = True
                    _committed_loc = _dock_input
                    print(f"[+{time.time()-t:5.1f}s] [accutrade] DOCK_LIST_EXPAND ok — typed {_ang_target_miles}")
                else:
                    print(f"[+{time.time()-t:5.1f}s] [accutrade] DOCK_LIST_EXPAND: no editable input found document-wide after expand — see input-inventory above; falling through")
            else:
                print(f"[+{time.time()-t:5.1f}s] [accutrade] DOCK_LIST_EXPAND: odometer-dock-list not present")
        except Exception as _dl_err:
            print(f"[+{time.time()-t:5.1f}s] [accutrade] DOCK_LIST_EXPAND err: {_dl_err}")

        _ang_filled = _dock_committed
        # Selector cascade — fallback only if the dock-list expand path did
        # not commit. Kept for DOM variants where the input IS in the scorecard.
        _ang_selectors = [
            "appraisal-widget-scorecard-odometer input",
            ".scorecard-odometer input",
            "input[placeholder*='odometer' i]",
            "input[aria-label*='odometer' i]",
            "input[id*='odometer' i]",
            "input[placeholder*='mileage' i]",
            "input[aria-label*='mileage' i]",
        ]
        for _sel in ([] if _ang_filled else _ang_selectors):
            try:
                _loc = page.locator(_sel).first
                if _loc.count() == 0:
                    continue
                _loc.scroll_into_view_if_needed(timeout=1500)
                _loc.click(timeout=2000)
                # Select-all + Delete (clears any pre-populated default like 45,000)
                _loc.press("Control+A", timeout=1000)
                _loc.press("Delete", timeout=1000)
                # Type with delay so each keystroke arrives as a discrete
                # trusted event (Angular's CDK input event listener relies
                # on per-keystroke `input` events to update its model).
                _loc.type(_ang_target_miles, delay=40, timeout=4000)
                _loc.press("Tab", timeout=1500)
                _ang_filled = True
                _committed_loc = _loc
                print(f"[+{time.time()-t:5.1f}s] [accutrade] ANGULAR_FILL ok via {_sel!r}")
                break
            except Exception as _ang_sel_err:
                # Selector didn't match or actionability timed out — try next
                continue
        if not _ang_filled:
            print(f"[+{time.time()-t:5.1f}s] [accutrade] ANGULAR_FILL: no selector matched, falling through to JS dispatch")
    except Exception as _ang_err:
        print(f"[+{time.time()-t:5.1f}s] [accutrade] ANGULAR_FILL err: {_ang_err}")

    # Set mileage (legacy JS dispatch — kept as belt-and-suspenders fallback)
    page.evaluate(r"""(target) => {
        const targetStr = target.toLocaleString('en-US');
        const ins = []; function gather(root) {
            try { root.querySelectorAll('input[type="text"], input[type="number"], input:not([type])').forEach(el => ins.push(el));
                  root.querySelectorAll('*').forEach(el => { if (el.shadowRoot) gather(el.shadowRoot); });
            } catch(e) {} }
        gather(document);
        const RE = /^\s*\d{1,3}(,\d{3})*\s*$/;
        for (const i of ins) {
            const r = i.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) continue;
            const ph = (i.placeholder || '').toLowerCase();
            const aria = (i.getAttribute('aria-label') || '').toLowerCase();
            const id = (i.id || '').toLowerCase();
            const nb = []; let p = i.parentElement;
            for (let d = 0; d < 5 && p; d++) {
                const t = (p.textContent || '').toLowerCase();
                if (t.length < 400) nb.push(t); p = p.parentElement;
            }
            const nbStr = nb.join(' | ');
            const kw = ['odometer','mileage','miles'].some(w => ph.includes(w) || aria.includes(w) || id.includes(w) || nbStr.includes(w));
            const v = i.value || '';
            let valLooks = RE.test(v);
            if (valLooks) { const n = parseInt(v.replace(/,/g, '')); valLooks = n >= 1000 && n <= 999999; }
            if (!kw && !(valLooks && /\bmi\b/.test(nbStr))) continue;
            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            i.focus();
            setter.call(i, targetStr);
            i.dispatchEvent(new Event('input', {bubbles: true}));
            i.dispatchEvent(new Event('change', {bubbles: true}));
            i.dispatchEvent(new KeyboardEvent('keydown', {bubbles: true, key: 'Tab'}));
            i.dispatchEvent(new KeyboardEvent('keyup',   {bubbles: true, key: 'Tab'}));
            i.dispatchEvent(new Event('blur',   {bubbles: true}));
        }
    }""", int(miles))
    # MILEAGE_COMMIT_FIX_V2_2026_05_15: v1's "Mileage entered" badge / Odometer
    # regex was an unreliable signal — bid 1466 wrongly logged committed=True
    # while storing base-mileage values. v2 detects commit by VALUE CHANGE:
    # snapshot the 4 dollar values BEFORE entering miles, then poll until at
    # least one differs from snapshot. Cannot be fooled by static page text.
    #
    # NOTE: we capture the snapshot RIGHT NOW (after the dispatch above, before
    # waiting). Most pages already have base-mileage values rendered by the time
    # we get here. A few synchronous-recalc pages may already show updated
    # values, in which case "no change after entry" is correct — we degrade to
    # the legacy $-labels-present check after a brief settle period.
    def _read_4_values():
        return page.evaluate(r"""() => {
            const map = [
                ['Instant Offer','guaranteed_offer'],
                ['Target Auction','trade_in'],
                ['Target Retail','trade_market'],
                ['Wholesale / Average','market_avg'],
                ['Wholesale/Average','market_avg'],
                ['Wholesale Average','market_avg']
            ];
            const r = {guaranteed_offer:null, trade_in:null, trade_market:null, market_avg:null};
            const text = document.body.innerText || '';
            for (const [label, field] of map) {
                if (r[field] !== null) continue;
                const idx = text.indexOf(label);
                if (idx < 0) continue;
                const win = text.substring(idx + label.length, idx + label.length + 80);
                if (/^\s*\r?\n?\s*N\/A\b/i.test(win)) { r[field] = null; continue; }
                const m = win.match(/\$\s*([\d,]+)(?!\d)/);
                if (m) {
                    const n = parseInt(m[1].replace(/,/g, ''));
                    if (n > 100 && n < 10000000) r[field] = n;
                }
            }
            return r;
        }""") or {}

    # PRE_VALUES_BEFORE_DISPATCH_2026_05_28: use the snapshot taken BEFORE
    # the dispatch (above) instead of re-reading post-dispatch. If Cox
    # recalculated synchronously on input/blur the post-read would already
    # show new values and we'd fire a false-fail.
    pre_values = _pre_values_pre_dispatch
    # Strong trigger: real Playwright Tab keypress (v1 carry-over — still
    # better than nothing).
    try:
        page.keyboard.press('Tab')
    except Exception:
        pass

    # MILEAGE_REDISPATCH_RETRY_2026_05_28: Phase-1 poll 12s; on no change,
    # one Tab re-blur + 8s phase 2; then RE-TYPE rounds (below). ~7% of cold
    # forms drop the typed mileage and only re-typing on the warm page commits.

    # RETYPE_ROUNDS_2026_05_31: re-type the mileage into the winning input on
    # the now-warm form (a re-blur alone can't commit a value the cold form
    # never registered). Prefer the captured locator; else re-run the cascade.
    def _retype():
        _t = str(int(miles))
        if _committed_loc is not None:
            try:
                _committed_loc.click(timeout=2000)
                _committed_loc.press("Control+A", timeout=1000)
                _committed_loc.press("Delete", timeout=1000)
                _committed_loc.type(_t, delay=40, timeout=4000)
                _committed_loc.press("Tab", timeout=1500)
                return True
            except Exception:
                pass
        for _s in _ang_selectors:
            try:
                _l = page.locator(_s).first
                if _l.count() == 0:
                    continue
                _l.click(timeout=2000)
                _l.press("Control+A", timeout=1000)
                _l.press("Delete", timeout=1000)
                _l.type(_t, delay=40, timeout=4000)
                _l.press("Tab", timeout=1500)
                return True
            except Exception:
                continue
        return False

    deadline_phase1 = time.time() + 12
    mileage_committed = False
    last_post = pre_values
    while time.time() < deadline_phase1:
        post_values = _read_4_values()
        for k in ('guaranteed_offer', 'trade_in', 'trade_market', 'market_avg'):
            if pre_values.get(k) != post_values.get(k):
                mileage_committed = True
                break
        if mileage_committed:
            last_post = post_values
            break
        last_post = post_values
        time.sleep(0.4)

    # Phase 2: re-blur + extra 8s poll. Only fires when phase 1 produced
    # no value change. The extra Tab keypress nudges Angular's reactive form
    # which sometimes misses the initial blur on slow-hydrating pages.
    if not mileage_committed:
        print(f"[+{time.time()-t:5.1f}s] [accutrade] v2 phase 1 (12s) no value change — re-blurring + 8s phase 2")
        try:
            page.keyboard.press('Tab')
        except Exception:
            pass
        deadline_phase2 = time.time() + 8
        while time.time() < deadline_phase2:
            post_values = _read_4_values()
            for k in ('guaranteed_offer', 'trade_in', 'trade_market', 'market_avg'):
                if pre_values.get(k) != post_values.get(k):
                    mileage_committed = True
                    print(f"[+{time.time()-t:5.1f}s] [accutrade] v2 phase 2 RECOVERED — Cox responded after re-blur")
                    break
            if mileage_committed:
                last_post = post_values
                break
            last_post = post_values
            time.sleep(0.4)

    # RETYPE_ROUNDS_2026_05_31: phase 1+2 saw no change → cold form likely
    # dropped the value. Re-type up to 2x on the warm form, 5s poll each,
    # under a hard wall-clock cap so the worker can never hang. Zero added
    # time on the common (already-committed) path.
    _retype_cap = time.time() + 12
    _round = 0
    while not mileage_committed and _round < 2 and time.time() < _retype_cap:
        _round += 1
        print(f"[+{time.time()-t:5.1f}s] [accutrade] RETYPE round {_round} on warm form")
        if not _retype():
            break
        _rd = min(time.time() + 5, _retype_cap)
        while time.time() < _rd:
            post_values = _read_4_values()
            for k in ('guaranteed_offer', 'trade_in', 'trade_market', 'market_avg'):
                if pre_values.get(k) != post_values.get(k):
                    mileage_committed = True
                    print(f"[+{time.time()-t:5.1f}s] [accutrade] RETYPE round {_round} RECOVERED")
                    break
            last_post = post_values
            if mileage_committed:
                break
            time.sleep(0.4)

    # ACCUTRADE_SETTLE_DELAY_2026_05_15: bid 1503 case — once we detect ANY
    # value change, the other 3 panels may still be rendering. Wait 2.5s
    # for everything to settle before reading final values. Without this,
    # Instant Offer / Target Auction / Target Retail can come back NULL
    # while only market_avg (which lands earlier) gets captured.
    if mileage_committed:
        time.sleep(2.5)

    # MILEAGE_COMMIT_FIX_V3_2026_05_18 (bid 1779, McLaren GT):
    # The v2 value-change detector fires a false-FAIL when AccuTrade had a
    # PRIOR appraisal at the same (or numerically-near) mileage — recalc
    # produces no value delta because the cached values are already correct.
    # Symptom: pre==post but the mileage IS in the input and the page has
    # real non-null values to read.
    #
    # Degrade as the v2 docstring promised: if pre==post AFTER the 12s
    # timeout, confirm that (a) the typed mileage actually landed in an
    # input field, AND (b) the page has at least one non-null dollar value.
    # Both conditions = AccuTrade has the right state, just no recalc fired.
    # Treat as committed. False-positive risk (v1's bid 1466 case) is
    # mitigated because v1 fired on the badge alone before typing was
    # confirmed; here we require BOTH typed-value-in-input AND non-null
    # dollar values to be already present.
    if not mileage_committed:
        try:
            _typed_str = f"{int(miles):,}"
            _input_has_typed = page.evaluate(
                r"""(want) => {
                    const ins = []; function gather(root) {
                        try { root.querySelectorAll('input').forEach(el => ins.push(el));
                              root.querySelectorAll('*').forEach(el => { if (el.shadowRoot) gather(el.shadowRoot); });
                        } catch(e) {} }
                    gather(document);
                    for (const i of ins) {
                        const v = (i.value || '').replace(/[,\s]/g, '');
                        if (v && v === want) return true;
                    }
                    return false;
                }""",
                str(int(miles))
            )
            _has_values = any(v for v in last_post.values() if v)
            if _input_has_typed and _has_values:
                mileage_committed = True
                print(f"[+{time.time()-t:5.1f}s] [accutrade] commit-via-degrade: "
                      f"typed miles in input + page has values "
                      f"(pre==post — AccuTrade had prior appraisal at same miles)")
                time.sleep(2.5)
        except Exception as _deg_err:
            print(f"[+{time.time()-t:5.1f}s] [accutrade] degrade check err: {_deg_err}")

    # Refuse to store if commit never happened. This is intentionally strict —
    # the alternative (soft pass) is what stored bid 1466 wrong.
    if not mileage_committed:
        # ACCU_MANUAL_QUOTE_2026_05_28: AccuTrade has no automated valuation
        # for certain Bentley / Rolls-Royce / exotic VINs — page renders the
        # "contact inventory consultant via chat or by calling 1.800.215.0001"
        # banner with all 4 price panels at N/A. The value-change detector
        # fires a false-FAIL here because there are no values that COULD
        # change (Cox content gap, not a worker bug). Detect the banner and
        # exit cleanly with a precise reason so operator + AI assessment
        # both know AccuTrade has no data for this VIN class. Bid 2182
        # (2024 Bentley Continental GTC, VIN SCBDG4ZG5RC018285) hit this.
        try:
            _manual_quote = page.evaluate(
                r"() => /contact\s+inventory\s+consultant|1[\s.\-]?800[\s.\-]?215[\s.\-]?0001/i"
                r".test(document.body.innerText || '')"
            )
        except Exception:
            _manual_quote = False
        if _manual_quote:
            print(f"[+{time.time()-t:5.1f}s] [accutrade] manual-quote-only "
                  f"(Cox has no automated value) vin={vin} miles={miles}")
            return {
                "guaranteed_offer": None, "trade_in": None, "trade_market": None,
                "retail": None, "market_avg": None,
                "screenshot": None,
                "appraisal_url": page.url if "/appraisal/" in page.url else None,
                "selected_trim_text": selected_trim_text,
                "trim_select_source": trim_select_source,
                "not_available": True,
                "unavailable_reason": "accutrade_manual_quote_only",
            }

        # ACCU_CAPTURE_ONFAIL_2026_05_28: dump screenshot + HTML for forensics.
        # Bid 2182 (2024 Bentley) hit this path with no diagnostic detail
        # because worker_main.py masked the reason via key-name mismatch.
        # Now we save the actual page state so operator can SEE what AccuTrade
        # showed when mileage failed to commit.
        _fail_ts = int(time.time())
        _fail_ss = REPORTS_DIR / f"_failed_{vin}_{_fail_ts}.png"
        _fail_html = REPORTS_DIR / f"_failed_{vin}_{_fail_ts}.html"
        try:
            page.screenshot(path=str(_fail_ss), full_page=True)
        except Exception as _ssx:
            print(f"  [accutrade-fail] screenshot err: {_ssx}")
            _fail_ss = None
        try:
            _fail_html.write_text(page.content(), encoding="utf-8", errors="ignore")
        except Exception as _hx:
            print(f"  [accutrade-fail] html dump err: {_hx}")
            _fail_html = None
        # GUIDEBOOK_RECOVER_2026_06_01: if we're STILL on the read-only guidebook
        # at fail time (recovery exhausted its 3 tries), label it precisely so
        # the app autoretry + operator know this is the guidebook-routing miss,
        # not a generic commit flake. Still fail-CLOSED (not_available=True) so
        # base-miles defaults are never stored.
        _reason = "accutrade_stuck_on_guidebook" if _is_guidebook(page.url) else "mileage_did_not_commit_v2"
        if _fail_ss:
            _reason = _reason + " (ss=" + _fail_ss.name + ")"
        if _fail_html:
            _reason = _reason + " (html=" + _fail_html.name + ")"
        print(f"[+{time.time()-t:5.1f}s] [accutrade] FAIL: mileage_did_not_commit "
              f"pre={pre_values} post={last_post} miles={miles} forensics={_reason}")
        return {
            "guaranteed_offer": None, "trade_in": None, "trade_market": None,
            "retail": None, "market_avg": None,
            "screenshot": str(_fail_ss) if _fail_ss else None,
            "appraisal_url": page.url if "/appraisal/" in page.url else None,
            "selected_trim_text": selected_trim_text,
            "trim_select_source": trim_select_source,
            "not_available": True, "unavailable_reason": _reason,
        }

    values = page.evaluate(r"""() => {
        const map = [
            ['Instant Offer','guaranteed_offer'],
            ['Target Auction','trade_in'],
            ['Target Retail','trade_market'],
            ['Manheim','retail'],
            ['Wholesale / Average','market_avg'],
            ['Wholesale/Average','market_avg'],
            ['Wholesale Average','market_avg']
        ];
        const r = {guaranteed_offer:null, trade_in:null, trade_market:null, retail:null, market_avg:null};
        const text = document.body.innerText || '';
        for (const [label, field] of map) {
            if (r[field] !== null) continue;
            const idx = text.indexOf(label);
            if (idx < 0) continue;
            const win = text.substring(idx + label.length, idx + label.length + 80);
            if (/^\s*\r?\n?\s*N\/A\b/i.test(win)) { r[field] = null; continue; }
            const m = win.match(/\$\s*([\d,]+)(?!\d)/);
            if (m) {
                const n = parseInt(m[1].replace(/,/g, ''));
                if (n > 100 && n < 10000000) r[field] = n;
            }
        }
        return r;
    }""") or {}

    ts = int(time.time())
    screenshot = REPORTS_DIR / f"accutrade_{vin}_{ts}.png"
    try:
        page.screenshot(path=str(screenshot), full_page=True)
        try:
            from PIL import Image
            img = Image.open(screenshot)
            w, h = img.size
            img = img.crop((0, 0, int(w * 0.85), h))
            img.save(screenshot, optimize=True)
        except Exception: pass
    except Exception:
        screenshot = None

    appraisal_url = page.url if "/appraisal/" in page.url else None
    print(f"[+{time.time()-t:5.1f}s] [accutrade] done values={values} url={appraisal_url} committed=True (v2)")
    return {
        "guaranteed_offer": values.get("guaranteed_offer"),
        "trade_in": values.get("trade_in"),
        "trade_market": values.get("trade_market"),
        "retail": values.get("retail"),
        "market_avg": values.get("market_avg"),
        "screenshot": str(screenshot) if screenshot else None,
        "appraisal_url": appraisal_url,
        "selected_trim_text": selected_trim_text,
        "trim_select_source": trim_select_source,
    }
