"""Dashboard route."""
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.db import get_db
from app.services.recipes import get_cost_lookup

templates = Jinja2Templates(directory=Path(__file__).parent.parent / 'templates')

router = APIRouter()


@router.get('/', response_class=HTMLResponse)
async def dashboard(request: Request, date_from: str = '', date_to: str = ''):
    today = datetime.now().strftime('%Y-%m-%d')
    date_from = date_from or today
    date_to = date_to or today
    db = get_db()

    # Date presets for quick filter
    now = datetime.now()
    yesterday = (now - timedelta(days=1)).strftime('%Y-%m-%d')
    week_start = (now - timedelta(days=now.weekday())).strftime('%Y-%m-%d')
    month_start = now.replace(day=1).strftime('%Y-%m-%d')
    year_start = now.replace(month=1, day=1).strftime('%Y-%m-%d')
    last_7 = (now - timedelta(days=6)).strftime('%Y-%m-%d')
    last_30 = (now - timedelta(days=29)).strftime('%Y-%m-%d')

    presets = [
        ('Сьогодні', today, today),
        ('Вчора', yesterday, yesterday),
        ('Останні 7 днів', last_7, today),
        ('Цей тиждень', week_start, today),
        ('Останні 30 днів', last_30, today),
        ('Цей місяць', month_start, today),
        ('Цей рік', year_start, today),
    ]
    # Mark active preset
    active_preset = None
    for name, pf, pt in presets:
        if pf == date_from and pt == date_to:
            active_preset = name
            break

    # Main sales table
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
    ''', (date_from, date_to)).fetchall()

    # Merge variants into base products using GoPos item_group_id
    # Products in the same group are variants (e.g. Cappuccino/Cappuccino Iced)
    # We merge only redundant variants (name contains base name as prefix)
    # but keep distinct variants like "Iced" separate
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    group_map = {}  # product_name → base_product_name (for merging)
    if 'pos_products' in tables:
        # Get groups: find the "base" product in each group (shortest name or the one with recipe)
        groups_raw = db.execute('''
            SELECT product_name, gopos_group_id FROM pos_products
            WHERE gopos_group_id IS NOT NULL
        ''').fetchall()
        groups = {}  # group_id → [product_names]
        for r in groups_raw:
            groups.setdefault(r['gopos_group_id'], []).append(r['product_name'])

        products_with_recipe = set(
            r[0] for r in db.execute("SELECT DISTINCT product_name FROM recipes").fetchall()
        )

        for gid, names in groups.items():
            if len(names) <= 1:
                continue
            # Base = shortest name in group, or one with recipe
            base = min(names, key=len)
            for name in names:
                if name == base:
                    continue
                # Only merge if name is "Base Suffix" where Suffix is also a known product
                # This keeps "Cappuccino Iced" separate but merges "Cappuccino Cappuccino"
                if name.startswith(base + ' '):
                    suffix = name[len(base) + 1:]
                    if suffix in products_with_recipe or suffix == base.split()[-1]:
                        group_map[name] = base

    merged_sales = {}
    for s in sales:
        name = s['product_name']
        base_name = group_map.get(name, name)

        if base_name in merged_sales:
            m = merged_sales[base_name]
            m['quantity'] += s['quantity']
            m['total_money'] += s['total_money']
            m['net_total'] += s['net_total']
            m['discount'] += s['discount']
            m['net_profit'] += s['net_profit']
        else:
            merged_sales[base_name] = {
                'product_name': base_name,
                'quantity': s['quantity'],
                'total_money': s['total_money'],
                'net_total': s['net_total'],
                'discount': s['discount'],
                'net_profit': s['net_profit'],
            }
    sales = sorted(merged_sales.values(), key=lambda x: x['total_money'], reverse=True)

    # Totals
    totals = db.execute('''
        SELECT COALESCE(SUM(total_money),0) as revenue,
               COALESCE(SUM(net_total),0) as net_revenue,
               COALESCE(SUM(net_profit),0) as profit,
               COALESCE(SUM(quantity),0) as items,
               COALESCE(SUM(discount),0) as discounts,
               COUNT(DISTINCT product_name) as unique_products
        FROM sales WHERE date >= ? AND date <= ?
    ''', (date_from, date_to)).fetchone()

    # Top 5 products by revenue
    top_products = db.execute('''
        SELECT product_name, SUM(quantity) as qty, SUM(total_money) as rev
        FROM sales WHERE date >= ? AND date <= ?
        GROUP BY product_name ORDER BY rev DESC LIMIT 5
    ''', (date_from, date_to)).fetchall()

    # Top 5 by quantity
    top_by_qty = db.execute('''
        SELECT product_name, SUM(quantity) as qty, SUM(total_money) as rev
        FROM sales WHERE date >= ? AND date <= ?
        GROUP BY product_name ORDER BY qty DESC LIMIT 5
    ''', (date_from, date_to)).fetchall()

    # Daily breakdown (for charts / trend)
    daily = db.execute('''
        SELECT date, SUM(total_money) as revenue, SUM(quantity) as items
        FROM sales WHERE date >= ? AND date <= ?
        GROUP BY date ORDER BY date
    ''', (date_from, date_to)).fetchall()

    # Previous period comparison
    d_from = datetime.strptime(date_from, '%Y-%m-%d')
    d_to = datetime.strptime(date_to, '%Y-%m-%d')
    period_days = (d_to - d_from).days + 1
    prev_to = (d_from - timedelta(days=1)).strftime('%Y-%m-%d')
    prev_from = (d_from - timedelta(days=period_days)).strftime('%Y-%m-%d')

    prev_totals = db.execute('''
        SELECT COALESCE(SUM(total_money),0) as revenue,
               COALESCE(SUM(quantity),0) as items
        FROM sales WHERE date >= ? AND date <= ?
    ''', (prev_from, prev_to)).fetchone()

    # Low stock
    low_stock = db.execute('''
        SELECT name, quantity, unit, min_quantity
        FROM ingredients WHERE quantity <= min_quantity
    ''').fetchall()

    # ── Food Cost & P&L ──
    cost_lookup = get_cost_lookup()

    # Get pos_products categories for breakdown
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    pos_categories = {}
    if 'pos_products' in tables:
        for r in db.execute("SELECT product_name, category, pos_kind FROM pos_products").fetchall():
            pos_categories[r['product_name']] = {'category': r['category'], 'pos_kind': r['pos_kind']}

    # Build sales with food cost
    sales_with_cost = []
    total_cogs = 0
    category_breakdown = {}  # {category: {revenue, cogs, quantity}}
    for s in sales:
        unit_cost = cost_lookup.get(s['product_name'], 0)
        line_cost = unit_cost * s['quantity']
        food_cost_pct = (line_cost / s['total_money'] * 100) if s['total_money'] > 0 else 0
        total_cogs += line_cost

        # Category from pos_products
        pos_info = pos_categories.get(s['product_name'], {})
        category = pos_info.get('category', 'Інше') or 'Інше'
        pos_kind = pos_info.get('pos_kind', 'unclassified')

        # Skip ignored products from breakdown
        if pos_kind != 'ignore':
            if category not in category_breakdown:
                category_breakdown[category] = {'revenue': 0, 'cogs': 0, 'quantity': 0, 'products': 0}
            category_breakdown[category]['revenue'] += s['total_money']
            category_breakdown[category]['cogs'] += line_cost
            category_breakdown[category]['quantity'] += s['quantity']
            category_breakdown[category]['products'] += 1

        sales_with_cost.append({
            'product_name': s['product_name'],
            'quantity': s['quantity'],
            'total_money': s['total_money'],
            'net_total': s['net_total'],
            'discount': s['discount'],
            'net_profit': s['net_profit'],
            'unit_cost': unit_cost,
            'line_cost': line_cost,
            'food_cost_pct': food_cost_pct,
            'has_recipe': s['product_name'] in cost_lookup,
        })

    # Sort category breakdown by revenue desc
    category_list = []
    for cat, data in sorted(category_breakdown.items(), key=lambda x: x[1]['revenue'], reverse=True):
        data['category'] = cat
        data['gross_profit'] = data['revenue'] - data['cogs']
        data['food_cost_pct'] = (data['cogs'] / data['revenue'] * 100) if data['revenue'] > 0 else 0
        category_list.append(data)

    # P&L summary
    gross_profit = totals['revenue'] - total_cogs
    gross_margin = (gross_profit / totals['revenue'] * 100) if totals['revenue'] > 0 else 0
    avg_food_cost = (total_cogs / totals['revenue'] * 100) if totals['revenue'] > 0 else 0

    # ── ABC Analysis ──
    sorted_by_rev = sorted(sales_with_cost, key=lambda x: x['total_money'], reverse=True)
    cumulative = 0
    for item in sorted_by_rev:
        cumulative += item['total_money']
        pct = (cumulative / totals['revenue'] * 100) if totals['revenue'] > 0 else 0
        if pct <= 80:
            item['abc'] = 'A'
        elif pct <= 95:
            item['abc'] = 'B'
        else:
            item['abc'] = 'C'

    abc_counts = {'A': 0, 'B': 0, 'C': 0}
    abc_revenue = {'A': 0, 'B': 0, 'C': 0}
    for item in sorted_by_rev:
        abc_counts[item['abc']] += 1
        abc_revenue[item['abc']] += item['total_money']

    db.close()
    return templates.TemplateResponse(request, 'dashboard.html', context={
        'date_from': date_from, 'date_to': date_to,
        'presets': presets, 'active_preset': active_preset,
        'sales': sales_with_cost, 'totals': totals,
        'top_products': top_products, 'top_by_qty': top_by_qty,
        'daily': daily, 'period_days': period_days,
        'prev_totals': prev_totals,
        'low_stock': low_stock,
        'total_cogs': total_cogs, 'gross_profit': gross_profit,
        'gross_margin': gross_margin, 'avg_food_cost': avg_food_cost,
        'abc_counts': abc_counts, 'abc_revenue': abc_revenue,
        'category_breakdown': category_list,
    })
