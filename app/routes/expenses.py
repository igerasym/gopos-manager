"""Expenses routes (admin only): the "Гроші" page, expenses CRUD, invoices, supplier history."""
import calendar
import csv
import io
import json
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from app.db import get_db
from app.gopos_api import warsaw_today

log = logging.getLogger(__name__)

templates = Jinja2Templates(directory=Path(__file__).parent.parent / 'templates')

router = APIRouter()

CATEGORIES = ['Оренда', 'Зарплати', 'Бухгалтерія', 'Комунальні', 'Побут', 'Податки і ZUS', 'Логістика', 'Продукти', 'Інше']

# Food purchases (invoices). The month result subtracts the cost of what was SOLD
# (recipes x sales), so purchases are shown apart and not subtracted a second time.
FOOD_CATEGORY = 'Продукти'
CHART_SLOTS = 4   # categories with their own colour in the stacked bar; the rest share one

MONTHS_NOM = ['січень', 'лютий', 'березень', 'квітень', 'травень', 'червень',
              'липень', 'серпень', 'вересень', 'жовтень', 'листопад', 'грудень']
MONTHS_GEN = ['січня', 'лютого', 'березня', 'квітня', 'травня', 'червня',
              'липня', 'серпня', 'вересня', 'жовтня', 'листопада', 'грудня']
MONTHS_LOC = ['січні', 'лютому', 'березні', 'квітні', 'травні', 'червні',
              'липні', 'серпні', 'вересні', 'жовтні', 'листопаді', 'грудні']

DONE_MESSAGES = {
    'added': 'Витрату додано',
    'updated': 'Збережено',
    'deleted': 'Витрату видалено',
    'copied': 'Постійні витрати скопійовано',
    'nothing': 'Немає чого копіювати',
    'dismissed': 'Добре, цього місяця не рахуємо',
    'approved': 'Фактуру додано у витрати',
    'skipped': 'Фактуру пропущено',
    'manual': 'Фактуру внесено вручну',
}


def _dec(v) -> float:
    """'12,50' / '12.50' / '1 200' -> float; empty or bad -> 0.0."""
    if v is None:
        return 0.0
    s = str(v).strip().replace('\u202f', '').replace('\xa0', '').replace(' ', '').replace(',', '.')
    try:
        return float(s) if s else 0.0
    except ValueError:
        return 0.0


def _valid_month(m: str) -> bool:
    try:
        y, mo = int(m[:4]), int(m[5:7])
        return len(m) == 7 and m[4] == '-' and 1 <= mo <= 12 and 2000 <= y <= 2100
    except (ValueError, TypeError):
        return False


def shift_month(m: str, delta: int) -> str:
    y, mo = int(m[:4]), int(m[5:7]) + delta
    while mo < 1:
        y, mo = y - 1, mo + 12
    while mo > 12:
        y, mo = y + 1, mo - 12
    return f'{y}-{mo:02d}'


def month_label(m: str, case: str = 'nom', with_year: bool | None = None) -> str:
    """'2026-11' -> 'листопад' (nom) / 'листопада' (gen) / 'листопаді' (loc); year added when not current."""
    names = {'nom': MONTHS_NOM, 'gen': MONTHS_GEN, 'loc': MONTHS_LOC}[case]
    y, mo = int(m[:4]), int(m[5:7])
    s = names[mo - 1]
    if with_year is None:
        with_year = y != int(warsaw_today()[:4])
    return f'{s} {y}' if with_year else s


def _month_bounds(m: str) -> tuple[str, str]:
    y, mo = int(m[:4]), int(m[5:7])
    return f'{m}-01', f'{m}-{calendar.monthrange(y, mo)[1]:02d}'


def _redirect(month: str = '', done: str = '', anchor: str = '') -> RedirectResponse:
    q = []
    if month:
        q.append(f'month={month}')
    if done:
        q.append(f'done={done}')
    url = '/expenses' + ('?' + '&'.join(q) if q else '') + (f'#{anchor}' if anchor else '')
    return RedirectResponse(url, status_code=303)


def _clear_badges():
    try:
        from app.services import attention
        attention.clear_cache()
    except Exception:   # badge refresh is cosmetic
        pass


# ── Month result: revenue − all expenses (product purchases + other costs) ──
# Profit is the cash view, not revenue − recipe cost: recipes cover only part of the menu
# (the rest would silently cost 0), while invoices hold what was really paid.
# The recipe-based cost of sold goods stays as an informative number.

