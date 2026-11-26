"""Wastage (списання псування) routes."""
import csv
import io
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from app.auth import is_admin as _is_admin
from app.db import get_db
from app.gopos_api import month_bounds, warsaw_today
from app.services.wastage import price_rows as _price_rows

templates = Jinja2Templates(directory=Path(__file__).parent.parent / 'templates')
router = APIRouter()

REASONS = ['write-off', 'spoiled', 'broken', 'expired', 'overproduction', 'other']
REASON_UA = {
    'write-off': 'Списання (GoPos)',
    'spoiled': 'Зіпсовано',
    'broken': 'Розбито/розлито',
    'expired': 'Прострочено',
    'overproduction': 'Перевиробництво',
    'other': 'Інше',
}


@router.get('/wastage', response_class=HTMLResponse)
async def wastage_page(request: Request, month: str = ''):
    current_month = month or warsaw_today()[:7]
    month_start, month_end = month_bounds(current_month)

    db = get_db()

    # Combined view: GoPos write-offs (by product) + manual (by ingredient)
    rows = db.execute('''
        SELECT w.id, w.date, w.quantity, w.reason, w.note, w.source,
               COALESCE(i.name, w.product_name, '—') as label,
               COALESCE(i.unit, '—') as unit,
               COALESCE(i.unit_price, 0) as unit_price,
               w.quantity * COALESCE(i.unit_price, 0) as cost,
               u.display_name as user_name
        FROM wastage w
        LEFT JOIN ingredients i ON w.ingredient_id = i.id
        LEFT JOIN users u ON w.user_id = u.id
        WHERE w.date >= ? AND w.date < ?
        ORDER BY w.date DESC, w.id DESC
    ''', (month_start, month_end)).fetchall()

    rows = _price_rows(rows)
    total_cost = sum(r['cost'] for r in rows)
    unpriced = sorted({r['label'] for r in rows if r['source'] == 'gopos' and not r['cost_known']})
    gopos_count = sum(1 for r in rows if r['source'] == 'gopos')
    manual_count = len(rows) - gopos_count

    # By reason
    by_reason = {}
    for r in rows:
        key = r['reason']
        by_reason.setdefault(key, {'count': 0, 'cost': 0.0})
        by_reason[key]['count'] += 1
        by_reason[key]['cost'] += r['cost']

    # By product/ingredient (top losers — only where cost is known; the rest are not losses we can rank)
    by_item = {}
    for r in rows:
        if not r['cost_known']:
            continue
        key = r['label']
        by_item.setdefault(key, {'qty': 0.0, 'cost': 0.0, 'unit': r['unit']})
        by_item[key]['qty'] += r['quantity']
        by_item[key]['cost'] += r['cost']
    top_losers = sorted(by_item.items(), key=lambda x: x[1]['cost'], reverse=True)[:10]

    ingredients = db.execute('SELECT id, name, unit FROM ingredients ORDER BY name').fetchall()
    months = db.execute('''
        SELECT DISTINCT substr(date, 1, 7) as month FROM wastage ORDER BY month DESC
    ''').fetchall()
    db.close()

    return templates.TemplateResponse(request, 'wastage.html', context={
        'rows': rows, 'total_cost': total_cost, 'current_month': current_month,
        'by_reason': by_reason, 'reason_ua': REASON_UA,
        'manual_reasons': [r for r in REASONS if r != 'write-off'],
        'top_losers': top_losers, 'ingredients': ingredients, 'months': months,
        'gopos_count': gopos_count, 'manual_count': manual_count,
        'is_admin': _is_admin(request),
        'unpriced': unpriced,
        'now_date': warsaw_today(),
    })


@router.post('/wastage/add')
async def add_wastage(
    request: Request,
    ingredient_id: int = Form(...),
    quantity: float = Form(...),
    reason: str = Form('spoiled'),
    note: str = Form(''),
    date: str = Form(''),
):
    user = request.state.user
    today = date or warsaw_today()
    month = today[:7]
    if reason not in REASONS or reason == 'write-off':
        reason = 'other'  # 'write-off' is reserved for the GoPOS import

    db = get_db()
    # Deduct from stock
    db.execute(
        'UPDATE ingredients SET quantity = MAX(0, quantity - ?) WHERE id = ?',
        (quantity, ingredient_id)
    )
    db.execute(
        'INSERT INTO wastage (date, ingredient_id, quantity, reason, note, source, user_id) '
        'VALUES (?,?,?,?,?,\'manual\',?)',
        (today, ingredient_id, quantity, reason, note, user['id'])
    )
    db.commit()
    db.close()
    return RedirectResponse(f'/wastage?month={month}', status_code=303)


@router.post('/wastage/delete/{wastage_id}')
async def delete_wastage(wastage_id: int):
    db = get_db()
    row = db.execute(
        'SELECT ingredient_id, quantity, date, source FROM wastage WHERE id = ?',
        (wastage_id,)
    ).fetchone()
    if row:
        # Only restore stock for manual entries (GoPos entries don't double-deduct)
        if row['source'] == 'manual' and row['ingredient_id']:
            db.execute(
                'UPDATE ingredients SET quantity = quantity + ? WHERE id = ?',
                (row['quantity'], row['ingredient_id'])
            )
        db.execute('DELETE FROM wastage WHERE id = ?', (wastage_id,))
        db.commit()
        month = row['date'][:7]
    else:
        month = warsaw_today()[:7]
    db.close()
    return RedirectResponse(f'/wastage?month={month}', status_code=303)


@router.get('/export/wastage.csv')
async def export_wastage_csv(request: Request, month: str = ''):
    current_month = month or warsaw_today()[:7]
    month_start, month_end = month_bounds(current_month)

    db = get_db()
    rows = db.execute('''
        SELECT w.date, COALESCE(i.name, w.product_name, '—') as label,
               w.quantity, COALESCE(i.unit, '—') as unit,
               w.quantity * COALESCE(i.unit_price, 0) as cost,
               w.reason, w.note, w.source
        FROM wastage w
        LEFT JOIN ingredients i ON w.ingredient_id = i.id
        WHERE w.date >= ? AND w.date < ?
        ORDER BY w.date DESC
    ''', (month_start, month_end)).fetchall()
    db.close()
    rows = _price_rows(rows)

    buf = io.StringIO()
    w = csv.writer(buf)
    show_cost = _is_admin(request)
    header = ['Дата', 'Позиція', 'К-сть', 'Од.']
    if show_cost:
        header.append('Вартість (zł)')
    w.writerow(header + ['Причина', 'Джерело', 'Примітка'])
    for r in rows:
        row = [r['date'], r['label'], r['quantity'], r['unit']]
        if show_cost:
            row.append(round(r['cost'], 2))
        w.writerow(row + [REASON_UA.get(r['reason'], r['reason']),
                          'GoPos' if r['source'] == 'gopos' else 'Вручну', r['note'] or ''])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type='text/csv; charset=utf-8-sig',
        headers={'Content-Disposition': f'attachment; filename="wastage_{current_month}.csv"'},
    )
