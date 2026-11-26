"""Заготовки (sub-recipes, made in-house and used as an ingredient) routes."""
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse

from app.db import get_db
from app.routes.recipes import _back, _is_admin, _tab_counts
from app.services.recipes import (
    MENU_TABS, get_sub_recipe_usage, parse_num, recalc_sub_recipe_cost,
)
from app.services.units import from_display, to_display

templates = Jinja2Templates(directory=Path(__file__).parent.parent / 'templates')

router = APIRouter()

SUB_PATH = '/recipes/sub'
SAVED_MESSAGES = {
    'created': 'Заготовку створено',
    'line': 'Інгредієнт додано',
    'amount': 'Кількість збережено',
    'removed': 'Інгредієнт прибрано',
    'edited': 'Заготовку збережено',
}
UNITS = ('kg', 'L', 'szt')


@router.get('/recipes/sub', response_class=HTMLResponse)
async def sub_recipes_page(request: Request):
    is_admin = _is_admin(request)
    db = get_db()
    subs = db.execute('''
        SELECT sr.id, sr.yield_amount, sr.yield_unit, sr.description,
               i.id as ingredient_id, i.name, i.unit, i.unit_price
        FROM sub_recipes sr JOIN ingredients i ON sr.ingredient_id = i.id
        ORDER BY i.name
    ''').fetchall()

    preps = []
    for s in subs:
        items = []
        for it in db.execute('''
            SELECT sri.id, sri.amount, i.name as ingredient, i.unit, i.unit_price,
                   sri.ingredient_id,
                   sri.amount * COALESCE(i.unit_price, 0) as cost
            FROM sub_recipe_items sri JOIN ingredients i ON sri.ingredient_id = i.id
            WHERE sri.sub_recipe_id = ?
            ORDER BY i.name
        ''', (s['id'],)).fetchall():
            row = dict(it)
            row['display_amount'], row['display_unit'] = to_display(row['amount'], row['unit'])
            items.append(row)
        preps.append({
            'id': s['id'], 'sheet': f"prep-{s['id']}", 'name': s['name'],
            'ingredient_id': s['ingredient_id'], 'unit': s['unit'],
            'yield_amount': s['yield_amount'], 'yield_unit': s['yield_unit'],
            'description': s['description'] or '', 'items': items,
            'total_cost': sum((i['cost'] or 0) for i in items) if is_admin else None,
            'unit_price': (s['unit_price'] or 0) if is_admin else None,
        })

    usage = get_sub_recipe_usage(db)
    ingredients = db.execute('SELECT id, name, unit FROM ingredients ORDER BY name').fetchall()
    counts = _tab_counts(db)
    db.close()

    open_id = request.query_params.get('open', '')
    if not any(p['sheet'] == open_id for p in preps):
        open_id = ''
    return templates.TemplateResponse(request, 'sub_recipes.html', context={
        'preps': preps, 'usage': usage, 'ingredients': ingredients,
        'tab': 'prep', 'tabs': MENU_TABS, 'tab_counts': counts, 'is_admin': is_admin,
        'units': UNITS, 'open_id': open_id,
        'saved_msg': SAVED_MESSAGES.get(request.query_params.get('saved', ''), ''),
    })


def _back_sub(saved: str, sub_id: int | None = None):
    return _back(SUB_PATH, saved, f'prep-{sub_id}' if sub_id else '', fallback=SUB_PATH)


@router.post('/recipes/sub/create')
async def create_sub_recipe(
    name: str = Form(...), unit: str = Form('kg'),
    yield_amount: str = Form('1'), description: str = Form(''),
):
    name = name.strip()
    value = parse_num(yield_amount, 1.0)
    if not name or value is None or value <= 0:
        return _back_sub('')
    if unit not in UNITS:
        unit = 'kg'
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
        'VALUES (?, ?, ?, ?)', (ing['id'], value, unit, description)
    )
    db.commit()
    sub = db.execute('SELECT id FROM sub_recipes WHERE ingredient_id = ?', (ing['id'],)).fetchone()
    db.close()
    return _back_sub('created', sub['id'])


@router.post('/recipes/sub/{sub_id}/add')
async def add_sub_recipe_item(
    sub_id: int, ingredient_id: int = Form(...), amount: str = Form(...), display: str = Form(''),
):
    db = get_db()
    ing = db.execute('SELECT unit FROM ingredients WHERE id = ?', (ingredient_id,)).fetchone()
    value = parse_num(amount)
    if ing is None or value is None or value <= 0:
        db.close()
        return _back_sub('', sub_id)
    if display == '1':
        value = from_display(value, ing['unit'])
    db.execute(
        'INSERT INTO sub_recipe_items (sub_recipe_id, ingredient_id, amount) VALUES (?, ?, ?)',
        (sub_id, ingredient_id, value)
    )
    # Recalculate cost per unit
    recalc_sub_recipe_cost(db, sub_id)
    db.commit()
    db.close()
    return _back_sub('line', sub_id)


@router.post('/recipes/sub/{sub_id}/delete/{item_id}')
async def delete_sub_recipe_item(sub_id: int, item_id: int):
    db = get_db()
    db.execute('DELETE FROM sub_recipe_items WHERE id = ?', (item_id,))
    recalc_sub_recipe_cost(db, sub_id)
    db.commit()
    db.close()
    return _back_sub('removed', sub_id)


@router.post('/recipes/sub/{sub_id}/update/{item_id}')
async def update_sub_recipe_item(sub_id: int, item_id: int, request: Request):
    """Accepts `amount` in the stored unit, or `display_amount` (g/ml) + `base_unit`."""
    form = await request.form()
    if 'display_amount' in form:
        v = parse_num(form.get('display_amount'))
        amount = from_display(v, form.get('base_unit', '')) if v is not None else None
    else:
        amount = parse_num(form.get('amount'))
    if amount is None or amount <= 0:
        return _back_sub('', sub_id)
    db = get_db()
    db.execute('UPDATE sub_recipe_items SET amount = ? WHERE id = ?', (amount, item_id))
    recalc_sub_recipe_cost(db, sub_id)
    db.commit()
    db.close()
    return _back_sub('amount', sub_id)


@router.post('/recipes/sub/{sub_id}/edit')
async def edit_sub_recipe(sub_id: int, yield_amount: str = Form(...), description: str = Form('')):
    value = parse_num(yield_amount)
    if value is None or value <= 0:
        return _back_sub('', sub_id)
    db = get_db()
    db.execute('UPDATE sub_recipes SET yield_amount = ?, description = ? WHERE id = ?',
        (value, description, sub_id))
    recalc_sub_recipe_cost(db, sub_id)
    db.commit()
    db.close()
    return _back_sub('edited', sub_id)