def month_pnl(db, d_from: str, d_to: str) -> dict:
    """Sales side of the month result (shares the dashboard calculation)."""
    from app.routes.dashboard import compute_pnl
    return compute_pnl(db, d_from, d_to)


def missing_recurring(db, month: str) -> list[dict]:
    """Recurring costs of the previous month that are not entered (nor dismissed) for `month`.
    Same rule as the 'costs_incomplete' attention row."""
    from app.services.attention import missing_recurring_costs
    return missing_recurring_costs(db, month, shift_month(month, -1))


def month_result(db, month: str, today: str) -> dict:
    """Numbers for the 'Результат місяця' card: the month so far (or the whole past month),
    the forecast with last month's recurring costs, and the same days of the previous month."""
    m_from, m_to = _month_bounds(month)
    is_current = month == today[:7]
    is_future = m_from > today
    d_to = min(m_to, today) if is_current else m_to
    pnl = month_pnl(db, m_from, d_to)
    revenue = pnl['totals']['revenue']
    cogs = pnl['total_cogs']

    costs = db.execute('SELECT category, SUM(amount) AS total FROM expenses WHERE month = ? GROUP BY category',
                       (month,)).fetchall()
    food_purchases = sum(r['total'] for r in costs if r['category'] == FOOD_CATEGORY)
    other = sum(r['total'] for r in costs if r['category'] != FOOD_CATEGORY)
    profit = revenue - food_purchases - other

    missing = missing_recurring(db, month)
    missing_total = round(sum(r['amount'] for r in missing), 2)
    forecast = profit - missing_total

    # previous month: the same number of days for sales, the whole month for costs
    prev = shift_month(month, -1)
    p_from, p_end = _month_bounds(prev)
    days = int(d_to[8:10])
    p_to = min(p_end, f'{prev}-{days:02d}')
    prev_pnl = month_pnl(db, p_from, p_to)
    prev_full = prev_pnl if p_to == p_end else month_pnl(db, p_from, p_end)
    prev_costs = db.execute('SELECT COALESCE(SUM(amount),0) AS t FROM expenses WHERE month = ?',
                            (prev,)).fetchone()['t']
    prev_profit_full = prev_full['totals']['revenue'] - prev_costs

    cov = pnl['recipe_coverage']
    return {
        'from': m_from, 'to': d_to, 'is_current': is_current, 'is_future': is_future,
        'revenue': revenue, 'discounts': pnl['totals']['discounts'],
        'net_revenue': pnl['totals']['net_revenue'],
        'cogs': cogs, 'gross_profit': revenue - cogs,
        'gross_margin': (revenue - cogs) / revenue * 100 if revenue else 0,
        'food_share': cogs / revenue * 100 if revenue else 0,
        'other': other, 'food_purchases': food_purchases, 'profit': profit,
        'per100': profit / revenue * 100 if revenue else None,
        'missing': missing, 'missing_total': missing_total,
        'missing_categories': list(dict.fromkeys(r['category'] or r['name'] for r in missing)),
        'forecast': forecast, 'forecast_per100': forecast / revenue * 100 if revenue else None,
        'coverage_incomplete': bool(cov.get('without_recipe')) and revenue > 0,
        'uncovered_pct': cov.get('uncovered_pct', 0),
        'without_recipe': cov.get('without_recipe', 0),
        'prev': prev, 'has_prev': bool(prev_full['totals']['revenue'] or prev_costs),
        'prev_days': days, 'prev_partial': p_to != p_end,
        'prev_revenue': prev_pnl['totals']['revenue'],
        'prev_food_share': prev_pnl['avg_food_cost'],
        'prev_profit_full': prev_profit_full,
    }


def invoice_state(inv) -> str:
    """One status per invoice: failed | processing | new | waiting | added | skipped | duplicate."""
    ps, es = inv['parse_status'], inv['expense_status']
    if es == 'added':
        return 'added'
    if es == 'skipped':
        return 'skipped'
    if ps == 'duplicate':
        return 'duplicate'
    if ps == 'error' or (ps == 'parsed' and not (inv['total'] or 0) > 0):
        return 'failed'
    if ps == 'processing':
        return 'processing'
    if ps == 'parsed':
        return 'waiting'
    return 'new'


INVOICE_ORDER = {'failed': 0, 'waiting': 1, 'processing': 2, 'new': 3, 'added': 4, 'skipped': 5, 'duplicate': 6}


