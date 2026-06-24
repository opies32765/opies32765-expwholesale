-- ============================================================================
-- RECON_TRANSPORT_2026_06_24  —  Stage-0 (transport) delta. Requires
-- 2026-06-24_recon_init.sql first. RUN ONCE, BY HAND, ON C1 ONLY. Idempotent.
-- Adds the transit clocks to recon_units + the recon_transport detail table +
-- the 'transport' step (sort_order 5, before intake@10). The actual ops-sheet
-- SYNC is Phase 2 (ew_recon_transport_sync.py) — this just lays the schema.
-- ============================================================================
BEGIN;

ALTER TABLE recon_units ADD COLUMN IF NOT EXISTS acquired_at TIMESTAMPTZ;       -- auction win (Total-T2L origin)
ALTER TABLE recon_units ADD COLUMN IF NOT EXISTS in_transit_at TIMESTAMPTZ;     -- picked up
ALTER TABLE recon_units ADD COLUMN IF NOT EXISTS delivered_at TIMESTAMPTZ;      -- = entered_recon_at (Intake start)
ALTER TABLE recon_units ADD COLUMN IF NOT EXISTS transport_company TEXT;        -- DD / DC / REG
ALTER TABLE recon_units ADD COLUMN IF NOT EXISTS transport_ref TEXT;
CREATE INDEX IF NOT EXISTS ix_recon_units_stage0
    ON recon_units (store_id, sub_status, in_transit_at) WHERE status = 'in_transit_stage0';

CREATE TABLE IF NOT EXISTS recon_transport (
    id BIGSERIAL PRIMARY KEY,
    unit_id BIGINT REFERENCES recon_units(id) ON DELETE CASCADE,
    vin VARCHAR(17) NOT NULL,
    sub_status TEXT NOT NULL DEFAULT 'pending',
    pickup_loc TEXT, delivery_loc TEXT, pickup_zip TEXT, delivery_zip TEXT,
    est_pickup TEXT, est_delivery TEXT, row_status_flag TEXT, company TEXT,
    transport_cost NUMERIC(10,2), ops_note TEXT,
    is_our_unit BOOLEAN NOT NULL DEFAULT FALSE,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sub_changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_seen_at TIMESTAMPTZ, last_synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    source TEXT NOT NULL DEFAULT 'ops_sheet',
    CONSTRAINT recon_transport_substatus_chk CHECK (sub_status IN ('pending','in_transit','delivered'))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_recon_transport_vin_open
    ON recon_transport (vin) WHERE sub_status IN ('pending','in_transit');
CREATE INDEX IF NOT EXISTS ix_rt_vin ON recon_transport (vin);
CREATE INDEX IF NOT EXISTS ix_rt_our_open
    ON recon_transport (is_our_unit, sub_status) WHERE sub_status <> 'delivered';

-- transport step: sort_order 5 (before intake@10). sla_hours = in-transit target;
-- sla_hours_exotic slot reused as the pending-pickup target (documented overload).
INSERT INTO recon_step_defs
   (code,name,sort_order,sla_hours,sla_hours_exotic,owner_role,is_parallel,is_gate,is_terminal,is_pauses_sla)
VALUES ('transport','Transport (Stage 0)',5,72,96,'transport_coord',FALSE,FALSE,FALSE,FALSE)
ON CONFLICT (store_id, code) DO NOTHING;

COMMIT;
