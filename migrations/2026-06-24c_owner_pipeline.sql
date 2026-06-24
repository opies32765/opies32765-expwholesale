-- RECON_OWNER_PIPELINE_2026_06_24 — reshape the step model to the owners' workflow
-- + add per-car buying-from / sold-to / photo-flag / location fields + an email outbox.
-- Run once, by hand, AS expuser (owner-consistent). Idempotent. Cold tables.
BEGIN;

-- ── per-car fields ──────────────────────────────────────────────────────────
ALTER TABLE recon_units ADD COLUMN IF NOT EXISTS path TEXT;                 -- 'd2d' | 'd2h'
ALTER TABLE recon_units ADD COLUMN IF NOT EXISTS sold_to TEXT;             -- LSL customer_name (buyer)
ALTER TABLE recon_units ADD COLUMN IF NOT EXISTS sold_to_salesperson TEXT; -- LSL deal_sales_person_name
ALTER TABLE recon_units ADD COLUMN IF NOT EXISTS deal_status TEXT;         -- 'Booked'(sold) | 'Available'(not sold)
ALTER TABLE recon_units ADD COLUMN IF NOT EXISTS buying_from_type TEXT;    -- 'Individual' | 'Dealer'
ALTER TABLE recon_units ADD COLUMN IF NOT EXISTS needs_photos BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE recon_units ADD COLUMN IF NOT EXISTS location_label TEXT;      -- extracted from notes (9B)

-- ── email outbox (staged during testing; all routed to a test inbox) ─────────
CREATE TABLE IF NOT EXISTS recon_email_outbox (
    id BIGSERIAL PRIMARY KEY,
    unit_id BIGINT REFERENCES recon_units(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,                 -- austin_stage | rose_pickup | rose_delivered | austin_ready
    to_intended TEXT,                   -- who it WOULD go to (Rose/Austin) at go-live
    to_actual TEXT,                     -- where it actually went (test inbox while staged)
    subject TEXT, body TEXT,
    staged BOOLEAN NOT NULL DEFAULT TRUE,
    sent_ok BOOLEAN,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_recon_email_unit ON recon_email_outbox (unit_id);

-- ── deactivate old steps not in the owners' model ───────────────────────────
UPDATE recon_step_defs SET active=FALSE, updated_at=now() WHERE store_id=1
  AND code IN ('transport','intake','ucm_approval','parts','sublet','qc','frontline','wholesale');

-- ── upsert the owners' pipeline (reuses existing inspection/mechanical/body/detail/photos) ──
INSERT INTO recon_step_defs (code,name,sort_order,owner_role,is_gate,is_terminal,active) VALUES
 ('all','ALL',5,'',FALSE,FALSE,TRUE),
 ('d2d','Dealer-to-Dealer',10,'',FALSE,FALSE,TRUE),
 ('d2h','Dealer-to-Home',11,'',FALSE,FALSE,TRUE),
 ('in_transport','In Transport',20,'transport_coord',FALSE,FALSE,TRUE),
 ('arrived_dealer','Arrived at Dealer',30,'',FALSE,FALSE,TRUE),
 ('arrived_home','Arrived Home Base',31,'',FALSE,FALSE,TRUE),
 ('inspection','Inspection',40,'recon_tech',FALSE,FALSE,TRUE),
 ('mechanical','Mechanical / Service',50,'recon_tech',FALSE,FALSE,TRUE),
 ('body','Body / PDR / Glass',60,'body_lead',FALSE,FALSE,TRUE),
 ('detail','Detail',80,'detail',FALSE,FALSE,TRUE),
 ('photos','Photos',90,'merch',FALSE,FALSE,TRUE),
 ('ready','Ready',100,'',FALSE,FALSE,TRUE),
 ('picked_up','Picked Up from Home Base',110,'',FALSE,FALSE,TRUE)
ON CONFLICT (store_id, code) DO UPDATE SET
  name=EXCLUDED.name, sort_order=EXCLUDED.sort_order, owner_role=EXCLUDED.owner_role,
  is_gate=EXCLUDED.is_gate, is_terminal=EXCLUDED.is_terminal, active=TRUE, updated_at=now();

-- ── remap any existing open units off deactivated steps onto 'all' ──────────
UPDATE recon_units SET
   current_step_id=(SELECT id FROM recon_step_defs WHERE store_id=1 AND code='all'),
   current_step_entered_at=now(), updated_at=now()
 WHERE store_id=1 AND status IN ('in_transit_stage0','in_recon','on_hold')
   AND (current_step_id IS NULL OR current_step_id IN
        (SELECT id FROM recon_step_defs WHERE store_id=1 AND active=FALSE));

COMMIT;
