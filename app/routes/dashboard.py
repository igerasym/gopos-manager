"""Dashboard ("Сьогодні") routes, plus the sales / food cost / P&L queries other pages import."""
import csv
import io
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from app.db import get_db
from app import gopos_api
from app.gopos_api import warsaw_today
from app.services.attention import get_attention_items
from app.services.recipes import get_cost_lookup

templates = Jinja2Templates(directory=Path(__file__).parent.parent / 'templates')

router = APIRouter()

WEEKDAYS_UA = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'нд']
# Share of the price spent on ingredients above which a product is flagged "Висока".
FOOD_COST_HIGH = 35
# "more than last Wednesday": weekday in genitive.
LAST_WEEKDAY_UA = ['минулого понеділка', 'минулого вівторка', 'минулої середи', 'минулого четверга',
                   'минулої пʼятниці', 'минулої суботи', 'минулої неділі']
PERIODS = ('day', 'week', 'month', 'range')


# ── Period handling ──────────────────────────────────────────────────────────

def _iso(d) -> str:
    return d.strftime('%Y-%m-%d')


def _parse(v: str):
    try:
        return datetime.strptime(v, '%Y-%m-%d')
    except (TypeError, ValueError):
        return None


def resolve_period(db, today: str, p: str = '', date: str = '',
                   date_from: str = '', date_to: str = '') -> dict:
    """Turn query parameters into a period plus its comparison period.

    p: day | week | month | range. `date` opens one day. Bare date_from/date_to (old links)
    mean a range, or a day when equal. A running day is never compared with a full one:
    the comparison (`cmp_*`) covers completed days only; `cmp_ok` is False when none exist.
    """
    now = datetime.strptime(today, '%Y-%m-%d')
    one = timedelta(days=1)
    has_today = db.execute('SELECT 1 FROM sales WHERE date = ? LIMIT 1', (today,)).fetchone()
    # Before the first sale of the day an empty "today" is just a screen of zeros:
    # open on the last full day instead.
    default_day = now if has_today else now - one

    df, dt = _parse(date_from), _parse(date_to)
    d1 = _parse(date)
    if d1:
        p, start, end = 'day', d1, d1
    elif (date_from or date_to) and p not in PERIODS:
        start, end = df or dt or now, dt or df or now
        if end < start:
            start, end = end, start
        p = 'day' if start == end else 'range'
    else:
        if p not in PERIODS:
            p = 'day'
        if p == 'day':
            start = end = default_day
        elif p == 'week':
            start, end = now - timedelta(days=now.weekday()), now
        elif p == 'month':
            start, end = now.replace(day=1), now
        else:  # range without valid dates: last 30 days
            start, end = (df or now - timedelta(days=29)), (dt or now)
            if end < start:
                start, end = end, start

    length = (end - start).days + 1
    in_progress = end == now
    cmp_end = end - one if in_progress else end
    if p == 'day':
        prev_start = prev_end = start - timedelta(days=7)
        prev_label = LAST_WEEKDAY_UA[prev_start.weekday()]
    elif p == 'week':
        prev_start = start - timedelta(days=7)
        prev_end = prev_start + (cmp_end - start)
        prev_label = 'минулого тижня за ті самі дні'
    elif p == 'month':
        last_month_end = start - one
        prev_start = last_month_end.replace(day=1)
        prev_end = min(prev_start + (cmp_end - start), last_month_end)
        prev_label = 'минулого місяця за ті самі дні'
    else:
        prev_start = start - timedelta(days=length)
        prev_end = prev_start + (cmp_end - start)
        prev_label = 'попереднього періоду'
    # A single running day has nothing to compare with (half a day vs a full one is noise).
    cmp_ok = cmp_end >= start
    if p == 'day':
        cmp_ok = not in_progress
    return {
        'p': p, 'start': _iso(start), 'end': _iso(end), 'days': length,
        'is_today': start == end == now, 'is_yesterday': start == end == now - one,
        'in_progress': in_progress,
        'cmp_ok': cmp_ok, 'cmp_start': _iso(start), 'cmp_end': _iso(cmp_end),
        'prev_start': _iso(prev_start), 'prev_end': _iso(prev_end), 'prev_label': prev_label,
    }


def _revenue(db, d_from: str, d_to: str) -> float:
    return db.execute('SELECT COALESCE(SUM(total_money),0) FROM sales WHERE date >= ? AND date <= ?',
                      (d_from, d_to)).fetchone()[0]


