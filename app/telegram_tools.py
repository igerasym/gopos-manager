"""Read-only data tools for the Telegram chat bot.

Every tool only SELECTs, caps its rows and returns a small JSON-able dict. Cost, profit and
expense figures are `finance` tools: only chats listed as finance chats can use them (the web
app shows prices and costs to admins only, the bot keeps the same line).

Product names, ingredient names and the like come from POS and vendor invoices, so they are
untrusted text; the chat module tells the model to treat tool output as data.
"""
import re
from datetime import date, timedelta

from app.db import get_db

MAX_DAYS = 400          # longest range a tool will aggregate
MAX_DAILY_ROWS = 62     # day-by-day output cap
MAX_PRODUCTS = 15
WEEKDAYS_UA = ['понеділок', 'вівторок', 'середа', 'четвер', "п'ятниця", 'субота', 'неділя']

_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_MONTH_RE = re.compile(r'^\d{4}-\d{2}$')


def _day(value, name='date') -> str:
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise ValueError(f'{name} must be YYYY-MM-DD')
    date.fromisoformat(value)  # rejects 2026-02-31
    return value


def _range(args: dict, today: str) -> tuple:
    d_from = _day(args.get('date_from') or today, 'date_from')
    d_to = _day(args.get('date_to') or d_from, 'date_to')
    if d_from > d_to:
        raise ValueError('date_from is after date_to')
    if (date.fromisoformat(d_to) - date.fromisoformat(d_from)).days >= MAX_DAYS:
        raise ValueError(f'range is longer than {MAX_DAYS} days')
    return d_from, d_to


def _month(args: dict, today: str) -> str:
    m = args.get('month') or today[:7]
    if not isinstance(m, str) or not _MONTH_RE.match(m) or not 1 <= int(m[5:]) <= 12:
        raise ValueError('month must be YYYY-MM')
    return m


def _limit(args: dict, default: int, cap: int) -> int:
    try:
        n = int(args.get('limit', default))
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, cap))


def _r(x, nd=2):
    return round(float(x or 0), nd)


def _freshness(d_to: str, today: str) -> dict:
    """Extra fields when a range reaches the running business day: its figures are partial."""
    if d_to < today:
        return {}
    from app import gopos_api
    return {
        'includes_today_still_running': True,
        'today_last_refreshed_warsaw': gopos_api.LAST_INTRADAY_SYNC,
        'note': 'Today is partial: sales are refreshed from the POS about every 30 minutes '
                '(07:15-22:45 Warsaw) and the full sync runs in the evening. Say the figures are '
                'as of the refresh time, and do not treat them as the day total.',
    }


# ── tools ──

def sales_summary(args: dict, today: str) -> dict:
    d_from, d_to = _range(args, today)
    db = get_db()
    try:
        r = db.execute('''
            SELECT COALESCE(SUM(total_money),0) revenue, COALESCE(SUM(net_total),0) net,
                   COALESCE(SUM(discount),0) discounts, COALESCE(SUM(quantity),0) items,
                   COUNT(DISTINCT date) days
            FROM sales WHERE date BETWEEN ? AND ?''', (d_from, d_to)).fetchone()
    finally:
        db.close()
    return {
        'date_from': d_from, 'date_to': d_to,
        'revenue_gross_zl': _r(r['revenue']), 'revenue_net_of_vat_zl': _r(r['net']),
        'discounts_zl': _r(r['discounts']), 'items_sold': _r(r['items'], 0),
        'days_with_sales': r['days'],
        'avg_revenue_per_sales_day_zl': _r(r['revenue'] / r['days']) if r['days'] else None,
        **_freshness(d_to, today),
    }


def daily_revenue(args: dict, today: str) -> dict:
    d_from, d_to = _range(args, today)
    db = get_db()
    try:
        rows = db.execute('''
            SELECT date, SUM(total_money) revenue, SUM(quantity) items
            FROM sales WHERE date BETWEEN ? AND ? GROUP BY date ORDER BY date''',
                          (d_from, d_to)).fetchall()
    finally:
        db.close()
    truncated = len(rows) > MAX_DAILY_ROWS
    rows = rows[-MAX_DAILY_ROWS:]
    return {
        'days': [{'date': r['date'], 'weekday': WEEKDAYS_UA[date.fromisoformat(r['date']).weekday()],
                  'revenue_zl': _r(r['revenue']), 'items': _r(r['items'], 0)} for r in rows],
        'truncated_to_last_days': MAX_DAILY_ROWS if truncated else None,
        **_freshness(d_to, today),
    }


