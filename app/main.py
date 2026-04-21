"""Cafe Manager — FastAPI backend."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from db import get_db, init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title='Cafe Manager', lifespan=lifespan)
app.mount('/static', StaticFiles(directory='app/static'), name='static')
templates = Jinja2Templates(directory='app/templates')


# ── Dashboard ──────────────────────────────────────────────

@app.get('/', response_class=HTMLResponse)
async def dashboard(request: Request, date: str = ''):
    date = date or datetime.now().strftime('%Y-%m-%d')
    db = get_db()

    sales = db.execute('''
        SELECT product_name, quantity, total_money, net_profit
        FROM sales WHERE date = ? ORDER BY total_money DESC
    ''', (date,)).fetchall()

    totals = db.execute('''
        SELECT COALESCE(SUM(total_money),0) as revenue,
               COALESCE(SUM(net_profit),0) as profit,
               COALESCE(SUM(quantity),0) as items
        FROM sales WHERE date = ?
    ''', (date,)).fetchone()

    low_stock = db.execute('''
        SELECT name, quantity, unit, min_quantity
        FROM ingredients WHERE quantity <= min_quantity
    ''').fetchall()

    db.close()
    return templates.TemplateResponse('dashboard.html', {
        'request': request, 'date': date, 'sales': sales,
        'totals': totals, 'low_stock': low_stock,
    })


# ── Inventory ──────────────────────────────────────────────

@app.get('/inventory', response_class=HTMLResponse)
async def inventory_page(request: Request):
    db = get_db()
    items = db.execute(
        'SELECT * FROM ingredients ORDER BY name'
    ).fetchall()
    db.close()
    return templates.TemplateResponse('inventory.html', {
        'request': request, 'items': items,
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
    return templates.TemplateResponse('recipes.html', {
        'request': request, 'recipes': recipes,
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
async def trigger_sync():
    from gopos_sync import sync_today
    asyncio.create_task(sync_today())
    return RedirectResponse('/', status_code=303)


if __name__ == '__main__':
    import uvicorn
    uvicorn.run('main:app', host='0.0.0.0', port=8000, reload=True)
