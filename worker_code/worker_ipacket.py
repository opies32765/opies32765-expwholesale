"""iPacket lookup module — keep it simple: sticker pulls or it doesn't."""
import os, re, time
from pathlib import Path

REPORTS_DIR = Path(r"C:\worker\ipacket_reports")
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

EMAIL = os.environ.get("IPACKET_EMAIL", "opies32765@gmail.com")
PASSWORD = os.environ.get("IPACKET_PASSWORD", "Sedecremlun34$")
IPACKET_DPAPP = "https://dpapp.autoipacket.com/"
LOGIN_MARKERS = ("/login", "/signin", "/sign-in", "/forgot")



def _parse_sticker_text(text):
    """Regex-extract MSRP / base / colors / options from sticker text.
    Mirrors app.py _parse_sticker_text — keep them in sync."""
    out = {"total_msrp": None, "base_price": None,
           "exterior_color": None, "interior_color": None, "options": []}
    if not text or len(text) < 50:
        return out
    for pat in (r"TOTAL\s+(?:PREDICTED\s+)?PRICE\s*[:$]?\s*\$?\s*([\d,]+)",
                r"TOTAL\s+MSRP\s*[:$]?\s*\$?\s*([\d,]+)",
                r"(?<!BASE\s)MSRP\s*[:$]?\s*\$?\s*([\d,]+)"):
        m = re.search(pat, text, re.I)
        if m:
            try:
                v = int(m.group(1).replace(",", ""))
                if 1000 < v < 10_000_000:
                    out["total_msrp"] = v; break
            except ValueError: pass
    for pat in (r"BASE\s+SUGGESTED\s+PRICE\s*[:$]?\s*\$?\s*([\d,]+)",
                r"BASE\s+PRICE\s*[:$]?\s*\$?\s*([\d,]+)",
                r"BASE\s+MSRP\s*[:$]?\s*\$?\s*([\d,]+)"):
        m = re.search(pat, text, re.I)
        if m:
            try:
                v = int(m.group(1).replace(",", ""))
                if 1000 < v < 10_000_000:
                    out["base_price"] = v; break
            except ValueError: pass
    m = re.search(r"EXTERIOR(?:\s+COLOR)?[:\s]+([A-Za-z][A-Za-z\s/-]{2,40})", text, re.I)
    if m: out["exterior_color"] = m.group(1).strip().split(chr(10))[0].strip()
    m = re.search(r"INTERIOR(?:\s+COLOR)?[:\s]+([A-Za-z][A-Za-z\s/-]{2,40})", text, re.I)
    if m: out["interior_color"] = m.group(1).strip().split(chr(10))[0].strip()
    return out


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
    # Clear stale canvas signature + record pre-submit canvas size, so detector
    # waits for a NEW render instead of matching the prior lookup's stable canvas.
    page.evaluate(r"""() => {
        window.__ipk_canvas_sig = null;
        const view = document.querySelector(".stickerpull-view-container");
        const canvas = view ? view.querySelector(".react-pdf__Page__canvas") : null;
        window.__ipk_pre_submit_size = canvas ? (canvas.width + "x" + canvas.height) : null;
    }""")
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
            // PRIMARY: iPacket renders sticker via react-pdf to a
            //   <canvas class="react-pdf__Page__canvas">. The canvas grows
            //   as PDF.js paints more — we have to wait until it STOPS
            //   changing or we capture a partial render. Track dimensions
            //   between polls and require 2 consecutive identical readings.
            const view = document.querySelector('.stickerpull-view-container');
            if (view && view.offsetParent !== null) {
                const canvas = view.querySelector('.react-pdf__Page__canvas');
                if (canvas && canvas.width > 500 && canvas.height > 500) {
                    const sig = canvas.width + 'x' + canvas.height;
                    const presub = window.__ipk_pre_submit_size;
                    if (presub && presub === sig) {
                        return {state: 'pre_submit_canvas', size: sig};
                    }
                    const prev = window.__ipk_canvas_sig;
                    window.__ipk_canvas_sig = sig;
                    if (prev === sig) {
                        return {state: 'ready', size: sig};
                    }
                    return {state: 'rendering', size: sig};
                }
            }
            // PRIMARY-2: iPacket renders the sticker as an iframe whose
            //   src contains '/sticker/<VIN>?token=<jwt>'. The canvas lives
            //   INSIDE that iframe (same-origin policy hides it from us),
            //   but the iframe itself with our VIN in the src is the
            //   authoritative signal that iPacket has data for this VIN.
            //   This is the path that fires for fresh lookups (VIN not yet
            //   in Recent Pulls).
            const iframes = document.querySelectorAll(
                'iframe.ipacket-viewer, iframe[src*="document-viewer.autoipacket.com"]'
            );
            for (const f of iframes) {
                const src = (f.getAttribute('src') || '').toUpperCase();
                if (src.indexOf('/STICKER/' + v) >= 0 || src.indexOf('/' + v + '?') >= 0) {
                    return {state: 'ready', size: 'iframe-' + v};
                }
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

    print(f"[+{time.time()-t:5.1f}s] [ipacket] ready (canvas stable)")
    sticker_page = page
    time.sleep(2)

    ts = int(time.time())
    screenshot = REPORTS_DIR / f"ipacket_{vin}_{ts}.png"
    sticker_url = sticker_page.url

    # Capture: navigate to the iframe's src (document-viewer.autoipacket.com)
    # which loads the sticker as a standalone page (no dashboard chrome),
    # then take a full-page screenshot. This is exactly what produced bid
    # 731's clean sticker capture. iPacket's iframe URL has a JWT token in
    # the query string so it works as a standalone load.
    try:
        viewer_src = sticker_page.evaluate(
            "() => { const f = document.querySelector('iframe.ipacket-viewer, iframe[src*=\"document-viewer.autoipacket.com\"]'); return f ? f.src : null; }"
        )
        if viewer_src:
            print(f"[+{time.time()-t:5.1f}s] [ipacket] navigating to viewer src directly")
            try:
                sticker_page.goto(viewer_src, wait_until="domcontentloaded", timeout=30000)
            except Exception as nav_ex:
                print(f"[+{time.time()-t:5.1f}s] [ipacket] nav to viewer failed: {nav_ex}")
            # Brief wait for the sticker to render in the standalone view
            time.sleep(4)

        sticker_page.screenshot(path=str(screenshot), full_page=True)
        size = screenshot.stat().st_size
        print(f"[+{time.time()-t:5.1f}s] [ipacket] screenshot {size:,} bytes")
    except Exception as e:
        print(f"[+{time.time()-t:5.1f}s] [ipacket] capture FAIL: {e}")
        try:
            sticker_page.screenshot(path=str(screenshot), full_page=True)
        except Exception:
            screenshot = None

    # Extract text from the rendered sticker (react-pdf textLayer or body)
    # so worker can return MSRP/base/colors via regex — avoids the
    # server-side Gemini OCR fallback firing on every bid.
    sticker_text = ""
    try:
        sticker_text = sticker_page.evaluate(r"""
            () => {
                const layers = document.querySelectorAll(
                    ".react-pdf__Page__textContent, .textLayer"
                );
                if (layers.length) {
                    const out = [];
                    for (const l of layers) out.push(l.innerText || l.textContent || "");
                    return out.join("\n");
                }
                return document.body ? (document.body.innerText || "") : "";
            }
        """) or ""
    except Exception as e:
        print(f"[+{time.time()-t:5.1f}s] [ipacket] text extract FAIL: {e}")

    parsed = _parse_sticker_text(sticker_text) if sticker_text else {}
    if parsed.get("total_msrp"):
        _m = parsed.get("total_msrp") or 0
        _b = parsed.get("base_price") or 0
        _ec = parsed.get("exterior_color") or ""
        print(f"[+{time.time()-t:5.1f}s] [ipacket] regex MSRP=${_m:,} base=${_b:,} ext={_ec} text={len(sticker_text)} chars")

    if sticker_page is not page:
        try: sticker_page.close()
        except Exception: pass

    return {
        "screenshot": str(screenshot) if screenshot else None,
        "sticker_url": sticker_url,
        "total_msrp": parsed.get("total_msrp"),
        "base_price": parsed.get("base_price"),
        "exterior_color": parsed.get("exterior_color"),
        "interior_color": parsed.get("interior_color"),
        "raw": {"options": parsed.get("options", []),
                "text_chars": len(sticker_text)},
    }
