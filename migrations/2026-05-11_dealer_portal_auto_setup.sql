-- ===========================================================================
-- Dealer portal auto-setup
-- Idempotent stored function + AFTER-INSERT trigger so any new dealer
-- automatically gets: portal_slug, dashboard_token, mobile_token, and a
-- placeholder partner_users row (no password) wired so the frictionless
-- /partner/<slug>/d/<token> URL works immediately.
-- ===========================================================================

CREATE OR REPLACE FUNCTION ensure_dealer_portal(p_dealer_id integer)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    v_name text;
    v_existing_slug text;
    v_existing_dash text;
    v_existing_mob text;
    v_base_slug text;
    v_slug text;
    v_suffix integer := 0;
BEGIN
    SELECT name, portal_slug, dashboard_token, mobile_token
      INTO v_name, v_existing_slug, v_existing_dash, v_existing_mob
      FROM dealers WHERE id = p_dealer_id;
    IF NOT FOUND OR v_name IS NULL THEN
        RETURN;
    END IF;

    -- Base slug = lowercase alphanumeric of name, max 32 chars
    v_base_slug := substring(regexp_replace(lower(v_name), '[^a-z0-9]+', '', 'g'), 1, 32);
    IF v_base_slug = '' THEN
        v_base_slug := 'dealer' || p_dealer_id::text;
    END IF;

    -- portal_slug: set if NULL, dedupe with numeric suffix
    IF v_existing_slug IS NULL THEN
        v_slug := v_base_slug;
        WHILE EXISTS (
            SELECT 1 FROM dealers
            WHERE portal_slug = v_slug AND id != p_dealer_id
        ) LOOP
            v_suffix := v_suffix + 1;
            v_slug := substring(v_base_slug, 1, 32 - length(v_suffix::text)) || v_suffix::text;
        END LOOP;
        UPDATE dealers SET portal_slug = v_slug WHERE id = p_dealer_id;
    ELSE
        v_slug := v_existing_slug;
    END IF;

    -- dashboard_token + mobile_token: URL-safe random, 18 random bytes
    -- (24 base64 chars). Translate +/= so the value works in URLs.
    IF v_existing_dash IS NULL THEN
        UPDATE dealers
           SET dashboard_token = translate(encode(gen_random_bytes(18), 'base64'), '+/=', '-_.')
         WHERE id = p_dealer_id;
    END IF;
    IF v_existing_mob IS NULL THEN
        UPDATE dealers
           SET mobile_token = translate(encode(gen_random_bytes(18), 'base64'), '+/=', '-_.')
         WHERE id = p_dealer_id;
    END IF;

    -- Placeholder partner_user with NO password so the token URL has a
    -- session to bind to. Email is a sentinel ('pending@<slug>.invite')
    -- that won't collide with real opt-in emails. Dealer can later set
    -- a real email + password from /partner/<slug>/settings.
    IF NOT EXISTS (SELECT 1 FROM partner_users WHERE dealer_id = p_dealer_id) THEN
        INSERT INTO partner_users
            (dealer_id, email, full_name, password_hash, created_at,
             sms_opt_in, email_bid_alerts)
        VALUES
            (p_dealer_id,
             'pending@' || v_slug || '.invite',
             v_name || ' (placeholder)',
             NULL,
             NOW(),
             FALSE,
             TRUE);
    END IF;
END;
$$;

-- Trigger: fire on every dealer INSERT
CREATE OR REPLACE FUNCTION dealers_after_insert_portal_setup()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    PERFORM ensure_dealer_portal(NEW.id);
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_dealers_portal_setup ON dealers;
CREATE TRIGGER trg_dealers_portal_setup
AFTER INSERT ON dealers
FOR EACH ROW
EXECUTE FUNCTION dealers_after_insert_portal_setup();

