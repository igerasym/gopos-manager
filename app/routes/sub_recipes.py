"""Sub-recipes (напівфабрикати) routes."""
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.db import get_db

templates = Jinja2Templates(directory=Path(__file__).parent.parent / 'templates')

router = APIRouter()


def _recalc_sub_recipe_cost(db, sub_id):
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
    db.execute('UPDATE ingredients SET unit_price = ? WHERE id = ?', (round(cost_per_unit, 4), sr['ingredient_id']))


@router.get('/recipes/sub', response_class=HTMLResponse)
async def sub_recipes_page(request: Request):
    db = get_db()
    subs = db.execute('''
        SELECT sr.id, sr.yield_amount, sr.yield_unit, sr.description,
               i.id as ingredient_id, i.name, i.unit, i.unit_price
        FROM sub_recipes sr JOIN ingredients i ON sr.ingredient_id = i.id
        ORDER BY i.name
    ''').fetchall()

    sub_items = {}
    for s in subs:
        items = db.execute('''
            SELECT sri.id, sri.amount, i.name as ingredient, i.unit, i.unit_price,
                   sri.ingredient_id,
                   sri.amount * COALESCE(i.unit_price, 0) as cost
            FROM sub_recipe_items sri JOIN ingredients i ON sri.ingredient_id = i.id
            WHERE sri.sub_recipe_id = ?
            ORDER BY i.name
        ''', (s['id'],)).fetchall()
        sub_items[s['id']] = items

    ingredients = db.execute('SELECT id, name, unit FROM ingredients ORDER BY name').fetchall()
    db.close()
    return templates.TemplateResponse(request, 'sub_recipes.html', context={
        'subs': subs, 'sub_items': sub_items, 'ingredients': ingredients,
    })


@router.post('/recipes/sub/create')
async def create_sub_recipe(
    name: str = Form(...), unit: str = Form('kg'),
    yield_amount: float = Form(1), description: str = Form(''),
):
    db = get_db()
    # Create ingredient for this sub-recipe
    db.execute(
        'INSERT OR IGNORE INTO ingredients (name, unit, quantity, min_quantity, unit_price) '
        'VALUES (?, ?, 0, 0, 0)', (name, unit)
    )
    db.commit()
    ing = db.execute('SELECT id FROM ingredients WHERE name = ?', (name,)).fetchone()
    db.execute(
        'INSERT OR REPLACE INTO sub_recipes (ingredient_id, yield_amount, yield_unit, description) '
        'VALUES (?, ?, ?, ?)', (ing['id'], yield_amount, unit, description)
    )
    db.commit()
    db.close()
    return RedirectResponse('/recipes/sub', status_code=303)


@router.post('/recipes/sub/{sub_id}/add')
async def add_sub_recipe_item(
    sub_id: int, ingredient_id: int = Form(...), amount: float = Form(...),
):
    db = get_db()
    db.execute(
        'INSERT INTO sub_recipe_items (sub_recipe_id, ingredient_id, amount) VALUES (?, ?, ?)',
        (sub_id, ingredient_id, amount)
    )
    # Recalculate cost per unit
    _recalc_sub_recipe_cost(db, sub_id)
    db.commit()
    db.close()
    return RedirectResponse('/recipes/sub', status_code=303)


@router.post('/recipes/sub/{sub_id}/delete/{item_id}')
async def delete_sub_recipe_item(sub_id: int, item_id: int):
    db = get_db()
    db.execute('DELETE FROM sub_recipe_items WHERE id = ?', (item_id,))
    _recalc_sub_recipe_cost(db, sub_id)
    db.commit()
    db.close()
    return RedirectResponse('/recipes/sub', status_code=303)


@router.post('/recipes/sub/{sub_id}/update/{item_id}')
async def update_sub_recipe_item(sub_id: int, item_id: int, amount: float = Form(...)):
    db = get_db()
    db.execute('UPDATE sub_recipe_items SET amount = ? WHERE id = ?', (amount, item_id))
    _recalc_sub_recipe_cost(db, sub_id)
    db.commit()
    db.close()
    return RedirectResponse('/recipes/sub', status_code=303)


@router.post('/recipes/sub/{sub_id}/edit')
async def edit_sub_recipe(sub_id: int, yield_amount: float = Form(...), description: str = Form('')):
    db = get_db()
    db.execute('UPDATE sub_recipes SET yield_amount = ?, description = ? WHERE id = ?',
        (yield_amount, description, sub_id))
    _recalc_sub_recipe_cost(db, sub_id)
    db.commit()
    db.close()
    return RedirectResponse('/recipes/sub', status_code=303)
