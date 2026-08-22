"""Verify the PATCHED worker_vauto.py in-file: inject the real (now-patched)
JS_HELPERS and do exactly what the worker does -- setValue(VIN,Odo) then
clickGo() -- then check the decode fired and Save is accepted. No overrides.
Usage: verify_asworker.py VIN MILES
"""
import os, shutil, sys, time
os.environ.setdefault('EW_WORKER_ROOT', '/opt/ewworker')
sys.path.insert(0, '/opt/ewworker/code')
from playwright.sync_api import sync_playwright
import worker_vauto as WV

assert 'PROVISION_2026_08_18' in WV.JS_HELPERS, 'JS_HELPERS is NOT patched!'
SRC = '/opt/ewworker/vauto_profile'
PROFILE = '/tmp/vauto_verify_profile'
VIN = sys.argv[1] if len(sys.argv) > 1 else 'JTEFU5JR4R5314206'
MILES = sys.argv[2] if len(sys.argv) > 2 else '21266'
T0 = time.time()


def say(*a):
    print('[+%6.1fs]' % (time.time() - T0), *a, flush=True)


YEAR_JS = "() => { const e = window.__vauto.findByLabel('Year'); return e ? String(e.value) : null; }"
BANNER_JS = r"""() => { const a = document.querySelector('profit-time-guided-appraisal');
  if (!a || !a.shadowRoot) return '';
  const b = a.shadowRoot.querySelector('vauto-appraisal-user-action-banner');
  return b ? (b.innerText || b.textContent || '').replace(/\s+/g,' ').trim().slice(0,120) : ''; }"""


def main():
    if os.path.exists(PROFILE):
        shutil.rmtree(PROFILE, ignore_errors=True)
    shutil.copytree(SRC, PROFILE, symlinks=True, ignore_dangling_symlinks=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=PROFILE, headless=False, viewport={"width": 1400, "height": 950},
            user_agent=('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                        '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'),
            locale="en-US", timezone_id="America/New_York",
            args=["--disable-blink-features=AutomationControlled", "--no-first-run", "--no-default-browser-check"])
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(WV.VAUTO_APPRAISAL, wait_until='domcontentloaded', timeout=40000)
            time.sleep(4)
            if 'signin' in page.url:
                WV.auto_login(page, ctx)
                page.goto(WV.VAUTO_APPRAISAL, wait_until='domcontentloaded', timeout=40000)
                time.sleep(5)
            page.add_script_tag(content=WV.JS_HELPERS)
            page.wait_for_function("() => window.__vauto.findByLabel('VIN') != null", timeout=20000)
            page.wait_for_function("() => window.__vauto.findByLabel('Odometer') != null", timeout=20000)
            page.evaluate("""(d) => {
                window.__vauto.setValue(window.__vauto.findByLabel('VIN'), d.vin);
                window.__vauto.setValue(window.__vauto.findByLabel('Odometer'), d.miles);
            }""", {"vin": VIN, "miles": MILES})
            say('clickGo ->', page.evaluate("() => window.__vauto.clickGo()"))
            ok = False
            for i in range(12):
                time.sleep(1)
                if page.evaluate(YEAR_JS):
                    ok = True
                    say('DECODE OK after %ds; Year=%s' % (i + 1, page.evaluate(YEAR_JS)))
                    break
            if not ok:
                say('DECODE FAILED; banner=%r' % page.evaluate(BANNER_JS))
                print('VERIFY: FAIL')
                return
            for i in range(10):
                r = page.evaluate("() => window.__vauto.dismissDuplicate()")
                if r == 'ignored':
                    time.sleep(3); break
                if r == 'none':
                    break
                time.sleep(1)
            page.add_script_tag(content=WV.JS_HELPERS)
            page.evaluate("() => window.__vauto.clickActions()")
            time.sleep(1.5)
            page.evaluate("() => window.__vauto.clickSave()")
            time.sleep(6)
            banner = page.evaluate(BANNER_JS)
            say('after Save banner=%r' % banner)
            print('VERIFY:', 'PASS' if (ok and 'Year' not in banner) else 'FAIL')
        finally:
            try:
                ctx.close()
            except Exception:
                pass
    shutil.rmtree(PROFILE, ignore_errors=True)


if __name__ == '__main__':
    main()
