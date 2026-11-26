"""Recipe service — cost calculations, display conversion."""
from app.db import get_db
from app.services.units import to_display


def get_recipe_map_with_costs():
    """Get all recipes with display amounts and costs."""
    db = get_db()
    all_recipes = db.execute('''
        SELECT r.id, r.product_name, r.ingredient_id, i.name as ingredient,
               r.amount, i.unit, COALESCE(i.unit_price, 0) as unit_price,
               r.amount * COALESCE(i.unit_price, 0) as cost
        FROM recipes r JOIN ingredients i ON r.ingredient_id = i.id
        ORDER BY r.product_name, i.name
    ''').fetchall()
    db.close()

    recipe_map = {}
    cost_map = {}
    for r in all_recipes:
        row = dict(r)
        display_amount, display_unit = to_display(row['amount'], row['unit'])
        row['display_amount'] = display_amount
        row['display_unit'] = display_unit
        recipe_map.setdefault(row['product_name'], []).append(row)
        cost_map[row['product_name']] = cost_map.get(row['product_name'], 0) + (row['cost'] or 0)

    return recipe_map, cost_map


def get_selling_prices() -> dict:
    """Get selling price per product from GoPos API (stored in pos_products) or sales average."""
    db = get_db()
    # Prefer API price (current menu price)
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    if 'pos_products' in tables:
        rows = db.execute('''
            SELECT product_name, gopos_price FROM pos_products WHERE gopos_price > 0
        ''').fetchall()
        result = {r['product_name']: r['gopos_price'] for r in rows}
    else:
        result = {}

    # Fallback: average from sales for products not in pos_products
    avg_prices = db.execute('''
        SELECT product_name, SUM(total_money + discount) / SUM(quantity) as avg_price
        FROM sales WHERE quantity > 0
        GROUP BY product_name
    ''').fetchall()
    for r in avg_prices:
        if r['product_name'] not in result:
            result[r['product_name']] = r['avg_price']

    db.close()
    return result


def get_cost_lookup() -> dict:
    """Get unit cost per product using pos_products classification + recipes."""
    db = get_db()

    # Check if pos_products table exists (backward compat)
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    use_pos_products = 'pos_products' in tables

    # From recipes (prepared items)
    recipe_costs = db.execute('''
        SELECT r.product_name, SUM(r.amount * COALESCE(i.unit_price, 0)) as unit_cost
        FROM recipes r JOIN ingredients i ON r.ingredient_id = i.id
        GROUP BY r.product_name
    ''').fetchall()
    result = {r['product_name']: r['unit_cost'] for r in recipe_costs}

    if use_pos_products:
        # Resale items: explicit link via pos_products → ingredient unit_price
        resale = db.execute('''
            SELECT pp.product_name, COALESCE(i.unit_price, 0) as unit_cost
            FROM pos_products pp
            JOIN ingredients i ON pp.resale_ingredient_id = i.id
            WHERE pp.pos_kind = 'resale'
        ''').fetchall()
        for r in resale:
            if r['product_name'] not in result:
                result[r['product_name']] = r['unit_cost']

        # Variant products: inherit cost from base product
        # "Cappuccino big Cappuccino" → cost of "Cappuccino big"
        all_pos = db.execute("SELECT product_name FROM pos_products WHERE pos_kind = 'prepared'").fetchall()
        for row in all_pos:
            name = row['product_name']
            if name in result:
                continue
            # Find base: longest recipe product that is a prefix
            for base in sorted(result.keys(), key=len, reverse=True):
                if name != base and name.startswith(base + ' '):
                    suffix = name[len(base) + 1:]
                    if suffix in result or suffix == base:
                        result[name] = result[base]
                        break
    else:
        # Fallback: direct match (ingredient name = product name, no recipe)
        direct = db.execute('''
            SELECT i.name, COALESCE(i.unit_price, 0) as unit_cost
            FROM ingredients i
            JOIN (SELECT DISTINCT product_name FROM sales) s ON s.product_name = i.name
            WHERE i.name NOT IN (SELECT DISTINCT product_name FROM recipes)
        ''').fetchall()
        for d in direct:
            if d['name'] not in result:
                result[d['name']] = d['unit_cost']

    db.close()
    return result


