#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lsl_dealer_ledger.py  --  DEFINITIVE, collision-safe LSL dealer ledger (READ-ONLY).

Given an applicant identity (name, phone, email, optional license_url / address /
explicit supplier_id) this resolves the applicant to the correct Live-Sales-Log
entity id(s) using STRONG identity keys ONLY (never a bare name), and returns
their complete VERIFIED transaction ledger with EW:

    * cars EW BOUGHT from them   (payments ledger, money-out, id-based)
    * cars EW RESOLD from them   (deals.supplier_id overlay, id-based)
    * cars EW SOLD TO them       (deals customer side)

DATA MODEL (verified 2026-07-17 against /opt/livesaleslog/crm.db):

  ENTITY IDENTITY
  ---------------
  * suppliers.id is the canonical dealer/counterparty id.
  * payments.vendor_id references suppliers.id ONLY WHEN payee_type='Supplier'.
    For payee_type IN ('Customer','Bank') vendor_id lives in a DIFFERENT id
    space (individuals / lienholders); such a row that happens to collide with a
    suppliers.id is a FALSE FRIEND (11/11 Customer + 9/9 Bank name-mismatch).
    -> The $31k Mustang (vendor_id 1112114, payee_type='Customer') is a private
       individual named "Oscar Pastrana", NOT the dealer supplier #778781.
  * A single REAL dealer is SOMETIMES fragmented across >1 suppliers.id
    (e.g. Fort Lauderdale Collection 238 + 155182 share phone AND email) and
    multi-id is SOMETIMES distinct entities colliding by name (e.g. Carrio
    Motor Cars 31046 vs 80084, different phones). You CANNOT tell which from the
    name; only shared STRONG keys prove same-entity.
  * Strong keys are NOT globally unique: group-buyer / placeholder emails+phones
    map to dozens of unrelated dealers (canadagreg@ -> 38 names; 9999999999 ->
    26 names). A key is trustworthy for cross-name resolution ONLY when it maps
    to exactly ONE normalized supplier name; otherwise it may only corroborate
    the applicant's own name.

  TRANSACTION LOCATIONS  (authoritative key -> meaning)
  -----------------------------------------------------
  * payments  vendor_id (payee_type='Supplier', type='Purchased')  = EW BOUGHT a
              car from this dealer. Money-out truth, exists at payment time.
              (type='New' is a per-deal cost line item, NOT a purchase.)
  * deals     supplier_id  = EW RESOLD a car sourced from this dealer (adds
              front_value gross). supplier_id is reliably populated (29087/29133).
  * deals     customer_id  = EW SOLD a car TO this dealer. BUT customer_id is
              populated on only 46/29133 rows and references customers.id; the
              real sell-side link is customer_NAME (name-based -> advisory).
  * inventory purchased_from_id (purchased_from_type='Wholesaler') = corroborating
              "sourced into stock from" signal (superset of paid purchases).

  AUTHORITATIVE LINKAGE RULE  (zero false positive)
  -------------------------------------------------
  1. Resolve applicant -> suppliers.id set by STRONG keys, ranked:
       explicit supplier_id  >  exact license_url  >  exact email  >  exact phone.
     A key is applied for cross-name resolution only if it maps to ONE
     normalized name; a shared key only confirms ids whose name == applicant's.
  2. Expand to sibling ids that share BOTH the same normalized name AND a strong
     key with an already-resolved id (captures legit fragmentation; can never
     pull a differently-named entity).
  3. Pull id-keyed transactions for the resolved id set. NEVER use a bare name
     as a money key. Sell-side name matches are reported separately as ADVISORY.

