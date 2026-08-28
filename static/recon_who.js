/* RECON_WHO_2026_08_27 — the one-time "who's using this device" name picker.
 *
 * Everyone still signs in with the single shared EW credential. This asks each
 * BROWSER once who is on it, POSTs the answer to /api/recon/who (which sets a
 * year-long `ew_who` cookie), and from then on _actor() in recon_routes.py
 * stamps that name onto recon_step_events.moved_by / recon_audit.actor instead
 * of "operator".
 *
 * Deliberately a BROWSER gate, not a server one: Anna (voice), lsl_sync,
 * sheet_ingest and auto:recon-home write their own actor through other paths,
 * and a "no cookie, no move" rule on the endpoint would break all four.
 *
 * Fails OPEN. If /api/recon/who errors the board behaves exactly as it did
 * before — a name tag is never allowed to stand between anyone and a car.
 */
(function () {
  'use strict';
  if (window.__ewWho) { return; }
  window.__ewWho = true;

  var NAMES = [], current = null, scrim = null, chip = null, sw = null;

  function el(tag, cls, txt) {
    var n = document.createElement(tag);
    if (cls) { n.className = cls; }
    if (txt !== undefined) { n.textContent = txt; }
    return n;
  }

  /* same shape as _who_clean() server-side; the server is the authority */
  function clean(s) {
    s = (s || '').trim().slice(0, 40);
    return /^[A-Za-z0-9 .'-]+$/.test(s) ? s : '';
  }

  /* ── styles (injected so no recon template has to be restyled) ───────── */
  function styles() {
    var css = [
      '.ewwho-scrim{position:fixed;inset:0;background:rgba(23,27,31,.62);z-index:9000;',
        'display:grid;place-items:center;padding:18px;',
        'font:14px/1.5 -apple-system,"Segoe UI",Roboto,Arial,sans-serif}',
      '.ewwho-card{width:100%;max-width:430px;background:#fff;color:#1f2937;',
        'border-radius:8px;overflow:hidden;box-shadow:0 18px 48px rgba(15,20,25,.4)}',
      '.ewwho-cap{height:4px;background:#e23b3b}',
      '.ewwho-pad{padding:20px 22px}',
      '.ewwho-card h4{margin:0 0 7px;font-size:16.5px;font-weight:800}',
      '.ewwho-sub{margin:0 0 16px;font-size:13px;line-height:1.5;color:#4b5563}',
      '.ewwho-names{display:flex;flex-wrap:wrap;gap:7px}',
      '.ewwho-names button{font:600 13px/1 inherit;color:#1f2937;background:#fff;',
        'border:1px solid #d8dde2;padding:9px 13px;border-radius:5px;cursor:pointer}',
      '.ewwho-names button:hover{border-color:#e23b3b;color:#c0392b;background:#fdf5f5}',
      '.ewwho-names button:focus-visible{outline:2px solid #e23b3b;outline-offset:2px}',
      '.ewwho-names button.ewwho-other{color:#6b7280;border-style:dashed}',
      '.ewwho-other-box{display:none;gap:7px;margin-top:11px}',
      '.ewwho-other-box.on{display:flex}',
      '.ewwho-other-box input{flex:1;min-width:0;font:14px inherit;color:#1f2937;',
        'padding:9px 11px;border:1px solid #d8dde2;border-radius:5px;background:#fff}',
      '.ewwho-other-box input:focus{outline:none;border-color:#e23b3b}',
      '.ewwho-other-box button{font:700 13px/1 inherit;color:#fff;background:#c0392b;',
        'border:none;padding:0 16px;border-radius:5px;cursor:pointer}',
      '.ewwho-foot{border-top:1px solid #eef0f2;padding:11px 22px;background:#fafbfc;',
        'font-size:11.5px;color:#9ca3af}',
      '.ewwho-err{margin:10px 0 0;font-size:12px;color:#c0392b}',
      /* the chip */
      '.ewwho-chip{display:inline-flex;align-items:center;gap:6px;border:1px solid #4b5056;',
        'background:#22262a;padding:4px 9px 4px 7px;border-radius:6px;color:#fff;',
        'font:600 11.5px/1 -apple-system,"Segoe UI",Roboto,Arial,sans-serif;',
        'cursor:pointer;white-space:nowrap;vertical-align:middle}',
      '.ewwho-chip:hover{border-color:#6b7280}',
      '.ewwho-chip:focus-visible{outline:2px solid #e23b3b;outline-offset:2px}',
      '.ewwho-chip .ewwho-ini{width:18px;height:18px;border-radius:50%;background:#e23b3b;',
        'display:grid;place-items:center;font-size:9.5px;font-weight:800;color:#fff}',
      '.ewwho-chip .ewwho-car{color:#9ca3af;font-size:9px}',
      '.ewwho-chip.ewwho-float{position:fixed;top:11px;right:14px;z-index:8900}',
      /* switch menu */
      '.ewwho-switch{position:fixed;z-index:8950;background:#fff;border:1px solid #d8dde2;',
        'border-radius:6px;box-shadow:0 10px 26px rgba(15,20,25,.24);padding:6px;',
        'display:none;flex-direction:column;gap:1px;min-width:150px;max-height:60vh;',
        'overflow-y:auto;font:14px -apple-system,"Segoe UI",Roboto,Arial,sans-serif}',
      '.ewwho-switch.on{display:flex}',
      '.ewwho-switch .ewwho-lbl{font-size:10px;font-weight:700;letter-spacing:.5px;',
        'text-transform:uppercase;color:#9ca3af;padding:5px 9px 6px}',
      '.ewwho-switch button{font:600 13px/1 inherit;text-align:left;color:#1f2937;',
        'background:none;border:none;padding:8px 9px;border-radius:4px;cursor:pointer}',
      '.ewwho-switch button:hover{background:#f3f4f6}',
      '.ewwho-switch button.cur{color:#c0392b}',
      '.ewwho-switch button.cur:after{content:" \\2713";color:#e23b3b}'
    ].join('');
    var s = el('style');
    s.appendChild(document.createTextNode(css));
    document.head.appendChild(s);
  }

  /* ── save ────────────────────────────────────────────────────────────── */
  function save(name, onErr) {
    return fetch('/api/recon/who', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name })
    }).then(function (r) { return r.json(); }).then(function (j) {
      if (!j || !j.ok) { throw new Error((j && j.error) || 'save failed'); }
      current = j.current;
      if (j.names) { NAMES = j.names; }
      if (scrim) { scrim.remove(); scrim = null; }
      showChip();
    })['catch'](function (e) { if (onErr) { onErr(e); } });
  }

  /* ── the one-time picker ─────────────────────────────────────────────── */
  function showPicker() {
    scrim = el('div', 'ewwho-scrim');
    var card = el('div', 'ewwho-card');
    card.setAttribute('role', 'dialog');
    card.setAttribute('aria-modal', 'true');
    card.appendChild(el('div', 'ewwho-cap'));

    var pad = el('div', 'ewwho-pad');
    pad.appendChild(el('h4', null, 'One-time setup'));
    pad.appendChild(el('p', 'ewwho-sub',
      "We're now tagging who moves each car. Tap your name — " +
      "you'll only be asked once on this device."));

    var wrap = el('div', 'ewwho-names');
    var err = el('p', 'ewwho-err');
    err.style.display = 'none';

    function fail(e) {
      err.textContent = "Couldn't save that — check your connection and tap again.";
      err.style.display = 'block';
    }

    NAMES.forEach(function (n) {
      var b = el('button', null, n);
      b.type = 'button';
      b.addEventListener('click', function () { save(n, fail); });
      wrap.appendChild(b);
    });

    var box = el('div', 'ewwho-other-box');
    var inp = el('input');
    inp.type = 'text';
    inp.maxLength = 40;
    inp.placeholder = 'Type your name';
    inp.autocomplete = 'off';
    var go = el('button', null, 'Save');
    go.type = 'button';
    function saveOther() {
      var v = clean(inp.value);
      if (!v) { inp.focus(); return; }
      save(v, fail);
    }
    go.addEventListener('click', saveOther);
    inp.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); saveOther(); }
    });
    box.appendChild(inp);
    box.appendChild(go);

    var other = el('button', 'ewwho-other', '+ someone else');
    other.type = 'button';
    other.addEventListener('click', function () {
      box.classList.add('on');
      inp.focus();
    });
    wrap.appendChild(other);

    pad.appendChild(wrap);
    pad.appendChild(box);
    pad.appendChild(err);
    card.appendChild(pad);
    card.appendChild(el('div', 'ewwho-foot',
      'Not a password — just a name tag, so the board can show who did what.'));
    scrim.appendChild(card);
    document.body.appendChild(scrim);
  }

  /* ── the chip + switch menu ──────────────────────────────────────────── */
  function showChip() {
    if (chip) { chip.remove(); }
    chip = el('button', 'ewwho-chip');
    chip.type = 'button';
    chip.setAttribute('aria-haspopup', 'true');
    chip.setAttribute('aria-expanded', 'false');
    chip.setAttribute('title', 'Signed in as ' + current + ' on this device — tap to switch');
    chip.appendChild(el('span', 'ewwho-ini', current.charAt(0).toUpperCase()));
    chip.appendChild(el('span', null, current));
    chip.appendChild(el('span', 'ewwho-car', '▾'));

    /* sit inside the recon topbar where there is one; float otherwise */
    var host = document.querySelector('.top-right') || document.querySelector('.topbar');
    if (host) {
      if (host.classList.contains('top-right')) {
        host.insertBefore(chip, host.firstChild);
      } else {
        chip.style.marginLeft = 'auto';
        host.appendChild(chip);
      }
    } else {
      chip.classList.add('ewwho-float');
      document.body.appendChild(chip);
    }

    chip.addEventListener('click', function (e) {
      e.stopPropagation();
      toggleSwitch();
    });
  }

  function toggleSwitch() {
    if (sw && sw.classList.contains('on')) { closeSwitch(); return; }
    if (sw) { sw.remove(); }
    sw = el('div', 'ewwho-switch');
    sw.appendChild(el('span', 'ewwho-lbl', 'Not you? Switch'));
    NAMES.forEach(function (n) {
      var b = el('button', n === current ? 'cur' : null, n);
      b.type = 'button';
      b.addEventListener('click', function (e) {
        e.stopPropagation();
        closeSwitch();
        if (n !== current) { save(n); }
      });
      sw.appendChild(b);
    });
    sw.addEventListener('click', function (e) { e.stopPropagation(); });
    document.body.appendChild(sw);

    var r = chip.getBoundingClientRect();
    sw.style.top = (r.bottom + 6) + 'px';
    sw.style.right = Math.max(8, window.innerWidth - r.right) + 'px';
    sw.classList.add('on');
    chip.setAttribute('aria-expanded', 'true');
  }

  function closeSwitch() {
    if (sw) { sw.classList.remove('on'); }
    if (chip) { chip.setAttribute('aria-expanded', 'false'); }
  }

  document.addEventListener('click', closeSwitch);
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { closeSwitch(); }
  });

  /* ── boot ────────────────────────────────────────────────────────────── */
  function boot() {
    fetch('/api/recon/who', { headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (!j) { return; }
        NAMES = j.names || [];
        current = j.current || null;
        styles();
        if (current) { showChip(); } else if (NAMES.length) { showPicker(); }
      })['catch'](function () { /* fail open — board works exactly as before */ });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
