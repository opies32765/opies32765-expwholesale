"""Run a few queries as expuser (the gunicorn role) to see what gunicorn sees."""
import psycopg2
DSN = 'postgresql://expuser:ExpWholesale2026!@localhost:5433/expwholesale'
conn = psycopg2.connect(DSN)
cur = conn.cursor()
cur.execute("SELECT current_database(), current_user, inet_server_port()")
print('connection:', cur.fetchone())
cur.execute("SELECT status, COUNT(*) FROM bids GROUP BY status")
print('bid counts by status:', cur.fetchall())
cur.execute("SELECT COUNT(*) FROM bids")
print('total bids:', cur.fetchone()[0])
cur.execute("SELECT id, year, make, model FROM bids ORDER BY id DESC LIMIT 3")
print('latest 3:', cur.fetchall())
