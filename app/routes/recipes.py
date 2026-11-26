"""Меню (recipes) routes: dish list per tab, dish sheets, 'Без рецепту' banner."""
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.db import get_db
from app.services.recipes import (
    COST_SHARE_ALERT, MENU_TABS, TAB_PATHS, dish_economics, get_products_without_recipe,
    get_recipe_map_with_costs, get_selling_prices, parse_num, safe_next, tab_for_category,
)
from app.services.units import from_display

templates = Jinja2Templates(directory=Path(__file__).parent.parent / 'templates')

router = APIRouter()

# ?saved=<code> after a POST-redirect -> toast text on the page.
SAVED_MESSAGES = {
    'line': 'Інгредієнт додано',
    'card': 'Страву збережено',
    'amount': 'Кількість збережено',
    'removed': 'Інгредієнт прибрано',
    'deleted': 'Страву видалено',
}
SORTS = ('name', 'share', 'earn')


def _is_admin(request: Request) -> bool:
    user = getattr(request.state, 'user', None)
    if not user:
        return False
    return (user.get('role') if isinstance(user, dict) else user['role']) == 'admin'


def _back(next_url: str, saved: str = '', open_name: str = '', fallback: str = '/recipes'):
    """303 back to the Меню tab the form came from, with a toast code and the dish
    sheet to reopen. Only /recipes* targets are accepted."""
    target = safe_next(next_url, fallback)
    path = urlsplit(target).path or fallback
    params = {}
    if saved:
        params['saved'] = saved
    if open_name:
        params['open'] = open_name
    return RedirectResponse(f'{path}?{urlencode(params)}' if params else path, status_code=303)


@router.get('/recipes', response_class=HTMLResponse)
async def recipes_page(request: Request):
    return await _render_recipes(request, 'kitchen')


@router.get('/recipes/bar', response_class=HTMLResponse)
async def bar_page(request: Request):
    return await _render_recipes(request, 'bar')


@router.get('/recipes/bakery', response_class=HTMLResponse)
async def bakery_page(request: Request):
    return await _render_recipes(request, 'bakery')


def _tab_counts(db) -> dict:
    counts = {k: 0 for k, _, _ in MENU_TABS}
    for r in db.execute('SELECT category FROM recipe_cards').fetchall():
        counts[tab_for_category(r['category'])] += 1
    counts['prep'] = db.execute('SELECT COUNT(*) FROM sub_recipes').fetchone()[0]
    return counts


async def _render_recipes(request: Request, tab: str):
    is_admin = _is_admin(request)
    db = get_db()
    all_cards = db.execute('''
        SELECT product_name, category, portion_weight, description
        FROM recipe_cards ORDER BY category, product_name
    ''').fetchall()
    ingredients = db.execute('SELECT id, name, unit FROM ingredients ORDER BY name').fetchall()
    products = db.execute('SELECT DISTINCT product_name FROM sales ORDER BY product_name').fetchall()
    categories = [r['category'] for r in db.execute(
        "SELECT DISTINCT category FROM recipe_cards WHERE category != '' ORDER BY category").fetchall()]
    counts = _tab_counts(db)
    db.close()

    recipe_map, cost_map = get_recipe_map_with_costs()
    price_map = get_selling_prices() if is_admin else {}

    dishes = []
    for i, c in enumerate(all_cards):
        if tab_for_category(c['category']) != tab:
            continue
        name = c['product_name']
        lines = recipe_map.get(name, [])
        econ = dish_economics(cost_map.get(name), price_map.get(name)) if is_admin else None
        dishes.append({
            'id': f'dish-{i}', 'name': name, 'category': c['category'] or '',
            'portion_weight': c['portion_weight'] or '', 'description': c['description'] or '',
            'lines': lines, 'econ': econ,
        })

    sort = request.query_params.get('sort', 'name')
    if sort not in SORTS or not is_admin:
        sort = 'name'
    if sort == 'share':
        dishes.sort(key=lambda d: (d['econ']['share'] is None, -(d['econ']['share'] or 0), d['name'].lower()))
    elif sort == 'earn':
        dishes.sort(key=lambda d: (d['econ']['earn'] is None, -(d['econ']['earn'] or 0), d['name'].lower()))
    else:
        dishes.sort(key=lambda d: (d['category'].lower(), d['name'].lower()))

    # Products that have recipe lines but no card at all: 'Без картки'.
    card_names = {c['product_name'] for c in all_cards}
    orphans = [{'id': f'orphan-{i}', 'name': n, 'lines': recipe_map[n],
                'econ': dish_economics(cost_map.get(n), price_map.get(n)) if is_admin else None}
               for i, n in enumerate(sorted(set(recipe_map) - card_names))]

    missing = get_products_without_recipe()
    # Dish sheet ids by name so 'Без рецепту' rows of existing cards open that sheet.
    sheet_ids = {d['name']: d['id'] for d in dishes}

    open_name = request.query_params.get('open', '')
    open_id = sheet_ids.get(open_name) or next((o['id'] for o in orphans if o['name'] == open_name), '')

    tab_label = next(lbl for k, lbl, _ in MENU_TABS if k == tab)
    return templates.TemplateResponse(request, 'recipes.html', context={
        'tab': tab, 'tab_label': tab_label, 'tabs': MENU_TABS, 'tab_counts': counts,
        'tab_path': TAB_PATHS[tab], 'tab_paths': TAB_PATHS,
        'dishes': dishes, 'orphans': orphans, 'missing': missing, 'sheet_ids': sheet_ids,
        'ingredients': ingredients, 'products': products, 'categories': categories,
        'is_admin': is_admin, 'sort': sort, 'alert_pct': COST_SHARE_ALERT,
        'open_id': open_id,
        'saved_msg': SAVED_MESSAGES.get(request.query_params.get('saved', ''), ''),
    })


