"""Recipes routes."""
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.db import get_db
from app.services.recipes import get_recipe_map_with_costs, get_selling_prices
from app.services.units import from_display

templates = Jinja2Templates(directory=Path(__file__).parent.parent / 'templates')

router = APIRouter()


@router.get('/recipes', response_class=HTMLResponse)
async def recipes_page(request: Request):
    return await _render_recipes(request, 'recipes.html', exclude_categories=['Бар'])


@router.get('/recipes/bar', response_class=HTMLResponse)
async def bar_page(request: Request):
    return await _render_recipes(request, 'recipes.html', only_categories=['Бар'])


async def _render_recipes(request, template, exclude_categories=None, only_categories=None):
    db = get_db()

    cards = db.execute('''
        SELECT rc.product_name, rc.category, rc.portion_weight, rc.description
        FROM recipe_cards rc ORDER BY rc.category, rc.product_name
    ''').fetchall()

    # Filter by category
    all_cards = list(cards)  # unfiltered
    if only_categories:
        cards = [c for c in cards if c['category'] in only_categories]
    elif exclude_categories:
        cards = [c for c in cards if c['category'] not in exclude_categories]

    recipe_map, cost_map = get_recipe_map_with_costs()
    # Orphans = products with recipes but NO card at all (not just filtered out)
    all_card_names = {c['product_name'] for c in all_cards}
    orphan_products = sorted(set(recipe_map.keys()) - all_card_names) if not only_categories else []
    price_map = get_selling_prices()

    ingredients = db.execute('SELECT id, name, unit FROM ingredients ORDER BY name').fetchall()
    products = db.execute('SELECT DISTINCT product_name FROM sales ORDER BY product_name').fetchall()
    categories = db.execute('SELECT DISTINCT category FROM recipe_cards WHERE category != "" ORDER BY category').fetchall()
    db.close()
    return templates.TemplateResponse(request, template, context={
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
        amount = from_display(display_amount, base_unit)
    else:
        amount = float(form.get('amount', 0))

    db.execute('UPDATE recipes SET amount = ? WHERE id = ?', (amount, recipe_id))
    db.commit()
    db.close()
    return RedirectResponse('/recipes', status_code=303)