def checks_stats(db, d_from: str, d_to: str) -> dict:
    """Checks (orders), average check and takeaway share (of checks) from daily_takeaway."""
    r = db.execute('''SELECT COALESCE(SUM(orders),0) AS orders,
                             COALESCE(SUM(revenue),0) AS revenue,
                             COALESCE(SUM(takeaway_orders),0) AS t_orders
                      FROM daily_takeaway WHERE date >= ? AND date <= ?''', (d_from, d_to)).fetchone()
    if not r['orders']:
        return {'orders': None, 'avg': None, 'takeaway_pct': None}
    return {'orders': r['orders'], 'avg': r['revenue'] / r['orders'],
            'takeaway_pct': r['t_orders'] / r['orders'] * 100}


def week_bars(db, anchor: str, today: str) -> list[dict]:
    """Seven columns Mon..Sun of the week containing `anchor`, each with last week's ghost."""
    a = datetime.strptime(anchor, '%Y-%m-%d')
    monday = a - timedelta(days=a.weekday())
    rows = db.execute('SELECT date, SUM(total_money) AS rev FROM sales WHERE date >= ? AND date <= ? GROUP BY date',
                      (_iso(monday - timedelta(days=7)), _iso(monday + timedelta(days=6)))).fetchall()
    by_date = {r['date']: r['rev'] or 0 for r in rows}
    cols = []
    for i in range(7):
        d = monday + timedelta(days=i)
        ds = _iso(d)
        cols.append({'date': ds, 'label': WEEKDAYS_UA[i],
                     'now': by_date.get(ds, 0) if ds <= today else None,
                     'ghost': by_date.get(_iso(d - timedelta(days=7)), 0),
                     'is_today': ds == today})
    peak = max([c['ghost'] for c in cols] + [c['now'] or 0 for c in cols]) or 1
    for c in cols:
        c['ghost_h'] = round(c['ghost'] / peak * 100, 1)
        c['now_h'] = None if c['now'] is None else max(round(c['now'] / peak * 100, 1), 1.5)
    return cols


def top_products(db, d_from: str, d_to: str, limit: int = 5) -> dict:
    """Top-N by revenue ('rev') and by quantity ('qty'), with bar widths (% of the leader)."""
    out = {}
    for key in ('rev', 'qty'):
        rows = db.execute(f'''
            SELECT product_name, SUM(quantity) AS qty, SUM(total_money) AS rev
            FROM sales WHERE date >= ? AND date <= ?
            GROUP BY product_name ORDER BY {key} DESC LIMIT ?''', (d_from, d_to, limit)).fetchall()
        lead = (rows[0][key] if rows else 0) or 1
        out[key] = [{'name': r['product_name'], 'qty': r['qty'], 'rev': r['rev'],
                     'width': max(round(r[key] / lead * 100), 3)} for r in rows]
    return out


# ── Sales, food cost, P&L (importable: "Гроші" shows the P&L) ───────────────

def merged_sales(db, d_from: str, d_to: str) -> list[dict]:
    """Sales per product for the period; variants of one GoPOS group are merged into the base product."""
    sales = db.execute('''
        SELECT product_name,
               SUM(quantity) as quantity,
               SUM(total_money) as total_money,
               SUM(net_total) as net_total,
               SUM(discount) as discount,
               SUM(net_profit) as net_profit
        FROM sales WHERE date >= ? AND date <= ?
        GROUP BY product_name
        ORDER BY total_money DESC
    ''', (d_from, d_to)).fetchall()

    # Products in the same GoPOS group are variants (Cappuccino / Cappuccino Iced).
    # Merge only redundant variants (name = base + a known suffix), keep distinct ones like "Iced".
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    group_map = {}  # product_name -> base product name
    if 'pos_products' in tables:
        groups_raw = db.execute('''
            SELECT product_name, gopos_group_id FROM pos_products
            WHERE gopos_group_id IS NOT NULL
        ''').fetchall()
        groups = {}
        for r in groups_raw:
            groups.setdefault(r['gopos_group_id'], []).append(r['product_name'])
        products_with_recipe = set(
            r[0] for r in db.execute("SELECT DISTINCT product_name FROM recipes").fetchall()
        )
        for names in groups.values():
            if len(names) <= 1:
                continue
            base = min(names, key=len)
            for name in names:
                if name != base and name.startswith(base + ' '):
                    suffix = name[len(base) + 1:]
                    if suffix in products_with_recipe or suffix == base.split()[-1]:
                        group_map[name] = base

    merged = {}
    for s in sales:
        base_name = group_map.get(s['product_name'], s['product_name'])
        if base_name in merged:
            m = merged[base_name]
            for k in ('quantity', 'total_money', 'net_total', 'discount', 'net_profit'):
                m[k] += s[k]
        else:
            merged[base_name] = {
                'product_name': base_name, 'quantity': s['quantity'], 'total_money': s['total_money'],
                'net_total': s['net_total'], 'discount': s['discount'], 'net_profit': s['net_profit'],
            }
    return sorted(merged.values(), key=lambda x: x['total_money'], reverse=True)


