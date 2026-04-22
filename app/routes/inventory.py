"""Inventory routes — ingredients, deliveries, suppliers."""
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.db import get_db

templates = Jinja2Templates(directory=Path(__file__).parent.parent / 'templates')

router = APIRouter()


@router.get('/inventory', response_class=HTMLResponse)
async def inventory_page(request: Request, supplier: str = ''):
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
    suppliers = db.execute('SELECT id, name FROM suppliers ORDER BY name').fetchall()
    db.close()
    return templates.TemplateResponse(request, 'inventory.html', context={
        'items': items, 'suppliers': suppliers, 'active_supplier': supplier,
    })


@router.post('/inventory/add')
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


@router.post('/inventory/delivery')
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


@router.post('/inventory/supplier')
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


@router.post('/ingredients/update/{ingredient_id}')
async def update_ingredient_unit(ingredient_id: int, unit: str = Form(...)):
    db = get_db()
    db.execute('UPDATE ingredients SET unit = ? WHERE id = ?', (unit, ingredient_id))
    db.commit()
    db.close()
    return RedirectResponse('/recipes', status_code=303)


@router.post('/ingredients/edit/{ingredient_id}')
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


@router.post('/ingredients/delete/{ingredient_id}')
async def delete_ingredient(ingredient_id: int):
    db = get_db()
    # Remove all references
    db.execute('DELETE FROM sub_recipe_items WHERE ingredient_id = ?', (ingredient_id,))
    db.execute('DELETE FROM recipes WHERE ingredient_id = ?', (ingredient_id,))
    db.execute('DELETE FROM deliveries WHERE ingredient_id = ?', (ingredient_id,))
    db.execute('DELETE FROM inventory_deductions WHERE ingredient_id = ?', (ingredient_id,))
    db.execute('DELETE FROM stock_count_items WHERE ingredient_id = ?', (ingredient_id,))
    # Remove sub-recipe if this ingredient is one
    db.execute('DELETE FROM sub_recipe_items WHERE sub_recipe_id IN (SELECT id FROM sub_recipes WHERE ingredient_id = ?)', (ingredient_id,))
    db.execute('DELETE FROM sub_recipes WHERE ingredient_id = ?', (ingredient_id,))
    db.execute('DELETE FROM ingredients WHERE id = ?', (ingredient_id,))
    db.commit()
    db.close()
    return RedirectResponse('/inventory', status_code=303)


@router.get('/api/ingredient/{ingredient_id}/deps')
async def ingredient_deps(ingredient_id: int):
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
    return JSONResponse({
        'name': ing['name'] if ing else '',
        'recipes': [r['product_name'] for r in recipes],
        'sub_recipes': [s['name'] for s in subs],
    })
