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

    # All expenses for this month grouped by name
    expenses_raw = db.execute('''
        SELECT * FROM expenses WHERE month = ? ORDER BY amount DESC
    ''', (current_month,)).fetchall()

    # Group by name (vendor) — sum amounts for same name
    grouped = {}
    for e in expenses_raw:
        key = e['name']
        if key not in grouped:
            grouped[key] = {
                'name': key,
                'category': e['category'],
                'amount': 0,
                'count': 0,
                'items': [],
            }
        grouped[key]['amount'] += e['amount']
        grouped[key]['count'] += 1
        grouped[key]['items'].append(dict(e))

    expenses = sorted(grouped.values(), key=lambda x: x['amount'], reverse=True)

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

    # Load parsed invoices from DB for this month
    parsed_ids = set()
    pending_invoices = []
    all_invoices = []
    pending_items_counts = {}
    expense_to_invoice = {}  # expense_id -> {file_id, file_name, ...}
    try:
        db2 = get_db()
        parsed_rows = db2.execute('SELECT file_id FROM parsed_invoices').fetchall()
        parsed_ids = set(r['file_id'] for r in parsed_rows)
        pending_invoices = db2.execute(
            "SELECT * FROM parsed_invoices WHERE month = ? AND expense_status = 'pending' AND parse_status = 'parsed' AND total > 0 ORDER BY vendor",
            (current_month,)
        ).fetchall()
        all_invoices = db2.execute(
            "SELECT * FROM parsed_invoices WHERE month = ? ORDER BY expense_status, vendor",
            (current_month,)
        ).fetchall()
        # Count pending items per invoice
        rows = db2.execute('''
            SELECT parsed_invoice_id, COUNT(*) as cnt
            FROM invoice_items_pending
            WHERE status = 'pending'
            GROUP BY parsed_invoice_id
        ''').fetchall()
        pending_items_counts = {r['parsed_invoice_id']: r['cnt'] for r in rows}
        # Build expense -> invoice map
        for inv in all_invoices:
            if inv['expense_id']:
                expense_to_invoice[inv['expense_id']] = {
                    'file_id': inv['file_id'],
                    'file_name': inv['file_name'],
                    'invoice_number': inv['invoice_number'],
                }
        db2.close()
    except Exception as e:
        log.warning(f"Could not load parsed invoices: {e}")

    # Attach invoice info to expense items in groups
    for group in expenses:
        for item in group['items']:
            inv = expense_to_invoice.get(item['id'])
            item['invoice'] = inv

    return templates.TemplateResponse(request, 'expenses.html', context={
        'expenses': expenses, 'by_category': by_category,
        'total': total, 'current_month': current_month,
        'months': months, 'categories': CATEGORIES,
        'revenue': revenue, 'net_profit': net_profit,
        'pending_invoices': pending_invoices,
        'all_invoices': all_invoices,
        'pending_items_counts': pending_items_counts,
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
            (file_id, file_name, invoice_number, vendor, category, total, items_json, parse_status, expense_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'parsed', 'pending')
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


@router.post('/expenses/llm-classify/{invoice_id}')
async def llm_classify_invoice(invoice_id: int):
    """Use LLM to classify invoice and map items to ingredients."""
    db = get_db()
    inv = db.execute('SELECT * FROM parsed_invoices WHERE id = ?', (invoice_id,)).fetchone()
    if not inv:
        db.close()
        return JSONResponse({'error': 'Invoice not found'}, status_code=404)

    items = json.loads(inv['items_json']) if inv['items_json'] else []

    # Get all ingredients
    ingredients = [dict(r) for r in db.execute('SELECT id, name, unit FROM ingredients ORDER BY name').fetchall()]

    # Get existing mappings
    existing = {r['invoice_name']: r['ingredient_id']
                for r in db.execute('SELECT invoice_name, ingredient_id FROM ingredient_mappings').fetchall()}

    try:
        from app.llm import classify_invoice, map_items_to_ingredients

        # Classify vendor
        classification = classify_invoice(inv['vendor'] or '', inv['file_name'] or '', items)

        # Map items
        mappings = map_items_to_ingredients(items, ingredients, existing)

        # Save pending items for review
        db.execute('DELETE FROM invoice_items_pending WHERE parsed_invoice_id = ?', (invoice_id,))
        for m in mappings:
            db.execute('''
                INSERT INTO invoice_items_pending
                (parsed_invoice_id, invoice_name, quantity, unit_price, status, ingredient_id, suggested_ingredient, confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (invoice_id, m['invoice_name'], m.get('quantity', 0), m.get('unit_price', 0),
                  m['action'], m.get('ingredient_id'), m.get('suggested_name', ''), m.get('confidence', 0)))

        # Update invoice with LLM classification
        db.execute(
            'UPDATE parsed_invoices SET category = ?, vendor = ? WHERE id = ?',
            (classification['category'], classification['expense_name'] or inv['vendor'], invoice_id)
        )
        db.commit()
        db.close()
        return JSONResponse({
            'classification': classification,
            'mappings': mappings,
        })
    except Exception as e:
        log.error(f"LLM classify error: {e}")
        db.close()
        return JSONResponse({'error': str(e)[:200]}, status_code=500)


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
        'UPDATE parsed_invoices SET expense_status = ?, expense_id = ?, category = ? WHERE id = ?',
        ('added', expense_id, category, invoice_id)
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
    db.execute("UPDATE parsed_invoices SET expense_status = 'skipped' WHERE id = ?", (invoice_id,))
    db.commit()
    db.close()
    return RedirectResponse(f'/expenses?month={month}', status_code=303)


@router.get('/expenses/scan-drive')
async def scan_drive(month: str = ''):
    """Scan Google Drive folder — save new files to DB (without Textract parsing)."""
    current_month = month or datetime.now().strftime('%Y-%m')
    try:
        from app.gdrive_invoices import list_invoices_for_month

        files = list_invoices_for_month(current_month)

        db = get_db()
        parsed_ids = set(
            r['file_id'] for r in db.execute('SELECT file_id FROM parsed_invoices').fetchall()
        )

        supported = ['application/pdf', 'image/jpeg', 'image/png', 'image/tiff']
        new_files = []
        for f in files:
            if f['id'] not in parsed_ids and f['mimeType'] in supported:
                # Save to DB with parse_status=pending (not yet parsed by Textract)
                db.execute('''
                    INSERT OR IGNORE INTO parsed_invoices
                    (file_id, file_name, folder, month, parse_status, expense_status)
                    VALUES (?, ?, ?, ?, 'pending', 'pending')
                ''', (f['id'], f['name'], f.get('path', ''), current_month))
                new_files.append(f['name'])

        if new_files:
            db.commit()
        db.close()

        return JSONResponse({
            'total': len(files),
            'new_count': len(new_files),
            'new_files': new_files[:20],
        })
    except Exception as e:
        return JSONResponse({'error': str(e)[:200]}, status_code=500)


@router.post('/expenses/process-invoices')
async def process_all_invoices(month: str = Form('')):
    """Start invoice processing in background — returns immediately."""
    current_month = month or datetime.now().strftime('%Y-%m')

    import threading
    def run_processing():
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            _process_invoices_sync(current_month)
        except Exception as e:
            log.error(f"Background processing failed: {e}")
        finally:
            loop.close()

    threading.Thread(target=run_processing, daemon=True).start()
    return RedirectResponse(f'/expenses?month={current_month}', status_code=303)


def _process_invoices_sync(current_month: str):
    """Sync processing of all pending invoices — delegates to service."""
    from app.services.invoice_processing import process_invoices_for_month
    process_invoices_for_month(current_month)


@router.post('/expenses/items/confirm/{item_id}')
async def confirm_item_mapping(item_id: int, action: str = Form(...), ingredient_id: Optional[int] = Form(None), new_name: Optional[str] = Form('')):
    """Confirm a single item mapping: match existing / create new / skip."""
    db = get_db()
    item = db.execute('SELECT * FROM invoice_items_pending WHERE id = ?', (item_id,)).fetchone()
    if not item:
        db.close()
        return JSONResponse({'error': 'Item not found'}, status_code=404)

    invoice_name = item['invoice_name']
    final_ingredient_id = None

    if action == 'match' and ingredient_id:
        final_ingredient_id = ingredient_id
        # Update price
        if item['unit_price'] > 0:
            old_row = db.execute('SELECT unit_price FROM ingredients WHERE id = ?', (ingredient_id,)).fetchone()
            old_price = old_row['unit_price'] if old_row else 0
            db.execute('UPDATE ingredients SET unit_price = ? WHERE id = ?', (item['unit_price'], ingredient_id))
            db.execute(
                'INSERT INTO ingredient_price_history (ingredient_id, price, invoice_id) VALUES (?, ?, ?)',
                (ingredient_id, item['unit_price'], item['parsed_invoice_id'])
            )
            # Price alert
            try:
                from app.gdrive_invoices import check_price_alerts
                from app.telegram_bot import send_message
                alert = check_price_alerts(ingredient_id, item['unit_price'], old_price)
                if alert:
                    send_message("⚠️ <b>Зміна ціни</b>\n\n" + alert)
            except Exception:
                pass

    elif action == 'new' and new_name:
        # Create new ingredient
        cur = db.execute(
            'INSERT INTO ingredients (name, unit, quantity, min_quantity, unit_price) VALUES (?, ?, 0, 0, ?)',
            (new_name, 'szt', item['unit_price'])
        )
        final_ingredient_id = cur.lastrowid

    # Save mapping
    db.execute(
        'INSERT OR REPLACE INTO ingredient_mappings (invoice_name, ingredient_id, action) VALUES (?, ?, ?)',
        (invoice_name, final_ingredient_id, action)
    )
    # Update pending item
    db.execute(
        'UPDATE invoice_items_pending SET status = ?, ingredient_id = ? WHERE id = ?',
        ('confirmed', final_ingredient_id, item_id)
    )
    db.commit()
    db.close()
    return JSONResponse({'ok': True})


@router.get('/expenses/items/{invoice_id}')
async def get_invoice_items(invoice_id: int):
    """Get pending items for an invoice."""
    db = get_db()
    items = db.execute('''
        SELECT ip.*, i.name as ingredient_name
        FROM invoice_items_pending ip
        LEFT JOIN ingredients i ON ip.ingredient_id = i.id
        WHERE ip.parsed_invoice_id = ?
        ORDER BY ip.status, ip.confidence DESC
    ''', (invoice_id,)).fetchall()
    db.close()
    return JSONResponse({'items': [dict(r) for r in items]})
