"""Patch worker_vauto.py: make clickGo() commit the VIN (focus/change/blur/
focusout) before clicking Go. Fixes ProVision 2026.8.18 which stopped decoding
JS-injected VINs -> Save failed "Enter Year/Make/Model" -> appraisal never saved.

Idempotent. Backs up. Verifies the result still compiles and that exactly one
clickGo definition remains, now async with the marker.
Usage: patch_clickgo.py /opt/ewworker/code/worker_vauto.py
"""
import io, os, py_compile, shutil, sys, time

PATH = sys.argv[1] if len(sys.argv) > 1 else '/opt/ewworker/code/worker_vauto.py'
MARKER = 'PROVISION_2026_08_18'

OLD = """  function clickGo() {
    const app = document.querySelector('profit-time-guided-appraisal');
    if (!app || !app.shadowRoot) return false;
    const btn = app.shadowRoot.querySelector('#vehicle-info-go');
    if (!btn) return false;
    btn.click();
    return true;
  }"""

NEW = """  async function clickGo() {
    const app = document.querySelector('profit-time-guided-appraisal');
    if (!app || !app.shadowRoot) return false;
    // PROVISION_2026_08_18: the VIN/Odometer fields now commit to the vehicle
    // decode only on focusout. setValue() fires input/change/keyup but never
    // blur, so Go decoded nothing and Save failed "Enter the Vehicle Year,
    // Make, Model" -> the appraisal was never saved. Also guards the odometer
    // committing so miles can't silently land null. Commit both, then click Go.
    for (const _lab of ['VIN', 'Odometer']) {
      const _el = findByLabel(_lab);
      if (_el) {
        _el.focus();
        _el.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
        _el.dispatchEvent(new FocusEvent('blur', { bubbles: false, composed: true }));
        _el.dispatchEvent(new FocusEvent('focusout', { bubbles: true, composed: true }));
      }
    }
    await new Promise(function (r) { setTimeout(r, 300); });
    const btn = app.shadowRoot.querySelector('#vehicle-info-go');
    if (!btn) return false;
    btn.click();
    return true;
  }"""


def main():
    with io.open(PATH, 'r', encoding='utf-8') as f:
        src = f.read()
    if MARKER in src:
        print('ALREADY PATCHED (%s present) — no change' % MARKER)
        return 0
    if OLD not in src:
        print('ERROR: exact clickGo block not found — refusing to patch %s' % PATH)
        return 2
    if src.count(OLD) != 1:
        print('ERROR: expected exactly 1 clickGo block, found %d' % src.count(OLD))
        return 3
    bak = PATH + '.bak.provisionfix.' + time.strftime('%Y%m%d-%H%M%S')
    shutil.copy2(PATH, bak)
    out = src.replace(OLD, NEW)
    with io.open(PATH, 'w', encoding='utf-8') as f:
        f.write(out)
    try:
        py_compile.compile(PATH, doraise=True)
    except py_compile.PyCompileError as e:
        shutil.copy2(bak, PATH)
        print('ERROR: patched file does not compile — ROLLED BACK. %s' % e)
        return 4
    assert out.count('function clickGo') == 1, 'clickGo count wrong after patch'
    assert MARKER in out
    print('PATCHED OK. backup=%s' % bak)
    return 0


if __name__ == '__main__':
    sys.exit(main())