@router.get('/expenses', response_class=HTMLResponse)
async def expenses_page(request: Request, month: str = '', done: str = ''):
    today = warsaw_today()
    current_month = month if _valid_month(month) else today[:7]
    db = get_db()

    expenses_raw = [dict(r) for r in db.execute(
        'SELECT * FROM expenses WHERE month = ? ORDER BY amount DESC', (current_month,)).fetchall()]

    result = month_result(db, current_month, today)

    # invoices of the month, one list
    all_invoices = []
    expense_to_invoice = {}
    try:
        all_invoices = [dict(r) for r in db.execute(
            'SELECT * FROM parsed_invoices WHERE month = ? ORDER BY vendor', (current_month,)).fetchall()]
        pending_items_counts = {r['parsed_invoice_id']: r['cnt'] for r in db.execute('''
            SELECT parsed_invoice_id, COUNT(*) as cnt FROM invoice_items_pending
            WHERE status = 'pending' GROUP BY parsed_invoice_id''').fetchall()}
    except Exception as e:
        log.warning(f"Could not load parsed invoices: {e}")
        pending_items_counts = {}
    for inv in all_invoices:
        inv['state'] = invoice_state(inv)
        inv['pending_items'] = pending_items_counts.get(inv['id'], 0)
        if inv['expense_id']:
            expense_to_invoice[inv['expense_id']] = inv
    all_invoices.sort(key=lambda i: (INVOICE_ORDER[i['state']], -(i['total'] or 0)))
    waiting = [i for i in all_invoices if i['state'] == 'waiting']

    db.close()

    # expenses per category: other costs in the stacked bar, food purchases apart
    by_cat = {}
    for e in expenses_raw:
        inv = expense_to_invoice.get(e['id'])
        e['invoice_file_id'] = inv['file_id'] if inv else ''
        cat = e['category'] or 'Інше'
        c = by_cat.setdefault(cat, {'category': cat, 'total': 0, 'items': []})
        c['total'] += e['amount']
        c['items'].append(e)
    other_cats = sorted((c for c in by_cat.values() if c['category'] != FOOD_CATEGORY),
                        key=lambda c: c['total'], reverse=True)
    other_total = sum(c['total'] for c in other_cats)
    for i, c in enumerate(other_cats):
        c['slot'] = min(i, CHART_SLOTS) + 1
        c['pct'] = c['total'] / other_total * 100 if other_total else 0
    bar = [{'slot': c['slot'], 'label': c['category'], 'pct': c['pct']} for c in other_cats[:CHART_SLOTS]]
    rest = other_cats[CHART_SLOTS:]
    if rest:
        bar.append({'slot': CHART_SLOTS + 1, 'label': 'Решта', 'pct': sum(c['pct'] for c in rest)})

    prev_m = shift_month(current_month, -1)
    return templates.TemplateResponse(request, 'expenses.html', context={
        'current_month': current_month, 'today': today,
        'month_nom': month_label(current_month), 'month_gen': month_label(current_month, 'gen'),
        'prev_month': prev_m, 'next_month': shift_month(current_month, 1),
        'prev_nom': month_label(prev_m), 'prev_loc': month_label(prev_m, 'loc'),
        'prev_gen': month_label(prev_m, 'gen'),
        'next_nom': month_label(shift_month(current_month, 1)),
        'has_next': current_month < today[:7],
        'r': result, 'categories': CATEGORIES,
        'other_cats': other_cats, 'other_total': other_total, 'bar': bar,
        'food': by_cat.get(FOOD_CATEGORY),
        'has_expenses': bool(expenses_raw),
        'all_invoices': all_invoices, 'waiting': waiting,
        'waiting_total': sum(i['total'] or 0 for i in waiting),
        'has_processing': any(i['state'] == 'processing' for i in all_invoices),
        'done_message': DONE_MESSAGES.get(done, ''),
    })


@router.post('/expenses/add')
async def add_expense(
    name: str = Form(...), category: str = Form('Інше'),
    amount: Optional[str] = Form(''), month: str = Form(''),
    note: str = Form(''), recurring: str = Form(''),
):
    month = month if _valid_month(month) else warsaw_today()[:7]
    db = get_db()
    db.execute(
        'INSERT INTO expenses (name, category, amount, month, recurring, note) VALUES (?, ?, ?, ?, ?, ?)',
        (name.strip(), category, _dec(amount), month, 1 if recurring else 0, note)
    )
    db.commit()
    db.close()
    _clear_badges()
    return _redirect(month, 'added')


