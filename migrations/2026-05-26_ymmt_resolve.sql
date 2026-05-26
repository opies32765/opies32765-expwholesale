-- ============================================================
-- 2026-05-26 — YMMT resolve plumbing
--
-- Adds ymmt_id (FK to ymmt_catalog) + resolved_at + confidence to both
-- dealer_inventory and bids, plus a forever cache so the same raw
-- (year, make, model, trim) string only runs through fuzzy/LLM once.
--
-- Strictly additive. No existing column is touched. Caller policy
-- (decided 2026-05-26): rows with ymmt_id IS NULL are excluded from
-- buy_profile rollups and match scoring — "we don't know the trim,
-- so we don't use the vehicle as data."
--
-- Apply on C1 only:
--   ssh root@62.146.226.100 'sudo -u postgres psql -p 5433 -d expwholesale \
--     -f /opt/expwholesale/migrations/2026-05-26_ymmt_resolve.sql'
-- ============================================================

BEGIN;

-- ── dealer_inventory: tag every active/sold unit with its catalog row ──
ALTER TABLE dealer_inventory
  ADD COLUMN IF NOT EXISTS ymmt_id          INTEGER REFERENCES ymmt_catalog(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS ymmt_resolved_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS ymmt_confidence  NUMERIC(3,2);

CREATE INDEX IF NOT EXISTS idx_dealer_inventory_ymmt_id
  ON dealer_inventory(ymmt_id) WHERE ymmt_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_dealer_inventory_dealer_ymmt
  ON dealer_inventory(dealer_id, ymmt_id) WHERE ymmt_id IS NOT NULL;

-- ── bids: same tagging on the demand side ──
-- (bids already has canon_trim from an earlier LLM pass; ymmt_id is the
-- new canonical link that points to the actual catalog row.)
ALTER TABLE bids
  ADD COLUMN IF NOT EXISTS ymmt_id          INTEGER REFERENCES ymmt_catalog(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS ymmt_resolved_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS ymmt_confidence  NUMERIC(3,2);

CREATE INDEX IF NOT EXISTS idx_bids_ymmt_id
  ON bids(ymmt_id) WHERE ymmt_id IS NOT NULL;

-- ── Resolve cache: forever store of (raw → ymmt_id) decisions ──
-- raw_model_n / raw_trim_n are the normalized forms (lowercased, punctuation
-- stripped, glued-trim split via ymmt_match._norm_for_key). Stable across
-- cosmetic variation, so "GLS450 4MATIC®" and "gls 450 4matic" hit the same row.
-- ymmt_id NULL is a VALID cached decision = "system can't classify this trim
-- cleanly" — we cache the negative so we don't re-LLM the same dud forever.
CREATE TABLE IF NOT EXISTS ymmt_resolve_cache (
  id            SERIAL PRIMARY KEY,
  year          INTEGER NOT NULL,
  make          TEXT    NOT NULL,           -- UPPER()
  raw_model_n   TEXT    NOT NULL,           -- normalized form
  raw_trim_n    TEXT    NOT NULL,           -- normalized form ('' allowed)
  ymmt_id       INTEGER REFERENCES ymmt_catalog(id) ON DELETE SET NULL,
  confidence    NUMERIC(3,2),
  source        TEXT    NOT NULL DEFAULT 'unknown',  -- fuzzy / cache / claude_sonnet_4_6 / no_candidates / etc.
  reason        TEXT,
  decided_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (year, make, raw_model_n, raw_trim_n)
);

CREATE INDEX IF NOT EXISTS idx_ymmt_resolve_cache_lookup
  ON ymmt_resolve_cache(year, make, raw_model_n, raw_trim_n);

CREATE INDEX IF NOT EXISTS idx_ymmt_resolve_cache_ymmt_id
  ON ymmt_resolve_cache(ymmt_id) WHERE ymmt_id IS NOT NULL;

-- Ownership for app role
ALTER TABLE ymmt_resolve_cache OWNER TO expuser;
GRANT SELECT, INSERT, UPDATE ON ymmt_resolve_cache TO expuser;
GRANT USAGE, SELECT ON SEQUENCE ymmt_resolve_cache_id_seq TO expuser;

COMMIT;

-- ── Sanity: confirm columns exist + cache table empty ──
SELECT 'dealer_inventory.ymmt_id'  AS col, COUNT(*) FILTER (WHERE ymmt_id IS NOT NULL) AS resolved,
       COUNT(*) AS total FROM dealer_inventory
UNION ALL
SELECT 'bids.ymmt_id', COUNT(*) FILTER (WHERE ymmt_id IS NOT NULL), COUNT(*) FROM bids
UNION ALL
SELECT 'ymmt_resolve_cache', COUNT(*), COUNT(*) FROM ymmt_resolve_cache;
