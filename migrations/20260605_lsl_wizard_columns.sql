-- LSL_WIZARD_2026_06_05: columns for the guided "Book to LSL" wizard
-- (staging + Evelyn payoff SMS/MMS + DL quickdrop + disposition/fees/referral).
-- Applied live 2026-06-05/06 via ad-hoc ALTER; this file makes the schema reproducible from git.
ALTER TABLE bids ADD COLUMN IF NOT EXISTS lsl_buyer_customer_id integer;
ALTER TABLE bids ADD COLUMN IF NOT EXISTS lsl_buyer_dealer_name text;
ALTER TABLE bids ADD COLUMN IF NOT EXISTS lsl_sell_type text;
ALTER TABLE bids ADD COLUMN IF NOT EXISTS lsl_disposition_intention text;
ALTER TABLE bids ADD COLUMN IF NOT EXISTS lsl_possession_status text;
ALTER TABLE bids ADD COLUMN IF NOT EXISTS lsl_not_sold boolean DEFAULT false;
ALTER TABLE bids ADD COLUMN IF NOT EXISTS lsl_inventory_id bigint;
ALTER TABLE bids ADD COLUMN IF NOT EXISTS lsl_stock_no text;
ALTER TABLE bids ADD COLUMN IF NOT EXISTS lsl_good_until text;
ALTER TABLE bids ADD COLUMN IF NOT EXISTS lsl_lien_account_no text;
ALTER TABLE bids ADD COLUMN IF NOT EXISTS lsl_text_evelyn boolean DEFAULT false;
ALTER TABLE bids ADD COLUMN IF NOT EXISTS lsl_total_supp_costs integer;
ALTER TABLE bids ADD COLUMN IF NOT EXISTS lsl_fee_pack integer;
ALTER TABLE bids ADD COLUMN IF NOT EXISTS lsl_fee_transport integer;
ALTER TABLE bids ADD COLUMN IF NOT EXISTS lsl_fee_referral integer;
ALTER TABLE bids ADD COLUMN IF NOT EXISTS lsl_fee_recon integer;
ALTER TABLE bids ADD COLUMN IF NOT EXISTS lsl_fee_mcd integer;
ALTER TABLE lsl_pending_customers ADD COLUMN IF NOT EXISTS drivers_license text;
ALTER TABLE lsl_pending_customers ADD COLUMN IF NOT EXISTS ssn_last4 text;
ALTER TABLE lsl_pending_customers ADD COLUMN IF NOT EXISTS dl_photo_path text;