def weekday_averages(args: dict, today: str) -> dict:
    d_from, d_to = _range(args, today)
    db = get_db()
    try:
        rows = db.execute('''
            SELECT date, SUM(total_money) revenue FROM sales
            WHERE date BETWEEN ? AND ? GROUP BY date''', (d_from, d_to)).fetchall()
    finally:
        db.close()
    buckets = {i: [] for i in range(7)}
    for r in rows:
        buckets[date.fromisoformat(r['date']).weekday()].append(r['revenue'] or 0)
    return {'date_from': d_from, 'date_to': d_to, 'weekdays': [
        {'weekday': WEEKDAYS_UA[i], 'days': len(v), 'avg_revenue_zl': _r(sum(v) / len(v)) if v else None}
        for i, v in buckets.items()]}


def top_products(args: dict, today: str) -> dict:
    d_from, d_to = _range(args, today)
    by = args.get('by', 'revenue')
    if by not in ('revenue', 'quantity'):
        raise ValueError("by must be 'revenue' or 'quantity'")
    order = 'SUM(total_money)' if by == 'revenue' else 'SUM(quantity)'
    n = _limit(args, 7, MAX_PRODUCTS)
    db = get_db()
    try:
        rows = db.execute(f'''
            SELECT product_name, SUM(quantity) qty, SUM(total_money) revenue
            FROM sales WHERE date BETWEEN ? AND ?
            GROUP BY product_name ORDER BY {order} DESC LIMIT ?''', (d_from, d_to, n)).fetchall()
    finally:
        db.close()
    return {'date_from': d_from, 'date_to': d_to, 'ranked_by': by, 'products': [
        {'product': r['product_name'], 'quantity': _r(r['qty'], 0), 'revenue_zl': _r(r['revenue'])}
        for r in rows], **_freshness(d_to, today)}


def product_sales(args: dict, today: str) -> dict:
    d_from, d_to = _range(args, today)
    name = str(args.get('product') or '').strip()
    if not name:
        raise ValueError('product is required')
    db = get_db()
    try:
        rows = db.execute('''
            SELECT product_name, SUM(quantity) qty, SUM(total_money) revenue, SUM(discount) discount
            FROM sales WHERE date BETWEEN ? AND ? AND instr(lower(product_name), lower(?)) > 0
            GROUP BY product_name ORDER BY SUM(total_money) DESC LIMIT 10''',
                          (d_from, d_to, name)).fetchall()
    finally:
        db.close()
    return {'date_from': d_from, 'date_to': d_to, 'matches': [
        {'product': r['product_name'], 'quantity': _r(r['qty'], 0), 'revenue_zl': _r(r['revenue']),
         'discount_zl': _r(r['discount'])} for r in rows], **_freshness(d_to, today)}


def low_stock(args: dict, today: str) -> dict:
    db = get_db()
    try:
        rows = db.execute('''
            SELECT i.name, i.quantity, i.unit, i.min_quantity, s.name AS supplier
            FROM ingredients i LEFT JOIN suppliers s ON i.supplier_id = s.id
            WHERE i.min_quantity > 0 AND i.quantity <= i.min_quantity
            ORDER BY s.name, i.name LIMIT 60''').fetchall()
    finally:
        db.close()
    return {'items_below_minimum': [
        {'ingredient': r['name'], 'on_hand': _r(r['quantity'], 3), 'unit': r['unit'],
         'minimum': _r(r['min_quantity'], 3), 'supplier': r['supplier']} for r in rows]}


def stock_item(args: dict, today: str) -> dict:
    name = str(args.get('ingredient') or '').strip()
    if not name:
        raise ValueError('ingredient is required')
    db = get_db()
    try:
        rows = db.execute('''
            SELECT name, quantity, unit, min_quantity FROM ingredients
            WHERE instr(lower(name), lower(?)) > 0 ORDER BY name LIMIT 10''', (name,)).fetchall()
    finally:
        db.close()
    return {'matches': [{'ingredient': r['name'], 'on_hand': _r(r['quantity'], 3), 'unit': r['unit'],
                         'minimum': _r(r['min_quantity'], 3)} for r in rows]}


