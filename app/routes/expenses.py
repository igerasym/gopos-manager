"""Expenses routes (admin only)."""
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.db import get_db

log = logging.getLogger(__name__)

templates = Jinja2Templates(directory=Path(__file__).parent.parent / 'templates')

router = APIRouter()

CATEGORIES = ['Оренда', 'Зарплати', 'Бухгалтерія', 'Комунальні', 'Підписки', 'Побут', 'Податки і ZUS', 'Логістика', 'Продукти', 'Інше']


@router.get('/expenses', response_class=HTMLResponse)
async def expenses_page(request: Request, month: str = ''):
    current_month = month or datetime.now().strftime('%Y-%m')
    db = get_db()

    # Auto-populate: add any recurring from previous month that are missing in current
    prev = db.execute('''
        SELECT DISTINCT month FROM expenses
        WHERE recurring > 0 AND month < ?
        ORDER BY month DESC LIMIT 1
    ''', (current_month,)).fetchone()

    if prev:
        prev_recurring = db.execute(
            'SELECT name, category, amount, recurring, note FROM expenses WHERE month = ? AND recurring > 0',
            (prev['month'],)
        ).fetchall()
        existing_names = set(
            r['name'] for r in db.execute(
                'SELECT name FROM expenses WHERE month = ?', (current_month,)
            ).fetchall()
        )
        dismissed_names = set(
            r['name'] for r in db.execute(
                'SELECT name FROM expenses_dismissed WHERE month = ?', (current_month,)
            ).fetchall()
        )
        added = 0
        for r in prev_recurring:
            if r['name'] not in existing_names and r['name'] not in dismissed_names:
                amt = r['amount'] if r['recurring'] == 1 else 0
                db.execute(
                    'INSERT INTO expenses (name, category, amount, month, recurring, note) VALUES (?, ?, ?, ?, ?, ?)',
                    (r['name'], r['category'], amt, current_month, r['recurring'], r['note'])
                )
                added += 1
        if added:
            db.commit()

    expenses = db.execute('''
        SELECT * FROM expenses WHERE month = ?
        ORDER BY CASE recurring WHEN 1 THEN 0 WHEN 2 THEN 1 ELSE 2 END, name
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

    # Load invoices from Google Drive for this month
    invoices = []
    parsed_ids = set()
    try:
        from app.gdrive_invoices import list_invoices_for_month
        invoices = list_invoices_for_month(current_month)
        # Get already parsed file IDs
        db2 = get_db()
        parsed_rows = db2.execute('SELECT file_id FROM parsed_invoices').fetchall()
        parsed_ids = set(r['file_id'] for r in parsed_rows)
        db2.close()
    except Exception as e:
        log.warning(f"Could not load invoices: {e}")

    return templates.TemplateResponse(request, 'expenses.html', context={
        'expenses': expenses, 'by_category': by_category,
        'total': total, 'current_month': current_month,
        'months': months, 'categories': CATEGORIES,
        'revenue': revenue, 'opex': opex, 'net_profit': net_profit,
        'invoices': invoices, 'parsed_ids': parsed_ids,
    })


@router.post('/expenses/add')
async def add_expense(
    name: str = Form(...), category: str = Form('Інше'),
    amount: Optional[str] = Form(''), month: str = Form(''),
    recurring: int = Form(0), note: str = Form(''),
):
    month = month or datetime.now().strftime('%Y-%m')
    try:
        amt = float(amount) if amount else 0.0
    except (ValueError, TypeError):
        amt = 0.0
    db = get_db()
    db.execute(
        'INSERT INTO expenses (name, category, amount, month, recurring, note) VALUES (?, ?, ?, ?, ?, ?)',
        (name, category, amt, month, recurring, note)
    )
    db.commit()
    db.close()
    return RedirectResponse(f'/expenses?month={month}', status_code=303)


@router.post('/expenses/update/{expense_id}')
async def update_expense(
    expense_id: int,
    amount: Optional[str] = Form(None),
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
        try:
            amt = float(amount) if amount else 0.0
        except (ValueError, TypeError):
            amt = 0.0
        updates.append('amount = ?')
        params.append(amt)
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
        # Cycle: 0 (one-time) → 1 (fixed) → 2 (variable) → 0
        new_val = (row['recurring'] + 1) % 3
        db.execute('UPDATE expenses SET recurring = ? WHERE id = ?', (new_val, expense_id))
        db.commit()
    m = row['month'] if row else ''
    db.close()
    return RedirectResponse(f'/expenses?month={m}', status_code=303)


@router.post('/expenses/delete/{expense_id}')
async def delete_expense(expense_id: int):
    db = get_db()
    row = db.execute('SELECT name, month, recurring FROM expenses WHERE id = ?', (expense_id,)).fetchone()
    m = row['month'] if row else ''
    # If recurring, remember it was dismissed so it won't auto-populate again
    if row and row['recurring'] > 0:
        db.execute(
            'INSERT OR IGNORE INTO expenses_dismissed (name, month) VALUES (?, ?)',
            (row['name'], row['month'])
        )
    db.execute('DELETE FROM expenses WHERE id = ?', (expense_id,))
    db.commit()
    db.close()
    return RedirectResponse(f'/expenses?month={m}', status_code=303)



@router.post('/expenses/parse-invoice/{file_id}')
async def parse_invoice(file_id: str):
    """Parse a single invoice from Google Drive using AWS Textract."""
    try:
        from app.gdrive_invoices import download_file, parse_invoice_textract
        import json

        file_bytes = download_file(file_id)
        result = parse_invoice_textract(file_bytes)
        result['file_id'] = file_id

        # Save to parsed_invoices table
        db = get_db()
        db.execute('''
            INSERT OR REPLACE INTO parsed_invoices (file_id, file_name, vendor, total, items_json)
            VALUES (?, ?, ?, ?, ?)
        ''', (file_id, result.get('file_name', ''), result.get('vendor', ''),
              result.get('total', 0), json.dumps(result.get('items', []), ensure_ascii=False)))
        db.commit()
        db.close()

        return JSONResponse(result)
    except Exception as e:
        log.error(f"Invoice parse error: {e}")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/expenses/process-invoices')
async def process_all_invoices(month: str = Form('')):
    """Parse all unparsed invoices for the month and add totals to expenses."""
    import json
    from app.gdrive_invoices import list_invoices_for_month, download_file, parse_invoice_textract

    current_month = month or datetime.now().strftime('%Y-%m')
    db = get_db()

    # Get already parsed file_ids
    parsed_ids = set(
        r['file_id'] for r in db.execute('SELECT file_id FROM parsed_invoices').fetchall()
    )

    # Get invoices for this month
    try:
        invoices = list_invoices_for_month(current_month)
    except Exception as e:
        db.close()
        return RedirectResponse(f'/expenses?month={current_month}', status_code=303)

    supported = ['application/pdf', 'image/jpeg', 'image/png', 'image/tiff']
    new_parsed = []
    errors = []

    for f in invoices:
        if f['id'] in parsed_ids:
            continue
        if f['mimeType'] not in supported:
            continue
        try:
            file_bytes = download_file(f['id'])
            result = parse_invoice_textract(file_bytes)
            db.execute('''
                INSERT OR REPLACE INTO parsed_invoices (file_id, file_name, folder, month, vendor, total, items_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (f['id'], f['name'], f.get('path', ''), current_month,
                  result.get('vendor', ''), result.get('total', 0),
                  json.dumps(result.get('items', []), ensure_ascii=False)))
            new_parsed.append({'name': f['name'], 'vendor': result.get('vendor', ''), 'total': result.get('total', 0)})
        except Exception as e:
            errors.append(f"{f['name']}: {str(e)[:80]}")

    db.commit()
    db.close()

    # Send Telegram notification
    try:
        from app.telegram_bot import send_message
        if new_parsed:
            msg = f"📄 <b>Фактури оброблені ({current_month})</b>\n\n"
            for p in new_parsed:
                msg += f"✅ {p['name']}\n   {p['vendor']} — {p['total']:.2f} zł\n"
            if errors:
                msg += f"\n❌ Помилки ({len(errors)}):\n"
                for e in errors[:5]:
                    msg += f"  • {e}\n"
            send_message(msg)
    except Exception:
        pass

    return RedirectResponse(f'/expenses?month={current_month}', status_code=303)
