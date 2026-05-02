"""First-time vAuto login — auto-fills creds, waits up to 5 min for 2FA,
detects success across all open tabs (Cox redirects through new pages)."""
import os
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

PROFILE_DIR = Path(r"C:\worker\vauto_profile")
PROFILE_DIR.mkdir(parents=True, exist_ok=True)

VAUTO_USERNAME = os.environ.get("VAUTO_USERNAME", "OscarPas")
VAUTO_PASSWORD = os.environ.get("VAUTO_PASSWORD", "Sedecremlun34$")

VAUTO_HOME = "https://www2.vauto.com/"
SUCCESS_HOSTS = (
    "provision.vauto.app.coxautoinc.com",
    "vauto.app.coxautoinc.com",
    "www2.vauto.com",
    "app.vauto.com",
)
SIGNIN_MARKERS = ("signin.coxautoinc", "okta", "/u/login", "bridge-id")
MAX_WAIT_SEC = 300  # 5 minutes for 2FA + trust device


def find_success_url(ctx):
    """Return any URL across all pages that's clearly post-login."""
    for pg in ctx.pages:
        try:
            url = pg.url
        except Exception:
            continue
        if not url:
            continue
        if any(h in url for h in SUCCESS_HOSTS) and not any(s in url for s in SIGNIN_MARKERS):
            return url
    return None


print("=" * 60)
print("First-time vAuto login")
print("=" * 60)
print(f"Profile: {PROFILE_DIR}")
print()
print("Script will:")
print("  1. Auto-fill username + password")
print("  2. WAIT for you to complete 2FA in the Chromium window")
print("  3. CHECK 'Trust this device' before submitting 2FA")
print("  4. Auto-detect when you reach the dashboard, then exit")
print()
print(f"Waiting up to {MAX_WAIT_SEC} seconds for the full flow.")
print("=" * 60)

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=False,
        viewport={"width": 1400, "height": 950},
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(VAUTO_HOME, wait_until="domcontentloaded", timeout=30000)
    print(f"\n[*] Opened: {page.url}")

    t0 = time.time()
    last_action = ""

    while time.time() - t0 < MAX_WAIT_SEC:
        # Check all pages for success
        success = find_success_url(ctx)
        if success:
            elapsed = int(time.time() - t0)
            print(f"\n[OK] Logged in after {elapsed}s. URL: {success}")
            print("[OK] Trusted-device cookie should be saved in profile dir.")
            try:
                target = next((pg for pg in ctx.pages if pg.url == success), page)
                target.screenshot(path=r"C:\worker\vauto_dashboard.png", full_page=False)
                print("[OK] Screenshot: C:\\worker\\vauto_dashboard.png")
            except Exception as e:
                print(f"[!] screenshot failed: {e}")
            break

        # Try to auto-fill username (Cox bridge-id 1st step)
        try:
            uf = page.query_selector(
                'input[type="email"], input[name="email"], input[name="username"], input[id="username"]'
            )
            if uf and uf.is_visible() and last_action != "username":
                uf.fill(VAUTO_USERNAME)
                btn = page.query_selector(
                    'button[type="submit"], button:has-text("Next"), button:has-text("Continue")'
                )
                if btn:
                    btn.click()
                else:
                    uf.press("Enter")
                last_action = "username"
                print(f"  [{int(time.time()-t0):3d}s] auto-filled username")
                time.sleep(2)
                continue
        except Exception:
            pass

        # Try to auto-fill password (Cox bridge-id 2nd step)
        try:
            pw = page.query_selector('input[type="password"]')
            if pw and pw.is_visible() and last_action != "password":
                pw.fill(VAUTO_PASSWORD)
                btn = page.query_selector(
                    'button[type="submit"], button:has-text("Sign in"), button:has-text("Sign In"), button:has-text("Log in")'
                )
                if btn:
                    btn.click()
                else:
                    pw.press("Enter")
                last_action = "password"
                print(f"  [{int(time.time()-t0):3d}s] auto-filled password — NOW DO 2FA + check 'Trust this device'")
                time.sleep(3)
                continue
        except Exception:
            pass

        # Print URL every 5 sec for visibility
        elapsed = int(time.time() - t0)
        if elapsed % 5 == 0:
            urls = []
            for pg in ctx.pages:
                try: urls.append(pg.url[:70])
                except Exception: pass
            print(f"  [{elapsed:3d}s] waiting... {urls}")

        time.sleep(1)
    else:
        print(f"\n[FAIL] Timeout after {MAX_WAIT_SEC}s — manual intervention needed")

    print("\n[*] Closing context (flushes cookies to disk)...")
    ctx.close()

print("\n=== DONE ===")
