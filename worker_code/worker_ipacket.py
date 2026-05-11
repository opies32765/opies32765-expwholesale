"""iPacket lookup module — keep it simple: sticker pulls or it doesn't."""
import os, re, time
from pathlib import Path

REPORTS_DIR = Path(r"C:\worker\ipacket_reports")
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

EMAIL = os.environ.get("IPACKET_EMAIL", "opies32765@gmail.com")
PASSWORD = os.environ.get("IPACKET_PASSWORD", "Sedecremlun34$")
IPACKET_DPAPP = "https://dpapp.autoipacket.com/"
LOGIN_MARKERS = ("/login", "/signin", "/sign-in", "/forgot")

# 2026-05-08: JWT auto-refresh — capture the Authorization Bearer token from
# any XHR request to autoipacket.com domains, POST it back to the EW server's
# /api/ipacket/refresh_token. This keeps vauto_session label='ipacket' fresh
# as long as ANY worker is doing iPacket pulls — same pattern as the vAuto
# cookie_keeper. Caught after 76 silent comp_msrp failures from a stale JWT.
EW_SERVER = os.environ.get("EW_SERVER", "https://experience-wholesale.net")

def _post_jwt_refresh(jwt):
    """Best-effort POST of fresh iPacket JWT. Server UPSERTs (idempotent)."""
    if not jwt or not jwt.startswith("eyJ"):
        return
    try:
        import json as _json
        from urllib import request as _ureq
        data = _json.dumps({"jwt": jwt}).encode("utf-8")
        req = _ureq.Request(
            f"{EW_SERVER}/api/ipacket/refresh_token",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _ureq.urlopen(req, timeout=8) as resp:
            if resp.status == 200:
                print(f"[ipacket] JWT auto-refreshed ({len(jwt)} chars)")
    except Exception as e:
        # Best-effort — never fail a bid pull because the refresh POST hiccuped
        print(f"[ipacket] JWT refresh skipped: {type(e).__name__}: {str(e)[:80]}")


def _attach_jwt_capture(page):
    """Hook page.on('request') to capture Authorization Bearer tokens from
    autoipacket.com XHRs and POST them to the refresh endpoint. Idempotent —
    only POSTs when the token actually changes."""
    last = {"token": None}
    def _on_request(request):
        try:
            url = request.url
            if "autoipacket.com" not in url:
                return
            auth = (request.headers or {}).get("authorization", "")
            if not auth or not auth.lower().startswith("bearer "):
                return
            tok = auth.split(None, 1)[1].strip()
            if not tok.startswith("eyJ") or tok == last["token"]:
                return
            last["token"] = tok
            _post_jwt_refresh(tok)
        except Exception:
            pass
    try:
        page.on("request", _on_request)
    except Exception:
        pass



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


def is_logged_in(page_or_url):
    """Detect login state. Accepts either a Playwright Page (preferred —
    inspects DOM) or a URL string (legacy / cross-tab iteration).

    2026-05-11: rewritten after iPacket moved to PKCE auth. The new flow
    redirects to auth.autoipacket.com/?redirect=...&pkce=...&client_id=...
    (no /login in path, double-slash + query string defeats endswith), and
    after a stale session lands on dpapp.autoipacket.com/stickerpull, the
    SPA renders the login form INLINE at that URL — so URL-only matching
    can't tell logged-in from logged-out. We now also DOM-check for the
    presence of an email+password input pair, which is the unmistakable
    fingerprint of the login form regardless of what URL is in the bar.
    """
    # String path — legacy callsites + cross-tab iteration in auto_login()
    if isinstance(page_or_url, str):
        url = page_or_url
        if "autoipacket.com" not in url: return False
        if any(m in url for m in LOGIN_MARKERS): return False
        if "auth.autoipacket.com" in url: return False
        return True
    # Page path — preferred
    page = page_or_url
    try:
        url = page.url
    except Exception:
        return False
    if "autoipacket.com" not in url: return False
    if any(m in url for m in LOGIN_MARKERS): return False
    if "auth.autoipacket.com" in url: return False
    # DOM check: an inline-rendered login form has visible email AND password
    # inputs. Both required so a stray hidden password reset field doesn't
    # false-positive a real dashboard page.
    try:
        has_login_form = page.evaluate(r"""() => {
            const inputs = document.querySelectorAll('input');
            let hasEmail = false, hasPwd = false;
            for (const i of inputs) {
                if (i.offsetWidth === 0 || i.offsetHeight === 0) continue;
                const type = (i.type || '').toLowerCase();
                const name = (i.name || '').toLowerCase();
                const ph = (i.placeholder || '').toLowerCase();
                if (type === 'password' || name === 'password') hasPwd = true;
                if (type === 'email' || name === 'email' || ph === 'email' || name === 'username') hasEmail = true;
            }
            return hasEmail && hasPwd;
        }""")
        if has_login_form:
            return False
    except Exception:
        pass
    return True


def auto_login(page, ctx, max_seconds=60):
    t0 = time.time(); last = ""
    while time.time() - t0 < max_seconds:
        for pg in ctx.pages:
            try:
                if is_logged_in(pg): return True
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
    _attach_jwt_capture(page)  # 2026-05-08: keep server JWT fresh on every call
    page.goto(IPACKET_DPAPP, wait_until="domcontentloaded", timeout=30000); time.sleep(3)
    if not is_logged_in(page):
        if not auto_login(page, ctx):
            return {"error": "auto_login_failed"}
    page = next((pg for pg in ctx.pages if is_logged_in(pg)), page)

    # Make sure we're on the sticker pull page
    page.goto("https://dpapp.autoipacket.com/stickerpull", wait_until="domcontentloaded", timeout=20000)
    time.sleep(3)
    # 2026-05-11: after /stickerpull goto, re-verify the page didn't render
    # the login form inline. iPacket's PKCE-era behavior is that a stale
    # session lands you on /stickerpull-the-URL with /login-the-content.
    if not is_logged_in(page):
        print(f"[+{time.time()-t:5.1f}s] [ipacket] /stickerpull rendered login form inline -- re-auth")
        if not auto_login(page, ctx):
            return {"error": "auto_login_failed"}
        page.goto("https://dpapp.autoipacket.com/stickerpull", wait_until="domcontentloaded", timeout=20000)
        time.sleep(3)

    # ===== V9 fast-path (additive, repeat-VIN only) =====
    # If this VIN is already in iPacket's Recent Sticker Pulls table, the
    # .pull-history-table-download anchor IS the visible "View Sticker" link.
    # Its React onClick uses dashboard auth (the href itself 403s on direct
    # fetch). Clicking bypasses the silent rate-limit-on-repeat-submit that
    # produces blank screenshots. Strictly additive: falls through to the
    # original Submit flow for fresh VINs OR if V9 doesn't actually render.
    try:
        cached_clicked = page.evaluate(r"""(v) => {
            const V = (v||'').toUpperCase();
            const anchors = document.querySelectorAll('.pull-history-table-download[href]');
            for (const a of anchors) {
                const h = (a.getAttribute('href') || '').toUpperCase();
                if (h.endsWith('/' + V)) { a.click(); return true; }
            }
            return false;
        }""", vin)
    except Exception as v9_ev_err:
        print(f"[+{time.time()-t:5.1f}s] [ipacket] V9 cached-check error: {v9_ev_err}")
        cached_clicked = False

    if cached_clicked:
        print(f"[+{time.time()-t:5.1f}s] [ipacket] V9 VIN cached -- clicked Recent Pulls anchor, waiting for viewer")
        time.sleep(3)
        v9_deadline = time.time() + 22
        v9_ready = False
        while time.time() < v9_deadline:
            try:
                st = page.evaluate(r"""(v) => {
                    const V = (v||'').toUpperCase();
                    const f = document.querySelector('iframe.ipacket-viewer, iframe[src*="document-viewer.autoipacket.com"]');
                    if (f) {
                        const src = (f.getAttribute('src') || '').toUpperCase();
                        if (src.indexOf('/STICKER/' + V) >= 0 || src.indexOf('/' + V + '?') >= 0) return 'iframe';
                    }
                    const view = document.querySelector('.stickerpull-view-container');
                    if (view) {
                        const c = view.querySelector('.react-pdf__Page__canvas');
                        if (c && c.width > 1000 && c.height > 1000) return 'canvas';
                    }
                    return null;
                }""", vin)
            except Exception:
                st = None
            if st: v9_ready = True; print(f"[+{time.time()-t:5.1f}s] [ipacket] V9 viewer ready via {st}"); break
            time.sleep(0.5)

        if v9_ready:
            try:
                ts = int(time.time())
                v9_screenshot = REPORTS_DIR / f"ipacket_{vin}_{ts}.png"
                v9_url = page.url
                v9_target = page
                try:
                    viewer_src = page.evaluate(
                        "() => { const f = document.querySelector('iframe.ipacket-viewer, iframe[src*=\"document-viewer.autoipacket.com\"]'); return f ? f.src : null; }"
                    )
                    if viewer_src:
                        print(f"[+{time.time()-t:5.1f}s] [ipacket] V9 navigating to viewer src for capture")
                        v9_target.goto(viewer_src, wait_until="domcontentloaded", timeout=30000)
                        time.sleep(5)
                except Exception as v9_nav_err:
                    print(f"[+{time.time()-t:5.1f}s] [ipacket] V9 viewer-src nav error: {v9_nav_err}")
                v9_target.screenshot(path=str(v9_screenshot), full_page=True)
                v9_size = v9_screenshot.stat().st_size if v9_screenshot.exists() else 0
                v9_text = ""
                try:
                    v9_text = v9_target.evaluate(r"""() => {
                        const layers = document.querySelectorAll(".react-pdf__Page__textContent, .textLayer");
                        if (layers.length) {
                            const out = []; for (const l of layers) out.push(l.innerText || l.textContent || "");
                            return out.join("\n");
                        }
                        return document.body ? (document.body.innerText || "") : "";
                    }""") or ""
                except Exception:
                    pass
                print(f"[+{time.time()-t:5.1f}s] [ipacket] V9 capture: text_chars={len(v9_text)} screenshot={v9_size:,}b")
                # Sanity check: must contain pricing markers — otherwise V9 didn't actually capture a real sticker, fall through
                v9_upper = v9_text.upper()
                v9_markers = [m for m in ("MSRP","TOTAL PRICE","SUGGESTED","AS DELIVERED PRICE","TOTAL VEHICLE PRICE","VEHICLE PRICE") if m in v9_upper]
                if v9_markers:
                    v9_parsed = _parse_sticker_text(v9_text)
                    if v9_parsed.get("total_msrp"):
                        print(f"[+{time.time()-t:5.1f}s] [ipacket] V9 SUCCESS MSRP=${v9_parsed.get('total_msrp') or 0:,} base=${v9_parsed.get('base_price') or 0:,}")
                    return {
                        "screenshot": str(v9_screenshot) if v9_screenshot.exists() else None,
                        "sticker_url": v9_url,
                        "total_msrp": v9_parsed.get("total_msrp"),
                        "base_price": v9_parsed.get("base_price"),
                        "exterior_color": v9_parsed.get("exterior_color"),
                        "interior_color": v9_parsed.get("interior_color"),
                        "raw": {"options": v9_parsed.get("options", []), "v9_path": True, "text_chars": len(v9_text)},
                    }
                else:
                    # 2026-05-10 fix: rasterized PDF stickers (Bentley, Rolls Royce, etc.)
                    # render fine visually but have no selectable text -> no markers found.
                    # If V9 captured a substantial screenshot (>=100KB), trust it as a real
                    # render. Mini-page Layer 3 fallback already handles missing parsed fields.
                    # Falling through here used to trigger the repeat-VIN rate-limit on Submit
                    # and produce a 51KB blank capture. Verified on bid 1127 (Bentayga).
                    if v9_size >= 100_000:
                        print(f"[+{time.time()-t:5.1f}s] [ipacket] V9 SUCCESS (no text markers but screenshot={v9_size:,}b — rasterized PDF, accepting visual)")
                        return {
                            "screenshot": str(v9_screenshot) if v9_screenshot.exists() else None,
                            "sticker_url": v9_url,
                            "total_msrp": None,
                            "base_price": None,
                            "exterior_color": None,
                            "interior_color": None,
                            "raw": {"options": [], "v9_path": True, "text_chars": len(v9_text), "rasterized_pdf": True},
                        }
                    print(f"[+{time.time()-t:5.1f}s] [ipacket] V9 captured but no pricing markers and screenshot={v9_size:,}b too small -- falling through to Submit flow")
                    # Restore page state best we can: navigate back to dashboard for Submit flow to work
                    try:
                        page.goto("https://dpapp.autoipacket.com/stickerpull", wait_until="domcontentloaded", timeout=15000)
                        time.sleep(2)
                    except Exception: pass
            except Exception as v9_cap_err:
                print(f"[+{time.time()-t:5.1f}s] [ipacket] V9 capture error: {v9_cap_err} -- falling through")
        else:
            print(f"[+{time.time()-t:5.1f}s] [ipacket] V9 click didn't render viewer in 22s -- falling through")
            # Make sure we're back on dashboard so Submit flow has a known starting state
            try:
                page.goto("https://dpapp.autoipacket.com/stickerpull", wait_until="domcontentloaded", timeout=15000)
                time.sleep(2)
            except Exception: pass
    # ===== END V9 fast-path =====

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
    if f != "filled":
        # 2026-05-11: capture page state for diagnostics. iPacket changes their
        # auth/UI without notice (see is_logged_in DOM rewrite). Saving a
        # screenshot + URL on every vin-input-not-found means the next break
        # is debuggable in one screenshot instead of a multi-day investigation.
        try:
            ts = int(time.time())
            dbg = REPORTS_DIR / f"vin_not_found_{vin}_{ts}.png"
            page.screenshot(path=str(dbg), full_page=True)
            print(f"[+{time.time()-t:5.1f}s] [ipacket] vin_input_not_found url={page.url!r} debug={dbg.name}")
        except Exception as _dbg:
            print(f"[+{time.time()-t:5.1f}s] [ipacket] vin_input_not_found url={page.url!r} (debug save failed: {_dbg})")
        return {"error": "vin_input_not_found"}

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
    state = "waiting"; msg = ""; canvas_size = None
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
        if st.get("size"): canvas_size = st.get("size")
        # Within the grace window, ignore "unavailable" — it's almost always
        # a stale element from the previous lookup or a permanent UI banner.
        if state == "unavailable" and (time.time() - submit_ts) < UNAVAIL_GRACE_SEC:
            state = "waiting"
        if state in ("unavailable", "ready"): break
        time.sleep(0.4)

    if state != "ready":
        # Refinement E: BEFORE giving up, check Recent Pulls — if iPacket has a
        # cached sticker for this VIN, the dashboard exposes a download link.
        # Fetch it; if PDF, navigate Chromium to it (built-in PDF viewer renders
        # text-selectable) and fall through to extraction.
        recovered = False
        try:
            recent_url = page.evaluate(r"""(v) => {
                const V = (v||'').toUpperCase();
                const rows = document.querySelectorAll('.pull-history-table-download[href]');
                for (const a of rows) {
                    const h = a.getAttribute('href') || '';
                    if (h.toUpperCase().endsWith('/' + V)) return h;
                }
                return null;
            }""", vin)
        except Exception:
            recent_url = None
        if recent_url:
            try:
                resp = page.context.request.get(recent_url, timeout=15000)
                ct = (resp.headers.get("content-type") or "").lower()
                if resp.ok and ct.startswith("application/pdf"):
                    print(f"[+{time.time()-t:5.1f}s] [ipacket] recent-pulls PDF found, recovering")
                    ts = int(time.time())
                    pdf_path = REPORTS_DIR / f"ipacket_{vin}_{ts}.pdf"
                    pdf_path.write_bytes(resp.body())
                    try:
                        page.goto(recent_url, wait_until="domcontentloaded", timeout=30000)
                        time.sleep(4)
                        recovered = True
                    except Exception as nav_ex:
                        print(f"[+{time.time()-t:5.1f}s] [ipacket] PDF nav failed: {nav_ex}")
            except Exception as fx:
                print(f"[+{time.time()-t:5.1f}s] [ipacket] recent-pulls fetch failed: {fx}")

        if not recovered:
            # Refinement D: rich grep-able diagnostic + Refinement C: screenshot in return
            ts = int(time.time())
            screenshot_path = REPORTS_DIR / f"debug_{vin}_{ts}.png"
            try:
                dbg = REPORTS_DIR / f"debug_{vin}_{ts}.html"
                dbg.write_text(page.content(), encoding="utf-8", errors="ignore")
                page.screenshot(path=str(screenshot_path), full_page=True)
            except Exception as e:
                print(f"[ipacket] debug dump failed: {e}")
            try:
                diag = page.evaluate(r"""() => {
                    const view = document.querySelector('.stickerpull-view-container');
                    const c = view ? view.querySelector('.react-pdf__Page__canvas') : null;
                    const layers = document.querySelectorAll('.react-pdf__Page__textContent, .textLayer');
                    let txt = '';
                    for (const l of layers) txt += (l.innerText || l.textContent || '') + '\n';
                    if (!txt && document.body) txt = document.body.innerText || '';
                    const f = document.querySelector('iframe.ipacket-viewer, iframe[src*="document-viewer.autoipacket.com"]');
                    return {cw: c?c.width:0, ch: c?c.height:0, text: (txt||'').slice(0, 8000), iframe: f? (f.getAttribute('src')||''): null};
                }""") or {}
            except Exception:
                diag = {}
            dtxt = (diag.get("text") or "").upper()
            MARKERS = ("MSRP","TOTAL PRICE","SUGGESTED","AS DELIVERED PRICE","TOTAL VEHICLE PRICE","VEHICLE PRICE")
            found = [m for m in MARKERS if m in dtxt]
            cw, ch = diag.get("cw") or 0, diag.get("ch") or 0
            canvas_str = f"{cw}x{ch}" if (cw or ch) else "no canvas"
            iframe_str = diag.get("iframe") or "none"
            try: ss_bytes = screenshot_path.stat().st_size if screenshot_path.exists() else 0
            except Exception: ss_bytes = 0
            print(f"[ipacket] DIAG bid={vin} canvas={canvas_str} text_chars={len(diag.get('text') or '')} markers={found} iframe_src={iframe_str} screenshot_bytes={ss_bytes}")
            return {
                "not_available": True,
                "reason": msg or "viewer_did_not_render",
                "screenshot": str(screenshot_path) if screenshot_path.exists() else None,
                "sticker_url": page.url,
            }

    print(f"[+{time.time()-t:5.1f}s] [ipacket] ready (canvas stable)")
    sticker_page = page
    time.sleep(8)  # bumped 2s -> 8s 2026-05-08 — bid 1060 captured spinner; give canvas more time to actually render the sticker PDF after JS detector says ready

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

    # Refinement B: text-marker sanity check. Refinement F (v5): if WARN
    # fires, escalate to Recent Pulls PDF fallback — the standalone viewer
    # didn't actually render but iPacket's cache may still have the sticker.
    if sticker_text:
        TXT_U = sticker_text.upper()
        MARKERS = ("MSRP","TOTAL PRICE","SUGGESTED","AS DELIVERED PRICE","TOTAL VEHICLE PRICE","VEHICLE PRICE")
        found_markers = [m for m in MARKERS if m in TXT_U]
        if not found_markers and len(sticker_text) > 200:
            print(f"[+{time.time()-t:5.1f}s] [ipacket] WARN suspect render: text_chars={len(sticker_text)} but no pricing markers found — trying Recent Pulls fallback")
            try:
                # V7: navigate dashboard, find the Recent Pulls row containing
                # this VIN, grab the "View Sticker" link, navigate sticker_page
                # to its href. The download endpoint requires a Bearer JWT we
                # don't have (returned 403). The "View Sticker" link uses the
                # dashboard's own auth flow.
                page.goto("https://dpapp.autoipacket.com/stickerpull", wait_until="domcontentloaded", timeout=20000)
                time.sleep(2)
                view_info = page.evaluate(r"""(v) => {
                    const V = (v||'').toUpperCase();
                    // First: find any element on the page whose direct text matches the VIN
                    // (don't rely on <tr>; iPacket may use divs).
                    const all = document.querySelectorAll('*');
                    for (const el of all) {
                        const t = (el.innerText || '').toUpperCase();
                        if (!t.includes(V)) continue;
                        // Pick the smallest container that has the VIN — walk down to find
                        // the tightest row-like ancestor with reasonable size.
                        if ((el.children || []).length > 30) continue; // too big
                        if (t.length > 1500) continue;
                        const tag = el.tagName;
                        // Want a row container — must contain VIN AND ideally a "view" or click-able cell
                        if (!['TR','DIV','LI','SECTION'].includes(tag)) continue;
                        // Make sure parent isn't an even smaller VIN-matching element
                        // (otherwise we get a giant wrapper)
                        return {
                            tag: tag,
                            cls: (el.className || '').slice(0,80),
                            text: (el.innerText || '').slice(0, 500),
                            html: el.outerHTML.slice(0, 3000),
                            children_count: (el.children || []).length,
                        };
                    }
                    return null;
                }""", vin)
                if view_info:
                    print(f"[+{time.time()-t:5.1f}s] [ipacket] V8 DIAG match tag={view_info.get('tag')} cls={view_info.get('cls')!r} children={view_info.get('children_count')} text={view_info.get('text')[:200]!r}")
                    print(f"[+{time.time()-t:5.1f}s] [ipacket] V8 DIAG html={view_info.get('html')!r}")
                else:
                    print(f"[+{time.time()-t:5.1f}s] [ipacket] V8 DIAG VIN not found in any element")
            except Exception as fx:
                print(f"[+{time.time()-t:5.1f}s] [ipacket] V8 fallback errored: {fx}")

    parsed = _parse_sticker_text(sticker_text) if sticker_text else {}
    if parsed.get("total_msrp"):
        _m = parsed.get("total_msrp") or 0
        _b = parsed.get("base_price") or 0
        _ec = parsed.get("exterior_color") or ""
        print(f"[+{time.time()-t:5.1f}s] [ipacket] regex MSRP=${_m:,} base=${_b:,} ext={_ec} text={len(sticker_text)} chars")

    # 2026-05-09: Permanent fix for blank-screenshot / iPacket repeat-VIN rate-limit.
    # When markers missing AND parser yielded nothing useful AND screenshot is small,
    # return not_available=True so the server doesn't store an empty success row
    # (which renders as a blank screenshot in the mini-page).
    # 2026-05-10: Made size-aware. Rasterized PDF stickers (Bentley, exotics) have
    # no selectable text but render fine visually >=100KB. Don't false-positive them.
    _txt_u = (sticker_text or "").upper()
    _has_markers = any(m in _txt_u for m in ("MSRP","TOTAL PRICE","SUGGESTED","AS DELIVERED PRICE","TOTAL VEHICLE PRICE","VEHICLE PRICE"))
    _has_data = bool(parsed.get("total_msrp") or parsed.get("base_price") or parsed.get("exterior_color") or parsed.get("interior_color"))
    try:
        _ss_bytes = screenshot.stat().st_size if (screenshot and screenshot.exists()) else 0
    except Exception:
        _ss_bytes = 0
    if not _has_markers and not _has_data and _ss_bytes < 100_000:
        if sticker_page is not page:
            try: sticker_page.close()
            except Exception: pass
        print(f"[+{time.time()-t:5.1f}s] [ipacket] BLANK-CAPTURE -> not_available (text={len(sticker_text or '')} chars, no markers, screenshot={_ss_bytes:,}b<100KB) -- likely iPacket repeat-VIN rate-limit")
        return {
            "screenshot": str(screenshot) if screenshot else None,
            "sticker_url": sticker_url,
            "not_available": True,
            "reason": "iPacket sticker did not render (blank capture). Likely repeat-VIN rate-limit; another bid for same VIN may have full data.",
        }
    if not _has_markers and not _has_data and _ss_bytes >= 100_000:
        print(f"[+{time.time()-t:5.1f}s] [ipacket] no text markers but screenshot={_ss_bytes:,}b -- accepting as rasterized-PDF visual sticker")

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
