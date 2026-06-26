-- 2026-06-26c: normalized short label for the Shipping Arranged column.
-- The local 9B extracts "<Company> pickup/delivery" from whatever free text the
-- user types, so the dashboard stays aligned. Raw text stays in *_note.
ALTER TABLE recon_units ADD COLUMN IF NOT EXISTS shipping_arranged_who text;
