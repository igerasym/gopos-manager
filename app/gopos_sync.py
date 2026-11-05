"""GoPOS sync utilities — inventory deduction.

NOTE: Sales sync moved to gopos_api.py (uses GoPos REST API).
This file retains deduct_inventory() which is called from gopos_api.py.
"""
import logging
import threading

from app.db import get_db

log = logging.getLogger(__name__)

# Manual /sync and the scheduled sync run in separate threads; two concurrent
# reverse-and-deduct passes for the same date would reverse twice.
_DEDUCT_LOCK = threading.Lock()


def deduct_inventory(date: str):
    """Deduct ingredients from inventory based on sales and recipes.
    Safe to call multiple times — reverses previous deduction for this date first.

    inventory_deductions stores what was ACTUALLY taken off (stock is clamped at
    0), so reversing never adds back more than was removed.
    """
    with _DEDUCT_LOCK:
        db = get_db()
        try:
            db.execute('BEGIN IMMEDIATE')
            _deduct(db, date)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    log.info(f'Inventory deducted for {date}')


def _deduct(db, date: str):
    # Reverse previous deduction for this date (if any)
    prev = db.execute(
        'SELECT ingredient_id, SUM(amount) as total '
        'FROM inventory_deductions WHERE date = ? GROUP BY ingredient_id',
        (date,)
    ).fetchall()
    for p in prev:
        db.execute(
            'UPDATE ingredients SET quantity = quantity + ? WHERE id = ?',
            (p['total'], p['ingredient_id'])
        )
    db.execute('DELETE FROM inventory_deductions WHERE date = ?', (date,))

    # Deduct based on current sales
    rows = db.execute(
        'SELECT product_name, quantity FROM sales WHERE date = ?', (date,)
    ).fetchall()

    for row in rows:
        recipes = db.execute('''
            SELECT r.ingredient_id, r.amount, i.name
            FROM recipes r JOIN ingredients i ON r.ingredient_id = i.id
            WHERE r.product_name = ?
        ''', (row['product_name'],)).fetchall()

        for recipe in recipes:
            total = recipe['amount'] * row['quantity']
            cur = db.execute('SELECT quantity FROM ingredients WHERE id = ?',
                             (recipe['ingredient_id'],)).fetchone()
            on_hand = max(0.0, cur['quantity'] or 0) if cur else 0.0
            applied = min(total, on_hand)
            db.execute(
                'UPDATE ingredients SET quantity = quantity - ? WHERE id = ?',
                (applied, recipe['ingredient_id'])
            )
            db.execute(
                'INSERT INTO inventory_deductions (date, ingredient_id, amount) VALUES (?, ?, ?)',
                (date, recipe['ingredient_id'], applied)
            )
