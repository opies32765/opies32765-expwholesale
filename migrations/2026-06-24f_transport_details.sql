-- Transport details: editable pickup (seller) + delivery (buyer) address/phone/
-- contact overrides on each car, plus a reusable carrier list for the dropdown.
-- Run as expuser so the app (expuser) owns these objects.
BEGIN;
ALTER TABLE recon_units ADD COLUMN IF NOT EXISTS pickup_address   TEXT;
ALTER TABLE recon_units ADD COLUMN IF NOT EXISTS pickup_phone     TEXT;
ALTER TABLE recon_units ADD COLUMN IF NOT EXISTS pickup_contact   TEXT;
ALTER TABLE recon_units ADD COLUMN IF NOT EXISTS delivery_address TEXT;
ALTER TABLE recon_units ADD COLUMN IF NOT EXISTS delivery_phone   TEXT;
ALTER TABLE recon_units ADD COLUMN IF NOT EXISTS delivery_contact TEXT;

CREATE TABLE IF NOT EXISTS recon_transport_companies (
    id         SERIAL PRIMARY KEY,
    name       TEXT UNIQUE NOT NULL,
    active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- the one carrier we know we use; Austin adds the rest inline from the car page
INSERT INTO recon_transport_companies (name) VALUES ('Dealer Direct')
ON CONFLICT (name) DO NOTHING;

-- tiny key/value store for the incremental LSL sync high-water mark
CREATE TABLE IF NOT EXISTS recon_kv (
    k          TEXT PRIMARY KEY,
    v          TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMIT;
