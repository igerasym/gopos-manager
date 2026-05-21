import sqlite3
db = sqlite3.connect("/app/data/cafe.db")
db.execute("""CREATE TABLE IF NOT EXISTS vendor_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor_pattern TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL DEFAULT 'Продукти',
    expense_name TEXT NOT NULL DEFAULT ''
)""")
db.execute("""CREATE TABLE IF NOT EXISTS ingredient_price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ingredient_id INTEGER NOT NULL,
    price REAL NOT NULL,
    date TEXT,
    invoice_id INTEGER,
    note TEXT DEFAULT ''
)""")
db.commit()
print("vendor_rules + ingredient_price_history created OK")
db.close()
