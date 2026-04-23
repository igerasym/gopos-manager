"""Stock count routes."""
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.db import get_db

templates = Jinja2Templates(directory=Path(__file__).parent.parent / 'templates')

router = APIRouter()


@router.get('/inventory/count', response_class=HTMLResponse)
async def stock_count_page(request: Request):
    db = get_db()
    items = db.execute('''
        SELECT id, name, unit, quantity FROM ingredients ORDER BY name
    ''').fetchall()
    db.close()
    return templates.TemplateResponse(request, 'stock_count.html', context={
        'items': items,
    })


@router.post('/inventory/count')
async def save_stock_count(request: Request):
    form = await request.form()
    user = request.state.user
    now = datetime.now()
    today = now.strftime('%Y-%m-%d')
    time_now = now.strftime('%H:%M')
    note = form.get('note', '')

    db = get_db()

    cur = db.execute(
        'INSERT INTO stock_counts (date, time, user_id, note) VALUES (?, ?, ?, ?)',
        (today, time_now, user['id'], note)
    )
    count_id = cur.lastrowid

    items = db.execute('SELECT id, quantity FROM ingredients').fetchall()
    for item in items:
        actual_str = form.get(f'qty_{item["id"]}', '')
        if actual_str == '':
            continue
        actual = float(actual_str)
        expected = item['quantity']
        difference = actual - expected

        db.execute(
            'INSERT INTO stock_count_items (stock_count_id, ingredient_id, expected, actual, difference) '
            'VALUES (?, ?, ?, ?, ?)',
            (count_id, item['id'], expected, actual, difference)
        )
        db.execute('UPDATE ingredients SET quantity = ? WHERE id = ?', (actual, item['id']))

    db.commit()
    db.close()
    return RedirectResponse(f'/inventory/count/{count_id}', status_code=303)


@router.get('/inventory/count/{count_id}', response_class=HTMLResponse)
async def stock_count_detail(request: Request, count_id: int):
    db = get_db()
    count = db.execute('''
        SELECT sc.*, u.display_name, u.username
        FROM stock_counts sc LEFT JOIN users u ON sc.user_id = u.id
        WHERE sc.id = ?
    ''', (count_id,)).fetchone()
    if not count:
        db.close()
        return RedirectResponse('/inventory/history', status_code=302)

    items = db.execute('''
        SELECT sci.*, i.name, i.unit
        FROM stock_count_items sci
        JOIN ingredients i ON sci.ingredient_id = i.id
        WHERE sci.stock_count_id = ?
        ORDER BY i.name
    ''', (count_id,)).fetchall()

    # Ingredients not yet in this count
    counted_ids = [it['ingredient_id'] for it in items]
    all_ingredients = db.execute('SELECT id, name, unit FROM ingredients ORDER BY name').fetchall()

    total_diff_value = sum(
        abs(it['difference']) * (db.execute(
            'SELECT COALESCE(unit_price, 0) as p FROM ingredients WHERE id = ?',
            (it['ingredient_id'],)
        ).fetchone()['p'] or 0)
        for it in items if it['difference'] < 0
    )

    db.close()
    return templates.TemplateResponse(request, 'stock_count_detail.html', context={
        'count': count, 'items': items, 'total_diff_value': total_diff_value,
        'all_ingredients': all_ingredients,
    })


@router.post('/inventory/count/{count_id}/update')
async def update_stock_count_item(request: Request, count_id: int):
    form = await request.form()
    db = get_db()

    item_id = int(form.get('item_id', 0))
    actual = float(form.get('actual', 0))

    # Update the count item
    old = db.execute('SELECT expected FROM stock_count_items WHERE id = ?', (item_id,)).fetchone()
    if old:
        difference = actual - old['expected']
        db.execute(
            'UPDATE stock_count_items SET actual = ?, difference = ? WHERE id = ?',
            (actual, difference, item_id)
        )
        # Also update ingredient stock
        ing = db.execute(
            'SELECT ingredient_id FROM stock_count_items WHERE id = ?', (item_id,)
        ).fetchone()
        if ing:
            db.execute('UPDATE ingredients SET quantity = ? WHERE id = ?', (actual, ing['ingredient_id']))

    db.commit()
    db.close()
    return RedirectResponse(f'/inventory/count/{count_id}', status_code=303)


@router.post('/inventory/count/{count_id}/add')
async def add_stock_count_item(request: Request, count_id: int):
    form = await request.form()
    db = get_db()

    ingredient_id = int(form.get('ingredient_id', 0))
    actual = float(form.get('actual', 0))

    # Check if already in this count
    existing = db.execute(
        'SELECT id FROM stock_count_items WHERE stock_count_id = ? AND ingredient_id = ?',
        (count_id, ingredient_id)
    ).fetchone()

    ing = db.execute('SELECT quantity FROM ingredients WHERE id = ?', (ingredient_id,)).fetchone()
    expected = ing['quantity'] if ing else 0
    difference = actual - expected

    if existing:
        db.execute(
            'UPDATE stock_count_items SET actual = ?, difference = ? WHERE id = ?',
            (actual, difference, existing['id'])
        )
    else:
        db.execute(
            'INSERT INTO stock_count_items (stock_count_id, ingredient_id, expected, actual, difference) '
            'VALUES (?, ?, ?, ?, ?)',
            (count_id, ingredient_id, expected, actual, difference)
        )

    db.execute('UPDATE ingredients SET quantity = ? WHERE id = ?', (actual, ingredient_id))
    db.commit()
    db.close()
    return RedirectResponse(f'/inventory/count/{count_id}', status_code=303)


@router.get('/inventory/history', response_class=HTMLResponse)
async def stock_count_history(request: Request):
    db = get_db()
    counts = db.execute('''
        SELECT sc.id, sc.date, sc.time, sc.note, sc.created_at,
               u.display_name, u.username,
               COUNT(sci.id) as item_count,
               SUM(CASE WHEN sci.difference < 0 THEN 1 ELSE 0 END) as shortage_count
        FROM stock_counts sc
        LEFT JOIN users u ON sc.user_id = u.id
        LEFT JOIN stock_count_items sci ON sci.stock_count_id = sc.id
        GROUP BY sc.id
        ORDER BY sc.date DESC, sc.id DESC
    ''').fetchall()
    db.close()
    return templates.TemplateResponse(request, 'stock_count_history.html', context={
        'counts': counts,
    })


@router.post('/inventory/count/{count_id}/delete')
async def delete_stock_count(count_id: int):
    db = get_db()
    db.execute('DELETE FROM stock_count_items WHERE stock_count_id = ?', (count_id,))
    db.execute('DELETE FROM stock_counts WHERE id = ?', (count_id,))
    db.commit()
    db.close()
    return RedirectResponse('/inventory/history', status_code=303)
