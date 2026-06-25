-- Out-for-recon: when the recon person ships a car out for work, a timer starts.
ALTER TABLE recon_units ADD COLUMN IF NOT EXISTS out_for_recon_at TIMESTAMPTZ;
ALTER TABLE recon_units ADD COLUMN IF NOT EXISTS out_for_recon_to TEXT;
