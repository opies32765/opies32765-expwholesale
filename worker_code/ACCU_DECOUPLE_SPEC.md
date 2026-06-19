# Decouple AccuTrade into a first-class, independent worker (operator-confirmed 2026-06-18)

**Operator intent:** AccuTrade was NEVER meant to be bundled with vAuto. It has its own
script (`worker_accutrade.py`), own browser profile (`C:\worker\accutrade_profile`), own
queue (`/api/accutrade/pending`) and own runner (`process_one_accutrade`). A speed
optimization folded all 3 legs into `process_bid.py` (vauto+accutrade+ipacket as parallel
threads under ONE `vauto` worker_job). That bundling is the regression: when the api_mode
vAuto job closes in ~45s, the slow AccuTrade browser leg gets **orphaned** (bid 3510: no
AccuTrade for 20 min until a manual reprocess; the dedicated runner never ran because
`accutrade_retry_at` was never armed). **Restore AccuTrade as its own worker.**

## Dependency model (IMPORTANT — AccuTrade is decoupled from vAuto's lifecycle, NOT from evidence)
AccuTrade's **trim-select** must run with evidence so the 9B picks the right trim:
- **Carfax trim must have surfaced** (Carfax is the primary trim signal), AND
- **iPacket, if it's going to be available**, should be present (MSRP/options help disambiguate).
This is the existing `ACCUTRADE_EVIDENCE_WAIT_SECONDS` behavior — preserve it, but make it
AccuTrade's OWN wait (gate the trim-select on Carfax(+iPacket) evidence), not a side effect
of being bundled with vAuto. AccuTrade does NOT wait on vAuto at all.

Graph:
- vAuto  → fully independent (books/MMR).
- Carfax → surfaces trim → feeds AccuTrade trim-select.
- iPacket→ if available, feeds AccuTrade trim-select + its own card.
- AccuTrade → own worker; gates trim-select on Carfax(+iPacket) evidence; independent of vAuto.

## ⚠ Atomicity (must ship together or you double-pull AccuTrade)
Turning on AccuTrade's own queue while it's STILL in the `process_bid` bundle = AccuTrade runs
twice per bid (two vendor appraisals). The two halves below MUST land together.

## Changes

### A. C1 (app.py) — feed the dedicated runner at intake (my half)
1. **Enqueue at intake:** when a bid gets a 17-char VIN (SMS intake + Quick Drop + bulk),
   mark it for AccuTrade (e.g. set `accutrade_retry_at = NOW()` or a dedicated
   `accutrade_pending` flag) so `/api/accutrade/pending` serves it as the PRIMARY path —
   not retry-only.
2. **Promote `/api/accutrade/pending`** (app.py ~16437) from retry-only to primary: serve a
   bid when it has a VIN, no usable AccuTrade row yet, is **not** claimed, AND the
   **trim evidence is ready** — Carfax trim present (and iPacket terminal if it's coming).
   Keep the existing `NOT EXISTS (accutrade with a value / terminal NA)` guard so a completed
   AccuTrade is never re-served (no double pull).
3. **Trim-evidence gate in the pending query:** only return the bid once Carfax trim is
   available (+ iPacket if available) — mirrors `ACCUTRADE_EVIDENCE_WAIT_SECONDS`, with a
   ceiling (e.g. 40s) so a missing iPacket can't stall AccuTrade forever.
4. Leave `accutrade_retry_at` arming for genuine drops as a backstop, but it's no longer the
   only path.

### B. Worker (6 VMs, process_bid.py) — remove AccuTrade from the bundle (other instance's half)
1. **Remove the AccuTrade thread from `process_bid`** — `process_bid` runs vAuto (and iPacket,
   TBD) only. No AccuTrade leg in the bundled job.
2. AccuTrade runs **solely** via `process_one_accutrade()` off `/api/accutrade/pending`
   (already implemented — it just becomes the primary path, not retry).
3. Keep AccuTrade on its own `accutrade_profile` (already separate) so it never contends with
   the vAuto profile.
4. Optional but recommended: log AccuTrade as its **own `worker_jobs` row** (job_type=
   'accutrade') so per-leg success/failure is visible and reclaimable (no more hidden
   sub-thread).

### C. Separate, related (per operator): listing-posting must NOT wait on the AI assessment
The bid listing renders enrichment per-leg from the DB as each lands; the AI price is a
non-blocking overlay. (Tracked separately — durable data-presence gate.)

## Net result
AccuTrade owns its lifecycle end-to-end (own queue → own runner → own profile → own script),
gated only on Carfax(+iPacket) trim evidence, never bundled with vAuto, and can't be orphaned
by a fast/slow/dead vAuto job. Bid 3510's failure mode becomes impossible.
