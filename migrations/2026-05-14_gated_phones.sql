-- 2026-05-14: gated_phones table for admin-driven phone-gate management.
-- The env vars PHASE2_PHONE_GATE / SOURCING_PHONE_GATE stay as a baseline
-- (safety floor for the long-standing numbers). Rows here UNION on top of
-- the env list and can be added/removed via /admin/phone-gates without a
-- service restart. gate_helpers.py reads both sources with a 30s cache.
CREATE TABLE IF NOT EXISTS gated_phones (
    id           SERIAL PRIMARY KEY,
    phone_digits VARCHAR(10) NOT NULL CHECK (phone_digits ~ '^[0-9]{10}$'),
    gate_type    VARCHAR(20) NOT NULL CHECK (gate_type IN ('full_broker', 'sourcing')),
    label        TEXT,
    added_by     TEXT,
    added_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    disabled_at  TIMESTAMPTZ,
    disabled_by  TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS gated_phones_active_uniq
    ON gated_phones (phone_digits, gate_type)
    WHERE disabled_at IS NULL;

CREATE INDEX IF NOT EXISTS gated_phones_by_gate
    ON gated_phones (gate_type) WHERE disabled_at IS NULL;
