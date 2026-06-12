import shutil, py_compile, sys
P = "/opt/expwholesale/app.py"
s = open(P).read()
if "COMBINED_EXTRACTOR_FALLBACK_2026_06_11" in s:
    print("already patched"); sys.exit(0)
ANCHOR = "    # VIN_OCR_V2_2026_05_27 — closed-loop retry. Before returning the best-guess"
if ANCHOR not in s:
    sys.stderr.write("anchor not found\n"); sys.exit(2)
BLOCK = (
"    # COMBINED_EXTRACTOR_FALLBACK_2026_06_11: the strict VIN-only prompts make the\n"
"    # local 9B ramble / misread a clean char (e.g. L->1 on the photographed-screen\n"
"    # VIN in bid 2909) or self-reject to NONE, while the CARFAX JSON extractor reads\n"
"    # the SAME image correctly (verified live on 1GKS2EKLXRR400751). Use it as a\n"
"    # cross-check BEFORE the best-guess fallback; accept ONLY if the ISO-3779 check\n"
"    # digit validates, so a misread can never ship the wrong car.\n"
"    try:\n"
"        _ci = extract_carfax_info(file_bytes, media_type)\n"
"        _cv = str((_ci or {}).get('vin') or '').strip().upper()\n"
"        _cm = re.search(r'\\b[A-HJ-NPR-Z0-9]{17}\\b', _cv)\n"
"        if _cm and vin_check_digit_valid(_cm.group(0)):\n"
"            print(f'[OCR] VIN via combined CARFAX extractor (check digit OK): {_cm.group(0)}', flush=True)\n"
"            return _cm.group(0)\n"
"    except Exception as _cee:\n"
"        print(f'[OCR] combined-extractor VIN fallback err: {_cee}', flush=True)\n"
"\n"
)
s = s.replace(ANCHOR, BLOCK + ANCHOR, 1)
bak = P + ".bak.20260611-vincombined"
shutil.copy(P, bak)
open(P, "w").write(s)
try:
    py_compile.compile(P, doraise=True); print("PATCHED OK; bak", bak)
except Exception as e:
    shutil.copy(bak, P); print("COMPILE FAIL reverted:", e); sys.exit(3)
