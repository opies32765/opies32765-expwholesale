# VM 120 Service Topology — 2026-05-06

Two NSSM services running concurrently on VM 120 (oscar-worker-1):

## EWEnrichRbook
- JOB_TYPES=rbook
- Profile dir: C:\worker\vauto_profile_rbook
- Worker ID: oscar-worker-1-rb
- Scrapes /api/competition/vehicles for retail comps

## EWEnrichMmr
- JOB_TYPES=manheim
- Profile dir: C:\worker\vauto_profile_mh
- Worker ID: oscar-worker-1-mh
- Scrapes vAuto Manheim transactions table

Each runs its own Chromium with its own vAuto login session. They process
different bids in parallel, allowing rbook + manheim to scrape concurrently
for the same bid (was sequential — ~25-30% faster steady state).

## Why two profile dirs (not one shared)
Playwright launch_persistent_context locks the profile dir — two processes
can't share a single profile. Each gets its own dir + its own login.
