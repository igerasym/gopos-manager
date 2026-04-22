"""Cafe Manager — FastAPI backend."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime

from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.db import get_db, init_db
from app.auth import get_current_user, can_access, create_default_admin, hash_password, verify_password, sign_cookie

BASE_DIR = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    create_default_admin()

    # Auto-sync every day at 23:00
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    scheduler = AsyncIOScheduler()

    async def scheduled_sync():
        from app.gopos_sync import sync_today
        await sync_today()

    scheduler.add_job(scheduled_sync, CronTrigger(hour=23, minute=0))
    scheduler.start()

    yield

    scheduler.shutdown()

app = FastAPI(title='The Frame Manager', lifespan=lifespan)
app.mount('/static', StaticFiles(directory=BASE_DIR / 'static'), name='static')
templates = Jinja2Templates(directory=BASE_DIR / 'templates')


# ── Auth middleware ─────────────────────────────────────────

from starlette.middleware.base import BaseHTTPMiddleware

class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # Public paths
        if path in ('/login', '/logout', '/static') or path.startswith('/static/'):
            return await call_next(request)

        user = get_current_user(request)
        if not user:
            return RedirectResponse('/login', status_code=302)

        if not can_access(user, path):
            # Redirect non-admin from dashboard to inventory
            if path == '/':
                return RedirectResponse('/inventory', status_code=302)
            return HTMLResponse('<h2>Access denied</h2><p>Your role does not have access to this page.</p><a href="/inventory">← Back</a>', status_code=403)

        request.state.user = user
        return await call_next(request)

app.add_middleware(AuthMiddleware)


# ── Login / Logout ─────────────────────────────────────────

@app.get('/login', response_class=HTMLResponse)
async def login_page(request: Request):
    user = get_current_user(request)
    if user:
        return RedirectResponse('/', status_code=302)
    return templates.TemplateResponse(request, 'login.html', context={'error': None})


@app.post('/login')
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    db = get_db()
    user = db.execute('SELECT username, password_hash FROM users WHERE username = ?', (username,)).fetchone()
    db.close()

    if not user or not verify_password(password, user['password_hash']):
        return templates.TemplateResponse(request, 'login.html', context={'error': 'Invalid username or password'})

    response = RedirectResponse('/', status_code=302)
    response.set_cookie('session', sign_cookie(username), httponly=True, max_age=86400 * 30)
    return response


@app.get('/logout')
async def logout():
    response = RedirectResponse('/login', status_code=302)
    response.delete_cookie('session')
    return response


# ── Dashboard ──────────────────────────────────────────────

@app.get('/', response_class=HTMLResponse)
async def dashboard(request: Request, date_from: str = '', date_to: str = ''):
    today = datetime.now().strftime('%Y-%m-%d')
    date_from = date_from or today
    date_to = date_to or today
    db = get_db()

    # Date presets for quick filter
    from datetime import timedelta
    now = datetime.now()
    yesterday = (now - timedelta(days=1)).strftime('%Y-%m-%d')
    week_start = (now - timedelta(days=now.weekday())).strftime('%Y-%m-%d')
    month_start = now.replace(day=1).strftime('%Y-%m-%d')
    year_start = now.replace(month=1, day=1).strftime('%Y-%m-%d')
    last_7 = (now - timedelta(days=6)).strftime('%Y-%m-%d')
    last_30 = (now - timedelta(days=29)).strftime('%Y-%m-%d')

    presets = [
        ('Today', today, today),
        ('Yesterday', yesterday, yesterday),
        ('Last 7 days', last_7, today),
        ('This week', week_start, today),
        ('Last 30 days', last_30, today),
        ('This month', month_start, today),
        ('This year', year_start, today),
    ]
    # Mark active preset
    active_preset = None
    for name, pf, pt in presets:
        if pf == date_from and pt == date_to:
            active_preset = name
            break

    # Main sales table
    sales = db.execute('''
        SELECT product_name,
               SUM(quantity) as quantity,
               SUM(total_money) as total_money,
               SUM(net_total) as net_total,
               SUM(discount) as discount,
               SUM(net_profit) as net_profit
        FROM sales WHERE date >= ? AND date <= ?
        GROUP BY product_name
        ORDER BY total_money DESC
    ''', (date_from, date_to)).fetchall()

    # Totals
    totals = db.execute('''
        SELECT COALESCE(SUM(total_money),0) as revenue,
               COALESCE(SUM(net_total),0) as net_revenue,
               COALESCE(SUM(net_profit),0) as profit,
               COALESCE(SUM(quantity),0) as items,
               COALESCE(SUM(discount),0) as discounts,
               COUNT(DISTINCT product_name) as unique_products
        FROM sales WHERE date >= ? AND date <= ?
    ''', (date_from, date_to)).fetchone()

    # Top 5 products by revenue
    top_products = db.execute('''
        SELECT product_name, SUM(quantity) as qty, SUM(total_money) as rev
        FROM sales WHERE date >= ? AND date <= ?
        GROUP BY product_name ORDER BY rev DESC LIMIT 5
    ''', (date_from, date_to)).fetchall()

    # Top 5 by quantity
    top_by_qty = db.execute('''
        SELECT product_name, SUM(quantity) as qty, SUM(total_money) as rev
        FROM sales WHERE date >= ? AND date <= ?
        GROUP BY product_name ORDER BY qty DESC LIMIT 5
    ''', (date_from, date_to)).fetchall()

    # Daily breakdown (for charts / trend)
    daily = db.execute('''
        SELECT date, SUM(total_money) as revenue, SUM(quantity) as items
        FROM sales WHERE date >= ? AND date <= ?
        GROUP BY date ORDER BY date
    ''', (date_from, date_to)).fetchall()

    # Previous period comparison
    d_from = datetime.strptime(date_from, '%Y-%m-%d')
    d_to = datetime.strptime(date_to, '%Y-%m-%d')
    period_days = (d_to - d_from).days + 1
    prev_to = (d_from - timedelta(days=1)).strftime('%Y-%m-%d')
    prev_from = (d_from - timedelta(days=period_days)).strftime('%Y-%m-%d')

    prev_totals = db.execute('''
        SELECT COALESCE(SUM(total_money),0) as revenue,
               COALESCE(SUM(quantity),0) as items
        FROM sales WHERE date >= ? AND date <= ?
    ''', (prev_from, prev_to)).fetchone()

    # Low stock
    low_stock = db.execute('''
        SELECT name, quantity, unit, min_quantity
        FROM ingredients WHERE quantity <= min_quantity
    ''').fetchall()

    # ── Food Cost & P&L ──
    # Get cost per product from recipes
    recipe_costs = db.execute('''
        SELECT r.product_name, SUM(r.amount * COALESCE(i.unit_price, 0)) as unit_cost
        FROM recipes r JOIN ingredients i ON r.ingredient_id = i.id
        GROUP BY r.product_name
    ''').fetchall()
    cost_lookup = {r['product_name']: r['unit_cost'] for r in recipe_costs}

    # Build sales with food cost
    sales_with_cost = []
    total_cogs = 0
    for s in sales:
        unit_cost = cost_lookup.get(s['product_name'], 0)
        line_cost = unit_cost * s['quantity']
        food_cost_pct = (line_cost / s['total_money'] * 100) if s['total_money'] > 0 else 0
        total_cogs += line_cost
        sales_with_cost.append({
            'product_name': s['product_name'],
            'quantity': s['quantity'],
            'total_money': s['total_money'],
            'net_total': s['net_total'],
            'discount': s['discount'],
            'net_profit': s['net_profit'],
            'unit_cost': unit_cost,
            'line_cost': line_cost,
            'food_cost_pct': food_cost_pct,
            'has_recipe': s['product_name'] in cost_lookup,
        })

    # P&L summary
    gross_profit = totals['revenue'] - total_cogs
    gross_margin = (gross_profit / totals['revenue'] * 100) if totals['revenue'] > 0 else 0
    avg_food_cost = (total_cogs / totals['revenue'] * 100) if totals['revenue'] > 0 else 0

    # ── ABC Analysis ──
    sorted_by_rev = sorted(sales_with_cost, key=lambda x: x['total_money'], reverse=True)
    cumulative = 0
    for item in sorted_by_rev:
        cumulative += item['total_money']
        pct = (cumulative / totals['revenue'] * 100) if totals['revenue'] > 0 else 0
        if pct <= 80:
            item['abc'] = 'A'
        elif pct <= 95:
            item['abc'] = 'B'
        else:
            item['abc'] = 'C'

    abc_counts = {'A': 0, 'B': 0, 'C': 0}
    abc_revenue = {'A': 0, 'B': 0, 'C': 0}
    for item in sorted_by_rev:
        abc_counts[item['abc']] += 1
        abc_revenue[item['abc']] += item['total_money']

    db.close()
    return templates.TemplateResponse(request, 'dashboard.html', context={
        'date_from': date_from, 'date_to': date_to,
        'presets': presets, 'active_preset': active_preset,
        'sales': sales_with_cost, 'totals': totals,
        'top_products': top_products, 'top_by_qty': top_by_qty,
        'daily': daily, 'period_days': period_days,
        'prev_totals': prev_totals,
        'low_stock': low_stock,
        'total_cogs': total_cogs, 'gross_profit': gross_profit,
        'gross_margin': gross_margin, 'avg_food_cost': avg_food_cost,
        'abc_counts': abc_counts, 'abc_revenue': abc_revenue,
    })


# ── Inventory ──────────────────────────────────────────────

@app.get('/inventory', response_class=HTMLResponse)
async def inventory_page(request: Request, supplier: str = ''):
    db = get_db()
    if supplier:
        items = db.execute('''
            SELECT i.*, s.name as supplier_name
            FROM ingredients i LEFT JOIN suppliers s ON i.supplier_id = s.id
            WHERE s.name = ? ORDER BY i.name
        ''', (supplier,)).fetchall()
    else:
        items = db.execute('''
            SELECT i.*, s.name as supplier_name
            FROM ingredients i LEFT JOIN suppliers s ON i.supplier_id = s.id
            ORDER BY i.name
        ''').fetchall()
    suppliers = db.execute('SELECT id, name FROM suppliers ORDER BY name').fetchall()
    db.close()
    return templates.TemplateResponse(request, 'inventory.html', context={
        'items': items, 'suppliers': suppliers, 'active_supplier': supplier,
    })


@app.post('/inventory/add')
async def add_ingredient(
    name: str = Form(...), unit: str = Form('g'),
    quantity: float = Form(0), min_quantity: float = Form(0),
):
    db = get_db()
    db.execute(
        'INSERT OR IGNORE INTO ingredients (name, unit, quantity, min_quantity) '
        'VALUES (?, ?, ?, ?)', (name, unit, quantity, min_quantity)
    )
    db.commit()
    db.close()
    return RedirectResponse('/inventory', status_code=303)


@app.post('/inventory/delivery')
async def add_delivery(
    ingredient_id: int = Form(...), quantity: float = Form(...),
    price: float = Form(0), note: str = Form(''),
    supplier_id: int = Form(None),
):
    db = get_db()
    db.execute(
        'INSERT INTO deliveries (ingredient_id, quantity, price, note, supplier_id) '
        'VALUES (?, ?, ?, ?, ?)', (ingredient_id, quantity, price, note, supplier_id if supplier_id else None)
    )
    db.execute(
        'UPDATE ingredients SET quantity = quantity + ? WHERE id = ?',
        (quantity, ingredient_id)
    )
    db.commit()
    db.close()
    return RedirectResponse('/inventory', status_code=303)


@app.post('/inventory/supplier')
async def add_supplier(
    name: str = Form(...), contact: str = Form(''), note: str = Form(''),
):
    db = get_db()
    db.execute(
        'INSERT OR IGNORE INTO suppliers (name, contact, note) VALUES (?, ?, ?)',
        (name, contact, note)
    )
    db.commit()
    db.close()
    return RedirectResponse('/inventory', status_code=303)


# ── Recipes ────────────────────────────────────────────────

@app.get('/recipes', response_class=HTMLResponse)
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
        recipe_map.setdefault(r['product_name'], []).append(r)
        cost_map[r['product_name']] = cost_map.get(r['product_name'], 0) + (r['cost'] or 0)

    orphan_products = sorted(set(recipe_map.keys()) - {c['product_name'] for c in cards})

    ingredients = db.execute('SELECT id, name, unit FROM ingredients ORDER BY name').fetchall()
    products = db.execute('SELECT DISTINCT product_name FROM sales ORDER BY product_name').fetchall()
    categories = db.execute('SELECT DISTINCT category FROM recipe_cards WHERE category != "" ORDER BY category').fetchall()
    db.close()
    return templates.TemplateResponse(request, 'recipes.html', context={
        'cards': cards, 'recipe_map': recipe_map, 'cost_map': cost_map,
        'orphan_products': orphan_products,
        'ingredients': ingredients, 'products': products,
        'categories': categories,
    })


@app.post('/recipes/add')
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


@app.post('/recipes/card')
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


@app.post('/recipes/delete/{recipe_id}')
async def delete_recipe(recipe_id: int):
    db = get_db()
    db.execute('DELETE FROM recipes WHERE id = ?', (recipe_id,))
    db.commit()
    db.close()
    return RedirectResponse('/recipes', status_code=303)


@app.post('/recipes/update/{recipe_id}')
async def update_recipe(recipe_id: int, amount: float = Form(...)):
    db = get_db()
    db.execute('UPDATE recipes SET amount = ? WHERE id = ?', (amount, recipe_id))
    db.commit()
    db.close()
    return RedirectResponse('/recipes', status_code=303)


@app.post('/ingredients/update/{ingredient_id}')
async def update_ingredient_unit(ingredient_id: int, unit: str = Form(...)):
    db = get_db()
    db.execute('UPDATE ingredients SET unit = ? WHERE id = ?', (unit, ingredient_id))
    db.commit()
    db.close()
    return RedirectResponse('/recipes', status_code=303)


@app.post('/ingredients/edit/{ingredient_id}')
async def edit_ingredient(
    ingredient_id: int,
    name: str = Form(None), quantity: float = Form(None),
    unit: str = Form(None), min_quantity: float = Form(None),
    unit_price: float = Form(None), supplier_id: int = Form(None),
    redirect: str = Form('/inventory'),
):
    db = get_db()
    if name is not None:
        db.execute('UPDATE ingredients SET name = ? WHERE id = ?', (name, ingredient_id))
    if quantity is not None:
        db.execute('UPDATE ingredients SET quantity = ? WHERE id = ?', (quantity, ingredient_id))
    if unit is not None:
        db.execute('UPDATE ingredients SET unit = ? WHERE id = ?', (unit, ingredient_id))
    if min_quantity is not None:
        db.execute('UPDATE ingredients SET min_quantity = ? WHERE id = ?', (min_quantity, ingredient_id))
    if unit_price is not None:
        db.execute('UPDATE ingredients SET unit_price = ? WHERE id = ?', (unit_price, ingredient_id))
    if supplier_id is not None:
        db.execute('UPDATE ingredients SET supplier_id = ? WHERE id = ?', (supplier_id if supplier_id else None, ingredient_id))
    db.commit()
    db.close()
    return RedirectResponse(redirect, status_code=303)


@app.post('/ingredients/delete/{ingredient_id}')
async def delete_ingredient(ingredient_id: int):
    db = get_db()
    db.execute('DELETE FROM recipes WHERE ingredient_id = ?', (ingredient_id,))
    db.execute('DELETE FROM ingredients WHERE id = ?', (ingredient_id,))
    db.commit()
    db.close()
    return RedirectResponse('/inventory', status_code=303)


# ── Users (admin only) ──────────────────────────────────────

@app.get('/users', response_class=HTMLResponse)
async def users_page(request: Request):
    db = get_db()
    users = db.execute('SELECT id, username, role, display_name FROM users ORDER BY username').fetchall()
    db.close()
    return templates.TemplateResponse(request, 'users.html', context={'users': users})


@app.post('/users/add')
async def add_user(
    username: str = Form(...), password: str = Form(...),
    role: str = Form('staff'), display_name: str = Form(''),
):
    db = get_db()
    db.execute(
        'INSERT OR IGNORE INTO users (username, password_hash, role, display_name) VALUES (?, ?, ?, ?)',
        (username, hash_password(password), role, display_name)
    )
    db.commit()
    db.close()
    return RedirectResponse('/users', status_code=303)


@app.post('/users/role/{user_id}')
async def change_role(user_id: int, role: str = Form(...)):
    db = get_db()
    db.execute('UPDATE users SET role = ? WHERE id = ?', (role, user_id))
    db.commit()
    db.close()
    return RedirectResponse('/users', status_code=303)


@app.post('/users/password/{user_id}')
async def change_password(user_id: int, password: str = Form(...)):
    db = get_db()
    db.execute('UPDATE users SET password_hash = ? WHERE id = ?', (hash_password(password), user_id))
    db.commit()
    db.close()
    return RedirectResponse('/users', status_code=303)


@app.post('/users/delete/{user_id}')
async def delete_user(user_id: int):
    db = get_db()
    db.execute('DELETE FROM users WHERE id = ? AND username != "admin"', (user_id,))
    db.commit()
    db.close()
    return RedirectResponse('/users', status_code=303)


# ── Sync trigger ───────────────────────────────────────────

@app.post('/sync')
async def trigger_sync(
    date_from: str = Form(''), date_to: str = Form(''),
):
    from app.gopos_sync import sync_today, sync_range
    if date_from and date_to:
        asyncio.create_task(sync_range(date_from, date_to))
    elif date_from:
        from app.gopos_sync import sync_date
        asyncio.create_task(sync_date(date_from))
    else:
        asyncio.create_task(sync_today())
    return RedirectResponse('/', status_code=303)


@app.get('/api/sync-status')
async def sync_status():
    db = get_db()
    row = db.execute(
        'SELECT status, message, started_at, finished_at FROM sync_log ORDER BY id DESC LIMIT 1'
    ).fetchone()
    db.close()
    if not row:
        return JSONResponse({'status': 'none'})
    return JSONResponse({
        'status': row['status'],
        'message': row['message'],
        'started_at': row['started_at'],
        'finished_at': row['finished_at'],
    })


if __name__ == '__main__':
    import uvicorn
    uvicorn.run('main:app', host='0.0.0.0', port=8000, reload=True)
