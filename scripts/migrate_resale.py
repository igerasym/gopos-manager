"""Migration: Add pos_products table + ingredients.kind column.

Run on prod: docker exec cafe-manager-cafe-1 python3 scripts/migrate_resale.py
"""
import sqlite3
import json
from datetime import datetime

DB_PATH = '/app/data/cafe.db'


def migrate():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    print("=" * 60)
    print("MIGRATION: Resale Items & COGS Tracking")
    print("=" * 60)

    # ─── Step 1: Add kind column to ingredients ───
    print("\n[1/5] Adding 'kind' column to ingredients...")
    cols = [r[1] for r in db.execute("PRAGMA table_info(ingredients)").fetchall()]
    if 'kind' not in cols:
        db.execute("ALTER TABLE ingredients ADD COLUMN kind TEXT NOT NULL DEFAULT 'raw'")
        print("  ✓ Added 'kind' column (default='raw')")
    else:
        print("  · Column already exists, skipping")

    # ─── Step 2: Create pos_products table ───
    print("\n[2/5] Creating pos_products table...")
    db.execute('''
        CREATE TABLE IF NOT EXISTS pos_products (
            product_name TEXT PRIMARY KEY,
            pos_kind TEXT NOT NULL DEFAULT 'unclassified',
            resale_ingredient_id INTEGER,
            category TEXT,
            classified_at TEXT,
            classified_by TEXT,
            FOREIGN KEY (resale_ingredient_id) REFERENCES ingredients(id)
        )
    ''')
    print("  ✓ Table created")

    # ─── Step 3: Populate pos_products from sales ───
    print("\n[3/5] Populating pos_products from sales...")
    all_products = db.execute("SELECT DISTINCT product_name FROM sales ORDER BY product_name").fetchall()
    existing = set(r[0] for r in db.execute("SELECT product_name FROM pos_products").fetchall())
    new_count = 0
    for row in all_products:
        name = row['product_name']
        if name not in existing:
            db.execute("INSERT INTO pos_products (product_name) VALUES (?)", (name,))
            new_count += 1
    print(f"  ✓ Inserted {new_count} products ({len(all_products)} total in sales)")

    # ─── Step 4: Auto-classify ───
    print("\n[4/5] Auto-classifying products...")
    now = datetime.now().isoformat()

    # Products with recipes → prepared
    prepared = db.execute('''
        UPDATE pos_products SET pos_kind = 'prepared', classified_at = ?, classified_by = 'auto'
        WHERE product_name IN (SELECT DISTINCT product_name FROM recipes)
          AND pos_kind = 'unclassified'
    ''', (now,))
    print(f"  ✓ {prepared.rowcount} products → prepared (have recipe)")

    # Resale: ingredients that match sales product_name and have no recipe
    resale_ings = db.execute('''
        SELECT i.id, i.name FROM ingredients i
        WHERE i.name IN (SELECT DISTINCT product_name FROM sales)
          AND i.name NOT IN (SELECT DISTINCT product_name FROM recipes)
    ''').fetchall()

    resale_count = 0
    for ing in resale_ings:
        db.execute('''
            UPDATE pos_products 
            SET pos_kind = 'resale', resale_ingredient_id = ?, classified_at = ?, classified_by = 'auto'
            WHERE product_name = ? AND pos_kind = 'unclassified'
        ''', (ing['id'], now, ing['name']))
        resale_count += 1
    print(f"  ✓ {resale_count} products → resale (ingredient name = product name)")

    # Mark resale ingredients
    db.execute('''
        UPDATE ingredients SET kind = 'resale'
        WHERE id IN (SELECT resale_ingredient_id FROM pos_products WHERE pos_kind = 'resale')
    ''')
    print(f"  ✓ Marked {resale_count} ingredients as kind='resale'")

    # Containers → ignore
    containers = ['To-go cup', 'To-go box']
    for c in containers:
        db.execute('''
            UPDATE pos_products SET pos_kind = 'ignore', classified_at = ?, classified_by = 'auto'
            WHERE product_name = ? AND pos_kind != 'ignore'
        ''', (now, c))
    ignored = db.execute("SELECT COUNT(*) FROM pos_products WHERE pos_kind = 'ignore'").fetchone()[0]
    print(f"  ✓ {ignored} products → ignore (containers)")

    # ─── Step 5: Summary ───
    print("\n[5/5] Summary:")
    stats = db.execute('''
        SELECT pos_kind, COUNT(*) as cnt 
        FROM pos_products 
        GROUP BY pos_kind 
        ORDER BY cnt DESC
    ''').fetchall()
    for s in stats:
        print(f"  {s['pos_kind']}: {s['cnt']}")

    # Show unclassified for manual review
    unclassified = db.execute('''
        SELECT pp.product_name, 
               COALESCE(SUM(s.quantity), 0) as qty,
               COALESCE(CAST(SUM(s.total_money) AS INTEGER), 0) as rev
        FROM pos_products pp
        LEFT JOIN sales s ON s.product_name = pp.product_name
        WHERE pp.pos_kind = 'unclassified'
        GROUP BY pp.product_name
        ORDER BY rev DESC
    ''').fetchall()
    print(f"\n  ⚠️  {len(unclassified)} products still UNCLASSIFIED:")
    for u in unclassified:
        print(f"    {u['product_name']}: {u['qty']:.0f} sht, {u['rev']} zl")

    db.commit()
    db.close()
    print("\n✅ Migration complete!")


if __name__ == '__main__':
    migrate()
