"""One-time AccuTrade login into a separate profile for parallel-AccuTrade worker.
Opens a Chromium window — log in (opies32765@gmail.com / Sedecremlun35$),
verify the dashboard loads, then press Enter to save."""
from playwright.sync_api import sync_playwright

PROFILE = r"C:\worker\accutrade_profile"

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        PROFILE, headless=False,
        viewport={"width": 1500, "height": 1000},
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto("https://appraiser3.accu-trade.com")
    print()
    print("=" * 60)
    print("LOGIN STEPS:")
    print("  1. Log in with opies32765@gmail.com / Sedecremlun35$")
    print("  2. Wait until you see the AccuTrade dashboard")
    print("  3. Come back here and press Enter")
    print("=" * 60)
    input("Press Enter when done to save cookies and exit...")
    ctx.close()
print("Profile saved at " + PROFILE)
