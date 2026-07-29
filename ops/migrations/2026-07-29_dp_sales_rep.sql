-- DP_SALES_REP_2026_07_29
-- Assign an approved dealer to the rep who owns the relationship.
-- Nullable, no default: an instant catalog-only change in PG, safe on a live table.
ALTER TABLE dealerprice_members ADD COLUMN IF NOT EXISTS sales_rep             text;
ALTER TABLE dealerprice_members ADD COLUMN IF NOT EXISTS sales_rep_assigned_by text;
ALTER TABLE dealerprice_members ADD COLUMN IF NOT EXISTS sales_rep_assigned_at timestamptz;

-- Assignment history: who moved an account and when. An account can change
-- hands more than once, so the current value alone is not enough to answer
-- "who had this dealer in March".
CREATE TABLE IF NOT EXISTS dealerprice_rep_assignments (
    id          serial PRIMARY KEY,
    member_id   integer NOT NULL REFERENCES dealerprice_members(id) ON DELETE CASCADE,
    rep         text,
    prev_rep    text,
    assigned_by text,
    assigned_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_dp_rep_assign_member ON dealerprice_rep_assignments(member_id);
CREATE INDEX IF NOT EXISTS ix_dp_members_sales_rep ON dealerprice_members(sales_rep);
