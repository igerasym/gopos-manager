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

        CREATE TABLE IF NOT EXISTS ingredients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            unit TEXT NOT NULL DEFAULT 'g',
            quantity REAL DEFAULT 0,
            min_quantity REAL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS recipes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_name TEXT NOT NULL,
            ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
            amount REAL NOT NULL,
            UNIQUE(product_name, ingredient_id)
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
