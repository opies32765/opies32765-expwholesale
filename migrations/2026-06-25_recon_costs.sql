-- Phase B: surface LSL recon + transport cost per car, and give the Recon
-- section its own notes thread. Run as expuser.
ALTER TABLE recon_units ADD COLUMN IF NOT EXISTS lsl_recon_cost     NUMERIC;
ALTER TABLE recon_units ADD COLUMN IF NOT EXISTS lsl_transport_cost NUMERIC;
ALTER TABLE recon_units ADD COLUMN IF NOT EXISTS lsl_attachments    INTEGER;
ALTER TABLE recon_units ADD COLUMN IF NOT EXISTS cost_synced_at     TIMESTAMPTZ;
-- notes get a category so the Recon panel keeps a thread separate from general notes
ALTER TABLE recon_notes ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT 'general';
