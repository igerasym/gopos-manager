"""Data-integrity & security guards: webhook auth/idempotency, day replace,
stock deduction bookkeeping, roles, schema on a fresh DB."""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import gopos_api as ga
from app.auth import can_access, sign_cookie, check_session_secret
from app.db import init_db, get_db


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    monkeypatch.setattr('app.db.DB_PATH', tmp_path / 'test.db')
    init_db()
    yield


def _it(name, paid, qty=1, item_id=1):
    return {'item_id': item_id, 'name': name, 'quantity': qty, 'status': 'ACTIVE',
            'sub_total_price': {'amount': paid}, 'total_price': {'amount': paid}}


def _order(oid, closed_at='2026-11-05T12:00:00', items=None):
    return {'id': oid, 'status': 'CLOSED', 'closed_at': closed_at,
            'total_price': {'amount': 20}, 'total_price_net': {'amount': 18.52},
            'items': items or [_it('Latte', 20)]}


# ─── business day ───

def test_business_date_cutoff():
    assert ga.business_date('2026-11-06T01:30:00') == '2026-11-05'
    assert ga.business_date('2026-11-06T05:00:00') == '2026-11-06'
    assert ga.business_date('2026-11-05T23:59:00') == '2026-11-05'
    # offset-aware timestamps are converted to Warsaw first (00:30Z = 01:30 CET)
    assert ga.business_date('2026-11-06T00:30:00+00:00') == '2026-11-05'


# ─── webhook ───

def _run_webhook(order):
    from app.routes import webhook
    with patch.object(ga, 'api_get', return_value={'data': order}), \
            patch.object(ga, 'get_item_tax_rates', return_value={1: 8}):
        webhook._process_order_webhook(str(order['id']))


def test_webhook_counts_an_order_once():
    order = _order(123)
    _run_webhook(order)
    _run_webhook(order)  # GoPOS retry / replay
    r = get_db().execute("SELECT quantity, total_money FROM sales WHERE product_name='Latte'").fetchone()
    assert (r['quantity'], r['total_money']) == (1, 20)


def test_webhook_late_night_order_goes_to_previous_day():
    _run_webhook(_order(7, closed_at='2026-11-06T02:10:00'))
    assert get_db().execute("SELECT date FROM sales").fetchone()['date'] == '2026-11-05'


def test_webhook_token_required_when_configured(monkeypatch):
    from app.main import app
    monkeypatch.setenv('GOPOS_WEBHOOK_TOKEN', 's3cret')
    c = TestClient(app)
    body = {'type': 'ORDER', 'resource_id': '1', 'event_type': 'UPDATED'}
    with patch('app.routes.webhook._process_order_webhook'):
        assert c.post('/api/webhook/gopos', json=body).status_code == 403
        assert c.post('/api/webhook/gopos?token=wrong', json=body).status_code == 403
        assert c.post('/api/webhook/gopos?token=s3cret', json=body).status_code == 200


def test_webhook_rejects_non_numeric_order_id(monkeypatch):
    from app.main import app
    monkeypatch.delenv('GOPOS_WEBHOOK_TOKEN', raising=False)
    c = TestClient(app)
    with patch('app.routes.webhook._process_order_webhook') as p:
        r = c.post('/api/webhook/gopos', json={'type': 'ORDER', 'resource_id': '../items'})
    assert r.status_code == 400 and not p.called


# ─── full sync ───

def _sync(orders):
    with patch.object(ga, 'api_get', side_effect=[{'data': orders}, {'data': []}]), \
            patch.object(ga, 'get_item_tax_rates', return_value={1: 8}):
        return ga.sync_orders_for_date('2026-11-05')


def test_full_sync_replaces_the_day():
    db = get_db()
    # stale row from a webhook for an order later voided
    db.execute("INSERT INTO sales (date, product_name, quantity, total_money) VALUES ('2026-11-05','Voided',1,30)")
    db.commit()
    _sync([_order(1)])
    names = [r[0] for r in get_db().execute("SELECT product_name FROM sales WHERE date='2026-11-05'")]
    assert names == ['Latte']


