"""Expenses routes (admin only)."""
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.db import get_db

templates = Jinja2Templates(directory=Path(__file__).parent.parent / 'templates')

router = APIRouter()

CATEGORIES = ['Оренда', 'Зарплати', 'Бухгалтерія', 'Комунальні', 'Підписки', 'Інше']


@router.get('/expenses', response_class=HTMLResponse)
async def expenses_page(request: Request, month: str = ''):
    current_month = month or datetime.now().strftime('%Y-%m')
    db = get_db()

    expenses = db.execute('''
        SELECT * FROM expenses WHERE month = ? ORDER BY category, name
    ''', (current_month,)).fetchall()

    # Totals by category
    by_category = db.execute('''
        SELECT category, SUM(amount) as total
        FROM expenses WHERE month = ?
        GROUP BY category ORDER BY total DESC
    ''', (current_month,)).fetchall()

    total = sum(r['total'] for r in by_category)

    # Available months
    months = db.execute('''
        SELECT DISTINCT month FROM expenses ORDER BY month DESC
    ''').fetchall()

    db.close()
    return templates.TemplateResponse(request, 'expenses.html', context={
        'expenses': expenses, 'by_category': by_category,
        'total': total, 'current_month': current_month,
        'months': months, 'categories': CATEGORIES,
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


@router.post('/expenses/delete/{expense_id}')
async def delete_expense(expense_id: int):
    db = get_db()
    month = db.execute('SELECT month FROM expenses WHERE id = ?', (expense_id,)).fetchone()
    m = month['month'] if month else ''
    db.execute('DELETE FROM expenses WHERE id = ?', (expense_id,))
    db.commit()
    db.close()
    return RedirectResponse(f'/expenses?month={m}', status_code=303)


@router.post('/expenses/copy-month')
async def copy_recurring(from_month: str = Form(...), to_month: str = Form(...)):
    """Copy recurring expenses from one month to another."""
    db = get_db()
    recurring = db.execute(
        'SELECT name, category, amount, recurring, note FROM expenses WHERE month = ? AND recurring = 1',
        (from_month,)
    ).fetchall()
    for r in recurring:
        db.execute(
            'INSERT OR IGNORE INTO expenses (name, category, amount, month, recurring, note) VALUES (?, ?, ?, ?, ?, ?)',
            (r['name'], r['category'], r['amount'], to_month, 1, r['note'])
        )
    db.commit()
    db.close()
    return RedirectResponse(f'/expenses?month={to_month}', status_code=303)
