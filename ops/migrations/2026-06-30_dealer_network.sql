-- DEALERPRICE_NETWORK_2026_06_30 — dealer vetting / network application gate.
-- Hand-run migration (NO import-time DDL, per recon precedent / HR8).
-- Run on C1 only:  psql -p 5433 -d expwholesale -f dp_network_migration.sql
-- Additive: two new tables, NO FK to bids (HR1 — can never block enrichment).
-- Idempotent: safe to re-run.

BEGIN;

-- ── Applications: every "Apply to the Network" submission lands here ─────────
CREATE TABLE IF NOT EXISTS dealer_applications (
    id                  SERIAL PRIMARY KEY,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    status              TEXT NOT NULL DEFAULT 'pending',   -- pending|approved|rejected|needs_info
    is_existing         BOOLEAN DEFAULT FALSE,             -- Q0: already an EW dealer?

    -- Identity
    dealership_name     TEXT,
    dba                 TEXT,
    dealer_group        TEXT,
    franchises          TEXT,                              -- free list / "Independent"
    entity_type         TEXT,                              -- LLC/Corp/Sole prop
    entity_state        TEXT,

    -- Scale & fit
    years_in_business   INTEGER,
    years_at_location   INTEGER,
    units_per_month     INTEGER,
    units_annual        INTEGER,
    avg_investment_band TEXT,                              -- band picker
    avg_investment_num  NUMERIC,
    credit_line         NUMERIC,
    floorplan_provider  TEXT,
    floorplan_line      NUMERIC,
    dealer_types        TEXT,                              -- csv (multi-select)
    primary_makes       TEXT,
    price_tier          TEXT,

    -- Legitimacy / verifiable
    license_number      TEXT,
    license_state       TEXT,
    license_exp         DATE,
    tax_id              TEXT,                              -- EIN (sensitive)
    bond_provider       TEXT,
    bond_amount         NUMERIC,
    physical_lot        BOOLEAN,
    lot_address         TEXT,
    website             TEXT,
    reputation_url      TEXT,                              -- Google / DealerRater
    auction_access      TEXT,                              -- Manheim/ADESA rep#

    -- Transaction readiness / references
    payment_ready       TEXT,
    bank_reference      TEXT,
    trade_reference     TEXT,
    referrer_name       TEXT,

    -- Contact
    contact_name        TEXT,
    contact_email       TEXT,
    contact_phone       TEXT,

    -- Consent / attestation
    attestation         BOOLEAN DEFAULT FALSE,
    tcpa_consent        BOOLEAN DEFAULT FALSE,

    -- Uploaded docs — PRIVATE paths on C1 (NOT under /static), served only via
    -- the auth-gated /network/application/<id>/doc/<which> route.
    license_doc_path    TEXT,
    taxid_doc_path      TEXT,

    -- Auto-checks (filled at intake): fuzzy match vs LSL counterparty roster
    name_match          JSONB,                             -- dealership vs roster
    referrer_match      JSONB,                             -- referrer vs roster

    notes               TEXT,
    raw_payload         JSONB,                             -- full submission for audit

    -- Review
    reviewer            TEXT,
    reviewed_at         TIMESTAMPTZ,
    review_notes        TEXT,
    member_id           INTEGER                            -- set on approve (-> dealerprice_members.id)
);
CREATE INDEX IF NOT EXISTS dealer_applications_status_idx
    ON dealer_applications (status, created_at DESC);
CREATE INDEX IF NOT EXISTS dealer_applications_email_idx
    ON dealer_applications (lower(contact_email));

-- ── Members: approved network dealers + the access token the submit gate checks
CREATE TABLE IF NOT EXISTS dealerprice_members (
    id              SERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    application_id  INTEGER REFERENCES dealer_applications(id),
    dealership_name TEXT,
    contact_name    TEXT,
    contact_email   TEXT,
    contact_phone   TEXT,
    token           TEXT UNIQUE NOT NULL,                  -- secrets.token_urlsafe(24)
    status          TEXT NOT NULL DEFAULT 'active',        -- active|suspended
    is_existing     BOOLEAN DEFAULT FALSE,                 -- came in via the existing-dealer path
    lsl_match       JSONB,                                 -- matched LSL counterparty, if any
    approved_by     TEXT,
    approved_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at    TIMESTAMPTZ,
    submit_count    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS dealerprice_members_token_idx
    ON dealerprice_members (token);
CREATE INDEX IF NOT EXISTS dealerprice_members_email_idx
    ON dealerprice_members (lower(contact_email));

COMMIT;

-- sanity
\echo 'dealer_applications + dealerprice_members ready:'
SELECT 'dealer_applications' AS t, count(*) FROM dealer_applications
UNION ALL SELECT 'dealerprice_members', count(*) FROM dealerprice_members;