def test_webhook_after_full_sync_does_not_double_count():
    order = _order(42)
    _sync([order])
    _run_webhook(order)  # late webhook for an order the sync already counted
    assert get_db().execute("SELECT quantity FROM sales WHERE product_name='Latte'").fetchone()[0] == 1


def test_empty_api_answer_keeps_existing_day():
    _sync([_order(1)])
    assert _sync([]) == 0
    assert get_db().execute("SELECT COUNT(*) FROM sales WHERE date='2026-11-05'").fetchone()[0] == 1


# ─── stock deduction ───

def test_deduction_records_only_what_was_taken():
    from app.gopos_sync import deduct_inventory
    db = get_db()
    db.execute("INSERT INTO ingredients (id, name, unit, quantity) VALUES (1, 'Milk', 'ml', 100)")
    db.execute("INSERT INTO recipes (product_name, ingredient_id, amount) VALUES ('Latte', 1, 200)")
    db.execute("INSERT INTO sales (date, product_name, quantity) VALUES ('2026-11-05', 'Latte', 1)")
    db.commit()
    deduct_inventory('2026-11-05')   # wants 200, only 100 on hand
    deduct_inventory('2026-11-05')   # re-run must not inflate stock
    db = get_db()
    assert db.execute("SELECT quantity FROM ingredients WHERE id=1").fetchone()[0] == 0
    assert db.execute("SELECT SUM(amount) FROM inventory_deductions").fetchone()[0] == 100
    # a later restock + re-run of the old day doesn't add back more than was taken
    db.execute("UPDATE ingredients SET quantity = quantity + 500 WHERE id=1")
    db.commit()
    deduct_inventory('2026-11-05')
    assert get_db().execute("SELECT quantity FROM ingredients WHERE id=1").fetchone()[0] == 400


# ─── auth ───

def test_unknown_role_gets_no_access():
    assert not can_access({'role': 'staff'}, '/')
    assert not can_access({'role': 'staff'}, '/users')
    assert can_access({'role': 'admin'}, '/users')
    assert can_access({'role': 'barista'}, '/inventory')
    assert not can_access({'role': 'barista'}, '/expenses')


def test_cannot_assign_unknown_role():
    from app.main import app
    db = get_db()
    db.execute("INSERT INTO users (username, password_hash, role) VALUES ('admin','x:y','admin')")
    db.execute("INSERT INTO users (id, username, password_hash, role) VALUES (5,'b','x:y','barista')")
    db.commit()
    TestClient(app).post('/users/role/5', data={'role': 'superuser'},
                         cookies={'session': sign_cookie('admin')}, follow_redirects=False)
    assert get_db().execute("SELECT role FROM users WHERE id=5").fetchone()[0] == 'barista'


@pytest.mark.parametrize('value', ['', 'the-frame-cafe-secret-2026', 'change-me-in-production'])
def test_default_session_secret_refused(monkeypatch, value):
    monkeypatch.setenv('SESSION_SECRET', value)
    with pytest.raises(RuntimeError):
        check_session_secret()


# ─── schema ───

def test_fresh_db_has_columns_code_relies_on():
    db = get_db()
    cols = lambda t: {r[1] for r in db.execute(f'PRAGMA table_info({t})')}
    assert 'kind' in cols('ingredients')
    assert 'supplier_id' in cols('deliveries')
    assert {'gopos_item_id', 'gopos_price', 'gopos_group_id'} <= cols('pos_products')
    init_db()  # idempotent on an existing DB


def test_api_get_all_starts_at_page_zero_and_stops_before_50():
    calls = []
    def fake(path, params):
        calls.append(params['page'])
        return {'data': [{'id': params['page']}]}  # never-ending endpoint
    with patch.object(ga, 'api_get', side_effect=fake):
        rows = ga.api_get_all('items')
    assert calls[0] == 0 and max(calls) == 49 and len(rows) == 50
