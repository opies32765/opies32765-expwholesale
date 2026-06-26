-- 2026-06-26b: generalize "Austin emailed" -> "Shipping arranged".
-- Shipping can be arranged by emailing Austin OR by management noting a company
-- (e.g. "XX dealer picking up"). One-shot, hand-run (HR8). Idempotent.
ALTER TABLE recon_units ADD COLUMN IF NOT EXISTS shipping_arranged_at   timestamptz;
ALTER TABLE recon_units ADD COLUMN IF NOT EXISTS shipping_arranged_via  text;   -- 'austin' | 'manual'
ALTER TABLE recon_units ADD COLUMN IF NOT EXISTS shipping_arranged_note text;

-- existing Austin emails become the 'austin' arrangement
UPDATE recon_units
   SET shipping_arranged_at  = austin_emailed_at,
       shipping_arranged_via = 'austin'
 WHERE austin_emailed_at IS NOT NULL
   AND shipping_arranged_at IS NULL;
