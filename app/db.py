import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / 'data' / 'cafe.db'


def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys = ON')
    return db


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

        CREATE TABLE IF NOT EXISTS sync_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            message TEXT DEFAULT ''
        );
    ''')
    db.commit()
    db.close()


if __name__ == '__main__':
    init_db()
    print(f'Database initialized at {DB_PATH}')
