import ast, shutil, sys
P = "/opt/expwholesale/ew_mcp.py"
s = open(P).read()
o = r'''{period_sql.replace("sold_at","d.sold_at")}'''
n = r'''{period_sql.replace("sold_at","d.sold_at").replace("created_at","d.created_at")}'''
c = s.count(o)
print("anchor count =", c)
if c != 1:
    print("ABORT: anchor mismatch -> NO WRITE"); sys.exit(1)
s = s.replace(o, n)
try:
    ast.parse(s)
except SyntaxError as e:
    print("ABORT: syntax error ->", e); sys.exit(2)
shutil.copy(P, P + ".bak.ambfix")
open(P, "w").write(s)
print("WROTE OK (backup: ew_mcp.py.bak.ambfix)")