def wastage_summary(args: dict, today: str, finance: bool = False) -> dict:
    month = _month(args, today)
    from app.services.recipes import get_cost_lookup
    db = get_db()
    try:
        y, m = int(month[:4]), int(month[5:])
        end = f'{y + (m == 12)}-{1 if m == 12 else m + 1:02d}-01'
        rows = db.execute('''
            SELECT w.quantity, w.reason, w.source,
                   COALESCE(i.name, w.product_name, '—') AS label,
                   COALESCE(i.unit, '—') AS unit,
                   w.quantity * COALESCE(i.unit_price, 0) AS cost
            FROM wastage w LEFT JOIN ingredients i ON w.ingredient_id = i.id
            WHERE w.date >= ? AND w.date < ?''', (month + '-01', end)).fetchall()
    finally:
        db.close()
    # Same pricing as the /wastage page: a GoPos write-off is a product, priced by its recipe.
    product_cost = get_cost_lookup()
    priced = []
    for r in rows:
        d = dict(r)
        if d['source'] == 'gopos':
            d['cost'] = d['quantity'] * (product_cost.get(d['label'], 0) or 0)
            d['unit'] = 'шт'
        priced.append(d)
    rows = priced
    by_reason, by_item = {}, {}
    for r in rows:
        br = by_reason.setdefault(r['reason'], {'records': 0, 'cost': 0.0})
        br['records'] += 1
        br['cost'] += r['cost']
        bi = by_item.setdefault(r['label'], {'quantity': 0.0, 'unit': r['unit'], 'cost': 0.0})
        bi['quantity'] += r['quantity']
        bi['cost'] += r['cost']
    top = sorted(by_item.items(), key=lambda kv: (-kv[1]['cost'], -kv[1]['quantity']))[:10]
    out = {
        'month': month, 'records': len(rows),
        'by_reason': [{'reason': k, 'records': v['records'],
                       **({'cost_zl': _r(v['cost'])} if finance else {})} for k, v in by_reason.items()],
        'top_items': [{'item': k, 'quantity': _r(v['quantity']), 'unit': v['unit'],
                       **({'cost_zl': _r(v['cost'])} if finance else {})} for k, v in top],
    }
    if finance:
        out['total_cost_zl'] = _r(sum(r['cost'] for r in rows))
        out['note'] = 'cost counts only items with a known recipe or ingredient price'
    return out


def expenses_summary(args: dict, today: str) -> dict:
    month = _month(args, today)
    db = get_db()
    try:
        cats = db.execute('''
            SELECT category, SUM(amount) total FROM expenses WHERE month = ?
            GROUP BY category ORDER BY total DESC''', (month,)).fetchall()
        rev = db.execute('SELECT COALESCE(SUM(total_money),0) FROM sales WHERE substr(date,1,7) = ?',
                         (month,)).fetchone()[0]
    finally:
        db.close()
    total = sum(c['total'] for c in cats)
    return {'month': month, 'revenue_gross_zl': _r(rev), 'expenses_total_zl': _r(total),
            'by_category': [{'category': c['category'], 'amount_zl': _r(c['total'])} for c in cats],
            'note': 'expenses come from approved invoices and manual entries; a month still in '
                    'progress may be incomplete'}


