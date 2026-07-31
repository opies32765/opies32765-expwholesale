#!/bin/bash
# DEALER_SCORECARD_2026_07_31 — rebuild the dealer profitability board.
# Cheap (~2s: one pass over a local SQLite mirror + 2,341 row rewrite). Runs every
# 15 min, offset 2 min behind LSL extract_recent (:00/:15/:30/:45), which upserts
# the last 7d of deals. extract_inventory runs */5. So a car EW buys shows up on
# the batting average within ~15-20 min of it booking, not overnight.
# Read-only against crm.db (HR6). Cannot touch the bid/enrichment path (HR1).
cd /opt/expwholesale || exit 1
exec /usr/bin/python3 dealer_scorecard.py >> /var/log/ew_dealer_scorecard.log 2>&1
