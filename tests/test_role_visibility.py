"""Prices and costs are admin-only: chef/barista must not see or change them."""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.db import init_db, get_db
from app.auth import hash_password, sign_cookie


@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    monkeypatch.setattr('app.db.DB_PATH', tmp_path / 'test.db')
    init_db()
    db = get_db()
    for name, role in (('admin', 'admin'), ('chef', 'chef')):
        db.execute(
            'INSERT INTO users (username, password_hash, role, display_name) VALUES (?, ?, ?, ?)',
            (name, hash_password('x'), role, name),
        )
    db.execute("INSERT INTO ingredients (id, name, unit, quantity, unit_price) VALUES (1, 'Mleko', 'L', 10, 4.2)")
    db.execute("INSERT INTO wastage (date, ingredient_id, quantity, reason, source) "
               "VALUES (date('now'), 1, 2, 'expired', 'manual')")
    db.commit()
    db.close()
    yield


def _client(user):
    c = TestClient(app, follow_redirects=False)
    c.cookies.set('session', sign_cookie(user))
    return c


def test_inventory_hides_prices_from_chef():
    html = _client('chef').get('/inventory').text
    assert 'Ціна/од.' not in html
    assert 'name="unit_price"' not in html
    assert 'name="price"' not in html


def test_inventory_shows_prices_to_admin():
    html = _client('admin').get('/inventory').text
    assert 'Ціна/од.' in html
    assert 'name="unit_price"' in html


def test_chef_cannot_change_ingredient_price():
    r = _client('chef').post('/ingredients/edit/1', data={'unit_price': '99'})
    assert r.status_code == 303
    db = get_db()
    assert db.execute('SELECT unit_price FROM ingredients WHERE id = 1').fetchone()[0] == 4.2
    db.close()


def test_admin_can_change_ingredient_price():
    _client('admin').post('/ingredients/edit/1', data={'unit_price': '5.5'})
    db = get_db()
    assert db.execute('SELECT unit_price FROM ingredients WHERE id = 1').fetchone()[0] == 5.5
    db.close()


def test_chef_delivery_price_is_ignored():
    _client('chef').post('/inventory/delivery', data={'ingredient_id': '1', 'quantity': '3', 'price': '50'})
    db = get_db()
    assert db.execute('SELECT price FROM deliveries').fetchone()[0] == 0
    db.close()


def test_wastage_page_hides_costs_from_chef():
    html = _client('chef').get('/wastage').text
    assert 'Вартість' not in html
    assert ' zł' not in html


def test_wastage_page_shows_costs_to_admin():
    html = _client('admin').get('/wastage').text
    assert 'Вартість списань' in html
    assert '8.40 zł' in html  # 2 L x 4.20


def test_wastage_csv_has_cost_column_only_for_admin():
    chef_csv = _client('chef').get('/export/wastage.csv').text
    admin_csv = _client('admin').get('/export/wastage.csv').text
    assert 'Вартість' not in chef_csv
    assert 'Вартість' in admin_csv


def test_chef_cannot_open_or_post_invoice_upload():
    c = _client('chef')
    assert c.get('/inventory/upload').status_code == 403
    assert c.post('/inventory/upload').status_code == 403
    assert c.post('/inventory/upload/confirm').status_code == 403


def test_admin_can_open_invoice_upload():
    assert _client('admin').get('/inventory/upload').status_code == 200


def test_chef_inventory_has_no_invoice_upload_link():
    assert '/inventory/upload' not in _client('chef').get('/inventory').text
    assert '/inventory/upload' in _client('admin').get('/inventory').text
