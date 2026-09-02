"""load_comps.py - ingest Edge Pipeline CSV exports into auction_comps.

Runs ON C1. Idempotent: ON CONFLICT DO UPDATE keyed on
(auction_slug, sale_date, stock_no), so re-running a file is safe and a
pre-sale row later gets its price filled in by the matching post-sale row
WITHOUT losing the vin the pre-sale row carried.

Filenames drive slug + outcome:
    edgepipeline_postsale_<slug>-all_YYYYMMDD.csv   -> sold
    edgepipeline_nosale_<slug>-all_YYYYMMDD.csv     -> no_sale
    edgepipeline_presale_<slug>-all_YYYYMMDD.csv    -> pre_sale
"""
import csv, glob, os, re, sys
import psycopg2, psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from edge_canon import canon_make, canon_model

DSN = os.environ.get("DATABASE_URL")
if not DSN:
    for ln in open("/etc/default/expwholesale-mcp"):
        if ln.strip().startswith("DATABASE_URL="):
            DSN = ln.strip().split("=", 1)[1].strip().strip('"').strip("'")

# The "-all" segment is NOT universal: Orlando Longwood exports as
# "...orlandolongwoodaafl-all_20260828.csv" but Pensacola exports as
# "...aaapensacola_20260901.csv". Make it optional or those files skip SILENTLY.
FNAME = re.compile(
    r"edgepipeline_(postsale|nosale|presale)_(.+?)(?:-all)?_(\d{4})(\d{2})(\d{2})\.csv$")
OUTCOME = {"postsale": "sold", "nosale": "no_sale", "presale": "pre_sale"}


def i(s):
    try:
        return int(float(str(s).replace(",", "").strip()))
    except Exception:
        return None


def f(s):
    try:
        return float(str(s).strip())
    except Exception:
        return None


def t(s):
    s = (s or "").strip()
    return s or None


UPSERT = """
INSERT INTO auction_comps
 (auction_slug, sale_date, stock_no, run_number, lane, lot, vin, year, make,
  model, style, color, odometer, has_cr, grade, lights, announcements,
  outcome, price, picture_count, canon_make, canon_model, source_file)
VALUES %s
ON CONFLICT (auction_slug, sale_date, stock_no) DO UPDATE SET
  -- never blank out a vin we already captured pre-sale
  vin           = COALESCE(auction_comps.vin, EXCLUDED.vin),
  price         = COALESCE(EXCLUDED.price, auction_comps.price),
  -- pre_sale is the weakest claim; a later sold/no_sale row wins
  outcome       = CASE WHEN EXCLUDED.outcome = 'pre_sale'
                       THEN auction_comps.outcome ELSE EXCLUDED.outcome END,
  grade         = COALESCE(EXCLUDED.grade, auction_comps.grade),
  odometer      = COALESCE(EXCLUDED.odometer, auction_comps.odometer),
  lights        = COALESCE(EXCLUDED.lights, auction_comps.lights),
  announcements = COALESCE(EXCLUDED.announcements, auction_comps.announcements),
  source_file   = EXCLUDED.source_file
"""


def main(paths):
    conn = psycopg2.connect(DSN, connect_timeout=15)
    conn.autocommit = False
    cur = conn.cursor()
    total = 0
    for path in sorted(paths):
        m = FNAME.search(os.path.basename(path))
        if not m:
            print("  skip (name):", os.path.basename(path))
            continue
        kind, slug, y, mo, d = m.groups()
        outcome = OUTCOME[kind]
        sale_date = f"{y}-{mo}-{d}"
        rows = []
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                stock = t(r.get("Stock #") or r.get("Stock Number"))
                if not stock:
                    continue
                price = i(r.get("Price"))
                rows.append((
                    slug, sale_date, stock,
                    t(r.get("Run Number")), t(r.get("Lane")), t(r.get("Lot")),
                    t(r.get("VIN") or r.get("Vin")),
                    i(r.get("Year")), t(r.get("Make")), t(r.get("Model")),
                    t(r.get("Style")), t(r.get("Color") or r.get("Exterior Color")),
                    i(r.get("Odometer") or r.get("Mileage")),
                    (t(r.get("CR")) or "").lower() in ("yes", "true"),
                    f(r.get("Grade")), t(r.get("Lights")), t(r.get("Announcements")),
                    outcome,
                    price if outcome == "sold" else None,
                    i(r.get("Picture Count")),
                    canon_make(r.get("Make")), canon_model(r.get("Model")),
                    os.path.basename(path),
                ))
        psycopg2.extras.execute_values(cur, UPSERT, rows, page_size=500)
        conn.commit()
        total += len(rows)
        print(f"  {os.path.basename(path):<62} {outcome:<8} {len(rows):>5}")
    cur.execute("SELECT outcome, count(*), count(vin) FROM auction_comps GROUP BY outcome ORDER BY 1")
    print("\n  outcome    rows   with_vin")
    for o, n, nv in cur.fetchall():
        print(f"  {o:<10} {n:>5}   {nv:>6}")
    conn.close()
    print(f"\nprocessed {total} rows")


if __name__ == "__main__":
    args = sys.argv[1:] or glob.glob("/opt/expwholesale/edge_comps_in/*.csv")
    main(args)
