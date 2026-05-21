import sqlite3
db = sqlite3.connect("/app/data/cafe.db")
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
    status TEXT DEFAULT 'pending',
    expense_id INTEGER DEFAULT NULL,
    parsed_at TEXT
)""")
db.execute("""CREATE TABLE IF NOT EXISTS ingredient_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_name TEXT NOT NULL UNIQUE,
    ingredient_id INTEGER,
    action TEXT DEFAULT 'match',
    created_at TEXT
)""")
db.execute("""CREATE TABLE IF NOT EXISTS invoice_items_pending (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parsed_invoice_id INTEGER NOT NULL,
    invoice_name TEXT NOT NULL,
    quantity REAL DEFAULT 0,
    unit_price REAL DEFAULT 0,
    total REAL DEFAULT 0,
    status TEXT DEFAULT 'pending',
    ingredient_id INTEGER,
    suggested_ingredient TEXT DEFAULT '',
    confidence REAL DEFAULT 0
)""")
db.commit()
print("All tables created OK")
db.close()
