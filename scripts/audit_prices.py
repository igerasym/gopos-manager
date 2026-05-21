import sqlite3
db = sqlite3.connect("/app/data/cafe.db")

drinks = [
    'Espresso', 'Doppio', 'Americano', 'Cappuccino', 'Cappuccino big',
    'Flat White', 'Latte', 'Latte Iced', 'Batch Brew', 'Batch Brew big',
    'Batch Brew big Iced', 'Matcha Latte', 'Matcha Latte Iced',
    'Matcha Tonic', 'Espresso Tonic', 'Espresso orange', 'Raf',
    'Hand Brew', 'Cocoa', 'Babyccino'
]

placeholders = ','.join(['?'] * len(drinks))

# Sales data
rows = db.execute(f'''
    SELECT product_name, SUM(quantity) as qty, SUM(total_money)/SUM(quantity) as avg_price
    FROM sales WHERE product_name IN ({placeholders})
    GROUP BY product_name ORDER BY avg_price DESC
''', drinks).fetchall()

# Recipe costs
costs = {}
recipes = db.execute(f'''
    SELECT r.product_name, SUM(r.amount * i.unit_price) as cost
    FROM recipes r JOIN ingredients i ON r.ingredient_id = i.id
    WHERE r.product_name IN ({placeholders})
    GROUP BY r.product_name
''', drinks).fetchall()
for r in recipes:
    costs[r[0]] = r[1]

print(f"{'Drink':<25} {'Price':>6} {'Cost':>6} {'Margin':>7} {'FC%':>5} {'Qty':>5}")
print("-" * 60)
for r in rows:
    name, qty, price = r[0], r[1], r[2]
    cost = costs.get(name, 0)
    margin = price - cost
    fc_pct = (cost / price * 100) if price > 0 else 0
    flag = " ⚠️" if fc_pct > 30 else ""
    print(f"{name:<25} {price:>6.1f} {cost:>6.2f} {margin:>7.2f} {fc_pct:>4.0f}%{flag} {qty:>5.0f}")

db.close()
