-- RECON_WHO_2026_08_27 — per-person attribution WITHOUT per-person logins.
--
-- recon_step_events.moved_by and recon_audit.actor have been recorded since day
-- one, but the shared EW login never sets session['username'], so _actor() fell
-- through to 'operator' for all 2,408 human moves. This table is the name list
-- behind the one-time picker; the picked name rides a year-long `ew_who` cookie
-- on that device and _actor() reads it.
--
-- Honor-system attribution for COORDINATION, not security — anyone can pick any
-- name. If real permissions are ever wanted, this table upgrades into accounts.

CREATE TABLE IF NOT EXISTS recon_actors (
    id          serial PRIMARY KEY,
    name        text NOT NULL UNIQUE,
    active      boolean NOT NULL DEFAULT true,
    sort_order  integer NOT NULL DEFAULT 500,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- case-insensitive uniqueness so a free-typed "joe" can never become a second
-- person alongside "Joe" (the API resolves to the existing row, this backs it).
CREATE UNIQUE INDEX IF NOT EXISTS recon_actors_name_lower_uq
    ON recon_actors (lower(name));

-- seeded in the order the operator gave them
INSERT INTO recon_actors (name, sort_order) VALUES
    ('Joe',     10),
    ('Todd',    20),
    ('Gregg',   30),
    ('Vlad',    40),
    ('Oscar',   50),
    ('Alan',    60),
    ('Steve',   70),
    ('Patty',   80),
    ('Danny',   90),
    ('Rob',    100),
    ('Denes',  110),
    ('Jordan', 120),
    ('Austin', 130)
ON CONFLICT (name) DO NOTHING;
