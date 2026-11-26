"""Invoice line price -> price per INGREDIENT unit.

An invoice line is priced per invoice unit: a 15 kg crate of oranges, a carton of 12 x 1 L milk,
a 10 L can of oil. An ingredient is priced per kg / L / szt. The pack factor is how many
ingredient units one invoice unit holds:

    price per ingredient unit = invoice unit price / pack factor
    quantity in ingredient units = invoice quantity * pack factor

Until 2026-11 the price was stored as printed (39.48 zl per carton became "39.48 zl per litre"),
which inflated every recipe that uses the ingredient.

The same invoice name can be sold both ways ("LAC.MLEKO UHT 3,2% 1L" appears as a carton at 39-50 zl
and as single litres at 4.42 zl), so one factor per name is only a candidate. Per line, the
candidates are tried in order and the first one whose price per ingredient unit lies within
0.4x-2.5x of the ingredient's current price wins:

1. the stored factor of the mapping (`ingredient_mappings.pack_factor`, a real pack size, never 1);
2. the model's conversion of this line (converted_qty / quantity);
3. 1, the old behaviour.

With no current price, or when no candidate fits, the first candidate is used.

The size written in a name ("200 G") is deliberately NOT read: Makro prices many such lines per kg,
so reading it would turn a right price into a wrong one (butter "200 G" at 42.49 zl/kg is not
212 zl/kg).
"""
def stored_factor(db, invoice_name: str):
    row = db.execute('SELECT pack_factor FROM ingredient_mappings WHERE invoice_name = ?',
                     (invoice_name,)).fetchone()
    return row['pack_factor'] if row and row['pack_factor'] and row['pack_factor'] > 0 else None


def candidates(db, invoice_name: str, llm_factor=None) -> list:
    """Pack factors to try for a line, best first, without duplicates; 1.0 is always last."""
    out = []
    for f in (stored_factor(db, invoice_name), llm_factor, 1.0):
        if f and f > 0 and not any(abs(f - o) < 1e-9 for o in out):
            out.append(float(f))
    return out


def choose(raw_price, current_price, factors: list) -> tuple:
    """(price per ingredient unit, factor) for an invoice price, see the module docstring."""
    raw = float(raw_price or 0)
    if raw > 0 and current_price and current_price > 0:
        for f in factors:
            if 0.4 <= (raw / f) / current_price <= 2.5:
                return raw / f, f
    return raw / factors[0], factors[0]


def price_and_factor(db, invoice_name: str, ingredient_id, raw_price, llm_factor=None) -> tuple:
    row = db.execute('SELECT unit_price FROM ingredients WHERE id = ?', (ingredient_id,)).fetchone() \
        if ingredient_id else None
    return choose(raw_price, row['unit_price'] if row else 0, candidates(db, invoice_name, llm_factor))


def price_per_unit(db, invoice_name: str, ingredient_id, raw_price, quantity=None, llm_factor=None) -> float:
    """`raw_price` (per invoice unit) as a price per ingredient unit."""
    return price_and_factor(db, invoice_name, ingredient_id, raw_price, llm_factor)[0]


def save_mapping(db, invoice_name: str, ingredient_id, action: str, pack_factor=None):
    """Upsert a mapping. An existing pack_factor is kept; only a real pack size (not 1) is stored."""
    if pack_factor is not None and abs(pack_factor - 1.0) < 1e-9:
        pack_factor = None
    db.execute('''
        INSERT INTO ingredient_mappings (invoice_name, ingredient_id, action, pack_factor)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(invoice_name) DO UPDATE SET
            ingredient_id = excluded.ingredient_id,
            action = excluded.action,
            pack_factor = COALESCE(ingredient_mappings.pack_factor, excluded.pack_factor)
    ''', (invoice_name, ingredient_id, action, pack_factor))
