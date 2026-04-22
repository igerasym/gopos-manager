"""Recipes routes."""
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.db import get_db

templates = Jinja2Templates(directory=Path(__file__).parent.parent / 'templates')

router = APIRouter()


@router.get('/recipes', response_class=HTMLResponse)
async def recipes_page(request: Request):
    db = get_db()

    cards = db.execute('''
        SELECT rc.product_name, rc.category, rc.portion_weight, rc.description
        FROM recipe_cards rc ORDER BY rc.category, rc.product_name
    ''').fetchall()

    all_recipes = db.execute('''
        SELECT r.id, r.product_name, r.ingredient_id, i.name as ingredient,
               r.amount, i.unit, COALESCE(i.unit_price, 0) as unit_price,
               r.amount * COALESCE(i.unit_price, 0) as cost
        FROM recipes r JOIN ingredients i ON r.ingredient_id = i.id
        ORDER BY r.product_name, i.name
    ''').fetchall()

    recipe_map = {}
    cost_map = {}
    for r in all_recipes:
        row = dict(r)
        # Display amounts in g/ml for recipes (more readable than kg/L)
        if row['unit'] == 'kg':
            row['display_amount'] = round(row['amount'] * 1000, 1)
            row['display_unit'] = 'g'
        elif row['unit'] == 'L':
            row['display_amount'] = round(row['amount'] * 1000, 1)
            row['display_unit'] = 'ml'
        else:
            row['display_amount'] = row['amount']
            row['display_unit'] = row['unit']
        recipe_map.setdefault(row['product_name'], []).append(row)
        cost_map[row['product_name']] = cost_map.get(row['product_name'], 0) + (row['cost'] or 0)

    orphan_products = sorted(set(recipe_map.keys()) - {c['product_name'] for c in cards})

    # Average selling price per product (without discounts = real menu price)
    avg_prices = db.execute('''
        SELECT product_name, SUM(total_money + discount) / SUM(quantity) as avg_price
        FROM sales WHERE quantity > 0
        GROUP BY product_name
    ''').fetchall()
    price_map = {r['product_name']: r['avg_price'] for r in avg_prices}

    ingredients = db.execute('SELECT id, name, unit FROM ingredients ORDER BY name').fetchall()
    products = db.execute('SELECT DISTINCT product_name FROM sales ORDER BY product_name').fetchall()
    categories = db.execute('SELECT DISTINCT category FROM recipe_cards WHERE category != "" ORDER BY category').fetchall()
    db.close()
    return templates.TemplateResponse(request, 'recipes.html', context={
        'cards': cards, 'recipe_map': recipe_map, 'cost_map': cost_map,
        'price_map': price_map,
        'orphan_products': orphan_products,
        'ingredients': ingredients, 'products': products,
        'categories': categories,
    })


@router.post('/recipes/add')
async def add_recipe(
    product_name: str = Form(...), ingredient_id: int = Form(...),
    amount: float = Form(...),
):
    db = get_db()
    db.execute(
        'INSERT OR REPLACE INTO recipes (product_name, ingredient_id, amount) '
        'VALUES (?, ?, ?)', (product_name, ingredient_id, amount)
    )
    db.commit()
    db.close()
    return RedirectResponse('/recipes', status_code=303)


@router.post('/recipes/card')
async def save_recipe_card(
    product_name: str = Form(...),
    category: str = Form(''),
    portion_weight: str = Form(''),
    description: str = Form(''),
):
    db = get_db()
    db.execute(
        'INSERT OR REPLACE INTO recipe_cards (product_name, category, portion_weight, description) '
        'VALUES (?, ?, ?, ?)', (product_name, category, portion_weight, description)
    )
    db.commit()
    db.close()
    return RedirectResponse('/recipes', status_code=303)


@router.post('/recipes/delete/{recipe_id}')
async def delete_recipe(recipe_id: int):
    db = get_db()
    db.execute('DELETE FROM recipes WHERE id = ?', (recipe_id,))
    db.commit()
    db.close()
    return RedirectResponse('/recipes', status_code=303)


@router.post('/recipes/update/{recipe_id}')
async def update_recipe(request: Request, recipe_id: int):
    form = await request.form()
    db = get_db()

    if 'display_amount' in form:
        display_amount = float(form.get('display_amount', 0))
        base_unit = form.get('base_unit', '')
        # Convert display (g/ml) back to storage (kg/L)
        if base_unit in ('kg', 'L'):
            amount = display_amount / 1000
        else:
            amount = display_amount
    else:
        amount = float(form.get('amount', 0))

    db.execute('UPDATE recipes SET amount = ? WHERE id = ?', (amount, recipe_id))
    db.commit()
    db.close()
    return RedirectResponse('/recipes', status_code=303)
