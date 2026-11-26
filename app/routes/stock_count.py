"""Stock count routes."""
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.db import get_db
from app.gopos_api import warsaw_today

templates = Jinja2Templates(directory=Path(__file__).parent.parent / 'templates')

router = APIRouter()

# A difference smaller than this (in the item's unit) is rounding noise, not a shortage.
DIFF_EPS = 0.05


def diff_status(difference: float) -> str:
    """'short' (нестача), 'over' (надлишок) or 'match' (збіг, |difference| < DIFF_EPS)."""
    if difference <= -DIFF_EPS:
        return 'short'
    if difference >= DIFF_EPS:
        return 'over'
    return 'match'


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
    # Business day (05:00 cutoff) so the count lines up with inventory_deductions, which are
    # keyed by business date; the clock time shown in history is Warsaw local, not UTC.
    today = warsaw_today()
    time_now = datetime.now(ZoneInfo('Europe/Warsaw')).strftime('%H:%M')
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

    # After a stock count we know the real on-hand quantity as of now.
    # Clear today's inventory_deductions so that the next scheduled re-sync
    # (04:30 UTC) re-applies deductions from scratch against the counted stock,
    # rather than reversing and re-adding on top of an already-adjusted quantity.
    db.execute('DELETE FROM inventory_deductions WHERE date = ?', (today,))

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

    rows = db.execute('''
        SELECT sci.*, i.name, i.unit, COALESCE(i.unit_price, 0) AS unit_price
        FROM stock_count_items sci
        JOIN ingredients i ON sci.ingredient_id = i.id
        WHERE sci.stock_count_id = ?
        ORDER BY i.name
    ''', (count_id,)).fetchall()
    all_ingredients = db.execute('SELECT id, name, unit FROM ingredients ORDER BY name').fetchall()
    db.close()

    items = []
    for r in rows:
        d = dict(r)
        d['status'] = diff_status(d['difference'])
        items.append(d)
    # Only real shortages count as a loss, at the current unit price.
    total_diff_value = sum(-d['difference'] * d['unit_price'] for d in items if d['status'] == 'short')

    return templates.TemplateResponse(request, 'stock_count_detail.html', context={
        'count': count, 'items': items, 'total_diff_value': total_diff_value,
        'short_count': sum(1 for d in items if d['status'] == 'short'),
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
               SUM(CASE WHEN sci.difference <= -? THEN 1 ELSE 0 END) as shortage_count
        FROM stock_counts sc
        LEFT JOIN users u ON sc.user_id = u.id
        LEFT JOIN stock_count_items sci ON sci.stock_count_id = sc.id
        GROUP BY sc.id
        ORDER BY sc.date DESC, sc.id DESC
    ''', (DIFF_EPS,)).fetchall()
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
