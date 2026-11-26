"""Inventory routes — Склад: stock list, order list per supplier, deliveries, ingredient CRUD."""
import math
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.db import get_db
from app.gopos_api import warsaw_today
from app.services.inventory import get_all_ingredients, get_suppliers, get_ingredient_deps

templates = Jinja2Templates(directory=Path(__file__).parent.parent / 'templates')

router = APIRouter()

UNITS = ['g', 'kg', 'ml', 'L', 'szt', 'pcs']
TABS = ('stock', 'order', 'deliveries')
# Suggested order = refill up to this many times the minimum.
REFILL_FACTOR = 2
NO_SUPPLIER = 'Без постачальника'


def _is_admin(request: Request) -> bool:
    user = getattr(request.state, 'user', None)
    return bool(user and user['role'] == 'admin')


def parse_decimal(value, default=None):
    """'1,5' / '1.5' / ' 2 ' -> float; '' / None -> default. Raises ValueError on junk."""
    if value is None:
        return default
    s = str(value).strip().replace('\u00a0', '').replace(' ', '').replace(',', '.')
    if s == '':
        return default
    return float(s)


def _parse_int(value):
    s = str(value or '').strip()
    return int(s) if s.isdigit() else None


def is_low(item) -> bool:
    return (item['min_quantity'] or 0) > 0 and (item['quantity'] or 0) <= item['min_quantity']


def stock_status(item) -> str:
    """'out' (nothing left, but something is expected), 'low', or 'ok'."""
    q, m = item['quantity'] or 0, item['min_quantity'] or 0
    if m > 0 and q <= 0:
        return 'out'
    if is_low(item):
        return 'low'
    return 'ok'


def round_order(amount: float, unit: str) -> float:
    """Round a needed amount UP to a step a person would actually order."""
    if amount <= 0:
        return 0
    u = (unit or '').lower()
    if u in ('szt', 'pcs', 'шт'):
        step = 1 if amount < 10 else 5 if amount < 50 else 10 if amount < 200 else 50
    elif u in ('g', 'ml'):
        step = 50 if amount < 1000 else 100
    else:  # kg, L
        step = 0.1 if amount < 1 else 0.5 if amount < 10 else 1
    return round(math.ceil(round(amount / step, 6)) * step, 3)


def build_order_list(items) -> list[dict]:
    """Group items at/below their minimum by supplier.

    Each line: suggested = refill to REFILL_FACTOR × minimum, rounded; cost ≈ suggested ×
    current unit price (None when the price is unknown). Groups sorted by total, then name;
    the 'no supplier' group goes last.
    """
    groups: dict[str, dict] = {}
    for i in items:
        if not is_low(i):
            continue
        need = REFILL_FACTOR * i['min_quantity'] - (i['quantity'] or 0)
        suggested = round_order(need, i['unit'])
        price = i['unit_price'] or 0
        line = {
            'id': i['id'], 'name': i['name'], 'unit': i['unit'],
            'quantity': max(i['quantity'] or 0, 0), 'min_quantity': i['min_quantity'],
            'suggested': suggested,
            'cost': suggested * price if price > 0 else None,
            'level_pct': max(0, min(100, round((i['quantity'] or 0) / i['min_quantity'] * 100))),
        }
        name = i['supplier_name'] or NO_SUPPLIER
        g = groups.setdefault(name, {'supplier': name, 'lines': [], 'total': 0.0, 'unpriced': 0})
        g['lines'].append(line)
        if line['cost'] is None:
            g['unpriced'] += 1
        else:
            g['total'] += line['cost']
    for g in groups.values():
        g['lines'].sort(key=lambda ln: ln['level_pct'])
    return sorted(groups.values(), key=lambda g: (g['supplier'] == NO_SUPPLIER, -g['total'], g['supplier']))


def order_text(group: dict) -> str:
    """Plain-text order list for one supplier (pasted into a message to the supplier)."""
    from app.templating import qty
    lines = [group['supplier'] + ':']
    lines += [f"- {ln['name']}: {qty(ln['suggested'], ln['unit'])}" for ln in group['lines']]
    return '\n'.join(lines)


def _wastage_reasons():
    from app.routes.wastage import manual_reason_choices  # lazy: wastage imports this module
    return manual_reason_choices()


def low_stock_count() -> int:
    db = get_db()
    n = db.execute('SELECT COUNT(*) FROM ingredients WHERE min_quantity > 0 AND quantity <= min_quantity').fetchone()[0]
    db.close()
    return n


