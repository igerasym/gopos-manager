import sqlite3
db = sqlite3.connect("/app/data/cafe.db")

# Check current schema
cols = [c[1] for c in db.execute("PRAGMA table_info(parsed_invoices)").fetchall()]
print("Current columns:", cols)

if 'parse_status' not in cols:
    # Recreate table with new schema
    db.execute("DROP TABLE IF EXISTS parsed_invoices")
    db.execute("""CREATE TABLE parsed_invoices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_id TEXT NOT NULL UNIQUE,
        file_name TEXT NOT NULL,
        invoice_number TEXT DEFAULT '',
        folder TEXT DEFAULT '',
        month TEXT DEFAULT '',
        vendor TEXT DEFAULT '',
        category TEXT DEFAULT 'Продукти',
        total REAL DEFAULT 0,
        items_json TEXT DEFAULT '[]',
        parse_status TEXT DEFAULT 'pending',
        expense_status TEXT DEFAULT 'pending',
        expense_id INTEGER DEFAULT NULL,
        parsed_at TEXT
    )""")
    db.commit()
    print("Recreated with parse_status + expense_status")
else:
    print("Already migrated")

db.close()
