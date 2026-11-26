"""Wastage (Списання) routes."""
import csv
import io
import re
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from app.db import get_db
from app.services.recipes import get_cost_lookup
from app.gopos_api import warsaw_today
from app.routes.inventory import low_stock_count, parse_decimal

templates = Jinja2Templates(directory=Path(__file__).parent.parent / 'templates')
router = APIRouter()

REASONS = ['write-off', 'spoiled', 'broken', 'expired', 'overproduction', 'other']
REASON_UA = {
    'write-off': 'Списано на касі',
    'spoiled': 'Зіпсовано',
    'broken': 'Розбито / розлито',
    'expired': 'Прострочено',
    'overproduction': 'Залишилось / перевиробництво',
    'other': 'Інше',
}
MONTHS_UA = ['січень', 'лютий', 'березень', 'квітень', 'травень', 'червень',
             'липень', 'серпень', 'вересень', 'жовтень', 'листопад', 'грудень']
_MONTH_RE = re.compile(r'^\d{4}-(0[1-9]|1[0-2])$')
_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def manual_reason_choices() -> list[tuple[str, str]]:
    """Reasons a person can pick (GoPOS write-offs are a source, not a reason)."""
    return [(r, REASON_UA[r]) for r in REASONS if r != 'write-off']


def _is_admin(request: Request) -> bool:
    user = getattr(request.state, 'user', None)
    return bool(user and user['role'] == 'admin')


def _month_bounds(month: str) -> tuple[str, str]:
    year, mon = int(month[:4]), int(month[5:7])
    end = f'{year + 1 if mon == 12 else year}-{1 if mon == 12 else mon + 1:02d}-01'
    return month + '-01', end


def shift_month(month: str, delta: int) -> str:
    year, mon = int(month[:4]), int(month[5:7]) - 1 + delta
    year += mon // 12
    return f'{year}-{mon % 12 + 1:02d}'


def month_label(month: str) -> str:
    return f'{MONTHS_UA[int(month[5:7]) - 1]} {month[:4]}'


def _price_rows(rows) -> list:
    """Rows as dicts with `cost`/`cost_known` filled in.

    Manual rows already carry quantity x ingredient price. A GoPos write-off is a product
    (e.g. 'Croissant'), so its cost is quantity x the product's recipe cost at current
    ingredient prices - the same figure the dashboard uses for COGS. No recipe -> unknown,
    shown as a dash, not as a free 0.00.
    """
    product_cost = get_cost_lookup()
    out = []
    for r in rows:
        d = dict(r)
        if d['source'] == 'gopos':
            unit_cost = product_cost.get(d['label'], 0) or 0
            d['cost'] = d['quantity'] * unit_cost
            d['cost_known'] = unit_cost > 0
            d['unit'] = 'szt'
        else:
            d['cost_known'] = d['cost'] > 0
        out.append(d)
    return out


def month_rows(db, month: str) -> list:
    """All wastage rows of a month (GoPOS write-offs by product + manual by ingredient), priced."""
    month_start, month_end = _month_bounds(month)
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
    return _price_rows(rows)


def summarize(rows: list) -> dict:
    """Known-cost total, top losses (known cost only), names without a cost, and by-reason
    totals where every GoPOS write-off counts under 'write-off' (its source, not a reason)."""
    total_cost = sum(r['cost'] for r in rows if r['cost_known'])
    unpriced = sorted({r['label'] for r in rows if not r['cost_known']})
    by_item = {}
    for r in rows:
        if not r['cost_known']:
            continue
        d = by_item.setdefault(r['label'], {'qty': 0.0, 'cost': 0.0, 'unit': r['unit'], 'source': r['source']})
        d['qty'] += r['quantity']
        d['cost'] += r['cost']
    top = sorted(by_item.items(), key=lambda x: x[1]['cost'], reverse=True)[:10]
    by_reason = {}
    for r in rows:
        key = 'write-off' if r['source'] == 'gopos' else r['reason']
        d = by_reason.setdefault(key, {'count': 0, 'cost': 0.0})
        d['count'] += 1
        d['cost'] += r['cost'] if r['cost_known'] else 0
    reasons = sorted(by_reason.items(), key=lambda x: (-x[1]['cost'], -x[1]['count']))
    return {'total_cost': total_cost, 'unpriced': unpriced, 'top_losers': top, 'by_reason': reasons}


