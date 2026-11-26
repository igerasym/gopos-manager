"""GoPos write-offs are priced from the product's recipe, not left at 0."""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.db import init_db, get_db
from app.auth import hash_password, sign_cookie
from app.templating import num, zl

TODAY = '2026-11-25'


@pytest.fixture(autouse=True)
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr('app.db.DB_PATH', tmp_path / 'test.db')
    monkeypatch.setattr('app.routes.wastage.warsaw_today', lambda: TODAY)
    init_db()
    db = get_db()
    db.execute("INSERT INTO users (username, password_hash, role, display_name) VALUES ('admin', ?, 'admin', 'A')",
               (hash_password('x'),))
    db.execute("INSERT INTO ingredients (id, name, unit, quantity, unit_price) VALUES (1, 'Mleko', 'L', 10, 4.2)")
    db.execute("INSERT INTO ingredients (id, name, unit, quantity, unit_price) VALUES (2, 'Kawa', 'kg', 5, 100)")
    # Latte = 0.2 L milk + 0.018 kg coffee = 0.84 + 1.80 = 2.64 per cup
    db.execute("INSERT INTO recipes (product_name, ingredient_id, amount) VALUES ('Latte', 1, 0.2)")
    db.execute("INSERT INTO recipes (product_name, ingredient_id, amount) VALUES ('Latte', 2, 0.018)")
    db.execute("INSERT INTO wastage (date, product_name, quantity, reason, source) "
               "VALUES (?, 'Latte', 3, 'write-off', 'gopos')", (TODAY,))
    db.execute("INSERT INTO wastage (date, product_name, quantity, reason, source) "
               "VALUES (?, 'Croissant', 2, 'write-off', 'gopos')", (TODAY,))
    db.execute("INSERT INTO wastage (date, ingredient_id, quantity, reason, source) "
               "VALUES (?, 1, 1, 'expired', 'manual')", (TODAY,))
    db.commit()
    db.close()
    yield


def _admin():
    c = TestClient(app, follow_redirects=False)
    c.cookies.set('session', sign_cookie('admin'))
    return c


def test_gopos_writeoff_is_priced_from_recipe():
    html = _admin().get('/wastage').text
    assert zl(7.92) in html           # 3 x 2.64


def test_total_includes_recipe_cost_and_manual_cost():
    html = _admin().get('/wastage').text
    assert num(12.12, 2) + '&nbsp;<small>zł</small>' in html  # 7.92 + 4.20 in the hero


def test_product_without_recipe_is_flagged_not_counted():
    html = _admin().get('/wastage').text
    assert 'Без ціни: Croissant' in html
    assert 'У страви немає рецепту' in html
    assert 'неповні дані' in html


def test_csv_has_recipe_cost_for_gopos_rows():
    csv_text = _admin().get('/export/wastage.csv').text
    assert 'Latte' in csv_text and '7.92' in csv_text
