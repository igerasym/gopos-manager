"""Invoice upload routes."""
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.db import get_db

templates = Jinja2Templates(directory=Path(__file__).parent.parent / 'templates')

router = APIRouter()


@router.get('/inventory/upload', response_class=HTMLResponse)
async def upload_invoice_page(request: Request):
    return templates.TemplateResponse(request, 'invoice_upload.html', context={})


@router.post('/inventory/upload')
async def upload_invoice(request: Request, file: UploadFile = File(...)):
    from app.invoice_parser import extract_text_from_pdf, parse_invoice_with_llm

    file_bytes = await file.read()
    pdf_text = extract_text_from_pdf(file_bytes)

    db = get_db()
    ingredients = db.execute(
        'SELECT id, name, unit, COALESCE(unit_price, 0) as unit_price FROM ingredients ORDER BY name'
    ).fetchall()
    ingredients_list = [dict(i) for i in ingredients]
    suppliers = db.execute('SELECT id, name FROM suppliers ORDER BY name').fetchall()
    db.close()

    result = parse_invoice_with_llm(pdf_text, ingredients_list)

    return templates.TemplateResponse(request, 'invoice_preview.html', context={
        'result': result,
        'filename': file.filename,
        'suppliers': suppliers,
    })


@router.post('/inventory/upload/confirm')
async def confirm_invoice(request: Request):
    form = await request.form()
    db = get_db()

    supplier_id = form.get('supplier_id') or None
    invoice_note = form.get('invoice_note', '')
    today = datetime.now().strftime('%Y-%m-%d')

    item_count = int(form.get('item_count', 0))
    for i in range(item_count):
        skip = form.get(f'skip_{i}')
        if skip:
            continue

        ingredient_id = form.get(f'ingredient_id_{i}')
        ingredient_name = form.get(f'ingredient_name_{i}', '')
        quantity = float(form.get(f'quantity_{i}', 0))
        unit = form.get(f'unit_{i}', 'szt')
        price_brutto = float(form.get(f'price_brutto_{i}', 0))
        price_per_unit = float(form.get(f'price_per_unit_{i}', 0))
        is_new = form.get(f'is_new_{i}') == 'true'

        if is_new or not ingredient_id:
            # Create new ingredient
            db.execute(
                'INSERT OR IGNORE INTO ingredients (name, unit, quantity, min_quantity, unit_price, supplier_id) '
                'VALUES (?, ?, 0, 0, ?, ?)',
                (ingredient_name, unit, price_per_unit, supplier_id)
            )
            db.commit()
            ing = db.execute('SELECT id FROM ingredients WHERE name = ?', (ingredient_name,)).fetchone()
            ingredient_id = ing['id'] if ing else None
        else:
            ingredient_id = int(ingredient_id)
            # Update price
            db.execute('UPDATE ingredients SET unit_price = ? WHERE id = ?', (price_per_unit, ingredient_id))

        if ingredient_id:
            # Add delivery
            db.execute(
                'INSERT INTO deliveries (date, ingredient_id, quantity, price, note, supplier_id) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (today, ingredient_id, quantity, price_brutto, invoice_note, supplier_id)
            )
            # Update stock
            db.execute(
                'UPDATE ingredients SET quantity = quantity + ? WHERE id = ?',
                (quantity, ingredient_id)
            )

    db.commit()
    db.close()
    return RedirectResponse('/inventory', status_code=303)
