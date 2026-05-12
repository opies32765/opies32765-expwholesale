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


def _ask_overseer(vin, bid_id, choices, timeout=15):
    """Ask EW's AI overseer which trim choice to click. Returns dict or None."""
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
            # Genuinely no modal even after forcing — bail to whatever's on page.
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

    # Set mileage
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
    # Was time.sleep(7) — replaced with poll-until-values-appear loop.
    # Most lookups finish recalc in 2-4s; we cap at 7s so worst case == old behavior.
    deadline = time.time() + 7
    while time.time() < deadline:
        ready = page.evaluate(r"""() => {
            const text = document.body.innerText || '';
            // Need at least 2 of the dollar-value labels to have a $value next to them
            const labels = ['Instant Offer','Target Auction','Target Retail','Wholesale'];
            let hits = 0;
            for (const lab of labels) {
                const idx = text.indexOf(lab);
                if (idx < 0) continue;
                const win = text.substring(idx, idx + 100);
                if (/\$\s*[\d,]{3,}/.test(win)) hits++;
            }
            return hits >= 2;
        }""")
        if ready: break
        time.sleep(0.4)

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
    print(f"[+{time.time()-t:5.1f}s] [accutrade] done values={values} url={appraisal_url}")
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
