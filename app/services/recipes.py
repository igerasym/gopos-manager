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
    """Get selling price per product from GoPos API (stored in pos_products) or sales average."""
    db = get_db()
    # Prefer API price (current menu price)
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    if 'pos_products' in tables:
        rows = db.execute('''
            SELECT product_name, gopos_price FROM pos_products WHERE gopos_price > 0
        ''').fetchall()
        result = {r['product_name']: r['gopos_price'] for r in rows}
    else:
        result = {}

    # Fallback: average from sales for products not in pos_products
    avg_prices = db.execute('''
        SELECT product_name, SUM(total_money + discount) / SUM(quantity) as avg_price
        FROM sales WHERE quantity > 0
        GROUP BY product_name
    ''').fetchall()
    for r in avg_prices:
        if r['product_name'] not in result:
            result[r['product_name']] = r['avg_price']

    db.close()
    return result


def get_cost_lookup() -> dict:
    """Get unit cost per product using pos_products classification + recipes."""
    db = get_db()

    # Check if pos_products table exists (backward compat)
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    use_pos_products = 'pos_products' in tables

    # From recipes (prepared items)
    recipe_costs = db.execute('''
        SELECT r.product_name, SUM(r.amount * COALESCE(i.unit_price, 0)) as unit_cost
        FROM recipes r JOIN ingredients i ON r.ingredient_id = i.id
        GROUP BY r.product_name
    ''').fetchall()
    result = {r['product_name']: r['unit_cost'] for r in recipe_costs}

    if use_pos_products:
        # Resale items: explicit link via pos_products → ingredient unit_price
        resale = db.execute('''
            SELECT pp.product_name, COALESCE(i.unit_price, 0) as unit_cost
            FROM pos_products pp
            JOIN ingredients i ON pp.resale_ingredient_id = i.id
            WHERE pp.pos_kind = 'resale'
        ''').fetchall()
        for r in resale:
            if r['product_name'] not in result:
                result[r['product_name']] = r['unit_cost']

        # Variant products: inherit cost from base product
        # "Cappuccino big Cappuccino" → cost of "Cappuccino big"
        all_pos = db.execute("SELECT product_name FROM pos_products WHERE pos_kind = 'prepared'").fetchall()
        for row in all_pos:
            name = row['product_name']
            if name in result:
                continue
            # Find base: longest recipe product that is a prefix
            for base in sorted(result.keys(), key=len, reverse=True):
                if name != base and name.startswith(base + ' '):
                    suffix = name[len(base) + 1:]
                    if suffix in result or suffix == base:
                        result[name] = result[base]
                        break
    else:
        # Fallback: direct match (ingredient name = product name, no recipe)
        direct = db.execute('''
            SELECT i.name, COALESCE(i.unit_price, 0) as unit_cost
            FROM ingredients i
            JOIN (SELECT DISTINCT product_name FROM sales) s ON s.product_name = i.name
            WHERE i.name NOT IN (SELECT DISTINCT product_name FROM recipes)
        ''').fetchall()
        for d in direct:
            if d['name'] not in result:
                result[d['name']] = d['unit_cost']

    db.close()
    return result


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
