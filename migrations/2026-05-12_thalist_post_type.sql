-- 2026-05-12 (later): expand thalist scraper to also pull Broker Listings.
-- Adds post_type tracking so we can filter/audit which kind of post each
-- bid originated from. WI = Wholesale Inventory, BL = Broker Listing.

ALTER TABLE thalist_posts
    ADD COLUMN IF NOT EXISTS post_type_code VARCHAR(8),
    ADD COLUMN IF NOT EXISTS post_type_name TEXT;

CREATE INDEX IF NOT EXISTS idx_thalist_posts_type_code
    ON thalist_posts(post_type_code) WHERE post_type_code IS NOT NULL;
