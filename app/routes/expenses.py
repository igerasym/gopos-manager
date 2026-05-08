"""Expenses routes (admin only)."""
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.db import get_db

templates = Jinja2Templates(directory=Path(__file__).parent.parent / 'templates')

router = APIRouter()

CATEGORIES = ['Оренда', 'Зарплати', 'Бухгалтерія', 'Комунальні', 'Підписки', 'Побут', 'Податки і ZUS', 'Логістика', 'Продукти', 'Інше']


@router.get('/expenses', response_class=HTMLResponse)
async def expenses_page(request: Request, month: str = ''):
    current_month = month or datetime.now().strftime('%Y-%m')
    db = get_db()

    # Auto-populate recurring expenses if this month has none yet
    existing_recurring = db.execute(
        'SELECT COUNT(*) as cnt FROM expenses WHERE month = ? AND recurring = 1',
        (current_month,)
    ).fetchone()['cnt']

    if existing_recurring == 0:
        # Find the latest month that has recurring expenses
        prev = db.execute('''
            SELECT DISTINCT month FROM expenses
            WHERE recurring = 1 AND month < ?
            ORDER BY month DESC LIMIT 1
        ''', (current_month,)).fetchone()
        if prev:
            recurring_items = db.execute(
                'SELECT name, category, amount, note FROM expenses WHERE month = ? AND recurring = 1',
                (prev['month'],)
            ).fetchall()
            for r in recurring_items:
                db.execute(
                    'INSERT INTO expenses (name, category, amount, month, recurring, note) VALUES (?, ?, ?, ?, 1, ?)',
                    (r['name'], r['category'], r['amount'], current_month, r['note'])
                )
            if recurring_items:
                db.commit()

    expenses = db.execute('''
        SELECT * FROM expenses WHERE month = ? ORDER BY recurring DESC, name
    ''', (current_month,)).fetchall()

    # Totals by category
    by_category = db.execute('''
        SELECT category, SUM(amount) as total
        FROM expenses WHERE month = ?
        GROUP BY category ORDER BY total DESC
    ''', (current_month,)).fetchall()

    total = sum(r['total'] for r in by_category)

    # Revenue for this month from sales
    month_start = current_month + '-01'
    # Last day of month
    year, mon = int(current_month[:4]), int(current_month[5:7])
    if mon == 12:
        month_end = f'{year + 1}-01-01'
    else:
        month_end = f'{year}-{mon + 1:02d}-01'

    revenue_row = db.execute('''
        SELECT COALESCE(SUM(total_money), 0) as revenue
        FROM sales WHERE date >= ? AND date < ?
    ''', (month_start, month_end)).fetchone()
    revenue = revenue_row['revenue']

    # OpEx = total expenses minus "Продукти" (COGS tracked separately via inventory)
    opex = sum(r['total'] for r in by_category if r['category'] != 'Продукти')
    net_profit = revenue - total

    # Available months
    months = db.execute('''
        SELECT DISTINCT month FROM expenses ORDER BY month DESC
    ''').fetchall()

    db.close()
    return templates.TemplateResponse(request, 'expenses.html', context={
        'expenses': expenses, 'by_category': by_category,
        'total': total, 'current_month': current_month,
        'months': months, 'categories': CATEGORIES,
        'revenue': revenue, 'opex': opex, 'net_profit': net_profit,
    })


@router.post('/expenses/add')
async def add_expense(
    name: str = Form(...), category: str = Form('Інше'),
    amount: float = Form(...), month: str = Form(''),
    recurring: int = Form(0), note: str = Form(''),
):
    month = month or datetime.now().strftime('%Y-%m')
    db = get_db()
    db.execute(
        'INSERT INTO expenses (name, category, amount, month, recurring, note) VALUES (?, ?, ?, ?, ?, ?)',
        (name, category, amount, month, recurring, note)
    )
    db.commit()
    db.close()
    return RedirectResponse(f'/expenses?month={month}', status_code=303)


@router.post('/expenses/update/{expense_id}')
async def update_expense(
    expense_id: int,
    amount: Optional[float] = Form(None),
    name: Optional[str] = Form(None),
    category: Optional[str] = Form(None),
    recurring: Optional[int] = Form(None),
):
    db = get_db()
    row = db.execute('SELECT month FROM expenses WHERE id = ?', (expense_id,)).fetchone()
    m = row['month'] if row else ''

    updates = []
    params = []
    if amount is not None:
        updates.append('amount = ?')
        params.append(amount)
    if name:
        updates.append('name = ?')
        params.append(name)
    if category:
        updates.append('category = ?')
        params.append(category)
    if recurring is not None:
        updates.append('recurring = ?')
        params.append(recurring)

    if updates:
        params.append(expense_id)
        db.execute(f'UPDATE expenses SET {", ".join(updates)} WHERE id = ?', params)
        db.commit()
    db.close()
    return RedirectResponse(f'/expenses?month={m}', status_code=303)


@router.post('/expenses/toggle-recurring/{expense_id}')
async def toggle_recurring(expense_id: int):
    db = get_db()
    row = db.execute('SELECT month, recurring FROM expenses WHERE id = ?', (expense_id,)).fetchone()
    if row:
        new_val = 0 if row['recurring'] else 1
        db.execute('UPDATE expenses SET recurring = ? WHERE id = ?', (new_val, expense_id))
        db.commit()
    m = row['month'] if row else ''
    db.close()
    return RedirectResponse(f'/expenses?month={m}', status_code=303)


@router.post('/expenses/delete/{expense_id}')
async def delete_expense(expense_id: int):
    db = get_db()
    month = db.execute('SELECT month FROM expenses WHERE id = ?', (expense_id,)).fetchone()
    m = month['month'] if month else ''
    db.execute('DELETE FROM expenses WHERE id = ?', (expense_id,))
    db.commit()
    db.close()
    return RedirectResponse(f'/expenses?month={m}', status_code=303)


