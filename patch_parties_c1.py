import ast, shutil, sys
P = "/opt/expwholesale/ew_mcp.py"
s = open(P).read()

ANCHOR = '    return await asyncio.to_thread(_lsl_data_query_impl, agg, agg_field, group_by, filters, period, order, limit, basis)'

NEWCODE = ANCHOR + r'''


def _lsl_deal_parties_impl(query="", limit=10):
    import sqlite3, os as _os, re as _re
    PATH = _os.environ.get("LSL_DB_PATH", "/opt/livesaleslog/crm.db")
    if not _os.path.exists(PATH):
        return {"error": "lsl ledger not found"}
    where, params = [], []
    q = (query or "").strip()
    if q:
        if " " not in q and _re.match(r"^[A-Za-z0-9]{11,17}$", q):
            where.append("UPPER(d.vin_no) LIKE ?"); params.append("%" + q.upper() + "%")
        else:
            where.append("(UPPER(d.vehicle_info) LIKE UPPER(?) OR UPPER(d.stock_no) LIKE UPPER(?))")
            params += ["%" + q + "%", "%" + q + "%"]
    wsql = (" WHERE " + " AND ".join(where)) if where else ""
    lim = max(1, min(int(limit) if limit else 10, 50))
    try:
        c = sqlite3.connect("file:%s?mode=ro" % PATH, uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        cur = c.cursor()
        cur.execute(
            "SELECT d.vehicle_info, d.vin_no, d.stock_no, "
            "COALESCE(i.source, d.supplier_name) AS bought_from, "
            "i.customer_name AS sold_to, i.sale_status AS sale_status, "
            "d.sales_person AS sales_rep, d.buyer_name AS our_buyer, "
            "d.sale_price, d.sold_at "
            "FROM deals d LEFT JOIN inventory i ON i.stock_no = d.stock_no"
            + wsql + " ORDER BY d.sold_at DESC LIMIT ?", params + [lim])
        rows = [dict(r) for r in cur.fetchall()]
        c.close()
        return {"n": len(rows), "rows": rows}
    except Exception as e:
        return {"error": "query failed: %s" % e}


@mcp.tool()
async def lsl_deal_parties(query: str = "", limit: int = 10) -> dict:
    """OWNER. Who we BOUGHT a car FROM and who we SOLD it TO (BOTH dealers), plus the rep, for a
    specific car or recent deals. query = VIN, stock number, or 'year make model' (e.g. '2022 VW Atlas');
    empty = most recent deals. Returns per car: vehicle_info, bought_from (the dealer we bought from),
    sold_to (the dealer we sold to), sales_rep, our_buyer, sale_status, sale_price, sold_at."""
    return await asyncio.to_thread(_lsl_deal_parties_impl, query, limit)'''

c = s.count(ANCHOR)
print("anchor count =", c)
if c != 1:
    print("ABORT: anchor mismatch -> NO WRITE"); sys.exit(1)
s = s.replace(ANCHOR, NEWCODE)
try:
    ast.parse(s)
except SyntaxError as e:
    print("ABORT: syntax error ->", e); sys.exit(2)
shutil.copy(P, P + ".bak.parties")
open(P, "w").write(s)
print("WROTE OK (backup: ew_mcp.py.bak.parties)")
