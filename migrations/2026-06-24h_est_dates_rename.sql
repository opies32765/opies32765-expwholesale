-- Estimated pickup/delivery dates (Austin picks them on the car page) +
-- rename the In Transport step to "In Transit". Run as expuser.
ALTER TABLE recon_units ADD COLUMN IF NOT EXISTS est_pickup_date   DATE;
ALTER TABLE recon_units ADD COLUMN IF NOT EXISTS est_delivery_date DATE;
UPDATE recon_step_defs SET name='In Transit' WHERE store_id=1 AND code='in_transport';