@router.post('/expenses/copy-recurring')
async def copy_recurring(month: str = Form('')):
    """Enter every recurring cost still missing this month with last month's amount (idempotent)."""
    month = month if _valid_month(month) else warsaw_today()[:7]
    db = get_db()
    missing = missing_recurring(db, month)
    note = f'як у {month_label(shift_month(month, -1), "loc")}'
    for r in missing:
        db.execute(
            'INSERT INTO expenses (name, category, amount, month, recurring, note) VALUES (?, ?, ?, ?, 1, ?)',
            (r['name'], r['category'], r['amount'], month, note))
    db.commit()
    db.close()
    _clear_badges()
    return _redirect(month, 'copied' if missing else 'nothing')


@router.post('/expenses/dismiss-missing')
async def dismiss_missing(name: str = Form(...), month: str = Form('')):
    """'Цього місяця не буде': a recurring cost stops counting as missing for this month."""
    month = month if _valid_month(month) else warsaw_today()[:7]
    db = get_db()
    db.execute('INSERT OR IGNORE INTO expenses_dismissed (name, month) VALUES (?, ?)', (name, month))
    db.commit()
    db.close()
    _clear_badges()
    return _redirect(month, 'dismissed')


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
        updates.append('amount = ?')
        params.append(_dec(amount))
    if name and name.strip():
        updates.append('name = ?')
        params.append(name.strip())
    if category:
        updates.append('category = ?')
        params.append(category)

    if updates:
        params.append(expense_id)
        db.execute(f'UPDATE expenses SET {", ".join(updates)} WHERE id = ?', params)
        db.commit()
    db.close()
    _clear_badges()
    return _redirect(m, 'updated')


@router.post('/expenses/delete/{expense_id}')
async def delete_expense(expense_id: int):
    db = get_db()
    row = db.execute('SELECT month FROM expenses WHERE id = ?', (expense_id,)).fetchone()
    m = row['month'] if row else ''
    db.execute('DELETE FROM expenses WHERE id = ?', (expense_id,))
    db.commit()
    db.close()
    _clear_badges()
    return _redirect(m, 'deleted')


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

    month = inv['month'] or warsaw_today()[:7]
    vendor = inv['vendor'] or inv['file_name']
    amount = inv['total']

    # Claim the invoice first: a double click / repeated POST must not add a
    # second expense. Only the request that flips 'pending' proceeds.
    claimed = db.execute(
        "UPDATE parsed_invoices SET expense_status = 'adding' WHERE id = ? AND expense_status = 'pending'",
        (invoice_id,)).rowcount
    if not claimed:
        db.close()
        return RedirectResponse('/expenses', status_code=303)

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
            # Recalculate any sub-recipes (напівфабрикати) that use this ingredient
            from app.services.recipes import recalc_sub_recipes_for_ingredient
            recalc_sub_recipes_for_ingredient(db, ing_id)

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

    _clear_badges()
    return _redirect(month, 'approved', 'invoices')


@router.post('/expenses/skip-invoice/{invoice_id}')
async def skip_invoice(invoice_id: int):
    """Skip a parsed invoice."""
    db = get_db()
    inv = db.execute('SELECT month FROM parsed_invoices WHERE id = ?', (invoice_id,)).fetchone()
    month = inv['month'] if inv else ''
    db.execute("UPDATE parsed_invoices SET expense_status = 'skipped' WHERE id = ?", (invoice_id,))
    db.commit()
    db.close()
    _clear_badges()
    return _redirect(month, 'skipped', 'invoices')


@router.post('/expenses/manual-invoice/{invoice_id}')
async def manual_invoice(invoice_id: int, vendor: str = Form(...), total: str = Form(...),
                         category: str = Form('Продукти')):
    """'Ввести вручну' for an invoice that was not recognised: owner types the supplier and the
    sum, then it is added to expenses exactly like an approved one."""
    amount = _dec(total)
    db = get_db()
    inv = db.execute('SELECT month, expense_status FROM parsed_invoices WHERE id = ?', (invoice_id,)).fetchone()
    if not inv or inv['expense_status'] != 'pending' or amount <= 0 or not vendor.strip():
        db.close()
        return _redirect(inv['month'] if inv else '', anchor='invoices')
    db.execute("UPDATE parsed_invoices SET vendor = ?, total = ?, parse_status = 'parsed' WHERE id = ?",
               (vendor.strip(), amount, invoice_id))
    db.commit()
    db.close()
    await approve_invoice(invoice_id, category)
    return _redirect(inv['month'], 'manual', 'invoices')


