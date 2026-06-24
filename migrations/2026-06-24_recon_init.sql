-- ============================================================================
-- RECON_INIT_2026_06_24  —  EW Recon (Time-to-Line tracker) canonical schema.
-- Cluster 'ewreplica' :5433, DB expwholesale.  RUN ONCE, BY HAND, ON C1 ONLY.
-- NOT an import-time _ensure_*() call (HR8: boot-DDL freeze). Idempotent.
-- All recon_* tables are net-new/cold: no readers, no FK to bids/LSL, so recon
-- can never block or be blocked by the enrichment pipeline (HR1).
-- ============================================================================
BEGIN;

-- 1. STEP CATALOG (configurable, reorderable, per-store) ----------------------
CREATE TABLE IF NOT EXISTS recon_step_defs (
    id SERIAL PRIMARY KEY,
    code TEXT NOT NULL, name TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 100,
    sla_hours NUMERIC(7,2), sla_hours_exotic NUMERIC(7,2),
    owner_role TEXT,
    is_parallel BOOLEAN NOT NULL DEFAULT FALSE,
    is_gate BOOLEAN NOT NULL DEFAULT FALSE,
    is_terminal BOOLEAN NOT NULL DEFAULT FALSE,
    is_pauses_sla BOOLEAN NOT NULL DEFAULT FALSE,
    store_id INTEGER NOT NULL DEFAULT 1,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT recon_step_defs_code_store_uq UNIQUE (store_id, code)
);
CREATE INDEX IF NOT EXISTS ix_recon_step_defs_store_order
    ON recon_step_defs (store_id, sort_order) WHERE active;

