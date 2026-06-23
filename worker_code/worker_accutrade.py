"""AccuTrade lookup module - dashboard-v3 HYBRID (2026-06-22).

AccuTrade's dashboard-v3 redesign (Showroom/Driveway, direct /appraisal/new route)
broke the old "+/Dealer Acquisition" DOM nav -> vin_input_not_found on every VIN.

New flow:  login -> goto /appraisal/new -> enter VIN (formcontrolname=vinInput)
  -> 9B overseer picks trim if a modal appears -> TYPE odometer (formcontrolname=
  odometer) which triggers AccuTrade's own client-side mileage math + autosave
  -> read the mileage-adjusted values via the sirius API (GET /accuprice/appraisal/{id}).

API auth = the sirius `token` header captured from the live browser's own requests
(the only thing the API needs; cookies/Bearer not required for this read). This keeps
AccuTrade's proprietary mileage adjustment (done client-side) while dropping all the
fragile DOM value-scraping. Drop-in: same lookup() signature + return shape.
"""
import os, re, time
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
SIRIUS = "https://sirius-api-production.accu-trade.com"
QP = "?u=" + EMAIL.replace("@", "%40") + "&is_mobile=false&client_platform=chrome"
LOGIN_MARKERS = ("auth0.accu-trade.com", "/u/login", "/auth/login")
SUCCESS_PATHS = ("/dashboard", "/appraisal", "/vehicle", "/home", "/index", "/performance-center")


def _ask_overseer(vin, bid_id, choices, timeout=65):
    """Ask EW's 9B overseer which trim choice to pick. Returns dict or None."""
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
    if "appraiser3.accu-trade.com" not in url:
        return False
    if any(m in url for m in LOGIN_MARKERS):
        return False
    return any(p in url for p in SUCCESS_PATHS)


def auto_login(page, ctx, max_seconds=60):
    t0 = time.time(); last = ""
    while time.time() - t0 < max_seconds:
        for pg in ctx.pages:
            try:
                if is_logged_in(pg.url):
                    return True
            except Exception:
                pass
        try:
            uf = page.query_selector('input[type="email"], input[name="email"], input[name="username"], input[id="username"]')
            if uf and uf.is_visible() and last != "user":
                uf.fill(EMAIL)
                btn = page.query_selector('button[type="submit"], button:has-text("Continue"), button:has-text("Next")')
                (btn.click() if btn else uf.press("Enter"))
                last = "user"; time.sleep(2); continue
        except Exception:
            pass
        try:
            pw = page.query_selector('input[type="password"]')
            if pw and pw.is_visible() and last != "pass":
                pw.fill(PASSWORD)
                btn = page.query_selector('button[type="submit"], button:has-text("Sign in"), button:has-text("Log in")')
                (btn.click() if btn else pw.press("Enter"))
                last = "pass"; time.sleep(3); continue
        except Exception:
            pass
        time.sleep(1)
    return False


_TRIM_SELECTORS = ("new-appraisal-trim-choice", ".new-appraisal-trim-choice",
                   "article.select-container", "[role='option']")


def _scrape_trim_choices(page):
    """Shadow-DOM-piercing scrape of the trim-choice modal (locator auto-pierces)."""
    out = []
    try:
        for sel in _TRIM_SELECTORS:
            loc = page.locator(sel)
            try:
                n = loc.count()
            except Exception:
                continue
            for i in range(n):
                el = loc.nth(i)
                try:
                    if not el.is_visible(timeout=300):
                        continue
                    txt = (el.text_content(timeout=600) or "").strip()
                except Exception:
                    continue
                txt = re.sub(r"\s+", " ", txt)
                txt = re.sub(r"\s*(?:keyboard_arrow_right|chevron_right|expand_more|more_vert|arrow_forward)\s*$",
                             "", txt, flags=re.IGNORECASE).strip()
                if txt and len(txt) < 200:
                    out.append({"index": len(out), "dom_index": i, "text": txt, "_sel": sel})
            if out:
                break
    except Exception:
        pass
    return out


def _pick_trim(choices, vin, bid_id, trim):
    ov = _ask_overseer(vin, bid_id, [{"index": c["index"], "text": c["text"]} for c in choices])
    if ov and ov.get("index") is not None:
        i = int(ov["index"])
        txt = ov.get("text") or (choices[i]["text"] if 0 <= i < len(choices) else None)
        return i, txt, (ov.get("source") or "llm")
    if trim:
        for c in choices:
            if trim.lower() in c["text"].lower():
                return c["index"], c["text"], "fuzzy_hint"
    return 0, choices[0]["text"], "first_visible"


def _click_trim(page, choices, idx):
    c = choices[idx] if 0 <= idx < len(choices) else choices[0]
    try:
        tgt = page.locator(c["_sel"]).nth(c["dom_index"])
        tgt.scroll_into_view_if_needed(timeout=2000)
        tgt.click(timeout=5000)
    except Exception as e:
        print(f"[accutrade-v3] trim click err: {e}")


def _read_accuprice(page, aid, token):
    try:
        r = page.request.get(
            SIRIUS + "/accuprice/appraisal/" + str(aid) + QP,
            headers={"token": token or "", "accept": "application/json, text/plain, */*",
                     "origin": ACCUTRADE_URL, "referer": ACCUTRADE_URL + "/"})
        if r.ok:
            d = r.json()
            return d[0] if isinstance(d, list) and d else (d if isinstance(d, dict) else None)
    except Exception as e:
        print(f"[accutrade-v3] accuprice read err: {e}")
    return None