@router.get('/wastage', response_class=HTMLResponse)
async def wastage_page(request: Request, month: str = '', saved: str = ''):
    today = warsaw_today()
    current_month = month if _MONTH_RE.match(month or '') else today[:7]
    prev_month = shift_month(current_month, -1)
    next_month = shift_month(current_month, 1)

    db = get_db()
    rows = month_rows(db, current_month)
    summary = summarize(rows)
    prev_total = summarize(month_rows(db, prev_month))['total_cost']
    month_start, month_end = _month_bounds(current_month)
    revenue = db.execute('SELECT COALESCE(SUM(total_money), 0) FROM sales WHERE date >= ? AND date < ?',
                         (month_start, month_end)).fetchone()[0]
    ingredients = db.execute('SELECT id, name, unit FROM ingredients ORDER BY name').fetchall()
    db.close()

    gopos_count = sum(1 for r in rows if r['source'] == 'gopos')
    return templates.TemplateResponse(request, 'wastage.html', context={
        'rows': rows, 'current_month': current_month, 'month_label': month_label(current_month),
        'prev_month': prev_month, 'next_month': next_month if next_month <= today[:7] else '',
        'is_current_month': current_month == today[:7],
        'total_cost': summary['total_cost'], 'unpriced': summary['unpriced'],
        'top_losers': summary['top_losers'], 'by_reason': summary['by_reason'],
        'prev_total': prev_total, 'prev_month_label': month_label(prev_month),
        'revenue': revenue,
        'share_pct': summary['total_cost'] / revenue * 100 if revenue else None,
        'reason_ua': REASON_UA,
        'wastage_reasons': manual_reason_choices(),
        'wastage_ingredients': ingredients,
        'gopos_count': gopos_count, 'manual_count': len(rows) - gopos_count,
        'is_admin': _is_admin(request),
        'now_date': today, 'saved': saved,
        'low_count': low_stock_count(),
    })


@router.post('/wastage/add')
async def add_wastage(
    request: Request,
    ingredient_id: int = Form(...),
    quantity: str = Form(...),
    reason: str = Form('spoiled'),
    note: str = Form(''),
    date: str = Form(''),
):
    user = request.state.user
    today = date if _DATE_RE.match(date or '') else warsaw_today()
    month = today[:7]
    qty_v = parse_decimal(quantity, 0)
    if reason not in REASON_UA or reason == 'write-off':
        reason = 'other'

    db = get_db()
    # Deduct from stock
    db.execute(
        'UPDATE ingredients SET quantity = MAX(0, quantity - ?) WHERE id = ?',
        (qty_v, ingredient_id)
    )
    db.execute(
        'INSERT INTO wastage (date, ingredient_id, quantity, reason, note, source, user_id) '
        'VALUES (?,?,?,?,?,\'manual\',?)',
        (today, ingredient_id, qty_v, reason, note, user['id'])
    )
    db.commit()
    db.close()
    return RedirectResponse(f'/wastage?month={month}&saved=add', status_code=303)


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
    return RedirectResponse(f'/wastage?month={month}&saved=delete', status_code=303)


@router.get('/export/wastage.csv')
async def export_wastage_csv(request: Request, month: str = ''):
    current_month = month if _MONTH_RE.match(month or '') else warsaw_today()[:7]
    month_start, month_end = _month_bounds(current_month)

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
                          'Каса' if r['source'] == 'gopos' else 'Вручну', r['note'] or ''])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type='text/csv; charset=utf-8-sig',
        headers={'Content-Disposition': f'attachment; filename="wastage_{current_month}.csv"'},
    )