def _amount_from_form(amount, display: str, unit: str | None) -> float | None:
    value = parse_num(amount)
    if value is None or value <= 0:
        return None
    return from_display(value, unit or '') if display == '1' else value


@router.post('/recipes/add')
async def add_recipe(
    product_name: str = Form(...), ingredient_id: int = Form(...),
    amount: str = Form(...), display: str = Form(''), next: str = Form(''),
):
    product_name = product_name.strip()
    db = get_db()
    ing = db.execute('SELECT unit FROM ingredients WHERE id = ?', (ingredient_id,)).fetchone()
    value = _amount_from_form(amount, display, ing['unit'] if ing else None)
    if not product_name or not ing or value is None:
        db.close()
        return _back(next, open_name=product_name)
    db.execute(
        'INSERT OR REPLACE INTO recipes (product_name, ingredient_id, amount) '
        'VALUES (?, ?, ?)', (product_name, ingredient_id, value)
    )
    db.commit()
    db.close()
    return _back(next, 'line', product_name)


@router.post('/recipes/card')
async def save_recipe_card(
    product_name: str = Form(...),
    category: str = Form(''),
    portion_weight: str = Form(''),
    description: str = Form(''),
    ingredient_id: str = Form(''),
    amount: str = Form(''),
    display: str = Form(''),
    next: str = Form(''),
):
    """Create or update a dish card; optionally add its first ingredient line too
    (the 'Додати рецепт' sheet)."""
    product_name = product_name.strip()
    if not product_name:
        return _back(next)
    db = get_db()
    db.execute(
        'INSERT OR REPLACE INTO recipe_cards (product_name, category, portion_weight, description) '
        'VALUES (?, ?, ?, ?)', (product_name, category.strip(), portion_weight.strip(), description)
    )
    if ingredient_id.strip().isdigit():
        ing = db.execute('SELECT id, unit FROM ingredients WHERE id = ?', (int(ingredient_id),)).fetchone()
        value = _amount_from_form(amount, display, ing['unit'] if ing else None)
        if ing and value is not None:
            db.execute('INSERT OR REPLACE INTO recipes (product_name, ingredient_id, amount) VALUES (?, ?, ?)',
                       (product_name, ing['id'], value))
    db.commit()
    db.close()
    # Land on the tab the dish now belongs to.
    return _back('', 'card', product_name, fallback=TAB_PATHS[tab_for_category(category)])


@router.post('/recipes/card/delete')
async def delete_recipe_card(product_name: str = Form(...), next: str = Form('')):
    """Delete a dish: its card and its ingredient lines."""
    db = get_db()
    db.execute('DELETE FROM recipes WHERE product_name = ?', (product_name,))
    db.execute('DELETE FROM recipe_cards WHERE product_name = ?', (product_name,))
    db.commit()
    db.close()
    return _back(next, 'deleted')


@router.post('/recipes/delete/{recipe_id}')
async def delete_recipe(recipe_id: int, request: Request):
    form = await request.form()
    db = get_db()
    row = db.execute('SELECT product_name FROM recipes WHERE id = ?', (recipe_id,)).fetchone()
    db.execute('DELETE FROM recipes WHERE id = ?', (recipe_id,))
    db.commit()
    db.close()
    return _back(form.get('next', ''), 'removed', row['product_name'] if row else '')


@router.post('/recipes/update/{recipe_id}')
async def update_recipe(request: Request, recipe_id: int):
    form = await request.form()
    db = get_db()
    row = db.execute('SELECT product_name FROM recipes WHERE id = ?', (recipe_id,)).fetchone()

    if 'display_amount' in form:
        display_amount = parse_num(form.get('display_amount'))
        amount = from_display(display_amount, form.get('base_unit', '')) if display_amount is not None else None
    else:
        amount = parse_num(form.get('amount'))

    if amount is not None and amount > 0:
        db.execute('UPDATE recipes SET amount = ? WHERE id = ?', (amount, recipe_id))
        db.commit()
    db.close()
    return _back(form.get('next', ''), 'amount' if amount else '', row['product_name'] if row else '')
