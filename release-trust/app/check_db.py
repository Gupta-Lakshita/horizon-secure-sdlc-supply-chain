import sqlite3

conn = sqlite3.connect("release_trust.db")
cur = conn.cursor()

tables = cur.execute(
    "SELECT name FROM sqlite_master WHERE type='table'"
).fetchall()

print("TABLES:")
for t in tables:
    print(" -", t[0])

for (table,) in tables:
    print(f"\n=== {table} ===")
    cur.execute(f"PRAGMA table_info({table})")
    for row in cur.fetchall():
        print(row)

conn.close()