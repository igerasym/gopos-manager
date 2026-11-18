"""Wastage (списання псування) routes."""
import csv
import io
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from app.db import get_db

templates = Jinja2Templates(directory=Path(__file__).parent.parent / 'templates')
router = APIRouter()

REASONS = ['spoiled', 'broken', 'expired', 'overproduction', 'other']
REASON_UA = {
    'spoiled': 'Зіпсовано',
    'broken': 'Розбито/розлито',
    'expired': 'Прострочено',
    'overproduction': 'Перевиробництво',
    'other': 'Інше',
}


@router.get('/wastage', response_class=HTMLResponse)
async def wastage_page(request: Request, month: str = ''):
    current_month = month or datetime.now().strftime('%Y-%m')
    month_start = current_month + '-01'
    year, mon = int(current_month[:4]), int(current_month[5:7])
    month_end = f'{year + 1 if mon == 12 else year}-{1 if mon == 12 else mon + 1:02d}-01'

    db = get_db()
    rows = db.execute('''
        SELECT w.id, w.date, w.quantity, w.reason, w.note,
               i.name as ingredient, i.unit, COALESCE(i.unit_price, 0) as unit_price,
               w.quantity * COALESCE(i.unit_price, 0) as cost,
               u.display_name as user_name
        FROM wastage w
        JOIN ingredients i ON w.ingredient_id = i.id
        LEFT JOIN users u ON w.user_id = u.id
        WHERE w.date >= ? AND w.date < ?
        ORDER BY w.date DESC, w.id DESC
    ''', (month_start, month_end)).fetchall()

    # Totals
    total_cost = sum(r['cost'] for r in rows)

    # By reason
    by_reason = {}
    for r in rows:
        key = r['reason']
        by_reason.setdefault(key, {'count': 0, 'cost': 0.0})
        by_reason[key]['count'] += 1
        by_reason[key]['cost'] += r['cost']

    # By ingredient (top losers)
    by_ingredient = {}
    for r in rows:
        key = r['ingredient']
        by_ingredient.setdefault(key, {'qty': 0.0, 'cost': 0.0, 'unit': r['unit']})
        by_ingredient[key]['qty'] += r['quantity']
        by_ingredient[key]['cost'] += r['cost']
    top_losers = sorted(by_ingredient.items(), key=lambda x: x[1]['cost'], reverse=True)[:10]

    ingredients = db.execute('SELECT id, name, unit FROM ingredients ORDER BY name').fetchall()
    months = db.execute('''
        SELECT DISTINCT substr(date, 1, 7) as month FROM wastage ORDER BY month DESC
    ''').fetchall()
    db.close()

    return templates.TemplateResponse(request, 'wastage.html', context={
        'rows': rows, 'total_cost': total_cost, 'current_month': current_month,
        'by_reason': by_reason, 'reason_ua': REASON_UA, 'reasons': REASONS,
        'top_losers': top_losers, 'ingredients': ingredients, 'months': months,
        'now_date': datetime.now().strftime('%Y-%m-%d'),
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
    today = date or datetime.now().strftime('%Y-%m-%d')
    month = today[:7]

    db = get_db()
    # Deduct from stock
    db.execute(
        'UPDATE ingredients SET quantity = MAX(0, quantity - ?) WHERE id = ?',
        (quantity, ingredient_id)
    )
    db.execute(
        'INSERT INTO wastage (date, ingredient_id, quantity, reason, note, user_id) VALUES (?,?,?,?,?,?)',
        (today, ingredient_id, quantity, reason, note, user['id'])
    )
    db.commit()
    db.close()
    return RedirectResponse(f'/wastage?month={month}', status_code=303)


@router.post('/wastage/delete/{wastage_id}')
async def delete_wastage(wastage_id: int):
    db = get_db()
    row = db.execute('SELECT ingredient_id, quantity, date FROM wastage WHERE id = ?', (wastage_id,)).fetchone()
    if row:
        # Restore stock
        db.execute(
            'UPDATE ingredients SET quantity = quantity + ? WHERE id = ?',
            (row['quantity'], row['ingredient_id'])
        )
        db.execute('DELETE FROM wastage WHERE id = ?', (wastage_id,))
        db.commit()
        month = row['date'][:7]
    else:
        month = datetime.now().strftime('%Y-%m')
    db.close()
    return RedirectResponse(f'/wastage?month={month}', status_code=303)


@router.get('/export/wastage.csv')
async def export_wastage_csv(month: str = ''):
    current_month = month or datetime.now().strftime('%Y-%m')
    month_start = current_month + '-01'
    year, mon = int(current_month[:4]), int(current_month[5:7])
    month_end = f'{year + 1 if mon == 12 else year}-{1 if mon == 12 else mon + 1:02d}-01'

    db = get_db()
    rows = db.execute('''
        SELECT w.date, i.name, w.quantity, i.unit,
               w.quantity * COALESCE(i.unit_price, 0) as cost,
               w.reason, w.note
        FROM wastage w JOIN ingredients i ON w.ingredient_id = i.id
        WHERE w.date >= ? AND w.date < ?
        ORDER BY w.date DESC
    ''', (month_start, month_end)).fetchall()
    db.close()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['Дата', 'Інгредієнт', 'К-сть', 'Од.', 'Вартість (zł)', 'Причина', 'Примітка'])
    for r in rows:
        w.writerow([r['date'], r['name'], r['quantity'], r['unit'],
                    round(r['cost'], 2), REASON_UA.get(r['reason'], r['reason']), r['note'] or ''])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type='text/csv; charset=utf-8-sig',
        headers={'Content-Disposition': f'attachment; filename="wastage_{current_month}.csv"'},
    )
