-- Wholesaler Portal — wholesalers without scraped sites submit vehicles
-- through the same /partner/<slug> portal, but submissions land in a review
-- queue (/wholesaler-<reviewer>) and are invisible to the EW Buy Center
-- until the assigned reviewer (Oscar) approves.
--
-- Deploy on Contabo 1 (primary, port 5433) as postgres superuser.

-- 1. dealers.portal_mode — 'inventory' is the existing scraped-dealer behavior;
--    'wholesaler' turns off inventory rendering and gates submitted bids on
--    review_status approval before they reach bidders.
ALTER TABLE dealers
    ADD COLUMN IF NOT EXISTS portal_mode TEXT NOT NULL DEFAULT 'inventory';

-- 2. dealers.url — wholesalers have no website. Existing scraped dealers
--    keep their URLs. Allow NULL for wholesaler rows. Unique constraint
--    stays — PG allows multiple NULLs under UNIQUE.
ALTER TABLE dealers ALTER COLUMN url DROP NOT NULL;

-- 3. bids review pipeline. NULL = bid is visible immediately (default for
--    every existing bid path). 'pending' = wholesaler-submitted, hidden
--    from Buy Center, awaiting reviewer. 'approved' = reviewer pushed it
--    through (visible). 'rejected' = killed (hidden, audit retained).
ALTER TABLE bids ADD COLUMN IF NOT EXISTS review_status TEXT;
ALTER TABLE bids ADD COLUMN IF NOT EXISTS review_by     TEXT;
ALTER TABLE bids ADD COLUMN IF NOT EXISTS review_at     TIMESTAMPTZ;
ALTER TABLE bids ADD COLUMN IF NOT EXISTS review_note   TEXT;

-- Reviewer's queue is "all pending bids for my salesperson", scanned by
-- created_at. Partial index keeps it cheap on the much larger main table.
CREATE INDEX IF NOT EXISTS idx_bids_review_pending
    ON bids(salesperson, created_at DESC)
    WHERE review_status = 'pending';

-- Permissions — expuser is the runtime role; superuser had to add columns
-- but expuser must still read/write them.
GRANT SELECT, UPDATE (review_status, review_by, review_at, review_note)
    ON bids TO expuser;
