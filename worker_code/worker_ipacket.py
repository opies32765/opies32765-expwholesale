"""iPacket lookup module — keep it simple: sticker pulls or it doesn't."""
import os, time
from pathlib import Path

REPORTS_DIR = Path(r"C:\worker\ipacket_reports")
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

EMAIL = os.environ.get("IPACKET_EMAIL", "opies32765@gmail.com")
PASSWORD = os.environ.get("IPACKET_PASSWORD", "Sedecremlun34$")
IPACKET_DPAPP = "https://dpapp.autoipacket.com/"
LOGIN_MARKERS = ("/login", "/signin", "/sign-in", "/forgot")


def is_logged_in(url):
    if "autoipacket.com" not in url: return False
    if any(m in url for m in LOGIN_MARKERS): return False
    return not url.endswith("auth.autoipacket.com/")


def auto_login(page, ctx, max_seconds=60):
    t0 = time.time(); last = ""
    while time.time() - t0 < max_seconds:
        for pg in ctx.pages:
            try:
                if is_logged_in(pg.url): return True
            except Exception: pass
        try:
            uf = page.query_selector('input[type="email"], input[name="email"], input[name="username"]')
            if uf and uf.is_visible() and last != "user":
                uf.fill(EMAIL)
                btn = page.query_selector('button[type="submit"], button:has-text("Continue"), button:has-text("Next"), button:has-text("Sign in")')
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


def lookup(page, ctx, vin, t):
    print(f"[+{time.time()-t:5.1f}s] [ipacket] start")
    page.goto(IPACKET_DPAPP, wait_until="domcontentloaded", timeout=30000); time.sleep(3)
    if not is_logged_in(page.url):
        if not auto_login(page, ctx):
            return {"error": "auto_login_failed"}
    page = next((pg for pg in ctx.pages if is_logged_in(pg.url)), page)

    # Make sure we're on the sticker pull page
    page.goto("https://dpapp.autoipacket.com/stickerpull", wait_until="domcontentloaded", timeout=20000)
    time.sleep(3)

    # Fill VIN
    f = page.evaluate(r"""(vin) => {
        const inputs = document.querySelectorAll('input');
        for (const i of inputs) {
            const ph = (i.placeholder || '').toUpperCase();
            const name = (i.name || '').toLowerCase();
            const id = (i.id || '').toLowerCase();
            if (ph.includes('VIN') || name.includes('vin') || id.includes('vin') || ph.includes('ENTER A VALID')) {
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
    if f != "filled": return {"error": "vin_input_not_found"}

    time.sleep(0.5)
    page.evaluate(r"""() => {
        const btns = document.querySelectorAll('button, input[type="submit"], a');
        for (const b of btns) {
            const txt = (b.textContent || b.value || '').trim().toLowerCase();
            if (txt === 'submit' || (b.className || '').toLowerCase().includes('submit')) { b.click(); return 'clicked'; }
        }
        return 'not_found';
    }""")
    print(f"[+{time.time()-t:5.1f}s] [ipacket] submitted")

    submit_ts = time.time()
    deadline = submit_ts + 30  # bumped from 18 to 30 — long stickers can take time
    state = "waiting"; msg = ""
    UNAVAIL_GRACE_SEC = 4.0  # ignore "unavailable" toasts for the first 4s — stale UI / pre-existing banners
    READY_PRIORITY = True    # if both ready+unavailable present, ready wins
    while time.time() < deadline:
        st = page.evaluate(r"""(vin) => {
            const v = (vin || '').toUpperCase();
            // PRIMARY: iPacket renders the sticker inline inside .stickerpull-view-container
            //   when a pull succeeds. The .module-pdf or .document-viewer-container child
            //   appears once the sticker is loaded. This is the real "ready" signal.
            const view = document.querySelector('.stickerpull-view-container');
            if (view && view.offsetParent !== null) {
                const viewer = view.querySelector('.document-viewer-container, .module-pdf, .module-viewer-container');
                if (viewer) return {state: 'ready'};
            }
            // SECONDARY: pull-history-table-download with our VIN's download URL
            //   (download URLs end in /<VIN>)
            const dl = document.querySelectorAll('.pull-history-table-download[href]');
            for (const a of dl) {
                if ((a.getAttribute('href') || '').toUpperCase().endsWith('/' + v)) {
                    return {state: 'ready'};
                }
            }
            // UNAVAILABLE toast (only trusted after grace period — handled in Python)
            const toasts = document.querySelectorAll(
                '.Toastify__toast, .Toastify__toast-body, [class*="Toastify__toast"], '
                + '.toast, .notification, [role="alert"], [role="status"]');
            for (const t of toasts) {
                if (t.offsetParent === null) continue;
                const txt = (t.innerText || '').toLowerCase();
                if (!txt) continue;
                if (txt.includes('unavailable') || txt.includes('unfortunately')
                    || txt.includes('not available') || txt.includes('restrictions apply')) {
                    return {state: 'unavailable', msg: txt.substring(0, 200)};
                }
            }
            return {state: 'waiting'};
        }""", vin) or {}
        state = st.get("state", "waiting"); msg = st.get("msg", "")
        # Within the grace window, ignore "unavailable" — it's almost always
        # a stale element from the previous lookup or a permanent UI banner.
        if state == "unavailable" and (time.time() - submit_ts) < UNAVAIL_GRACE_SEC:
            state = "waiting"
        if state in ("unavailable", "ready"): break
        time.sleep(0.4)

    if state != "ready":
        print(f"[+{time.time()-t:5.1f}s] [ipacket] not_available ({state})")
        # Dump page HTML + screenshot for offline debugging of false-negatives
        try:
            ts = int(time.time())
            dbg = REPORTS_DIR / f"debug_{vin}_{ts}.html"
            dbg.write_text(page.content(), encoding="utf-8", errors="ignore")
            page.screenshot(path=str(REPORTS_DIR / f"debug_{vin}_{ts}.png"), full_page=True)
            print(f"[ipacket] DEBUG dump: {dbg}")
        except Exception as e:
            print(f"[ipacket] debug dump failed: {e}")
        return {"not_available": True, "reason": msg or "no sticker"}

    print(f"[+{time.time()-t:5.1f}s] [ipacket] ready (inline viewer)")
    # Sticker is already rendered inline in .stickerpull-view-container.
    # No need to click "View Sticker" / open new tab — just screenshot the
    # current page after a brief settle for the PDF render.
    sticker_page = page
    time.sleep(2)

    time.sleep(3)
    ts = int(time.time())
    screenshot = REPORTS_DIR / f"ipacket_{vin}_{ts}.png"
    try:
        sticker_page.screenshot(path=str(screenshot), full_page=True)
        size = screenshot.stat().st_size
        print(f"[+{time.time()-t:5.1f}s] [ipacket] screenshot {size:,} bytes")
    except Exception as e:
        print(f"[+{time.time()-t:5.1f}s] [ipacket] screenshot FAIL: {e}")
        screenshot = None

    sticker_url = sticker_page.url
    if sticker_page is not page:
        try: sticker_page.close()
        except Exception: pass

    return {
        "screenshot": str(screenshot) if screenshot else None,
        "sticker_url": sticker_url,
    }