def food_cost(args: dict, today: str) -> dict:
    d_from, d_to = _range(args, today)
    from app.services.recipes import get_cost_lookup
    cost = get_cost_lookup()
    db = get_db()
    try:
        rows = db.execute('''
            SELECT product_name, SUM(quantity) qty, SUM(total_money) revenue
            FROM sales WHERE date BETWEEN ? AND ? GROUP BY product_name''', (d_from, d_to)).fetchall()
    finally:
        db.close()
    revenue = sum(r['revenue'] or 0 for r in rows)
    priced_rev = cogs = 0.0
    no_recipe = []
    for r in rows:
        unit = cost.get(r['product_name'], 0) or 0
        if unit > 0:
            cogs += unit * (r['qty'] or 0)
            priced_rev += r['revenue'] or 0
        elif (r['revenue'] or 0) > 0:
            no_recipe.append((r['product_name'], r['revenue']))
    no_recipe.sort(key=lambda x: -x[1])
    return {
        'date_from': d_from, 'date_to': d_to, 'revenue_gross_zl': _r(revenue),
        'cogs_zl': _r(cogs),
        'food_cost_pct_of_priced_revenue': _r(cogs / priced_rev * 100, 1) if priced_rev else None,
        'recipe_coverage_pct_of_revenue': _r(priced_rev / revenue * 100, 1) if revenue else None,
        'biggest_products_without_recipe': [{'product': n, 'revenue_zl': _r(v)} for n, v in no_recipe[:5]],
    }


# ── registry ──

_DATE_PROPS = {
    'date_from': {'type': 'string', 'description': 'first day, YYYY-MM-DD (business day, starts 05:00)'},
    'date_to': {'type': 'string', 'description': 'last day inclusive, YYYY-MM-DD; defaults to date_from'},
}


def _spec(name, description, props, required=()):
    return {'toolSpec': {'name': name, 'description': description, 'inputSchema': {'json': {
        'type': 'object', 'properties': props, 'required': list(required)}}}}


TOOLS = {
    'sales_summary': (_spec('sales_summary', 'Revenue, discounts, items sold and average per sales day for a date range.', _DATE_PROPS, ['date_from']), sales_summary, False),
    'daily_revenue': (_spec('daily_revenue', 'Revenue and items per day (with weekday) for a date range, at most 62 days.', _DATE_PROPS, ['date_from']), daily_revenue, False),
    'weekday_averages': (_spec('weekday_averages', 'Average daily revenue per weekday over a date range (shows which weekdays are strong or weak).', _DATE_PROPS, ['date_from', 'date_to']), weekday_averages, False),
    'top_products': (_spec('top_products', 'Best-selling products in a date range, ranked by revenue or quantity.', {**_DATE_PROPS, 'by': {'type': 'string', 'enum': ['revenue', 'quantity']}, 'limit': {'type': 'integer', 'description': 'max 15, default 7'}}, ['date_from']), top_products, False),
    'product_sales': (_spec('product_sales', 'Sales of products whose name contains the given text, for a date range.', {**_DATE_PROPS, 'product': {'type': 'string'}}, ['date_from', 'product']), product_sales, False),
    'low_stock': (_spec('low_stock', 'Ingredients at or below their minimum stock level right now.', {}), low_stock, False),
    'stock_item': (_spec('stock_item', 'Current stock of ingredients whose name contains the given text.', {'ingredient': {'type': 'string'}}, ['ingredient']), stock_item, False),
    'wastage_summary': (_spec('wastage_summary', 'Write-offs / spoilage for a month: records by reason and the top written-off items.', {'month': {'type': 'string', 'description': 'YYYY-MM, default current month'}}), wastage_summary, False),
    'expenses_summary': (_spec('expenses_summary', 'Expenses by category for a month, with that month\'s revenue.', {'month': {'type': 'string', 'description': 'YYYY-MM, default current month'}}), expenses_summary, True),
    'food_cost': (_spec('food_cost', 'Cost of goods sold and food cost % for a date range, from recipes and ingredient prices, with recipe coverage.', _DATE_PROPS, ['date_from']), food_cost, True),
}


def tool_specs(finance: bool) -> list:
    return [spec for spec, _fn, fin in TOOLS.values() if finance or not fin]


def run_tool(name: str, args, today: str, finance: bool) -> dict:
    """Execute one tool. Never raises: errors go back to the model as {'error': ...}."""
    entry = TOOLS.get(name)
    if not entry or (entry[2] and not finance):
        return {'error': f'unknown tool {name}'}
    try:
        fn = entry[1]
        args = args if isinstance(args, dict) else {}
        if fn is wastage_summary:
            return fn(args, today, finance=finance)
        return fn(args, today)
    except ValueError as e:
        return {'error': str(e)[:200]}
    except Exception as e:  # a broken tool must not kill the chat; the type is enough
        return {'error': f'tool failed: {type(e).__name__}'}
