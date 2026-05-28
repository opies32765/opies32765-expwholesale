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
        # ACCU_TRIM_BUTTON_FALLBACK_2026_05_28: Bentley/exotic modal renders
        # trim choices as plain <button>/<div> elements with chevrons, NOT
        # the Angular custom element 'new-appraisal-trim-choice'. Bid 2182
        # (2024 Bentley Continental GTC) hit empty choices[] -> trim picker
        # never fired -> worker bailed mileage_did_not_commit_v2. Fallback:
        # find clickable elements inside the appraisal-new modal whose text
        # matches a body-style + engine-spec pattern (e.g. "A CONVERTIBLE
        # 4.0L V8 TURBO"). Tag each row with _selector so the click handler
        # below knows whether to click the Angular element or the fallback.
        _scrape_choices_js = r"""() => {
            const out = [];
            const stripGlyphs = (el) => {
                const clone = el.cloneNode(true);
                clone.querySelectorAll('mat-icon, .mat-icon, .material-icons, svg, i.material-icons-outlined').forEach(e => e.remove());
                let txt = (clone.textContent || '').trim().replace(/\s+/g, ' ');
                txt = txt.replace(/\s*(?:keyboard_arrow_right|keyboard_arrow_left|chevron_right|chevron_left|arrow_forward|arrow_back|arrow_drop_down|expand_more|more_vert)\s*$/i, '').trim();
                return txt;
            };

            // ── Path 1: Angular custom element (Cox v1 layout) ──
            let nodes = document.querySelectorAll('new-appraisal-trim-choice');
            if (!nodes.length) nodes = document.querySelectorAll('.new-appraisal-trim-choice');
            nodes.forEach((c, i) => {
                if (!c.offsetParent) return;
                const txt = stripGlyphs(c);
                if (txt) out.push({index: out.length, dom_index: i, text: txt, _selector: 'angular'});
            });
            if (out.length) return out;

            // ── Path 1.5: Cox v2 layout used for Bentley/exotics (BENTLEY_TRIM_2026_05_28) ──
            // <article class="select-container single-trim"> wraps each trim row.
            // Bid 2182 (2024 Bentley Continental GTC) hit this; v1 selector
            // returned zero matches.
            let v2 = document.querySelectorAll('article.select-container');
            v2.forEach((c, i) => {
                if (!c.offsetParent) return;
                const txt = stripGlyphs(c);
                if (txt) out.push({index: out.length, dom_index: i, text: txt, _selector: 'v2_article'});
            });
            if (out.length) return out;

            // ── Path 2: button-fallback (Bentley/exotic modal layouts) ──
            // Find a parent container that looks like the appraisal-new modal:
            // header text "Start a New Appraisal" OR "Select a trim".
            const modalHosts = [];
            document.querySelectorAll('h1, h2, h3, h4, [class*="dialog"], [class*="modal"]').forEach(h => {
                const t = (h.textContent || '').toLowerCase();
                if (t.includes('start a new appraisal') || t.includes('select a trim')) {
                    // climb 5 levels max to find a containing block
                    let p = h; for (let d=0; d<5 && p; d++) { if (p) modalHosts.push(p); p = p.parentElement; }
                }
            });
            const seen = new Set();
            const cands = [];
            // Body-style + engine-spec pattern: matches "A CONVERTIBLE 4.0L V8 TURBO",
            // "GT3 COUPE 4.0L 6 CYL", "BASE SEDAN 3.0L I6", etc.
            const TRIM_RE = /\b(COUPE|SEDAN|CONVERTIBLE|HATCHBACK|WAGON|SUV|TRUCK|ROADSTER|CABRIOLET|FASTBACK|HARDTOP|PICKUP|VAN|MINIVAN|CROSSOVER)\b.*\b\d+(?:\.\d+)?L\s*(?:V\d+|I\d+|R\d+|\d+\s*CYL)/i;
            const inAnyHost = (el) => modalHosts.length === 0 || modalHosts.some(h => h.contains(el));
            // Scan likely-clickable elements first
            const clickable = document.querySelectorAll('button, a, [role="button"], [tabindex], li, div');
            clickable.forEach(el => {
                if (!el.offsetParent) return;
                if (seen.has(el)) return;
                if (!inAnyHost(el)) return;
                // Skip if any DESCENDANT (other than self) is also a clickable match — we want
                // the innermost matching element. (Container divs would over-match.)
                const txt = stripGlyphs(el);
                if (!txt || txt.length < 6 || txt.length > 120) return;
                if (!TRIM_RE.test(txt)) return;
                // Reject if a child element ALSO matches — climb only to leaf clickables
                let childMatch = false;
                el.querySelectorAll('*').forEach(d => {
                    if (d === el) return;
                    if (!d.offsetParent) return;
                    const dt = stripGlyphs(d);
                    if (dt && dt.length < 120 && TRIM_RE.test(dt)) childMatch = true;
                });
                if (childMatch) return;
                seen.add(el);
                cands.push({el, txt});
            });
            cands.forEach((c, i) => {
                out.push({index: out.length, dom_index: i, text: c.txt, _selector: 'button_fallback'});
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

                # ACCU_TRIM_BUTTON_FALLBACK_2026_05_28: re-do the same dual-path
                # scan as the scraper, then click the Nth match. We re-scan
                # here (vs taking targetDomIndex by stale ref) because Angular
                # may have re-rendered between scrape + click.
                page.evaluate(r"""(targetDomIndex) => {
                    function fc(el) { try { el.click(); } catch(e) {}
                        try { el.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, view:window})); } catch(e) {} }
                    const stripGlyphs = (el) => {
                        const clone = el.cloneNode(true);
                        clone.querySelectorAll('mat-icon, .mat-icon, .material-icons, svg, i.material-icons-outlined').forEach(e => e.remove());
                        let txt = (clone.textContent || '').trim().replace(/\s+/g, ' ');
                        txt = txt.replace(/\s*(?:keyboard_arrow_right|keyboard_arrow_left|chevron_right|chevron_left|arrow_forward|arrow_back|arrow_drop_down|expand_more|more_vert)\s*$/i, '').trim();
                        return txt;
                    };
                    // Path 1: Angular custom element (Cox v1)
                    let nodes = document.querySelectorAll('new-appraisal-trim-choice');
                    if (!nodes.length) nodes = document.querySelectorAll('.new-appraisal-trim-choice');
                    const visible = [];
                    nodes.forEach(c => { if (c.offsetParent) visible.push(c); });
                    if (visible.length) {
                        const best = visible[targetDomIndex] || visible[0];
                        if (!best) return 'no_target_angular';
                        fc(best);
                        const inner = best.querySelector('.new-appraisal-trim-choice, .text');
                        if (inner) fc(inner);
                        return 'clicked_trim_angular';
                    }
                    // Path 1.5: Cox v2 layout (BENTLEY_TRIM_2026_05_28)
                    // article.select-container wraps each trim row.
                    let v2 = document.querySelectorAll('article.select-container');
                    const v2_visible = [];
                    v2.forEach(c => { if (c.offsetParent) v2_visible.push(c); });
                    if (v2_visible.length) {
                        const best = v2_visible[targetDomIndex] || v2_visible[0];
                        if (!best) return 'no_target_v2';
                        fc(best);
                        // Also click the inner .trim div in case article-level click is no-op
                        const inner = best.querySelector('.trim');
                        if (inner) fc(inner);
                        return 'clicked_trim_v2';
                    }
                    // Path 2: button-fallback
                    const modalHosts = [];
                    document.querySelectorAll('h1, h2, h3, h4, [class*="dialog"], [class*="modal"]').forEach(h => {
                        const t = (h.textContent || '').toLowerCase();
                        if (t.includes('start a new appraisal') || t.includes('select a trim')) {
                            let p = h; for (let d=0; d<5 && p; d++) { if (p) modalHosts.push(p); p = p.parentElement; }
                        }
                    });
                    const inAnyHost = (el) => modalHosts.length === 0 || modalHosts.some(h => h.contains(el));
                    const TRIM_RE = /\b(COUPE|SEDAN|CONVERTIBLE|HATCHBACK|WAGON|SUV|TRUCK|ROADSTER|CABRIOLET|FASTBACK|HARDTOP|PICKUP|VAN|MINIVAN|CROSSOVER)\b.*\b\d+(?:\.\d+)?L\s*(?:V\d+|I\d+|R\d+|\d+\s*CYL)/i;
                    const seen = new Set();
                    const cands = [];
                    document.querySelectorAll('button, a, [role="button"], [tabindex], li, div').forEach(el => {
                        if (!el.offsetParent) return;
                        if (seen.has(el)) return;
                        if (!inAnyHost(el)) return;
                        const txt = stripGlyphs(el);
                        if (!txt || txt.length < 6 || txt.length > 120) return;
                        if (!TRIM_RE.test(txt)) return;
                        let childMatch = false;
                        el.querySelectorAll('*').forEach(d => {
                            if (d === el) return;
                            if (!d.offsetParent) return;
                            const dt = stripGlyphs(d);
                            if (dt && dt.length < 120 && TRIM_RE.test(dt)) childMatch = true;
                        });
                        if (childMatch) return;
                        seen.add(el);
                        cands.push(el);
                    });
                    if (!cands.length) return 'no_target_fallback';
                    const target = cands[targetDomIndex] || cands[0];
                    fc(target);
                    return 'clicked_trim_fallback';
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

    pre_values = _read_4_values()
    # Strong trigger: real Playwright Tab keypress (v1 carry-over — still
    # better than nothing).
    try:
        page.keyboard.press('Tab')
    except Exception:
        pass

    deadline = time.time() + 12
    mileage_committed = False
    last_post = pre_values
    while time.time() < deadline:
        post_values = _read_4_values()
        # Committed if ANY value differs from snapshot (handles N/A->value
        # and value->different-value).
        for k in ('guaranteed_offer', 'trade_in', 'trade_market', 'market_avg'):
            if pre_values.get(k) != post_values.get(k):
                mileage_committed = True
                break
        if mileage_committed:
            last_post = post_values
            break
        last_post = post_values
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
        _reason = "mileage_did_not_commit_v2"
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
