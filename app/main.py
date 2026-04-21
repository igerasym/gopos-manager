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

BASE_DIR = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()

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

app = FastAPI(title='Cafe Manager', lifespan=lifespan)
app.mount('/static', StaticFiles(directory=BASE_DIR / 'static'), name='static')
templates = Jinja2Templates(directory=BASE_DIR / 'templates')


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

    db.close()
    return templates.TemplateResponse(request, 'dashboard.html', context={
        'date_from': date_from, 'date_to': date_to,
        'presets': presets, 'active_preset': active_preset,
        'sales': sales, 'totals': totals,
        'top_products': top_products, 'top_by_qty': top_by_qty,
        'daily': daily, 'period_days': period_days,
        'prev_totals': prev_totals,
        'low_stock': low_stock,
    })


# ── Inventory ──────────────────────────────────────────────

@app.get('/inventory', response_class=HTMLResponse)
async def inventory_page(request: Request):
    db = get_db()
    items = db.execute(
        'SELECT * FROM ingredients ORDER BY name'
    ).fetchall()
    db.close()
    return templates.TemplateResponse(request, 'inventory.html', context={
        'items': items,
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
):
    db = get_db()
    db.execute(
        'INSERT INTO deliveries (ingredient_id, quantity, price, note) '
        'VALUES (?, ?, ?, ?)', (ingredient_id, quantity, price, note)
    )
    db.execute(
        'UPDATE ingredients SET quantity = quantity + ? WHERE id = ?',
        (quantity, ingredient_id)
    )
    db.commit()
    db.close()
    return RedirectResponse('/inventory', status_code=303)


# ── Recipes ────────────────────────────────────────────────

@app.get('/recipes', response_class=HTMLResponse)
async def recipes_page(request: Request):
    db = get_db()
    recipes = db.execute('''
        SELECT r.id, r.product_name, i.name as ingredient, r.amount, i.unit
        FROM recipes r JOIN ingredients i ON r.ingredient_id = i.id
        ORDER BY r.product_name
    ''').fetchall()
    ingredients = db.execute('SELECT id, name, unit FROM ingredients ORDER BY name').fetchall()
    products = db.execute('SELECT DISTINCT product_name FROM sales ORDER BY product_name').fetchall()
    db.close()
    return templates.TemplateResponse(request, 'recipes.html', context={
        'recipes': recipes,
        'ingredients': ingredients, 'products': products,
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


@app.post('/recipes/delete/{recipe_id}')
async def delete_recipe(recipe_id: int):
    db = get_db()
    db.execute('DELETE FROM recipes WHERE id = ?', (recipe_id,))
    db.commit()
    db.close()
    return RedirectResponse('/recipes', status_code=303)


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