def lookup(page, ctx, vin, miles, t, trim=None, bid_id=None):
    print(f"[+{time.time()-t:5.1f}s] [accutrade-v3] start vin={vin} miles={miles}")
    tok = {"v": None}
    def _grab(req):
        try:
            if "sirius-api" in req.url and not tok["v"]:
                tt = req.headers.get("token")
                if tt:
                    tok["v"] = tt
        except Exception:
            pass
    try:
        ctx.on("request", _grab)
    except Exception:
        pass

    page.goto(ACCUTRADE_URL, wait_until="domcontentloaded", timeout=30000)
    if not is_logged_in(page.url):
        if not auto_login(page, ctx):
            return {"error": "auto_login_failed"}
    page = next((pg for pg in ctx.pages if is_logged_in(pg.url)), page)

    # 1) direct route to the new-appraisal page (dashboard-v3)
    page.goto(ACCUTRADE_URL + "/appraisal/new", wait_until="domcontentloaded", timeout=30000)
    try:
        vin_loc = page.locator('input[formcontrolname="vinInput"], input[placeholder="Enter VIN"]').first
        vin_loc.wait_for(timeout=20000)
    except Exception:
        return {"error": "vin_input_not_found"}
    vin_loc.click(); vin_loc.fill(vin); time.sleep(0.4); vin_loc.press("Enter")

    # 2) reach the appraisal: trim modal (overseer picks) or direct nav.
    # Re-click the chosen trim each pass until it navigates (some trims need 2 clicks /
    # the modal debounces) and also click any Continue/Appraise button that appears.
    selected_trim_text = None; trim_select_source = None; aid = None
    chosen_txt = None; chosen_idx = 0
    deadline = time.time() + 45
    while time.time() < deadline:
        m = re.search(r"/appraisal/(\d+)", page.url or "")
        if m and "/new" not in page.url:
            aid = m.group(1); break
        choices = _scrape_trim_choices(page)
        if choices:
            if chosen_txt is None:
                chosen_idx, selected_trim_text, trim_select_source = _pick_trim(choices, vin, bid_id, trim)
                chosen_txt = selected_trim_text
                print(f"[+{time.time()-t:5.1f}s] [accutrade-v3] trim[{chosen_idx}]='{selected_trim_text}' src={trim_select_source}")
            tgt = next((c for c in choices if c["text"] == chosen_txt), None)
            if tgt is None and 0 <= chosen_idx < len(choices):
                tgt = choices[chosen_idx]
            _click_trim(page, [tgt or choices[0]], 0)
            time.sleep(1.2)
        else:
            # no trim modal showing — nudge any Continue/Appraise/Start button
            try:
                page.get_by_role("button", name=re.compile(r"continue|appraise|next|start", re.I)).first.click(timeout=1000)
            except Exception:
                pass
            time.sleep(0.5)
    if not aid:
        return {"error": "appraisal_not_created"}
    print(f"[+{time.time()-t:5.1f}s] [accutrade-v3] appraisal id={aid}")

    # 3) type odometer -> AccuTrade's client-side mileage compute + autosave
    if miles and int(miles) > 0:
        try:
            odo = page.locator('input[formcontrolname="odometer"]').first
            odo.wait_for(timeout=15000); odo.scroll_into_view_if_needed()
            odo.click(); odo.press("Control+a"); odo.press("Delete")
            odo.type(str(int(miles)), delay=60); odo.press("Tab")
            time.sleep(6)
        except Exception as e:
            print(f"[+{time.time()-t:5.1f}s] [accutrade-v3] odometer set failed: {e}")

    # 4) read mileage-adjusted values via the sirius API
    row = _read_accuprice(page, aid, tok["v"])
    if not row or row.get("trade") is None:
        time.sleep(3)
        row = _read_accuprice(page, aid, tok["v"])
    appraisal_url = f"{ACCUTRADE_URL}/appraisal/{aid}"
    if not row or row.get("trade") is None:
        return {"error": "values_unavailable", "appraisal_url": appraisal_url,
                "selected_trim_text": selected_trim_text, "trim_select_source": trim_select_source}

    # screenshot of the appraisal page for the bid card
    ts = int(time.time()); screenshot = REPORTS_DIR / f"accutrade_{vin}_{ts}.png"
    try:
        page.screenshot(path=str(screenshot), full_page=True)
        try:
            from PIL import Image
            img = Image.open(screenshot); w, h = img.size
            img.crop((0, 0, int(w * 0.85), h)).save(screenshot, optimize=True)
        except Exception:
            pass
    except Exception:
        screenshot = None

    print(f"[+{time.time()-t:5.1f}s] [accutrade-v3] done trade={row.get('trade')} "
          f"market={row.get('market')} retail={row.get('targetRetail')} odo={row.get('odometer')}")
    return {
        "guaranteed_offer": row.get("trade"),
        "trade_in": row.get("market"),
        "trade_market": None,
        "retail": row.get("targetRetail"),
        "market_avg": None,
        "screenshot": str(screenshot) if screenshot else None,
        "appraisal_url": appraisal_url,
        "selected_trim_text": selected_trim_text,
        "trim_select_source": trim_select_source,
        "raw_json": row,
    }
