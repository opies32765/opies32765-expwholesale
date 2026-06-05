import ast, shutil, sys
P = "/opt/expwholesale/ew_mcp.py"
s = open(P).read()
edits = []

# 1. lsl_deals_booked signature: add basis
edits.append(("deals_sig",
'''async def lsl_deals_booked(
    caller_name: str,
    caller_pin: str = "",
    period: str = "yesterday",
) -> dict:''',
'''async def lsl_deals_booked(
    caller_name: str,
    caller_pin: str = "",
    period: str = "yesterday",
    basis: str = "sold",
) -> dict:'''))

# 2. when basis=bought, flip the built period clause from sold_at to created_at
edits.append(("deals_basis",
'''    if not period_sql:
        return {"error": f"unsupported period {period!r}; supported:''',
'''    if str(basis).strip().lower().startswith("b") and period_sql:
        period_sql = period_sql.replace("sold_at", "created_at")

    if not period_sql:
        return {"error": f"unsupported period {period!r}; supported:'''))

ok = True
for name, o, n in edits:
    c = s.count(o)
    print("%-12s anchor count = %d" % (name, c))
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
shutil.copy(P, P + ".bak.basisdeals")
open(P, "w").write(s)
print("WROTE OK (backup: ew_mcp.py.bak.basisdeals)")
