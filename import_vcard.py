"""Import iCloud/Google vCard into bill_contacts.

Usage: python3 import_vcard.py /path/to/contacts.vcf
"""
import os, re, sys
import psycopg2

PG = dict(host="localhost", port=5433, dbname="expwholesale",
          user="expuser", password="ExpWholesale2026!")


def normalize_phone(raw):
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    # US numbers: 10 digits → +1XXXXXXXXXX; 11 with leading 1 → +1 + last 10
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if len(digits) >= 11 and len(digits) <= 15:
        # already international
        return "+" + digits
    return None  # too short / weird


def parse_vcards(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    cards = []
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if line == "BEGIN:VCARD":
            current = {"name": None, "phones": []}
        elif line == "END:VCARD":
            if current:
                cards.append(current)
                current = None
        elif current is not None:
            if line.startswith("FN:"):
                current["name"] = line[3:].strip()
            elif line.startswith("FN;"):
                # FN;CHARSET=UTF-8:Name
                _, _, val = line.partition(":")
                current["name"] = val.strip()
            elif line.startswith("TEL"):
                # TEL;TYPE=CELL:+1...   or   TEL;type=CELL,VOICE,pref:+1...
                _, _, val = line.partition(":")
                low = line.lower()
                kind = "other"
                if "cell" in low or "mobile" in low or "iphone" in low:
                    kind = "mobile"
                elif "work" in low:
                    kind = "work"
                elif "home" in low:
                    kind = "home"
                ph = normalize_phone(val)
                if ph:
                    current["phones"].append((kind, ph))
    return cards


def pick_best_phone(phones):
    """Prefer mobile > work > home > other. Return one number."""
    if not phones:
        return None
    order = {"mobile": 0, "work": 1, "home": 2, "other": 3}
    phones_sorted = sorted(phones, key=lambda kp: order.get(kp[0], 99))
    return phones_sorted[0][1]


def main(path):
    if not os.path.exists(path):
        sys.exit(f"file not found: {path}")
    cards = parse_vcards(path)
    print(f"parsed {len(cards)} vcards from {path}")

    conn = psycopg2.connect(**PG)
    conn.autocommit = False
    cur = conn.cursor()

    imported = updated = skipped = 0
    for c in cards:
        name = (c.get("name") or "").strip()
        ph = pick_best_phone(c.get("phones") or [])
        if not name or not ph:
            skipped += 1
            continue
        try:
            cur.execute("""
                INSERT INTO bill_contacts (name, phone_e164, source, bill_can_text)
                VALUES (%s, %s, 'icloud_vcard', FALSE)
                ON CONFLICT (phone_e164)
                DO UPDATE SET name = EXCLUDED.name, updated_at = NOW()
                RETURNING (xmax = 0) AS inserted
            """, (name, ph))
            was_insert = cur.fetchone()[0]
            if was_insert:
                imported += 1
            else:
                updated += 1
        except Exception as e:
            print(f"  err {name} ({ph}): {e}")
            conn.rollback()
            continue
    conn.commit()
    print(f"\n  imported new: {imported}")
    print(f"  updated existing: {updated}")
    print(f"  skipped (no name/phone): {skipped}")
    print(f"  total in table: ", end="")
    cur.execute("SELECT count(*) FROM bill_contacts")
    print(cur.fetchone()[0])
    print(f"\nNone are yet allowed to text. To enable a contact:")
    print(f"  UPDATE bill_contacts SET bill_can_text=TRUE WHERE name ILIKE '%joe%';")
    cur.close()
    conn.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: import_vcard.py /path/to/contacts.vcf")
    main(sys.argv[1])
