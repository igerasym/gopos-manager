"""Recipe service — cost calculations, display conversion."""
from app.db import get_db
from app.services.units import to_display


def get_recipe_map_with_costs():
    """Get all recipes with display amounts and costs."""
    db = get_db()
    all_recipes = db.execute('''
        SELECT r.id, r.product_name, r.ingredient_id, i.name as ingredient,
               r.amount, i.unit, COALESCE(i.unit_price, 0) as unit_price,
               r.amount * COALESCE(i.unit_price, 0) as cost
        FROM recipes r JOIN ingredients i ON r.ingredient_id = i.id
        ORDER BY r.product_name, i.name
    ''').fetchall()
    db.close()

    recipe_map = {}
    cost_map = {}
    for r in all_recipes:
        row = dict(r)
        display_amount, display_unit = to_display(row['amount'], row['unit'])
        row['display_amount'] = display_amount
        row['display_unit'] = display_unit
        recipe_map.setdefault(row['product_name'], []).append(row)
        cost_map[row['product_name']] = cost_map.get(row['product_name'], 0) + (row['cost'] or 0)

    return recipe_map, cost_map


def get_selling_prices() -> dict:
    """Get average selling price per product (before discounts)."""
    db = get_db()
    avg_prices = db.execute('''
        SELECT product_name, SUM(total_money + discount) / SUM(quantity) as avg_price
        FROM sales WHERE quantity > 0
        GROUP BY product_name
    ''').fetchall()
    db.close()
    return {r['product_name']: r['avg_price'] for r in avg_prices}


def get_cost_lookup() -> dict:
    """Get unit cost per product from recipes."""
    db = get_db()
    recipe_costs = db.execute('''
        SELECT r.product_name, SUM(r.amount * COALESCE(i.unit_price, 0)) as unit_cost
        FROM recipes r JOIN ingredients i ON r.ingredient_id = i.id
        GROUP BY r.product_name
    ''').fetchall()
    db.close()
    return {r['product_name']: r['unit_cost'] for r in recipe_costs}


def recalc_sub_recipe_cost(db, sub_id):
    """Recalculate unit_price for a sub-recipe ingredient."""
    sr = db.execute('SELECT ingredient_id, yield_amount FROM sub_recipes WHERE id = ?', (sub_id,)).fetchone()
    if not sr:
        return
    total_cost = db.execute('''
        SELECT COALESCE(SUM(sri.amount * COALESCE(i.unit_price, 0)), 0) as cost
        FROM sub_recipe_items sri JOIN ingredients i ON sri.ingredient_id = i.id
        WHERE sri.sub_recipe_id = ?
    ''', (sub_id,)).fetchone()['cost']

    cost_per_unit = total_cost / sr['yield_amount'] if sr['yield_amount'] > 0 else 0
    db.execute('UPDATE ingredients SET unit_price = ? WHERE id = ?',
               (round(cost_per_unit, 4), sr['ingredient_id']))