@router.get('/inventory', response_class=HTMLResponse)
async def inventory_page(request: Request, supplier: str = '', tab: str = '', f: str = '', saved: str = ''):
    is_admin = _is_admin(request)
    all_items = get_all_ingredients()
    suppliers = get_suppliers()
    low_items = [i for i in all_items if is_low(i)]
    if tab not in TABS:
        tab = 'order' if (low_items and is_admin) else 'stock'

    ctx = {
        'tab': tab, 'suppliers': suppliers, 'active_supplier': supplier, 'active_filter': f,
        'is_admin': is_admin, 'all_items': all_items, 'low_count': len(low_items),
        'units': UNITS, 'saved': saved,
        'wastage_reasons': _wastage_reasons(), 'now_date': warsaw_today(),
    }
    if tab == 'stock':
        items = all_items
        if supplier:
            items = [i for i in items if (i['supplier_name'] or '') == supplier]
        if f == 'low':
            items = [i for i in items if stock_status(i) != 'ok']
        ctx.update({
            'items': items,
            'status': {i['id']: stock_status(i) for i in all_items},
            'stock_value': sum(max(i['quantity'] or 0, 0) * (i['unit_price'] or 0) for i in all_items),
            'unpriced_count': sum(1 for i in all_items if not (i['unit_price'] or 0) and (i['quantity'] or 0) > 0),
            'supplier_counts': {s['name']: sum(1 for i in all_items if i['supplier_name'] == s['name']) for s in suppliers},
        })
    elif tab == 'order':
        groups = build_order_list(all_items)
        for g in groups:
            g['text'] = order_text(g)
        ctx.update({
            'groups': groups,
            'order_total': sum(g['total'] for g in groups),
            'order_lines': sum(len(g['lines']) for g in groups),
            'order_unpriced': sum(g['unpriced'] for g in groups),
            'order_text_all': '\n\n'.join(g['text'] for g in groups),
        })
    else:
        db = get_db()
        deliveries = db.execute('''
            SELECT d.id, d.date, d.quantity, d.price, d.note, i.name, i.unit, s.name AS supplier_name
            FROM deliveries d
            JOIN ingredients i ON d.ingredient_id = i.id
            LEFT JOIN suppliers s ON d.supplier_id = s.id
            WHERE d.date >= date(?, '-60 days')
            ORDER BY d.date DESC, d.id DESC
            LIMIT 200
        ''', (warsaw_today(),)).fetchall()
        db.close()
        ctx.update({
            'deliveries': deliveries,
            'deliveries_total': sum(d['price'] or 0 for d in deliveries),
        })
    return templates.TemplateResponse(request, 'inventory.html', context=ctx)


@router.post('/inventory/add')
async def add_ingredient(
    request: Request,
    name: str = Form(...), unit: str = Form('g'),
    quantity: str = Form('0'), min_quantity: str = Form('0'),
    unit_price: str = Form(''), supplier_id: str = Form(''),
):
    price = parse_decimal(unit_price, 0) if _is_admin(request) else 0
    db = get_db()
    db.execute(
        'INSERT OR IGNORE INTO ingredients (name, unit, quantity, min_quantity, unit_price, supplier_id) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (name.strip(), unit, parse_decimal(quantity, 0), parse_decimal(min_quantity, 0), price,
         _parse_int(supplier_id))
    )
    db.commit()
    db.close()
    return RedirectResponse('/inventory?tab=stock&saved=add', status_code=303)


@router.post('/inventory/delivery')
async def add_delivery(
    request: Request,
    ingredient_id: int = Form(...), quantity: str = Form(...),
    price: str = Form('0'), note: str = Form(''),
    supplier_id: str = Form(''), redirect: str = Form(''),
):
    price_v = parse_decimal(price, 0) if _is_admin(request) else 0  # chef/barista don't see or set prices
    qty_v = parse_decimal(quantity, 0)
    db = get_db()
    db.execute(
        'INSERT INTO deliveries (date, ingredient_id, quantity, price, note, supplier_id) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (warsaw_today(), ingredient_id, qty_v, price_v, note, _parse_int(supplier_id))
    )
    db.execute(
        'UPDATE ingredients SET quantity = quantity + ? WHERE id = ?',
        (qty_v, ingredient_id)
    )
    db.commit()
    db.close()
    target = redirect if redirect.startswith('/inventory') else '/inventory?tab=deliveries'
    sep = '&' if '?' in target else '?'
    return RedirectResponse(f'{target}{sep}saved=delivery', status_code=303)


@router.post('/inventory/supplier')
async def add_supplier(
    name: str = Form(...), contact: str = Form(''), note: str = Form(''),
):
    db = get_db()
    db.execute(
        'INSERT OR IGNORE INTO suppliers (name, contact, note) VALUES (?, ?, ?)',
        (name, contact, note)
    )
    db.commit()
    db.close()
    return RedirectResponse('/inventory', status_code=303)