READ-ONLY: opens the DB mode=ro&immutable=1. Writes nothing. Restarts nothing.
"""
import sqlite3
import re
import argparse
import json
from collections import defaultdict

LSL_DB = "/opt/livesaleslog/crm.db"


def _conn():
    c = sqlite3.connect("file:%s?mode=ro&immutable=1" % LSL_DB, uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    return c


# ---- normalizers -----------------------------------------------------------
_SUF = ("incorporated", "corporation", "company", "llc", "inc", "corp", "ltd", "llp", "co")


def norm_name(n):
    s = re.sub(r"[^a-z0-9]", "", (n or "").lower())
    for suf in _SUF:
        if s.endswith(suf) and len(s) > len(suf) + 2:
            s = s[:-len(suf)]
            break
    return s


_JUNK_PHONES = {str(d) * 10 for d in range(10)} | {"1234567890"}


def norm_phone(v):
    d = re.sub(r"[^0-9]", "", v or "")
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) != 10 or d in _JUNK_PHONES:
        return ""
    return d


_JUNK_EMAIL = ("noemail", "none@", "no@", "@none", "@noemail", "test@", "@test.",
               "example.com", "@email.com", "@noemail.com", "123@")


def norm_email(v):
    e = (v or "").strip().lower()
    if "@" not in e or "." not in e.split("@")[-1]:
        return ""
    if any(j in e for j in _JUNK_EMAIL):
        return ""
    return e


def _d10(s):
    return (s or "")[:10] or None


# ---- supplier strong-key index (built once) --------------------------------
def _index(c):
    rows = c.execute(
        "SELECT id,name,office,primary_contact_mobile,primary_contact_email,"
        "email,full_address,license_url,city,state,status,is_blocked "
        "FROM suppliers WHERE name<>''").fetchall()
    by_id = {}
    email_ids = defaultdict(set)
    phone_ids = defaultdict(set)
    for r in rows:
        by_id[r["id"]] = r
        e = norm_email(r["primary_contact_email"]) or norm_email(r["email"])
        if e:
            email_ids[e].add(r["id"])
        for p in (norm_phone(r["office"]), norm_phone(r["primary_contact_mobile"])):
            if p:
                phone_ids[p].add(r["id"])
    return by_id, email_ids, phone_ids


def _names_of(by_id, ids):
    return {norm_name(by_id[i]["name"]) for i in ids}


# ---- resolver --------------------------------------------------------------
def resolve(c, name=None, phone=None, email=None, supplier_id=None,
            license_url=None, address=None):
    """Return (sorted_ids, resolution_detail_list, by_id_index).
    resolution_detail_list = [{'id':.., 'name':.., 'via':.., 'key':.., 'unique':bool}]"""
    by_id, email_ids, phone_ids = _index(c)
    app_norm = norm_name(name)
    detail = {}   # id -> dict

    def add(i, via, key, unique):
        if i in by_id and i not in detail:
            detail[i] = {"id": i, "name": by_id[i]["name"], "via": via,
                         "key": key, "unique": unique}

    # 1. explicit supplier_id (id equality — strongest)
    if supplier_id and int(supplier_id) in by_id:
        add(int(supplier_id), "explicit_id", str(supplier_id), True)

    # 2. exact license_url (authoritative doc; rarely available)
    lic = (license_url or "").strip()
    if lic:
        for i, r in by_id.items():
            if (r["license_url"] or "").strip() and (r["license_url"] or "").strip() == lic:
                add(i, "license_url", "exact", True)

    # 3. exact email
    e = norm_email(email)
    if e:
        ids = email_ids.get(e, set())
        unique = len(_names_of(by_id, ids)) <= 1
        for i in ids:
            if unique or norm_name(by_id[i]["name"]) == app_norm:
                add(i, "email" + ("" if unique else "+name(shared-key)"), e, unique)

    # 4. exact phone
    p = norm_phone(phone)
    if p:
        ids = phone_ids.get(p, set())
        unique = len(_names_of(by_id, ids)) <= 1
        for i in ids:
            if unique or norm_name(by_id[i]["name"]) == app_norm:
                add(i, "phone" + ("" if unique else "+name(shared-key)"), p, unique)

    # 5. fragmentation expansion — sibling ids that share BOTH the same
    #    normalized name AND a strong key (email or phone) with a resolved id.
    seeds = list(detail.keys())
    for si in seeds:
        sr = by_id[si]
        s_norm = norm_name(sr["name"])
        s_emails = {x for x in (norm_email(sr["primary_contact_email"]), norm_email(sr["email"])) if x}
        s_phones = {x for x in (norm_phone(sr["office"]), norm_phone(sr["primary_contact_mobile"])) if x}
        for j, r in by_id.items():
            if j in detail or norm_name(r["name"]) != s_norm:
                continue
            j_emails = {x for x in (norm_email(r["primary_contact_email"]), norm_email(r["email"])) if x}
            j_phones = {x for x in (norm_phone(r["office"]), norm_phone(r["primary_contact_mobile"])) if x}
            if (s_emails & j_emails) or (s_phones & j_phones):
                add(j, "fragment_sibling(of %d)" % si, "same_name+shared_key", True)

    ids = sorted(detail.keys())
    return ids, [detail[i] for i in ids], by_id


# ---- ledger ----------------------------------------------------------------
def ledger(c, ids, by_id):
    ids = sorted(set(ids))
    out = {
        "resolved_ids": ids,
        "buys": {"cars": 0, "total_paid": 0, "vins": [], "rows": []},
        "resold": {"deals": 0, "purchase_cost": 0, "gross_front": 0, "rows": []},
        "sells_id_verified": {"deals": 0, "total": 0, "rows": []},
        "sells_name_advisory": {"deals": 0, "total": 0, "rows": [], "matched_names": []},
        "inventory_sourced": 0,
    }
    if not ids:
        return out
    ph = ",".join("?" * len(ids))

    # --- BUYS: payments (money-out, authoritative) --------------------------
    pay = c.execute(
        "SELECT vin_no, amount, created_at, vendor_name, title_status "
        "FROM payments WHERE vendor_id IN (%s) "
        "AND payee_type='Supplier' AND type='Purchased'" % ph, ids).fetchall()
    buy_vins = set()
    for r in pay:
        v = (r["vin_no"] or "").strip()
        out["buys"]["rows"].append({
            "vin": v, "amount": int(r["amount"] or 0),
            "date": _d10(r["created_at"]), "as": r["vendor_name"],
            "title": r["title_status"]})
        out["buys"]["total_paid"] += int(r["amount"] or 0)
        if v:
            buy_vins.add(v)

    # --- RESOLD overlay: deals.supplier_id ----------------------------------
    dsup = c.execute(
        "SELECT vin_no, purchase_cost, front_value, sale_price, sold_at, supplier_name "
        "FROM deals WHERE supplier_id IN (%s)" % ph, ids).fetchall()
    for r in dsup:
        v = (r["vin_no"] or "").strip()
        out["resold"]["rows"].append({
            "vin": v, "purchase_cost": int(r["purchase_cost"] or 0),
            "gross_front": int(r["front_value"] or 0),
            "resale_price": int(r["sale_price"] or 0), "date": _d10(r["sold_at"])})
        out["resold"]["purchase_cost"] += int(r["purchase_cost"] or 0)
        out["resold"]["gross_front"] += int(r["front_value"] or 0)
        if v:
            buy_vins.add(v)
    out["resold"]["deals"] = len(dsup)

    out["buys"]["vins"] = sorted(buy_vins)
    out["buys"]["cars"] = len(buy_vins)

    # --- SELLS (id-verified): deals.customer_id -----------------------------
    dcid = c.execute(
        "SELECT vin_no, sale_price, sold_at, customer_name "
        "FROM deals WHERE customer_id IN (%s)" % ph, ids).fetchall()
    for r in dcid:
        out["sells_id_verified"]["rows"].append({
            "vin": (r["vin_no"] or "").strip(), "amount": int(r["sale_price"] or 0),
            "date": _d10(r["sold_at"]), "as": r["customer_name"]})
        out["sells_id_verified"]["total"] += int(r["sale_price"] or 0)
    out["sells_id_verified"]["deals"] = len(dcid)

    # --- SELLS (advisory, name-based): deals.customer_name == canonical ------
    # customer_id is null on ~all sell rows, so the sell side is name-linked by
    # schema necessity. Match ONLY the canonical resolved supplier name(s),
    # never the applicant's typed name, and require >=5 chars to blunt short
    # personal-name collisions. Reported SEPARATELY as advisory.
    canon = {norm_name(by_id[i]["name"]) for i in ids if len(norm_name(by_id[i]["name"])) >= 5}
    id_verified_vins = {r["vin"] for r in out["sells_id_verified"]["rows"]}
    # A wholesale deal MIRRORS supplier_name into customer_name with a NULL
    # customer_id, so a "sold to them" name-match whose VIN we already recorded
    # as a car we BOUGHT is that mirror, not a real sale (proven: the 10 Naples
    # "sells" are the same 10 VINs we sourced). Exclude bought VINs entirely.
    bought_vins = set(out["buys"]["vins"])
    if canon:
        distinct = c.execute(
            "SELECT DISTINCT customer_name FROM deals WHERE customer_name<>''").fetchall()
        match_names = [r["customer_name"] for r in distinct
                       if norm_name(r["customer_name"]) in canon]
        out["sells_name_advisory"]["matched_names"] = match_names
        if match_names:
            nph = ",".join("?" * len(match_names))
            rows = c.execute(
                "SELECT vin_no, sale_price, sold_at, customer_name FROM deals "
                "WHERE customer_name IN (%s) COLLATE NOCASE" % nph, match_names).fetchall()
            for r in rows:
                v = (r["vin_no"] or "").strip()
                if v and (v in id_verified_vins or v in bought_vins):
                    continue  # already counted id-verified, or a bought-car mirror
                out["sells_name_advisory"]["rows"].append({
                    "vin": v, "amount": int(r["sale_price"] or 0),
                    "date": _d10(r["sold_at"]), "as": r["customer_name"]})
                out["sells_name_advisory"]["total"] += int(r["sale_price"] or 0)
            out["sells_name_advisory"]["deals"] = len(out["sells_name_advisory"]["rows"])

    # --- corroboration: inventory sourced-from ------------------------------
    inv = c.execute(
        "SELECT COUNT(*) n FROM inventory WHERE purchased_from_id IN (%s) "
        "AND purchased_from_type='Wholesaler'" % ph, ids).fetchone()
    out["inventory_sourced"] = inv["n"] or 0
    return out


# ---- reporting -------------------------------------------------------------
def run(applicant):
    c = _conn()
    try:
        ids, detail, by_id = resolve(c, **applicant)
        led = ledger(c, ids, by_id)
    finally:
        c.close()
    return {"applicant": applicant, "resolution": detail, "ledger": led}


def _fmt(res):
    a = res["applicant"]
    L = res["ledger"]
    out = []
    out.append("=" * 74)
    out.append("APPLICANT: name=%r phone=%r email=%r%s" % (
        a.get("name"), a.get("phone"), a.get("email"),
        (" id=%s" % a["supplier_id"]) if a.get("supplier_id") else ""))
    if not res["resolution"]:
        out.append("  RESOLVED ENTITY: <none> — no strong-key (id/license/email/phone) match.")
    else:
        out.append("  RESOLVED ENTITY ID(S) (strong-key only):")
        for d in res["resolution"]:
            out.append("     id=%-8s %-32s via=%s key=%r unique_key=%s" % (
                d["id"], repr(d["name"])[:32], d["via"], d["key"], d["unique"]))
    b, rs = L["buys"], L["resold"]
    total_sourced_cost = b["total_paid"] + rs["purchase_cost"]
    out.append("  --- CARS EW BOUGHT (id-verified, payments Supplier/Purchased + deals.supplier_id) ---")
    out.append("     cars_bought(distinct VINs)=%d   total_sourced_cost=$%s   inventory_sourced_corrob=%d" % (
        b["cars"], "{:,}".format(total_sourced_cost), L["inventory_sourced"]))
    out.append("       - via payments (money-out logged): %d cars, $%s" % (
        len(b["rows"]), "{:,}".format(b["total_paid"])))
    if rs["deals"]:
        out.append("       - resold-only deals (sourced, no Purchased payment row): %d cars, purchase_cost=$%s, gross_front=$%s" % (
            rs["deals"], "{:,}".format(rs["purchase_cost"]), "{:,}".format(rs["gross_front"])))
    for r in b["rows"]:
        out.append("       BUY  %-19s $%-10s %s  [title:%s]" % (
            r["vin"], "{:,}".format(r["amount"]), r["date"], r["title"]))
    sv, sa = L["sells_id_verified"], L["sells_name_advisory"]
    out.append("  --- CARS EW SOLD TO THEM ---")
    out.append("     id_verified(customer_id): deals=%d total=$%s" % (
        sv["deals"], "{:,}".format(sv["total"])))
    for r in sv["rows"]:
        out.append("       SELL %-19s $%-10s %s" % (r["vin"], "{:,}".format(r["amount"]), r["date"]))
    out.append("     name-based ADVISORY (customer_id is NULL in schema; matched canonical name(s) %s):" % (
        sa["matched_names"] or "none"))
    out.append("       deals=%d total=$%s" % (sa["deals"], "{:,}".format(sa["total"])))
    for r in sa["rows"]:
        out.append("       sell?%-19s $%-10s %s  as=%r" % (
            r["vin"], "{:,}".format(r["amount"]), r["date"], r["as"]))
    out.append("=" * 74)
    return "\n".join(out)


SELFTESTS = [
    {"label": "OpiesGarage (operator) — MUST be 0; NOT the other Oscar's Mustang",
     "applicant": {"name": "OpiesGarage", "phone": "407-430-9675", "email": "opies32765@gmail.com"}},
    {"label": "Auto Street Usa — MUST be 18 buys / $1,194,400",
     "applicant": {"name": "AutoStreetUSA", "phone": "561-685-0133", "email": "autostreetusa@gmail.com"}},
    {"label": "The Naples Source Inc — buys + sells",
     "applicant": {"name": "The Naples Source Inc", "phone": "239-595-4021", "email": "Sam@thenaplessource.com"}},
]


def main():
    ap = argparse.ArgumentParser(description="Collision-safe LSL dealer ledger (read-only).")
    ap.add_argument("--name"); ap.add_argument("--phone"); ap.add_argument("--email")
    ap.add_argument("--id", type=int, dest="supplier_id")
    ap.add_argument("--license"); ap.add_argument("--address")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        for t in SELFTESTS:
            print("\n### " + t["label"])
            r = run(t["applicant"])
            print(_fmt(r))
            b = r["ledger"]["buys"]; rs = r["ledger"]["resold"]
            print(">>> HEADLINE: resolved_ids=%s  cars_bought=%d  payments_money_out=$%s  total_sourced_cost=$%s  sold_to_them=%d($%s)" % (
                r["ledger"]["resolved_ids"], b["cars"], "{:,}".format(b["total_paid"]),
                "{:,}".format(b["total_paid"] + rs["purchase_cost"]),
                r["ledger"]["sells_id_verified"]["deals"] + r["ledger"]["sells_name_advisory"]["deals"],
                "{:,}".format(r["ledger"]["sells_id_verified"]["total"] + r["ledger"]["sells_name_advisory"]["total"])))
        return

    applicant = {"name": args.name, "phone": args.phone, "email": args.email,
                 "supplier_id": args.supplier_id, "license_url": args.license,
                 "address": args.address}
    r = run(applicant)
    if args.json:
        print(json.dumps(r, indent=2, default=str))
    else:
        print(_fmt(r))


if __name__ == "__main__":
    main()