@router.get('/expenses/scan-drive')
async def scan_drive(month: str = ''):
    """Scan Google Drive folder — save new files to DB (without Textract parsing)."""
    current_month = month or warsaw_today()[:7]
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
    current_month = month or warsaw_today()[:7]

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
            # Recalculate any sub-recipes (напівфабрикати) that use this ingredient
            from app.services.recipes import recalc_sub_recipes_for_ingredient
            recalc_sub_recipes_for_ingredient(db, ingredient_id)
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
    """Get pending items for an invoice + ingredients list for autocomplete."""
    db = get_db()
    items = db.execute('''
        SELECT ip.*, i.name as ingredient_name
        FROM invoice_items_pending ip
        LEFT JOIN ingredients i ON ip.ingredient_id = i.id
        WHERE ip.parsed_invoice_id = ?
        ORDER BY ip.status, ip.confidence DESC
    ''', (invoice_id,)).fetchall()
    # Ingredients for autocomplete
    ingredients = db.execute('SELECT id, name FROM ingredients ORDER BY name').fetchall()
    db.close()
    return JSONResponse({
        'items': [dict(r) for r in items],
        'ingredients': [{'id': r['id'], 'name': r['name']} for r in ingredients],
    })


@router.get('/export/expenses.csv')
async def export_expenses_csv(month: str = ''):
    """Download expenses as CSV for the given month."""
    current_month = month or warsaw_today()[:7]
    db = get_db()
    rows = db.execute(
        'SELECT name, category, amount, month, note FROM expenses WHERE month = ? ORDER BY category, amount DESC',
        (current_month,)
    ).fetchall()
    db.close()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['Місяць', 'Категорія', 'Назва', 'Сума (zł)', 'Примітка'])
    for r in rows:
        w.writerow([r['month'], r['category'], r['name'], round(r['amount'], 2), r['note'] or ''])
    buf.seek(0)
    fname = f'expenses_{current_month}.csv'
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type='text/csv; charset=utf-8-sig',
        headers={'Content-Disposition': f'attachment; filename="{fname}"'},
    )


@router.get('/suppliers/history', response_class=HTMLResponse)
async def supplier_history(request: Request, vendor: str = ''):
    """Invoice history grouped by vendor with totals and trend."""
    db = get_db()

    # All approved invoices grouped by vendor
    vendors = db.execute('''
        SELECT vendor, COUNT(*) as count,
               SUM(total) as total_sum,
               MIN(month) as first_month,
               MAX(month) as last_month
        FROM parsed_invoices
        WHERE expense_status = 'added' AND vendor != ''
        GROUP BY vendor ORDER BY total_sum DESC
    ''').fetchall()

    invoices = []
    if vendor:
        invoices = db.execute('''
            SELECT pi.id, pi.file_name, pi.invoice_number, pi.month, pi.total,
                   pi.category, pi.vendor, pi.file_id,
                   e.amount as expense_amount
            FROM parsed_invoices pi
            LEFT JOIN expenses e ON pi.expense_id = e.id
            WHERE pi.expense_status = 'added' AND pi.vendor = ?
            ORDER BY pi.month DESC, pi.total DESC
        ''', (vendor,)).fetchall()

    db.close()

    def span(v):
        a, b = v['first_month'], v['last_month']
        if not (a and _valid_month(a)):
            return ''
        if a == b or not (b and _valid_month(b)):
            return month_label(a)
        return f'{month_label(a)} – {month_label(b)}'

    vendor_rows = [{**dict(v), 'span': span(v)} for v in vendors]
    by_month = []
    for inv in invoices:
        if not by_month or by_month[-1]['month'] != inv['month']:
            label = month_label(inv['month']) if _valid_month(inv['month'] or '') else (inv['month'] or '—')
            by_month.append({'month': inv['month'], 'label': label, 'total': 0, 'items': []})
        by_month[-1]['total'] += inv['total'] or 0
        by_month[-1]['items'].append(inv)

    return templates.TemplateResponse(request, 'supplier_history.html', context={
        'vendors': vendor_rows, 'selected_vendor': vendor, 'invoices': invoices, 'by_month': by_month,
        'vendors_total': sum(v['total_sum'] or 0 for v in vendors),
    })
