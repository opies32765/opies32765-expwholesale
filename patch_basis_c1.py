import ast, shutil, sys
P = "/opt/expwholesale/ew_mcp.py"
s = open(P).read()
edits = []

# 1. impl signature: add basis="sold"
edits.append(("impl_sig",
'def _lsl_data_query_impl(agg="list", agg_field="", group_by="", filters="", period="", order="desc", limit=25):',
'def _lsl_data_query_impl(agg="list", agg_field="", group_by="", filters="", period="", order="desc", limit=25, basis="sold"):'))

# 2. period map -> use _dc (created_at for "bought", sold_at default)
edits.append(("pmap",
r'''    p = (period or "").strip().lower()
    pmap = {
        "today": "date(sold_at)=date('now')",
        "yesterday": "date(sold_at)=date('now','-1 day')",
        "last_7_days": "sold_at >= date('now','-7 days')",
        "last_30_days": "sold_at >= date('now','-30 days')",
        "this_month": "strftime('%Y-%m',sold_at)=strftime('%Y-%m','now')",
        "last_month": "strftime('%Y-%m',sold_at)=strftime('%Y-%m','now','-1 month')",
        "ytd": "strftime('%Y',sold_at)=strftime('%Y','now')",
        "this_year": "strftime('%Y',sold_at)=strftime('%Y','now')",
        "last_year": "strftime('%Y',sold_at)=strftime('%Y','now','-1 year')",
    }''',
r'''    p = (period or "").strip().lower()
    _dc = "created_at" if str(basis).strip().lower().startswith("b") else "sold_at"
    pmap = {
        "today": f"date({_dc})=date('now')",
        "yesterday": f"date({_dc})=date('now','-1 day')",
        "last_7_days": f"{_dc} >= date('now','-7 days')",
        "last_30_days": f"{_dc} >= date('now','-30 days')",
        "this_month": f"strftime('%Y-%m',{_dc})=strftime('%Y-%m','now')",
        "last_month": f"strftime('%Y-%m',{_dc})=strftime('%Y-%m','now','-1 month')",
        "ytd": f"strftime('%Y',{_dc})=strftime('%Y','now')",
        "this_year": f"strftime('%Y',{_dc})=strftime('%Y','now')",
        "last_year": f"strftime('%Y',{_dc})=strftime('%Y','now','-1 year')",
    }'''))

# 3. ISO range clause -> use _dc
edits.append(("range",
r'''        a, b = p.split(":"); where.append("date(sold_at) BETWEEN ? AND ?"); params += [a, b]''',
r'''        a, b = p.split(":"); where.append(f"date({_dc}) BETWEEN ? AND ?"); params += [a, b]'''))

# 4. lsl_data_query signature: add basis
edits.append(("tool_sig",
'async def lsl_data_query(agg: str = "list", agg_field: str = "", group_by: str = "", filters: str = "", period: str = "", order: str = "desc", limit: int = 25) -> dict:',
'async def lsl_data_query(agg: str = "list", agg_field: str = "", group_by: str = "", filters: str = "", period: str = "", order: str = "desc", limit: int = 25, basis: str = "sold") -> dict:'))

# 5. pass basis through
edits.append(("tool_call",
'    return await asyncio.to_thread(_lsl_data_query_impl, agg, agg_field, group_by, filters, period, order, limit)',
'    return await asyncio.to_thread(_lsl_data_query_impl, agg, agg_field, group_by, filters, period, order, limit, basis)'))

ok = True
for name, o, n in edits:
    c = s.count(o)
    print("%-10s anchor count = %d" % (name, c))
    if c != 1:
        ok = False
if not ok:
    print("ABORT: anchor mismatch -> NO WRITE"); sys.exit(1)
for name, o, n in edits:
    s = s.replace(o, n)
try:
    ast.parse(s)
except SyntaxError as e:
    print("ABORT: syntax error ->", e); sys.exit(2)
shutil.copy(P, P + ".bak.basis")
open(P, "w").write(s)
print("WROTE OK (backup: ew_mcp.py.bak.basis)")