def period_totals(db, d_from: str, d_to: str):
    return db.execute('''
        SELECT COALESCE(SUM(total_money),0) as revenue,
               COALESCE(SUM(net_total),0) as net_revenue,
               COALESCE(SUM(net_profit),0) as profit,
               COALESCE(SUM(quantity),0) as items,
               COALESCE(SUM(discount),0) as discounts,
               COUNT(DISTINCT product_name) as unique_products
        FROM sales WHERE date >= ? AND date <= ?
    ''', (d_from, d_to)).fetchone()


def compute_pnl(db, d_from: str, d_to: str) -> dict:
    """Sales with food cost, COGS, gross profit, category breakdown, ABC and recipe coverage.

    Keys: sales (per product: unit_cost, line_cost, food_cost_pct, has_recipe, abc), totals,
    total_cogs, gross_profit, gross_margin, avg_food_cost, category_breakdown,
    abc_counts, abc_revenue, recipe_coverage.
    """
    sales = merged_sales(db, d_from, d_to)
    totals = period_totals(db, d_from, d_to)
    cost_lookup = get_cost_lookup()

    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    pos_categories = {}
    if 'pos_products' in tables:
        for r in db.execute("SELECT product_name, category, pos_kind FROM pos_products").fetchall():
            pos_categories[r['product_name']] = {'category': r['category'], 'pos_kind': r['pos_kind']}

    sales_with_cost = []
    total_cogs = 0
    category_breakdown = {}
    for s in sales:
        unit_cost = cost_lookup.get(s['product_name'], 0)
        line_cost = unit_cost * s['quantity']
        food_cost_pct = (line_cost / s['total_money'] * 100) if s['total_money'] > 0 else 0
        total_cogs += line_cost

        pos_info = pos_categories.get(s['product_name'], {})
        category = pos_info.get('category', 'Інше') or 'Інше'
        if pos_info.get('pos_kind', 'unclassified') != 'ignore':
            c = category_breakdown.setdefault(category, {'revenue': 0, 'cogs': 0, 'quantity': 0, 'products': 0})
            c['revenue'] += s['total_money']
            c['cogs'] += line_cost
            c['quantity'] += s['quantity']
            c['products'] += 1

        sales_with_cost.append({
            **s, 'unit_cost': unit_cost, 'line_cost': line_cost, 'food_cost_pct': food_cost_pct,
            'has_recipe': s['product_name'] in cost_lookup,
        })

    category_list = []
    for cat, data in sorted(category_breakdown.items(), key=lambda x: x[1]['revenue'], reverse=True):
        data['category'] = cat
        data['gross_profit'] = data['revenue'] - data['cogs']
        data['food_cost_pct'] = (data['cogs'] / data['revenue'] * 100) if data['revenue'] > 0 else 0
        category_list.append(data)

    gross_profit = totals['revenue'] - total_cogs
    gross_margin = (gross_profit / totals['revenue'] * 100) if totals['revenue'] > 0 else 0
    avg_food_cost = (total_cogs / totals['revenue'] * 100) if totals['revenue'] > 0 else 0

    # A = products bringing the first 80% of revenue, B = next 15%, C = the rest.
    cumulative = 0
    abc_counts = {'A': 0, 'B': 0, 'C': 0}
    abc_revenue = {'A': 0, 'B': 0, 'C': 0}
    for item in sorted(sales_with_cost, key=lambda x: x['total_money'], reverse=True):
        cumulative += item['total_money']
        pct = (cumulative / totals['revenue'] * 100) if totals['revenue'] > 0 else 0
        item['abc'] = 'A' if pct <= 80 else 'B' if pct <= 95 else 'C'
        abc_counts[item['abc']] += 1
        abc_revenue[item['abc']] += item['total_money']

    total_products = len(sales_with_cost)
    with_recipe = sum(1 for s in sales_with_cost if s['has_recipe'])
    uncovered_revenue = sum(s['total_money'] for s in sales_with_cost if not s['has_recipe'])
    recipe_coverage = {
        'total': total_products,
        'with_recipe': with_recipe,
        'without_recipe': total_products - with_recipe,
        'pct': round(with_recipe / total_products * 100) if total_products else 0,
        'uncovered_revenue': uncovered_revenue,
        'uncovered_pct': round(uncovered_revenue / totals['revenue'] * 100) if totals['revenue'] else 0,
    }
    return {
        'sales': sales_with_cost, 'totals': totals, 'total_cogs': total_cogs,
        'gross_profit': gross_profit, 'gross_margin': gross_margin, 'avg_food_cost': avg_food_cost,
        'category_breakdown': category_list, 'abc_counts': abc_counts, 'abc_revenue': abc_revenue,
        'recipe_coverage': recipe_coverage,
    }


