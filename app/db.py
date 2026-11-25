import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / 'data' / 'cafe.db'


def get_db() -> sqlite3.Connection:
    # Web requests, the scheduler and webhook threads all write here: wait for
    # a lock instead of failing with "database is locked".
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys = ON')
    db.execute('PRAGMA busy_timeout = 15000')
    return db


def enable_wal():
    """WAL lets readers work while a sync writes. Persistent, so set once at startup.
    Back up with `sqlite3 .backup` (or copy cafe.db together with -wal/-shm)."""
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.execute('PRAGMA journal_mode = WAL')
    db.close()


def init_db():
    db = get_db()
    db.executescript('''
        CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            product_name TEXT NOT NULL,
            quantity REAL DEFAULT 0,
            total_money REAL DEFAULT 0,
            net_total REAL DEFAULT 0,
            discount REAL DEFAULT 0,
            net_profit REAL DEFAULT 0,
            UNIQUE(date, product_name)
        );

        CREATE TABLE IF NOT EXISTS suppliers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            contact TEXT DEFAULT '',
            note TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS ingredients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            unit TEXT NOT NULL DEFAULT 'g',
            quantity REAL DEFAULT 0,
            min_quantity REAL DEFAULT 0,
            unit_price REAL DEFAULT 0,
            supplier_id INTEGER REFERENCES suppliers(id)
        );

        CREATE TABLE IF NOT EXISTS recipes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_name TEXT NOT NULL,
            ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
            amount REAL NOT NULL,
            UNIQUE(product_name, ingredient_id)
        );

        CREATE TABLE IF NOT EXISTS recipe_cards (
            product_name TEXT PRIMARY KEY,
            category TEXT DEFAULT '',
            portion_weight TEXT DEFAULT '',
            description TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT DEFAULT (date('now')),
            ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
            quantity REAL NOT NULL,
            price REAL DEFAULT 0,
            note TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS inventory_deductions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
            amount REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'staff',
            display_name TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS sub_recipes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ingredient_id INTEGER NOT NULL UNIQUE REFERENCES ingredients(id),
            yield_amount REAL NOT NULL DEFAULT 1,
            yield_unit TEXT NOT NULL DEFAULT 'kg',
            description TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS sub_recipe_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sub_recipe_id INTEGER NOT NULL REFERENCES sub_recipes(id),
            ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
            amount REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS stock_counts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            time TEXT DEFAULT '',
            user_id INTEGER REFERENCES users(id),
            note TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS stock_count_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_count_id INTEGER NOT NULL REFERENCES stock_counts(id),
            ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
            expected REAL DEFAULT 0,
            actual REAL DEFAULT 0,
            difference REAL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'Інше',
            amount REAL NOT NULL,
            month TEXT NOT NULL,
            recurring INTEGER DEFAULT 0,
            note TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS expenses_dismissed (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            month TEXT NOT NULL,
            UNIQUE(name, month)
        );

        CREATE TABLE IF NOT EXISTS parsed_invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id TEXT NOT NULL UNIQUE,
            file_name TEXT NOT NULL,
            file_hash TEXT DEFAULT '',
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
            parsed_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS ingredient_mappings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_name TEXT NOT NULL UNIQUE,
            ingredient_id INTEGER,
            action TEXT DEFAULT 'match',
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS invoice_items_pending (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parsed_invoice_id INTEGER NOT NULL REFERENCES parsed_invoices(id),
            invoice_name TEXT NOT NULL,
            quantity REAL DEFAULT 0,
            unit_price REAL DEFAULT 0,
            total REAL DEFAULT 0,
            status TEXT DEFAULT 'pending',
            ingredient_id INTEGER,
            suggested_ingredient TEXT DEFAULT '',
            confidence REAL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS vendor_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vendor_pattern TEXT NOT NULL UNIQUE,
            category TEXT NOT NULL DEFAULT 'Продукти',
            expense_name TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS ingredient_price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
            price REAL NOT NULL,
            date TEXT DEFAULT (date('now')),
            invoice_id INTEGER,
            note TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS sync_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            message TEXT DEFAULT ''
        );

        -- GoPOS orders already counted in `sales` (webhook + full sync), so a
        -- retried or replayed webhook can't add the same order twice.
        CREATE TABLE IF NOT EXISTS processed_orders (
            order_id TEXT PRIMARY KEY,
            date TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_processed_orders_date ON processed_orders(date);

        -- Takeaway vs dine-in per business day (an order with a To-go cup/box is takeaway).
        -- Written by sync_orders_for_date from the orders it already loads; read by the bot.
        CREATE TABLE IF NOT EXISTS daily_takeaway (
            date TEXT PRIMARY KEY,
            orders INTEGER NOT NULL,
            revenue REAL NOT NULL,
            takeaway_orders INTEGER NOT NULL,
            takeaway_revenue REAL NOT NULL
        );

        -- Wastage log: manual writeoffs for spoiled / broken / expired items
        -- source='manual': ingredient_id required, product_name optional
        -- source='gopos':  product_name required, ingredient_id NULL (resolved at display time)
        CREATE TABLE IF NOT EXISTS wastage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL DEFAULT (date('now')),
            ingredient_id INTEGER REFERENCES ingredients(id),
            product_name TEXT DEFAULT '',
            quantity REAL NOT NULL,
            reason TEXT NOT NULL DEFAULT 'spoiled',
            note TEXT DEFAULT '',
            source TEXT NOT NULL DEFAULT 'manual',
            user_id INTEGER REFERENCES users(id),
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_wastage_date ON wastage(date);
        CREATE INDEX IF NOT EXISTS idx_wastage_ingredient ON wastage(ingredient_id);
        CREATE INDEX IF NOT EXISTS idx_wastage_product ON wastage(product_name);

        -- "Що потребує уваги" rows a user hid with "Сховати до завтра".
        -- (kind, key) as produced by app/services/attention.py; hidden while today < until.
        CREATE TABLE IF NOT EXISTS attention_dismissed (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            kind TEXT NOT NULL,
            key TEXT NOT NULL DEFAULT '',
            until TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(user_id, kind, key)
        );

        CREATE TABLE IF NOT EXISTS pos_products (
            product_name TEXT PRIMARY KEY,
            pos_kind TEXT NOT NULL DEFAULT 'unclassified',
            resale_ingredient_id INTEGER,
            category TEXT,
            classified_at TEXT,
            classified_by TEXT,
            gopos_item_id INTEGER,
            gopos_price REAL,
            gopos_group_id INTEGER,
            FOREIGN KEY (resale_ingredient_id) REFERENCES ingredients(id)
        );
    ''')
    # Columns added after the original schema. Prod already has them (added by
    # hand / scripts/migrate_resale.py); a fresh DB needs them too.
    _add_column(db, 'ingredients', 'kind', "TEXT NOT NULL DEFAULT 'raw'")
    _add_column(db, 'deliveries', 'supplier_id', 'INTEGER REFERENCES suppliers(id)')
    _add_column(db, 'pos_products', 'gopos_item_id', 'INTEGER')
    _add_column(db, 'pos_products', 'gopos_price', 'REAL')
    _add_column(db, 'pos_products', 'gopos_group_id', 'INTEGER')
    db.commit()
    db.close()


def _add_column(db: sqlite3.Connection, table: str, column: str, ddl: str):
    """Idempotent ALTER TABLE ADD COLUMN (table/column names are code constants)."""
    cols = {r[1] for r in db.execute(f'PRAGMA table_info({table})')}
    if column not in cols:
        db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {ddl}')


if __name__ == '__main__':
    init_db()
    print(f'Database initialized at {DB_PATH}')
