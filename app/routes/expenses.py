"""Expenses routes (admin only)."""
import json
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

CATEGORIES = ['Оренда', 'Зарплати', 'Бухгалтерія', 'Комунальні', 'Побут', 'Податки і ZUS', 'Логістика', 'Продукти', 'Інше']


@router.get('/expenses', response_class=HTMLResponse)
async def expenses_page(request: Request, month: str = ''):
    current_month = month or datetime.now().strftime('%Y-%m')
    db = get_db()

    # All expenses for this month (sorted by amount desc)
    expenses = db.execute('''
        SELECT * FROM expenses WHERE month = ? ORDER BY amount DESC
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

    net_profit = revenue - total

    # Available months
    months = db.execute('''
        SELECT DISTINCT month FROM expenses ORDER BY month DESC
    ''').fetchall()

    db.close()

    # Load invoices from Google Drive for this month
    invoices = []
    parsed_ids = set()
    pending_invoices = []
    try:
        from app.gdrive_invoices import list_invoices_for_month
        invoices = list_invoices_for_month(current_month)
        db2 = get_db()
        parsed_rows = db2.execute('SELECT file_id FROM parsed_invoices').fetchall()
        parsed_ids = set(r['file_id'] for r in parsed_rows)
        pending_invoices = db2.execute(
            'SELECT * FROM parsed_invoices WHERE month = ? AND status = ? ORDER BY vendor',
            (current_month, 'pending')
        ).fetchall()
        db2.close()
    except Exception as e:
        log.warning(f"Could not load invoices: {e}")

    return templates.TemplateResponse(request, 'expenses.html', context={
        'expenses': expenses, 'by_category': by_category,
        'total': total, 'current_month': current_month,
        'months': months, 'categories': CATEGORIES,
        'revenue': revenue, 'net_profit': net_profit,
        'invoices': invoices, 'parsed_ids': parsed_ids,
        'pending_invoices': pending_invoices,
    })


@router.post('/expenses/add')
async def add_expense(
    name: str = Form(...), category: str = Form('Інше'),
    amount: Optional[str] = Form(''), month: str = Form(''),
    note: str = Form(''),
):
    month = month or datetime.now().strftime('%Y-%m')
    try:
        amt = float(amount) if amount else 0.0
    except (ValueError, TypeError):
        amt = 0.0
    db = get_db()
    db.execute(
        'INSERT INTO expenses (name, category, amount, month, recurring, note) VALUES (?, ?, ?, ?, 0, ?)',
        (name, category, amt, month, note)
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

    if updates:
        params.append(expense_id)
        db.execute(f'UPDATE expenses SET {", ".join(updates)} WHERE id = ?', params)
        db.commit()
    db.close()
    return RedirectResponse(f'/expenses?month={m}', status_code=303)


@router.post('/expenses/delete/{expense_id}')
async def delete_expense(expense_id: int):
    db = get_db()
    row = db.execute('SELECT month FROM expenses WHERE id = ?', (expense_id,)).fetchone()
    m = row['month'] if row else ''
    db.execute('DELETE FROM expenses WHERE id = ?', (expense_id,))
    db.commit()
    db.close()
    return RedirectResponse(f'/expenses?month={m}', status_code=303)


# ── Invoice parsing endpoints ──

@router.post('/expenses/parse-invoice/{file_id}')
async def parse_invoice(file_id: str):
    """Parse a single invoice from Google Drive using AWS Textract."""
    try:
        from app.gdrive_invoices import download_file, parse_invoice_textract, classify_vendor

        file_bytes = download_file(file_id)
        result = parse_invoice_textract(file_bytes)
        result['file_id'] = file_id

        # Auto-classify
        category, expense_name = classify_vendor(result.get('vendor', ''), '')
        result['category'] = category
        result['expense_name'] = expense_name

        # Save to parsed_invoices
        db = get_db()
        db.execute('''
            INSERT OR IGNORE INTO parsed_invoices
            (file_id, file_name, invoice_number, vendor, category, total, items_json, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
        ''', (file_id, '', result.get('invoice_number', ''),
              expense_name or result.get('vendor', ''), category,
              result.get('total', 0),
              json.dumps(result.get('items', []), ensure_ascii=False)))
        db.commit()
        db.close()

        return JSONResponse(result)
    except Exception as e:
        log.error(f"Invoice parse error: {e}")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/expenses/approve-invoice/{invoice_id}')
async def approve_invoice(invoice_id: int, category: str = Form('Продукти')):
    """Approve a parsed invoice — add it to expenses."""
    db = get_db()
    inv = db.execute('SELECT * FROM parsed_invoices WHERE id = ?', (invoice_id,)).fetchone()
    if not inv:
        db.close()
        return RedirectResponse('/expenses', status_code=303)

    month = inv['month'] or datetime.now().strftime('%Y-%m')
    vendor = inv['vendor'] or inv['file_name']
    amount = inv['total']

    # Add to expenses
    cur = db.execute(
        'INSERT INTO expenses (name, category, amount, month, recurring, note) VALUES (?, ?, ?, ?, 0, ?)',
        (vendor, category, amount, month, f"Фактура #{inv['invoice_number'] or inv['file_name']}")
    )
    expense_id = cur.lastrowid

    # Update invoice status
    db.execute(
        'UPDATE parsed_invoices SET status = ?, expense_id = ?, category = ? WHERE id = ?',
        ('approved', expense_id, category, invoice_id)
    )

    # Record price history for matched items
    items = json.loads(inv['items_json']) if inv['items_json'] else []
    price_alerts = []

    for item in items:
        if not item.get('name') or not item.get('unit_price'):
            continue
        mapping = db.execute(
            'SELECT ingredient_id FROM ingredient_mappings WHERE invoice_name = ?',
            (item['name'],)
        ).fetchone()
        if mapping and mapping['ingredient_id']:
            ing_id = mapping['ingredient_id']
            old_price_row = db.execute(
                'SELECT unit_price FROM ingredients WHERE id = ?', (ing_id,)
            ).fetchone()
            old_price = old_price_row['unit_price'] if old_price_row else 0
            new_price = item['unit_price']

            db.execute(
                'INSERT INTO ingredient_price_history (ingredient_id, price, invoice_id) VALUES (?, ?, ?)',
                (ing_id, new_price, invoice_id)
            )
            db.execute('UPDATE ingredients SET unit_price = ? WHERE id = ?', (new_price, ing_id))

            from app.gdrive_invoices import check_price_alerts
            alert = check_price_alerts(ing_id, new_price, old_price)
            if alert:
                price_alerts.append(alert)

    db.commit()
    db.close()

    if price_alerts:
        try:
            from app.telegram_bot import send_message
            send_message("⚠️ <b>Зміна цін</b>\n\n" + "\n".join(price_alerts))
        except Exception:
            pass

    return RedirectResponse(f'/expenses?month={month}', status_code=303)


@router.post('/expenses/skip-invoice/{invoice_id}')
async def skip_invoice(invoice_id: int):
    """Skip a parsed invoice."""
    db = get_db()
    inv = db.execute('SELECT month FROM parsed_invoices WHERE id = ?', (invoice_id,)).fetchone()
    month = inv['month'] if inv else ''
    db.execute('UPDATE parsed_invoices SET status = ? WHERE id = ?', ('skipped', invoice_id))
    db.commit()
    db.close()
    return RedirectResponse(f'/expenses?month={month}', status_code=303)


@router.post('/expenses/process-invoices')
async def process_all_invoices(month: str = Form('')):
    """Parse all unparsed invoices for the month."""
    from app.gdrive_invoices import list_invoices_for_month, download_file, parse_invoice_textract, classify_vendor

    current_month = month or datetime.now().strftime('%Y-%m')
    db = get_db()

    parsed_ids = set(
        r['file_id'] for r in db.execute('SELECT file_id FROM parsed_invoices').fetchall()
    )

    try:
        invoices = list_invoices_for_month(current_month)
    except Exception:
        db.close()
        return RedirectResponse(f'/expenses?month={current_month}', status_code=303)

    supported = ['application/pdf', 'image/jpeg', 'image/png', 'image/tiff']
    new_parsed = []
    errors = []

    for f in invoices:
        if f['id'] in parsed_ids or f['mimeType'] not in supported:
            continue
        try:
            file_bytes = download_file(f['id'])
            result = parse_invoice_textract(file_bytes)
            category, expense_name = classify_vendor(result.get('vendor', ''), f['name'])

            db.execute('''
                INSERT OR IGNORE INTO parsed_invoices
                (file_id, file_name, invoice_number, folder, month, vendor, category, total, items_json, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            ''', (f['id'], f['name'], result.get('invoice_number', ''),
                  f.get('path', ''), current_month,
                  expense_name or result.get('vendor', ''), category,
                  result.get('total', 0),
                  json.dumps(result.get('items', []), ensure_ascii=False)))
            new_parsed.append({'name': f['name'], 'vendor': expense_name, 'total': result.get('total', 0)})
        except Exception as e:
            errors.append(f"{f['name']}: {str(e)[:80]}")

    db.commit()
    db.close()

    try:
        from app.telegram_bot import send_message
        if new_parsed:
            msg = f"📄 <b>Фактури ({current_month})</b>\n\n"
            for p in new_parsed:
                msg += f"✅ {p['vendor']} — {p['total']:.0f} zł\n"
            if errors:
                msg += f"\n❌ Помилки: {len(errors)}\n"
            send_message(msg)
    except Exception:
        pass

    return RedirectResponse(f'/expenses?month={current_month}', status_code=303)
