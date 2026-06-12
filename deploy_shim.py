"""EW_SHIM_DEPLOY_2026_06_11: wire local_brain_shim into every process that calls
google.genai. Backs up + compiles each file; auto-reverts that file on compile failure.

app.py     : replace the dead _local_brain_call helper with `import local_brain_shim`,
             and remove the now-redundant inner gemini_call wrapper block (the shim is
             the single routing layer; it also gets the image-resized contents + browser UA).
others     : insert `import local_brain_shim` after any __future__/docstring header.
Covered call sites (all bottom out at Models.generate_content): gemini_call, gemini_text,
the direct assessment + ai_critique calls, dealer_intel_*, discover_dealer, voice's _gemini.
"""
import shutil, py_compile, sys, os

IMP = "import local_brain_shim  # EW_SHIM_2026_06_11: route ALL genai generate_content -> 9B brain, Gemini fallback"
BAKSFX = ".bak.20260611-shim"

def _save(path, s):
    bak = path + BAKSFX
    if not os.path.exists(bak):
        shutil.copy(path, bak)
    open(path, "w").write(s)
    try:
        py_compile.compile(path, doraise=True)
        return True, bak
    except Exception as e:
        shutil.copy(bak, path)
        return False, "COMPILE FAIL reverted: %s" % e

def patch_app(path):
    s = open(path).read()
    if "import local_brain_shim" in s and "EW_LOCAL_BRAIN_2026_06_11: try local" not in s:
        return "app: already patched"
    # 1) helper def -> shim import
    a = s.find("def _local_brain_call(prompt")
    b = s.find("def gemini_call(prompt")
    if a != -1 and b != -1 and a < b:
        s = s[:a] + IMP + "\n\n\n" + s[b:]
    elif "import local_brain_shim" not in s:
        return "app: ERROR anchors (_local_brain_call/gemini_call) not found"
    # 2) remove inner wrapper block inside gemini_call (keep `    import time as _time`)
    st = s.find("    # EW_LOCAL_BRAIN_2026_06_11: try local Qwen brain first")
    en = s.find("    import time as _time")
    if st != -1 and en != -1 and st < en:
        s = s[:st] + s[en:]
    if "import local_brain_shim" not in s:
        return "app: ERROR shim import missing after patch"
    ok, info = _save(path, s)
    return "app: %s (%s)" % ("OK" if ok else "FAIL", info)

def patch_simple(path):
    if not os.path.exists(path):
        return "%s: MISSING (skip)" % path
    s = open(path).read()
    if "import local_brain_shim" in s:
        return "%s: already" % path
    lines = s.split("\n")
    n = len(lines)
    i = 0
    if i < n and lines[i].startswith("#!"):
        i += 1
    # skip leading blank / comment lines (encoding cookie etc.)
    while i < n and (lines[i].strip() == "" or lines[i].lstrip().startswith("#")):
        i += 1
    # skip a module docstring if present
    if i < n:
        ls = lines[i].lstrip()
        for q in ('"""', "'''"):
            if ls.startswith(q):
                body = ls[3:]
                if body.count(q) >= 1:          # single-line docstring
                    i += 1
                else:
                    i += 1
                    while i < n and q not in lines[i]:
                        i += 1
                    i += 1                       # past the closing line
                break
    # skip __future__ imports (must remain first); insert after the last one
    j = i
    while j < n:
        st = lines[j].strip()
        if st.startswith("from __future__"):
            i = j + 1
        elif st == "" or st.startswith("#"):
            pass
        else:
            break
        j += 1
    lines.insert(i, IMP)
    ok, info = _save(path, "\n".join(lines))
    return "%s: %s (%s)" % (path, "OK" if ok else "FAIL", info)

os.chdir("/opt/expwholesale")
print(patch_app("app.py"))
for f in ("gemini_helper.py", "dealer_intel_newsletter.py", "dealer_intel_summary.py", "discover_dealer.py"):
    print(patch_simple(f))
