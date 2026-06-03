import sys
target = sys.argv[1]
anchor = sys.argv[2]
snip = sys.stdin.read()
s = open(target).read()
n = s.count(anchor)
assert n == 1, "anchor count %d for %r" % (n, anchor)
pos = s.index(anchor)
ls = s.rfind(chr(10), 0, pos) + 1
out = s[:ls] + snip + s[ls:]
open(target, "w").write(out)
print("SPLICED %s before %r" % (target, anchor))