def recalc_sub_recipe_cost(db, sub_id):
    """Recalculate unit_price for a sub-recipe ingredient."""
    sr = db.execute('SELECT ingredient_id, yield_amount FROM sub_recipes WHERE id = ?', (sub_id,)).fetchone()
    if not sr:
        return
    total_cost = db.execute('''
        SELECT COALESCE(SUM(sri.amount * COALESCE(i.unit_price, 0)), 0) as cost
        FROM sub_recipe_items sri JOIN ingredients i ON sri.ingredient_id = i.id
        WHERE sri.sub_recipe_id = ?
    ''', (sub_id,)).fetchone()['cost']

    cost_per_unit = total_cost / sr['yield_amount'] if sr['yield_amount'] > 0 else 0
    db.execute('UPDATE ingredients SET unit_price = ? WHERE id = ?',
               (round(cost_per_unit, 4), sr['ingredient_id']))


def recalc_sub_recipes_for_ingredient(db, ingredient_id: int):
    """After updating ingredient unit_price, recalculate all sub-recipes that use it.

    A sub-recipe's cost is derived from its raw ingredient prices (recalc_sub_recipe_cost).
    When an invoice updates a raw ingredient's price, any sub-recipe that includes that
    ingredient gets a stale cost until the next manual edit — this fixes it automatically.
    """
    affected = db.execute('''
        SELECT DISTINCT sri.sub_recipe_id
        FROM sub_recipe_items sri
        WHERE sri.ingredient_id = ?
    ''', (ingredient_id,)).fetchall()
    for row in affected:
        recalc_sub_recipe_cost(db, row['sub_recipe_id'])


# ── Меню page (recipes redesign) ──────────────────────────────────────────────

# Product share of the selling price above which a dish gets a red chip.
# Hard-coded until a per-category target setting exists (brief section 8).
COST_SHARE_ALERT = 35.0

# Days of sales that count as "sold recently" for the 'Без рецепту' banner.
NO_RECIPE_DAYS = 30

# Menu tabs: key -> (label, path). Card categories map onto tabs in `tab_for_category`.
MENU_TABS = [
    ('kitchen', 'Кухня', '/recipes'),
    ('bar', 'Бар', '/recipes/bar'),
    ('bakery', 'Випічка', '/recipes/bakery'),
    ('prep', 'Заготовки', '/recipes/sub'),
]
TAB_PATHS = {k: p for k, _, p in MENU_TABS}
BAR_CATEGORIES = {'бар', 'напої', 'кава'}
BAKERY_CATEGORIES = {'випічка', 'десерти', 'десерт'}


# Till (GoPOS) categories -> the Меню category a new dish card should start with.
POS_BAR_CATEGORIES = {'kawa', 'napoje', 'herbata', 'bar', 'напої', 'кава'}
POS_BAKERY_CATEGORIES = {'desery', 'wypieki', 'ciasta', 'випічка', 'десерти'}


def card_category_for_pos(pos_category: str | None) -> str:
    """'Kawa' -> 'Бар', 'Desery' -> 'Випічка', unknown -> '' (let the tab decide)."""
    c = (pos_category or '').strip().lower()
    if c in POS_BAR_CATEGORIES:
        return 'Бар'
    if c in POS_BAKERY_CATEGORIES:
        return 'Випічка'
    if c:
        return 'Кухня'
    return ''


def tab_for_category(category: str | None) -> str:
    """Recipe-card category -> menu tab key. Anything not bar/bakery is kitchen."""
    c = (category or '').strip().lower()
    if c in BAR_CATEGORIES:
        return 'bar'
    if c in BAKERY_CATEGORIES:
        return 'bakery'
    return 'kitchen'


def parse_num(value, default: float | None = None) -> float | None:
    """Parse a user-typed number: accepts '1,5' and '1.5' and surrounding spaces."""
    if value is None:
        return default
    s = str(value).strip().replace('\u202f', '').replace(' ', '').replace(',', '.')
    if not s:
        return default
    try:
        return float(s)
    except ValueError:
        return default