# ── Pages ────────────────────────────────────────────────────────────────────

@router.get('/', response_class=HTMLResponse)
async def dashboard(request: Request, p: str = '', date: str = '', date_from: str = '', date_to: str = ''):
    today = warsaw_today()
    db = get_db()
    per = resolve_period(db, today, p, date, date_from, date_to)
    start, end = per['start'], per['end']

    revenue = _revenue(db, start, end)
    checks = checks_stats(db, start, end)
    cmp_cur = _revenue(db, per['cmp_start'], per['cmp_end']) if per['cmp_ok'] else None
    cmp_prev = _revenue(db, per['prev_start'], per['prev_end']) if per['cmp_ok'] else None
    # A running single day: show what the same weekday brought last week as a plain fact.
    last_same_day = _revenue(db, per['prev_start'], per['prev_end']) if per['p'] == 'day' else None

    week = week_bars(db, end, today)
    week_total = sum(c['now'] or 0 for c in week)
    # Completed days of this week vs the same days last week (a running day would skew it).
    done = [c for c in week if c['now'] is not None and not c['is_today']]
    week_cmp = (sum(c['now'] for c in done), sum(c['ghost'] for c in done)) if done else None
    top = top_products(db, start, end)
    db.close()

    user = getattr(request.state, 'user', None)
    return templates.TemplateResponse(request, 'dashboard.html', context={
        'per': per, 'date_from': start, 'date_to': end,
        'revenue': revenue, 'checks': checks,
        'cmp_cur': cmp_cur, 'cmp_prev': cmp_prev, 'last_same_day': last_same_day,
        'week': week, 'week_total': week_total, 'week_cmp': week_cmp,
        'week_has_data': any((c['now'] or 0) > 0 or c['ghost'] > 0 for c in week),
        'top': top,
        'as_of': gopos_api.LAST_INTRADAY_SYNC,
        'attention_items': get_attention_items(user),
    })


@router.get('/dashboard/products', response_class=HTMLResponse)
async def products_page(request: Request, p: str = '', date: str = '', date_from: str = '', date_to: str = ''):
    """'Усі продукти': what sold, what it earned and how much of the price goes to ingredients."""
    today = warsaw_today()
    db = get_db()
    per = resolve_period(db, today, p, date, date_from, date_to)
    data = compute_pnl(db, per['start'], per['end'])
    db.close()
    return templates.TemplateResponse(request, 'today_products.html', context={
        'per': per, 'date_from': per['start'], 'date_to': per['end'],
        'food_cost_high': FOOD_COST_HIGH,
        'rows': data['sales'], 'revenue': data['totals']['revenue'],
        'total_cogs': data['total_cogs'], 'cost_share': data['avg_food_cost'],
        'coverage': data['recipe_coverage'],
    })


@router.get('/export/sales.csv')
async def export_sales_csv(date_from: str = '', date_to: str = ''):
    """Download sales as CSV for the given date range."""
    today = warsaw_today()
    date_from = date_from or today
    date_to = date_to or today
    db = get_db()
    rows = db.execute('''
        SELECT date, product_name,
               SUM(quantity) as quantity,
               SUM(total_money) as total_money,
               SUM(net_total) as net_total,
               SUM(discount) as discount
        FROM sales WHERE date >= ? AND date <= ?
        GROUP BY date, product_name
        ORDER BY date, product_name
    ''', (date_from, date_to)).fetchall()
    db.close()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['Дата', 'Продукт', 'К-сть', 'Виручка (брутто)', 'Виручка (нетто)', 'Знижка'])
    for r in rows:
        w.writerow([r['date'], r['product_name'], r['quantity'],
                    round(r['total_money'], 2), round(r['net_total'], 2), round(r['discount'], 2)])
    buf.seek(0)
    fname = f'sales_{date_from}_{date_to}.csv'
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type='text/csv; charset=utf-8-sig',
        headers={'Content-Disposition': f'attachment; filename="{fname}"'},
    )
