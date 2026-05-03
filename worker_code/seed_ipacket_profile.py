"""One-time iPacket login into a separate profile for parallel-iPacket worker.
Opens a Chromium window — log in (opies32765@gmail.com / Sedecremlun34$),
dismiss any popups, navigate to /stickerpull, then press Enter to save."""
from playwright.sync_api import sync_playwright

PROFILE = r"C:\worker\ipacket_profile"

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        PROFILE, headless=False,
        viewport={"width": 1500, "height": 1000},
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto("https://dpapp.autoipacket.com/")
    print()
    print("=" * 60)
    print("LOGIN STEPS:")
    print("  1. Log in with opies32765@gmail.com / Sedecremlun34$")
    print("  2. Dismiss any popups / accept terms")
    print("  3. Navigate to https://dpapp.autoipacket.com/stickerpull")
    print("  4. Verify the VIN input is visible")
    print("  5. Come back here and press Enter")
    print("=" * 60)
    input("Press Enter when done to save cookies and exit...")
    ctx.close()
print("Profile saved at " + PROFILE)