def safe_next(value, fallback: str = '/recipes') -> str:
    """Only allow redirects back into the Меню section (no open redirect)."""
    v = (value or '').strip()
    if v.startswith('/recipes') and not v.startswith('//') and '\\' not in v and '\n' not in v and '\r' not in v:
        return v
    return fallback


def dish_economics(cost: float | None, price: float | None) -> dict:
    """Cost, price, what we earn per portion and the product share of the price.

    `share` is None when there is no price or no known cost; `alert` is True when the
    share is above COST_SHARE_ALERT."""
    cost = cost or 0.0
    price = price or 0.0
    has_cost = cost > 0
    has_price = price > 0
    share = cost / price * 100 if has_cost and has_price else None
    earn = price - cost if has_cost and has_price else None
    return {
        'cost': cost if has_cost else None,
        'price': price if has_price else None,
        'earn': earn,
        'earn_pct': (earn / price * 100) if earn is not None else None,
        'share': share,
        'alert': share is not None and share > COST_SHARE_ALERT,
    }


def get_products_without_recipe(today=None, days: int = NO_RECIPE_DAYS) -> dict:
    """Products sold in the last `days` days that have no cost (no recipe lines, no
    resale link). Same rule as the 'Без рецепту' attention row.

    Returns {'items': [{product_name, revenue, share, has_card, tab}], 'count', 'share',
    'total_revenue'}; items sorted by revenue, highest first."""
    from datetime import date, timedelta
    if today is None:
        from app.gopos_api import warsaw_today
        today = warsaw_today()
    if isinstance(today, str):
        today = date.fromisoformat(today)
    start = (today - timedelta(days=days - 1)).isoformat()
    db = get_db()
    sales = db.execute(
        'SELECT product_name, SUM(total_money) AS rev FROM sales WHERE date >= ? AND date <= ? '
        'GROUP BY product_name ORDER BY rev DESC', (start, today.isoformat())).fetchall()
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    kinds = {}
    if 'pos_products' in tables:
        kinds = {r['product_name']: r['pos_kind'] for r in db.execute(
            'SELECT product_name, pos_kind FROM pos_products').fetchall()}
    cards = {r['product_name']: r['category'] or '' for r in db.execute(
        'SELECT product_name, category FROM recipe_cards').fetchall()}
    db.close()

    costs = get_cost_lookup()
    total = sum((r['rev'] or 0) for r in sales)
    items = []
    for r in sales:
        name = r['product_name']
        if kinds.get(name) in ('ignore', 'unclassified') or name in costs:
            continue
        rev = r['rev'] or 0
        items.append({'product_name': name, 'revenue': rev,
                      'share': rev / total * 100 if total else 0.0, 'has_card': name in cards,
                      'tab': tab_for_category(cards[name]) if name in cards else None})
    return {
        'items': items,
        'count': len(items),
        'share': sum(i['revenue'] for i in items) / total * 100 if total else 0.0,
        'total_revenue': total,
    }


def get_sub_recipe_usage(db) -> dict:
    """ingredient_id of a заготовка -> sorted list of {'name', 'kind'} that use it
    (kind 'dish' for menu recipes, 'prep' for other заготовки)."""
    usage: dict[int, list] = {}
    rows = db.execute('''
        SELECT DISTINCT r.ingredient_id, r.product_name AS name, 'dish' AS kind
        FROM recipes r WHERE r.ingredient_id IN (SELECT ingredient_id FROM sub_recipes)
        UNION
        SELECT DISTINCT sri.ingredient_id, i.name AS name, 'prep' AS kind
        FROM sub_recipe_items sri
        JOIN sub_recipes sr ON sr.id = sri.sub_recipe_id
        JOIN ingredients i ON i.id = sr.ingredient_id
        WHERE sri.ingredient_id IN (SELECT ingredient_id FROM sub_recipes)
    ''').fetchall()
    for r in rows:
        usage.setdefault(r['ingredient_id'], []).append({'name': r['name'], 'kind': r['kind']})
    for v in usage.values():
        v.sort(key=lambda x: (x['kind'] != 'dish', x['name'].lower()))
    return usage
