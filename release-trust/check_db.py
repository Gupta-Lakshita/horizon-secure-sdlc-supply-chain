import sqlite3

db = sqlite3.connect("release_trust.db")

tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()

print([t[0] for t in tables])

db.close()
