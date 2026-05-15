"""Patch vauto_verifier.py mid-loop recovery to try pool inject before auto_login.
Idempotent: detects already-patched state and skips."""
import sys, shutil, time

PATH = r"C:\verifier\vauto_verifier.py"

with open(PATH, "r", encoding="utf-8") as f:
    src = f.read()

MARKER = "# POOL_RECOVERY_PATCH_2026_05_15"
if MARKER in src:
    print("already patched, skipping")
    sys.exit(0)

OLD = """                    if 'redirected to login' in err_str.lower():
                        print('    -> Cox session expired mid-loop; '
                              'attempting auto-login + retry')
                        try:
                            if auto_login(driver, timeout=45.0):
                                print('    -> auto-login OK, retrying appraisal')"""

NEW = """                    if 'redirected to login' in err_str.lower():
                        # POOL_RECOVERY_PATCH_2026_05_15: try pool first so
                        # we don't burn the 2FA budget on every Cox TTL.
                        print('    -> Cox session expired mid-loop; '
                              'trying pool inject -> auto-login fallback')
                        _relogged = False
                        try:
                            if _inject_cookies_from_pool(driver):
                                print('    -> pool inject OK, retrying appraisal')
                                _relogged = True
                        except Exception as _pe:
                            print(f'    -> pool inject errored: '
                                  f'{type(_pe).__name__}: {_pe}')
                        try:
                            if _relogged or auto_login(driver, timeout=45.0):
                                if not _relogged:
                                    print('    -> auto-login OK, retrying appraisal')"""

if OLD not in src:
    sys.stderr.write("OLD block not found exactly; refusing to patch\n")
    sys.exit(2)

bak = PATH + ".bak.20260515-poolrecovery"
shutil.copy(PATH, bak)
src2 = src.replace(OLD, NEW, 1)
with open(PATH, "w", encoding="utf-8") as f:
    f.write(src2)
print(f"patched: {len(src)} -> {len(src2)} bytes (+{len(src2)-len(src)})")
print(f"backup: {bak}")
