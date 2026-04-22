"""Inventory routes — ingredients, deliveries, suppliers."""
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.db import get_db
from app.services.inventory import get_all_ingredients, get_suppliers, get_ingredient_deps

templates = Jinja2Templates(directory=Path(__file__).parent.parent / 'templates')

router = APIRouter()


@router.get('/inventory', response_class=HTMLResponse)
async def inventory_page(request: Request, supplier: str = ''):
    items = get_all_ingredients(supplier)
    suppliers = get_suppliers()
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
async def ingredient_deps_api(ingredient_id: int):
    return JSONResponse(get_ingredient_deps(ingredient_id))
