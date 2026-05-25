"""GoPOS sync utilities — inventory deduction.

NOTE: Sales sync moved to gopos_api.py (uses GoPos REST API).
This file retains deduct_inventory() which is called from gopos_api.py.
"""
import logging

from app.db import get_db

log = logging.getLogger(__name__)


def deduct_inventory(date: str):
    """Deduct ingredients from inventory based on sales and recipes.
    Safe to call multiple times — reverses previous deduction for this date first.
    """
    db = get_db()

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
            db.execute(
                'UPDATE ingredients SET quantity = MAX(0, quantity - ?) WHERE id = ?',
                (total, recipe['ingredient_id'])
            )
            db.execute(
                'INSERT INTO inventory_deductions (date, ingredient_id, amount) VALUES (?, ?, ?)',
                (date, recipe['ingredient_id'], total)
            )

    db.commit()
    db.close()
    log.info(f'Inventory deducted for {date}')