-- 2. USERS + ROSTER (stable IDs for leaderboard / role->phone) ----------------
CREATE TABLE IF NOT EXISTS recon_users (
    id SERIAL PRIMARY KEY, username TEXT UNIQUE, display_name TEXT,
    role TEXT, phone VARCHAR(20), active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS recon_roster (
    id SERIAL PRIMARY KEY, store_id INTEGER NOT NULL DEFAULT 1,
    role TEXT NOT NULL, step_code TEXT, user_id INTEGER REFERENCES recon_users(id),
    phone VARCHAR(20), escalation_phone VARCHAR(20),
    CONSTRAINT recon_roster_uq UNIQUE (store_id, role, step_code)
);

-- 3. VENDORS (sublet / external) ----------------------------------------------
CREATE TABLE IF NOT EXISTS recon_vendors (
    id SERIAL PRIMARY KEY, name TEXT NOT NULL, vendor_type TEXT,
    phone VARCHAR(20), email TEXT, default_sla_hours NUMERIC(7,2) DEFAULT 48,
    access_token TEXT, active BOOLEAN NOT NULL DEFAULT TRUE, notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_recon_vendors_token
    ON recon_vendors (access_token) WHERE access_token IS NOT NULL;

-- 4. APPROVAL TIERS (dollar-threshold routing) --------------------------------
CREATE TABLE IF NOT EXISTS recon_approval_tier (
    id SERIAL PRIMARY KEY, store_id INTEGER NOT NULL DEFAULT 1,
    tier TEXT NOT NULL,
    min_usd NUMERIC(10,2) NOT NULL DEFAULT 0,
    max_usd NUMERIC(10,2),
    auto_approve BOOLEAN NOT NULL DEFAULT FALSE,
    approver_role TEXT, target_minutes INTEGER,
    CONSTRAINT recon_approval_tier_uq UNIQUE (store_id, tier)
);

-- 5. HOLDING-COST CONFIG ------------------------------------------------------
CREATE TABLE IF NOT EXISTS recon_holding_cost_config (
    id SERIAL PRIMARY KEY, vehicle_class TEXT NOT NULL,
    per_day_usd NUMERIC(8,2) NOT NULL,
    depreciation_per_day_usd NUMERIC(8,2) DEFAULT 0, note TEXT,
    store_id INTEGER NOT NULL DEFAULT 1, active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT recon_hcc_class_store_uq UNIQUE (store_id, vehicle_class)
);

-- 6. RECON UNITS (one row per unit; owns the T2L clock) -----------------------
CREATE TABLE IF NOT EXISTS recon_units (
    id BIGSERIAL PRIMARY KEY,
    vin VARCHAR(17) NOT NULL, stock_no TEXT,
    lsl_inventory_ref BIGINT, bid_id BIGINT,            -- nullable LOGICAL refs; NOT FKs
    store_id INTEGER NOT NULL DEFAULT 1,
    year INTEGER, make TEXT, model TEXT, trim TEXT,
    exterior_color TEXT, miles INTEGER,
    vehicle_class TEXT NOT NULL DEFAULT 'default',
    entered_recon_at TIMESTAMPTZ,                       -- recon clock start (= delivered_at)
    frontline_ready_at TIMESTAMPTZ, exited_at TIMESTAMPTZ,
    current_step_id INTEGER REFERENCES recon_step_defs(id),
    current_step_entered_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'in_recon',
    sub_status TEXT,                                    -- transport: pending|in_transit|delivered
    purchase_cost NUMERIC(12,2),
    recon_estimate_total NUMERIC(12,2) DEFAULT 0,
    recon_actual_total NUMERIC(12,2) DEFAULT 0,
    holding_cost_accrued NUMERIC(12,2) DEFAULT 0,       -- cache only; live derivation is truth
    source TEXT NOT NULL DEFAULT 'lsl_sweep',
    is_exotic BOOLEAN NOT NULL DEFAULT FALSE,
    qc_signed_by TEXT, qc_signed_at TIMESTAMPTZ,
    not_available BOOLEAN NOT NULL DEFAULT FALSE, unavailable_reason TEXT,
    recon_token TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT recon_units_status_chk CHECK (status IN
        ('in_transit_stage0','in_recon','frontline_ready','wholesale','sold','removed','on_hold'))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_recon_units_vin_open
    ON recon_units (vin) WHERE status IN ('in_transit_stage0','in_recon','frontline_ready','on_hold');
CREATE INDEX IF NOT EXISTS ix_recon_units_vin ON recon_units (vin);
CREATE INDEX IF NOT EXISTS ix_recon_units_status ON recon_units (status);
CREATE INDEX IF NOT EXISTS ix_recon_units_current_step ON recon_units (current_step_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_recon_units_token
    ON recon_units (recon_token) WHERE recon_token IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_recon_units_open_board
    ON recon_units (store_id, current_step_id, current_step_entered_at)
    WHERE status IN ('in_recon','on_hold');

-- 7. STEP EVENTS (append-only = SOURCE OF TRUTH) ------------------------------
CREATE TABLE IF NOT EXISTS recon_step_events (
    id BIGSERIAL PRIMARY KEY,
    unit_id BIGINT NOT NULL REFERENCES recon_units(id) ON DELETE CASCADE,
    step_id INTEGER NOT NULL REFERENCES recon_step_defs(id),
    entered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    exited_at TIMESTAMPTZ, duration_sec BIGINT,
    sla_paused_sec BIGINT NOT NULL DEFAULT 0,
    moved_by TEXT, move_reason TEXT,
    is_rework BOOLEAN NOT NULL DEFAULT FALSE,
    from_step_id INTEGER REFERENCES recon_step_defs(id),
    auto BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_rse_unit ON recon_step_events (unit_id);
CREATE INDEX IF NOT EXISTS ix_rse_open ON recon_step_events (unit_id, step_id) WHERE exited_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_rse_step_entered ON recon_step_events (step_id, entered_at);

-- 8. ACTIVE STEPS (open parallel lanes RIGHT NOW) -----------------------------
CREATE TABLE IF NOT EXISTS recon_unit_active_steps (
    id BIGSERIAL PRIMARY KEY,
    unit_id BIGINT NOT NULL REFERENCES recon_units(id) ON DELETE CASCADE,
    step_id INTEGER NOT NULL REFERENCES recon_step_defs(id),
    event_id BIGINT REFERENCES recon_step_events(id),
    entered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    owner TEXT, vendor_id INTEGER REFERENCES recon_vendors(id),
    sla_due_at TIMESTAMPTZ, is_overdue BOOLEAN NOT NULL DEFAULT FALSE,
    CONSTRAINT recon_active_uq UNIQUE (unit_id, step_id)
);
CREATE INDEX IF NOT EXISTS ix_ras_due ON recon_unit_active_steps (sla_due_at);

-- 9. WORK ITEMS (estimate/approve/decline/actual) -----------------------------
CREATE TABLE IF NOT EXISTS recon_workitems (
    id BIGSERIAL PRIMARY KEY,
    unit_id BIGINT NOT NULL REFERENCES recon_units(id) ON DELETE CASCADE,
    step_id INTEGER REFERENCES recon_step_defs(id),
    category TEXT, description TEXT NOT NULL,
    estimate_cost NUMERIC(10,2) DEFAULT 0,
    approved_cost NUMERIC(10,2), actual_cost NUMERIC(10,2), hours_est NUMERIC(6,2),
    status TEXT NOT NULL DEFAULT 'proposed',
    assignee TEXT, assignee_kind TEXT NOT NULL DEFAULT 'internal',
    vendor_id INTEGER REFERENCES recon_vendors(id),
    sublet_out_at TIMESTAMPTZ, sublet_in_at TIMESTAMPTZ, sublet_due_at TIMESTAMPTZ,
    approved_by TEXT, approved_at TIMESTAMPTZ,
    declined_by TEXT, declined_at TIMESTAMPTZ,
    needs_approval_since TIMESTAMPTZ, rev INTEGER NOT NULL DEFAULT 0,
    completed_at TIMESTAMPTZ, created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT recon_wi_status_chk CHECK (status IN
        ('proposed','needs_approval','approved','declined','in_progress','done','reopened'))
);
CREATE INDEX IF NOT EXISTS ix_wi_unit ON recon_workitems (unit_id);
CREATE INDEX IF NOT EXISTS ix_wi_needs_approval
    ON recon_workitems (needs_approval_since) WHERE status = 'needs_approval';

-- 10. NOTES + TEMPLATES -------------------------------------------------------
CREATE TABLE IF NOT EXISTS recon_note_templates (
    id SERIAL PRIMARY KEY, code TEXT UNIQUE NOT NULL, label TEXT, body TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE TABLE IF NOT EXISTS recon_notes (
    id BIGSERIAL PRIMARY KEY,
    unit_id BIGINT NOT NULL REFERENCES recon_units(id) ON DELETE CASCADE,
    step_id INTEGER REFERENCES recon_step_defs(id),
    workitem_id BIGINT REFERENCES recon_workitems(id) ON DELETE SET NULL,
    author TEXT, body TEXT NOT NULL, template_code TEXT,
    is_internal BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_notes_unit_created ON recon_notes (unit_id, created_at);

-- 11. PHOTOS (mirrors bid_photos: local_path persisted synchronously) ---------
CREATE TABLE IF NOT EXISTS recon_photos (
    id BIGSERIAL PRIMARY KEY,
    unit_id BIGINT NOT NULL REFERENCES recon_units(id) ON DELETE CASCADE,
    step_id INTEGER REFERENCES recon_step_defs(id),
    workitem_id BIGINT REFERENCES recon_workitems(id) ON DELETE SET NULL,
    local_path TEXT, url TEXT, caption TEXT, uploaded_by TEXT,
    is_walkaround BOOLEAN NOT NULL DEFAULT FALSE,
    is_approval_media BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_photos_unit_step ON recon_photos (unit_id, step_id);

-- 12. NOTIFICATION LEDGER (single idempotency spine — HR3) --------------------
CREATE TABLE IF NOT EXISTS recon_alert_log (
    id BIGSERIAL PRIMARY KEY,
    notif_key TEXT UNIQUE NOT NULL,
    unit_id BIGINT, kind TEXT, target TEXT, channel TEXT,
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT now(), twilio_ok BOOLEAN
);

-- 13. AUDIT (append-only JSONB) ----------------------------------------------
CREATE TABLE IF NOT EXISTS recon_audit (
    id BIGSERIAL PRIMARY KEY,
    unit_id BIGINT REFERENCES recon_units(id) ON DELETE CASCADE,
    entity TEXT NOT NULL, entity_id BIGINT, action TEXT NOT NULL,
    actor TEXT, detail JSONB, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_audit_unit ON recon_audit (unit_id);

-- 14. SEED: canonical high-line step catalog (transport seeded in transport.sql)
INSERT INTO recon_step_defs
   (code,name,sort_order,sla_hours,sla_hours_exotic,owner_role,is_parallel,is_gate,is_terminal,is_pauses_sla)
VALUES
   ('intake','Intake / Check-in',10,4,4,'inventory_clerk',FALSE,FALSE,FALSE,FALSE),
   ('inspection','Inspection / MPI',20,4,8,'recon_tech',FALSE,FALSE,FALSE,FALSE),
   ('ucm_approval','UCM Approval',30,2,4,'ucm',FALSE,TRUE,FALSE,FALSE),
   ('parts','Parts Hold',40,8,8,'parts',TRUE,FALSE,FALSE,TRUE),
   ('mechanical','Mechanical / Service',50,8,24,'recon_tech',TRUE,FALSE,FALSE,FALSE),
   ('body','Body / PDR / Glass',60,24,72,'body_lead',TRUE,FALSE,FALSE,FALSE),
   ('sublet','Sublet',70,48,72,'vendor_coord',TRUE,FALSE,FALSE,FALSE),
   ('detail','Detail / Paint Correction',80,8,72,'detail',FALSE,FALSE,FALSE,FALSE),
   ('photos','Photos / Merchandising',90,4,8,'merch',TRUE,FALSE,FALSE,FALSE),
   ('qc','Final QC / Frontline Gate',100,2,4,'ucm',FALSE,TRUE,FALSE,FALSE),
   ('frontline','Frontline Ready',110,NULL,NULL,'ucm',FALSE,FALSE,TRUE,FALSE),
   ('wholesale','Wholesale',120,NULL,NULL,'ucm',FALSE,FALSE,TRUE,FALSE)
ON CONFLICT (store_id, code) DO NOTHING;

INSERT INTO recon_holding_cost_config (vehicle_class,per_day_usd,depreciation_per_day_usd,note) VALUES
   ('default',40.00,40.00,'NCM baseline ~$40/day'),
   ('mainstream',40.00,40.00,'mainstream used'),
   ('highline',65.00,40.00,'high-line floorplan principal higher'),
   ('exotic',85.00,50.00,'six-figure marques; floorplan + larger depreciation')
ON CONFLICT (store_id, vehicle_class) DO NOTHING;

INSERT INTO recon_approval_tier (tier,min_usd,max_usd,auto_approve,approver_role,target_minutes) VALUES
   ('auto',0,500,TRUE,'tech',0),
   ('svc_mgr',500,2500,FALSE,'service_manager',NULL),
   ('ucm',2500,7500,FALSE,'ucm',30),
   ('principal',7500,NULL,FALSE,'operator',NULL)
ON CONFLICT (store_id, tier) DO NOTHING;

COMMIT;
