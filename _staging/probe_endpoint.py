"""Run the EXACT same query sequence /api/bids runs, in order, on the same DSN
gunicorn uses, so we see where it goes wrong."""
import psycopg2, psycopg2.extras
DSN = 'postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale'
db = psycopg2.connect(DSN, cursor_factory=psycopg2.extras.RealDictCursor)
cur = db.cursor()

print('1. visitor insert')
try:
    cur.execute("""INSERT INTO dashboard_visitors (ip, last_seen) VALUES (%s, NOW())
                   ON CONFLICT (ip) DO UPDATE SET last_seen = NOW()""", ('127.0.0.1',))
    db.commit()
    print('   ok')
except Exception as e:
    print(f'   ERR: {e}')
    db.rollback()

print('2. stats query')
cur.execute("SELECT status, COUNT(*) as cnt FROM bids GROUP BY status")
rows = cur.fetchall()
print(f'   rows: {len(rows)}')
for r in rows[:5]:
    print(f'     {r}')

print('3. bids query, status=all (default), no params')
cur.execute("""
    SELECT b.id, b.year, b.make, b.model, b.status FROM bids b
    ORDER BY b.created_at DESC LIMIT 5
""")
rows = cur.fetchall()
print(f'   rows: {len(rows)}')
for r in rows:
    print(f'     {r}')
