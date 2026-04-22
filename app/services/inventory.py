"""Inventory service — ingredient CRUD and price calculations."""
from app.db import get_db


def get_all_ingredients(supplier: str = ''):
    """Get all ingredients with supplier and sub-recipe info."""
    db = get_db()
    if supplier:
        items = db.execute('''
            SELECT i.*, s.name as supplier_name,
                   CASE WHEN sr.id IS NOT NULL THEN 1 ELSE 0 END as is_sub_recipe
            FROM ingredients i
            LEFT JOIN suppliers s ON i.supplier_id = s.id
            LEFT JOIN sub_recipes sr ON sr.ingredient_id = i.id
            WHERE s.name = ? ORDER BY is_sub_recipe, i.name
        ''', (supplier,)).fetchall()
    else:
        items = db.execute('''
            SELECT i.*, s.name as supplier_name,
                   CASE WHEN sr.id IS NOT NULL THEN 1 ELSE 0 END as is_sub_recipe
            FROM ingredients i
            LEFT JOIN suppliers s ON i.supplier_id = s.id
            LEFT JOIN sub_recipes sr ON sr.ingredient_id = i.id
            ORDER BY is_sub_recipe, i.name
        ''').fetchall()
    db.close()
    return items


def get_suppliers():
    db = get_db()
    suppliers = db.execute('SELECT id, name FROM suppliers ORDER BY name').fetchall()
    db.close()
    return suppliers


def get_ingredient_deps(ingredient_id: int) -> dict:
    """Get recipes and sub-recipes that use this ingredient."""
    db = get_db()
    ing = db.execute('SELECT name FROM ingredients WHERE id = ?', (ingredient_id,)).fetchone()
    recipes = db.execute(
        'SELECT DISTINCT product_name FROM recipes WHERE ingredient_id = ?', (ingredient_id,)
    ).fetchall()
    subs = db.execute('''
        SELECT i.name FROM sub_recipe_items sri
        JOIN sub_recipes sr ON sri.sub_recipe_id = sr.id
        JOIN ingredients i ON sr.ingredient_id = i.id
        WHERE sri.ingredient_id = ?
    ''', (ingredient_id,)).fetchall()
    db.close()
    return {
        'name': ing['name'] if ing else '',
        'recipes': [r['product_name'] for r in recipes],
        'sub_recipes': [s['name'] for s in subs],
    }