@router.post('/ingredients/update/{ingredient_id}')
async def update_ingredient_unit(ingredient_id: int, unit: str = Form(...)):
    db = get_db()
    db.execute('UPDATE ingredients SET unit = ? WHERE id = ?', (unit, ingredient_id))
    db.commit()
    db.close()
    return RedirectResponse('/recipes', status_code=303)


@router.post('/ingredients/edit/{ingredient_id}')
async def edit_ingredient(
    request: Request,
    ingredient_id: int,
    name: str = Form(None), quantity: str = Form(None),
    unit: str = Form(None), min_quantity: str = Form(None),
    unit_price: str = Form(None), supplier_id: str = Form(None),
    redirect: str = Form('/inventory'),
):
    quantity_v = parse_decimal(quantity)
    min_v = parse_decimal(min_quantity)
    price_v = parse_decimal(unit_price) if _is_admin(request) else None  # only admin edits prices
    db = get_db()
    if name is not None and name.strip():
        db.execute('UPDATE ingredients SET name = ? WHERE id = ?', (name.strip(), ingredient_id))
    if quantity_v is not None:
        db.execute('UPDATE ingredients SET quantity = ? WHERE id = ?', (quantity_v, ingredient_id))
    if unit is not None and unit:
        db.execute('UPDATE ingredients SET unit = ? WHERE id = ?', (unit, ingredient_id))
    if min_v is not None:
        db.execute('UPDATE ingredients SET min_quantity = ? WHERE id = ?', (min_v, ingredient_id))
    if price_v is not None:
        db.execute('UPDATE ingredients SET unit_price = ? WHERE id = ?', (price_v, ingredient_id))
        # Recalculate any sub-recipes that use this ingredient
        from app.services.recipes import recalc_sub_recipes_for_ingredient
        recalc_sub_recipes_for_ingredient(db, ingredient_id)
    if supplier_id is not None:
        db.execute('UPDATE ingredients SET supplier_id = ? WHERE id = ?', (_parse_int(supplier_id), ingredient_id))
    db.commit()
    db.close()
    if not redirect.startswith('/') or redirect.startswith('//'):
        redirect = '/inventory'
    return RedirectResponse(redirect, status_code=303)


@router.post('/ingredients/delete/{ingredient_id}')
async def delete_ingredient(ingredient_id: int):
    db = get_db()
    # Remove all references
    db.execute('DELETE FROM sub_recipe_items WHERE ingredient_id = ?', (ingredient_id,))
    db.execute('DELETE FROM recipes WHERE ingredient_id = ?', (ingredient_id,))
    db.execute('DELETE FROM deliveries WHERE ingredient_id = ?', (ingredient_id,))
    db.execute('DELETE FROM inventory_deductions WHERE ingredient_id = ?', (ingredient_id,))
    db.execute('DELETE FROM stock_count_items WHERE ingredient_id = ?', (ingredient_id,))
    # Remove sub-recipe if this ingredient is one
    db.execute('DELETE FROM sub_recipe_items WHERE sub_recipe_id IN (SELECT id FROM sub_recipes WHERE ingredient_id = ?)', (ingredient_id,))
    db.execute('DELETE FROM sub_recipes WHERE ingredient_id = ?', (ingredient_id,))
    db.execute('DELETE FROM ingredients WHERE id = ?', (ingredient_id,))
    db.commit()
    db.close()
    return RedirectResponse('/inventory?tab=stock&saved=delete', status_code=303)


@router.get('/api/ingredient/{ingredient_id}/deps')
async def ingredient_deps_api(ingredient_id: int):
    return JSONResponse(get_ingredient_deps(ingredient_id))


@router.get('/api/ingredient/{ingredient_id}/price-history')
async def ingredient_price_history_api(request: Request, ingredient_id: int):
    """Last 20 price points for an ingredient (for sparkline chart). Admin only: prices."""
    if not _is_admin(request):
        return JSONResponse({'error': 'forbidden'}, status_code=403)
    db = get_db()
    rows = db.execute('''
        SELECT iph.price, iph.date, COALESCE(pi.vendor, pi.file_name, '') as source
        FROM ingredient_price_history iph
        LEFT JOIN parsed_invoices pi ON iph.invoice_id = pi.id
        WHERE iph.ingredient_id = ?
        ORDER BY iph.date DESC, iph.id DESC
        LIMIT 20
    ''', (ingredient_id,)).fetchall()
    current = db.execute('SELECT unit_price, name FROM ingredients WHERE id = ?', (ingredient_id,)).fetchone()
    db.close()
    return JSONResponse({
        'name': current['name'] if current else '',
        'current_price': current['unit_price'] if current else 0,
        'history': [{'price': r['price'], 'date': r['date'], 'source': r['source']} for r in reversed(rows)],
    })
