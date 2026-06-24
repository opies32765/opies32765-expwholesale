-- Ledger of every LSL car the recon sync has ever pulled, so a car the user
-- removes from the board is never re-imported, and back-dated/late-entered
-- today buys still import exactly once. Run as expuser.
CREATE TABLE IF NOT EXISTS recon_seen (
    vin        TEXT PRIMARY KEY,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT now()
);
