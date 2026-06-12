import shutil, py_compile, sys
P = "/opt/expwholesale/app.py"
s = open(P).read()
if "HARDEN_OCR_TEMP_2026_06_11" in s:
    print("already hardened"); sys.exit(0)

edits = [
    # VIN hw_prompt: attempt 0 greedy (deterministic best read), attempt 1 one varied retry.
    ("                             model='gemini-2.5-pro', max_tokens=2000,\n"
     "                             temperature=0.2 + attempt * 0.3)",
     "                             model='gemini-2.5-pro', max_tokens=2000,\n"
     "                             temperature=(0.0 if attempt == 0 else 0.4))  # HARDEN_OCR_TEMP_2026_06_11"),
    # Flash VIN cross-check -> greedy.
    ("    flash_result = gemini_call(VIN_PROMPT, image_bytes=file_bytes, mime=media_type,\n"
     "                               model='gemini-2.5-flash', max_tokens=100)",
     "    flash_result = gemini_call(VIN_PROMPT, image_bytes=file_bytes, mime=media_type,\n"
     "                               model='gemini-2.5-flash', max_tokens=100, temperature=0.0)  # HARDEN_OCR_TEMP_2026_06_11"),
    # Combined CARFAX extractor -> greedy (used as the VIN fallback + by the carfax worker).
    ("    raw = gemini_call(CARFAX_PROMPT, image_bytes=file_bytes, mime=media_type,\n"
     "                      model='gemini-2.5-flash', max_tokens=3000)",
     "    raw = gemini_call(CARFAX_PROMPT, image_bytes=file_bytes, mime=media_type,\n"
     "                      model='gemini-2.5-flash', max_tokens=3000, temperature=0.0)  # HARDEN_OCR_TEMP_2026_06_11"),
]
miss = [o for o, _ in edits if o not in s]
if miss:
    sys.stderr.write("anchor(s) not found: %d\n" % len(miss)); sys.exit(2)
for o, n in edits:
    s = s.replace(o, n, 1)
bak = P + ".bak.20260611-hardentemp"
shutil.copy(P, bak)
open(P, "w").write(s)
try:
    py_compile.compile(P, doraise=True); print("HARDENED OK (3 temps -> greedy); bak", bak)
except Exception as e:
    shutil.copy(bak, P); print("COMPILE FAIL reverted:", e); sys.exit(3)
