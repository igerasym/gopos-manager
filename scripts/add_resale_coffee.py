"""Create resale ingredients for coffee beans (matching POS product names)."""
import sqlite3

db = sqlite3.connect("/app/data/cafe.db")

# Resale coffee from Foundation invoice (April 2026), brutto prices for 25+ kg discount tier
# Format: (POS product name, unit_price brutto)
resale_coffee = [
    # Foundation FILTER 250g (price ~ 41-46 zł brutto, retail ~60-64)
    ('Foundation filter Blend Vanilla Bloom 250g', 50.43),
    ('Foundation filter Blend Berry Honey 250g', 33.21),  # 27 netto * 1.23
    ('Foundation filter ETHIOPIA GERA 250g', 33.21),
    ('Foundation filter ETHIOPIA URAGA 250g', 41.82),
    ('Foundation filter Burundi NYAGISHIRU 250g', 43.05),
    ('Foundation filter HONDURAS MARIO RODRIGUEZ 250g', 39.36),
    ('Foundation filter Pink Bloom 250g', 44.28),
    ('Foundation filter  Guatemala Oriente 250g', 44.28),  # 36 netto
    ('Foundation filter Costa Rica El Mango 250g', 41.82),
    ('Foundation filter Peru Amazonas 250g', 40.59),  # 33 netto
    ('Foundation filter Kenia Kegwa AB 250g', 39.36),
    ('Foundation filter Caramel Fruit 250g', 41.82),
    ('Foundation filter Cáscara Elida Geisha', 39.36),
    ('Foundation filter Colombia El Mirador 250g', 50.43),  # higher tier
    ('Foundation filter Rwanda Gisanga 250g', 44.28),
    # Foundation ESPRESSO 250g
    ('Foundation espresso COLOMBIA EXCELSO 250g', 27.06),
    ('Foundation espresso Brazil Mogiana 250g', 28.29),
    ('Foundation espresso Nicaragua SHG ep 250g', 28.29),
    # Drip bags
    ('Drip Bag Foundation blue', 8.0),    # ~10 zł brutto
    ('Drip Bag Foundation pink', 9.0),
    # Coffee Plant 250g — use 25-30 zł brutto estimate
    ('Coffee Plant espresso  Kolumbia Finca Los Robles 250g', 30.0),
    ('Coffee Plant espresso  Kolumbia Finca Los Robles 1kg', 110.0),
    ('Coffee Plant espresso Brazylia Mogiana 250g', 28.0),
    ('Coffee Plant espresso Brazylia Lua Roxa 250g', 28.0),
    ('Coffee Plant espresso Gentle Decaf 250g', 32.0),
    ('Coffee Plant filter FLOW Mellow Decaf 250g', 32.0),
    ('Coffee Plant filter Flow Brownie 250g', 28.0),
    ('Coffee Plant filter Meksyk Vayichil Decaf 250g', 32.0),
]

added = 0
updated = 0
for name, price in resale_coffee:
    existing = db.execute('SELECT id, unit_price FROM ingredients WHERE name = ?', (name,)).fetchone()
    if existing:
        db.execute('UPDATE ingredients SET unit_price = ? WHERE id = ?', (price, existing[0]))
        updated += 1
    else:
        db.execute(
            'INSERT INTO ingredients (name, unit, quantity, min_quantity, unit_price) VALUES (?, ?, 0, 1, ?)',
            (name, 'szt', price)
        )
        added += 1

db.commit()
print(f"Added: {added}, Updated: {updated}")

# Verify auto-match will work
print("\nSample auto-match check:")
for name, _ in resale_coffee[:3]:
    sales = db.execute(
        'SELECT SUM(quantity), SUM(total_money) FROM sales WHERE product_name = ?', (name,)
    ).fetchone()
    if sales[0]:
        print(f"  {name[:50]}: qty={sales[0]:.0f}, rev={sales[1]:.0f} zł")

db.close()
