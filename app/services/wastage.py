"""Wastage pricing, shared by the /wastage page, its CSV export and the Telegram bot."""
from app.services.recipes import get_cost_lookup


def price_rows(rows) -> list:
    """Rows as dicts with `cost`/`cost_known` filled in.

    Each row needs `source`, `label`, `quantity` and `cost` (quantity x ingredient price, 0 for a
    GoPos row). Manual rows already carry their cost. A GoPos write-off is a product
    (e.g. 'Croissant'), so its cost is quantity x the product's recipe cost at current ingredient
    prices, the same figure the dashboard uses for COGS. No recipe means unknown cost (shown as
    a dash), not a free 0.00.
    """
    product_cost = get_cost_lookup()
    out = []
    for r in rows:
        d = dict(r)
        if d['source'] == 'gopos':
            unit_cost = product_cost.get(d['label'], 0) or 0
            d['cost'] = d['quantity'] * unit_cost
            d['cost_known'] = unit_cost > 0
            d['unit'] = 'шт'
        else:
            d['cost_known'] = d['cost'] > 0
        out.append(d)
    return out
