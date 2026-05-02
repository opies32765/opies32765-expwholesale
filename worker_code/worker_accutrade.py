"""AccuTrade lookup module."""
import os, time
from pathlib import Path

REPORTS_DIR = Path(r"C:\worker\accutrade_reports")
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

EMAIL = os.environ.get("ACCUTRADE_EMAIL", "opies32765@gmail.com")
PASSWORD = os.environ.get("ACCUTRADE_PASSWORD", "Sedecremlun35$")
ACCUTRADE_URL = "https://appraiser3.accu-trade.com"
LOGIN_MARKERS = ("auth0.accu-trade.com", "/u/login", "/auth/login")
SUCCESS_PATHS = ("/dashboard", "/appraisal", "/vehicle", "/home", "/index", "/performance-center")


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


def lookup(page, ctx, vin, miles, t, trim=None):
    print(f"[+{time.time()-t:5.1f}s] [accutrade] start")
    page.goto(ACCUTRADE_URL, wait_until="domcontentloaded", timeout=30000)
    if not is_logged_in(page.url):
        if not auto_login(page, ctx):
            return {"error": "auto_login_failed"}
    print(f"[+{time.time()-t:5.1f}s] [accutrade] logged in")
    page = next((pg for pg in ctx.pages if is_logged_in(pg.url)), page)

    page.goto(ACCUTRADE_URL, wait_until="domcontentloaded", timeout=20000); time.sleep(2)
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
    time.sleep(1.5)
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
    time.sleep(2)

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
        time.sleep(1)
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
    time.sleep(1)
    page.evaluate(r"""() => {
        const all = document.querySelectorAll('*');
        for (const el of all) {
            let direct = '';
            for (const cn of el.childNodes) if (cn.nodeType === 3) direct += cn.textContent.trim();
            if (direct.toLowerCase() === 'search') { el.click(); return 'clicked'; }
        }
    }""")
    time.sleep(3)

    fast = page.evaluate(r"""() => {
        const url = window.location.href.toLowerCase();
        if (url.indexOf('/appraisal/') < 0) return false;
        if (url.indexOf('/new') >= 0 || url.indexOf('/auth/') >= 0) return false;
        const text = document.body.innerText || '';
        return (text.indexOf('Instant Offer') >= 0 || text.indexOf('Target Auction') >= 0)
               && /\$[\d,]{3,}/.test(text);
    }""")
    if not fast:
        time.sleep(2)
        page.evaluate(r"""(trimHint) => {
            function fc(el) { try { el.click(); } catch(e) {}
                try { el.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, view:window})); } catch(e) {} }
            const footers = document.querySelectorAll('footer');
            for (const f of footers) {
                if (!f.offsetParent) continue;
                const tt = (f.textContent || '');
                if (tt.indexOf('APPRAISAL') >= 0 && tt.indexOf('Target Trade') >= 0) {
                    let cur = f;
                    for (let j = 0; j < 6 && cur; j++) { fc(cur); cur = cur.parentElement; }
                    return 'clicked_existing';
                }
            }
            let choices = document.querySelectorAll('new-appraisal-trim-choice');
            if (!choices.length) choices = document.querySelectorAll('.new-appraisal-trim-choice');
            if (!choices.length) return 'no_modal';
            let best = choices[0];
            if (trimHint) {
                const h = trimHint.toLowerCase();
                for (const c of choices) {
                    if (!c.offsetParent) continue;
                    if ((c.textContent || '').toLowerCase().indexOf(h) >= 0) { best = c; break; }
                }
            }
            fc(best);
            const inner = best.querySelector('.new-appraisal-trim-choice, .text');
            if (inner) fc(inner);
            return 'clicked_trim';
        }""", trim or "")
        time.sleep(3)
        deadline = time.time() + 30
        while time.time() < deadline:
            if "/appraisal/" in page.url and "/new" not in page.url:
                break
            time.sleep(1)

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
    time.sleep(7)

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
    }
