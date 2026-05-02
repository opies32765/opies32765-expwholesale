"""First-time vAuto login on this VM.

Opens Chromium with a persistent profile at C:\\worker\\vauto_profile\\.
You complete the login + 2FA + 'trust this device' in the browser window.
Script polls the URL until you reach the vAuto dashboard, then exits.

Future runs on this VM will skip 2FA — the trusted-device cookie is
saved inside vauto_profile\\.
"""
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

PROFILE_DIR = Path(r"C:\worker\vauto_profile")
PROFILE_DIR.mkdir(parents=True, exist_ok=True)

VAUTO_URL = "https://www2.vauto.com/"
SUCCESS_HOSTS = (
    "provision.vauto.app.coxautoinc.com",
    "vauto.app.coxautoinc.com",
    "www2.vauto.com",
    "app.vauto.com",
)
# Anything matching these is still on a login flow, keep waiting:
SIGNIN_MARKERS = ("signin.coxautoinc", "okta", "/login", "bridge-id")

print("=" * 60)
print("vAuto first-time login on this VM")
print("=" * 60)
print(f"Profile: {PROFILE_DIR}")
print()
print("Credentials (from memory):")
print("  Username: OscarPas")
print("  Password: Sedecremlun34$")
print()
print("Steps in the Chromium window:")
print("  1. Enter username  -> Next")
print("  2. Enter password  -> Sign In")
print("  3. Complete 2FA challenge (SMS / auth app)")
print("  4. CHECK 'Trust this device' before submitting 2FA")
print("  5. Wait for vAuto dashboard to load")
print()
print("Script will detect success and exit automatically.")
print("=" * 60)

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=False,
        viewport={"width": 1400, "height": 950},
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(VAUTO_URL, wait_until="load")
    print(f"\n[*] Opened: {page.url}")
    print("[*] Polling for dashboard URL every 3 sec... (Ctrl+C to abort)\n")

    t0 = time.time()
    while True:
        # Cox redirects through MULTIPLE tabs — check all of them, not just the first.
        all_urls = []
        for pg in ctx.pages:
            try:
                all_urls.append(pg.url)
            except Exception:
                pass
        elapsed = int(time.time() - t0)
        primary = all_urls[0] if all_urls else "(no pages)"
        print(f"  [{elapsed:4d}s] {primary[:90]}  ({len(all_urls)} pages)")
        success_url = next((u for u in all_urls
                            if any(h in u for h in SUCCESS_HOSTS)
                            and not any(s in u for s in SIGNIN_MARKERS)), None)
        if success_url:
            print(f"\n[OK] Logged in. Final URL: {success_url}")
            print("[OK] Trusted-device cookie should now be saved in profile dir.")
            try:
                # Screenshot whichever page is actually on the dashboard
                target = next((pg for pg in ctx.pages if pg.url == success_url), page)
                target.screenshot(path=r"C:\worker\vauto_dashboard.png", full_page=False)
                print("[OK] Screenshot: C:\\worker\\vauto_dashboard.png")
            except Exception as e:
                print(f"[!] screenshot failed: {e}")
            break
        time.sleep(3)

    print("\n[*] Closing browser. Profile saved.")
    ctx.close()

print("\n=== DONE ===")
print("To re-test login persistence later, run this script again.")
print("On future runs you should land on the dashboard without 2FA.")
