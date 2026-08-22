# EW vAuto Appraisal-Save Outage & Fix — 2026-08-21 (RUNBOOK)

**Status: RESOLVED. All 8 workers patched & verified. Server-side-save rewrite considered and deliberately SHELVED (operator: "it's working, leave it").**

Copies of this doc live on: **local PC** `C:\Users\Home\EW_VAUTO_SAVE_FIX_20260821.md` · **C1** `/opt/expwholesale/ops/docs/` · **C2** (mirror) `/opt/expwholesale/ops/docs/` · **NAS** (via nightly robocopy of `C:\Users\Home` + `nas-save`).
Related: memory `project_ew_vauto_vin_decode_focusout_20260821`, `C:\Users\Home\EW_FOLLOWUPS.md` (top entry).

---

## 1. Symptom
From **2026-08-21 ~09:00** vAuto appraisals stopped saving. Bids enriched with book values (MMR/KBB) but had **no `appraisal_url`**, so `kick_direct_enrichment` bailed ("direct deferred — no appraisal_url"), no rBook competitive set, no Manheim transactions, and cars were priced on **0 rbook · 0 mmr_tx** (blind). Last good bid **6156 (08-20 17:43)**; first broken **6157 (08-21 09:00)** — a clean overnight cutover. Appeared intermittent (some bids saved) because of warm-page caching of the old front-end.

## 2. Root cause — a ProVision UI release, NOT us, NOT 2FA
Cox pushed ProVision release **`2026.8.18.173136`** to the tenant overnight 08-20→08-21. It changed the VIN input so it **commits its value to the vehicle decode only on `focusout`/`blur`**. The worker sets the VIN with a JS `setValue()` that fires `input`/`change`/`keyup` but **never `blur`** — so after the update, clicking **Go** decoded an *empty* model: Year/Make/Model stayed blank, an error modal fired, and **Save was rejected** with *"Enter the Vehicle Year, Make, Model."* → the appraisal was never created (`no_rows` server-side).

Ruled out with evidence:
- **NOT 2FA / login / self-heal:** `auto_login OK` on every worker and every probe.
- **NOT Cox's decode being broken:** their BFF decode (`POST /api/appraisal/vehicleInfo?strictYMM=true`) returns the full vehicle fine. The UI decode works in **~1 s** the instant the field is committed (blurred). The "UI decode vs API decode disagree" observation misled an earlier session toward a big server-side rewrite — the real fix is one line.
- **NOT a timing race, NOT per-worker:** waiting 30 s never populated YMM; the same VIN failed on multiple workers.

## 3. The fix — one function (`PROVISION_2026_08_18`)
In `worker_vauto.py`, `JS_HELPERS`, replace `clickGo()` so it commits the VIN **and** Odometer (focus + `change`+`blur`+`focusout`), waits 300 ms, then clicks Go. One change fixes both call sites (lines ~598 and ~891). `async` is fine — Playwright awaits the returned promise.

```javascript
  async function clickGo() {
    const app = document.querySelector('profit-time-guided-appraisal');
    if (!app || !app.shadowRoot) return false;
    // PROVISION_2026_08_18: VIN/Odometer commit to the decode only on focusout.
    // setValue() fires input/change/keyup but never blur, so Go decoded an empty
    // model and Save was rejected "Enter Year/Make/Model". Commit both first.
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
  }
```

Idempotent patcher (backs up, py_compile auto-rollback, refuses on mismatch): **`ops/scripts/patch_clickgo_vauto.py`** (this bundle). Verified end-to-end 5×: decode in ~1 s, Save accepted, real appraisals created and resolvable server-side.

## 4. Worker fleet — map & how to (re)deploy
All 8 run an identical `worker_vauto.py` (md5 `28b25e85439f2606a9c560d3b9463bd0` pre-patch). **There is NO code-sync — each box is hand-patched.**

| worker_id | host | guest | OS | path / restart |
|---|---|---|---|---|
| vm-worker-20 | pve (192.168.1.209) | CT 130 | Linux | `/opt/ewworker/code/worker_vauto.py` · `systemctl restart ewworker` |
| vm-worker-21 | 192.168.1.132 | CT 131 | Linux | same |
| vm-worker-1 | pve (.209) | **VM 9000 `win10-template`** | Windows | `C:\worker\worker_vauto.py` · `Restart-Service EWWorker` |
| vm-worker-2 | pve (.209) | VM 100 | Windows | same |
| vm-worker-7 | pve115 (.115) | VM 111 | Windows | same |
| vm-worker-6 | pve115 (.115) | VM 116 (`vm-worker-6-new`) | Windows | same |
| vm-worker-11 | pve-pc2 (.23) | VM 122 | Windows | same |
| vm-worker-13 | pve-pc2 (.23) | VM 124 | Windows | same |

Windows workers run under the NSSM service **`EWWorker`**, Python at `C:\Program Files\Python311\python.exe`. Reach them via `ssh root@<pve-host>` → `qm guest exec <vmid> -- powershell ...` (base64 `-EncodedCommand`; single-quote escaping dies across ssh→qm→PS). Linux via `pct exec <ctid>`.

**Re-apply to one worker:** push `patch_clickgo_vauto.py` to the box, run it with that box's python against its `worker_vauto.py` (prints `PATCHED OK` or `ALREADY PATCHED`), then restart the worker (NSSM `Restart-Service EWWorker` / Linux `systemctl restart ewworker`). Confirm: `PROVISION_2026_08_18` present + service active. Scratch probes that verify a real save: `vauto_save_probe*.py` / `verify_asworker.py` (in the session scratchpad; also copied to this bundle).

## 5. Rescue of the 6 blind bids (done)
6157/6158 (Karma Revero), 6162 (Porsche 718), 6164 (BMW X5), 6165 (Sentra), 6168 (Lexus GX): appraisals created via patched worker code on CT130, then resolved + POSTed to `/api/vauto/url_capture_result`. **Trap:** the URL-backfill cron has a **6-attempt-per-bid give-up**; it had exhausted it on these while the appraisals didn't yet exist, so the cron would NOT self-heal them — had to POST directly. `LATE_RBOOK_REASSESS` re-fired the price. All now have appraisal_url + rBook + Manheim.

## 6. SHELVED (do not build unless revisited): server-side save
Considered and **deliberately not built** (operator: it's working, leave it). Feasible: the save is one call — `PUT https://slot2.bff.megazord.vauto.app.coxautoinc.com/api/appraisal` (cookie-auth JSON: `appraisalId, entityId=jwaCvVdjsSF…, appraiserId=FjnHVkTmE8l…, status:"InProcess", vehicle{…from /api/appraisal/vehicleInfo}, competition{…from /api/competition/radarData}`). The `appraisalId` is a per-page-load draft minted into the `Default.aspx?new=true` HTML (scrape it; a reused default id → "422 Appraisal not found"). Building this would make the save immune to ProVision UI churn (ends the whack-a-mole). Full spec in `EW_FOLLOWUPS.md`. Caveats: reverse-engineers an internal API; real logged-in write session (same account-risk class as the iPacket rule).

## 7. Prevention notes for future sessions
- A ProVision UI release can break the browser-driven save again at any time. First check: run a save probe, watch for the "Enter the Vehicle Year, Make, Model" banner and whether the VIN decodes. If a new UI contract broke, the fix is again in `worker_vauto.py` `JS_HELPERS`, re-deployed to all 8 workers.
- Consider fetching `JS_HELPERS` from C1 at runtime so UI fixes become a one-place edit (no code-sync exists across the fleet today).
