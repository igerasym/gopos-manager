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


def test_webhook_ignores_non_numeric_order_id_with_200(monkeypatch):
    """Never 4xx on a payload we can't use: GoPOS blocks the webhook on 400/404/422."""
    from app.main import app
    monkeypatch.delenv('GOPOS_WEBHOOK_TOKEN', raising=False)
    c = TestClient(app)
    with patch('app.routes.webhook._process_order_webhook') as p:
        r = c.post('/api/webhook/gopos', json={'type': 'ORDER', 'resource_id': '../items'})
    assert r.status_code == 200 and r.json()['status'] == 'ignored' and not p.called


def test_webhook_never_answers_4xx_to_unusable_bodies(monkeypatch):
    from app.main import app
    monkeypatch.delenv('GOPOS_WEBHOOK_TOKEN', raising=False)
    c = TestClient(app)
    with patch('app.routes.webhook._process_order_webhook') as p, \
            patch('app.routes.webhook._sync_products_background') as s:
        for kwargs in ({'content': b''}, {'content': b'not json'}, {'json': [1, 2]},
                       {'json': {'type': 'PING'}}, {'json': {}}):
            r = c.post('/api/webhook/gopos', **kwargs)
            assert r.status_code == 200, kwargs
    assert not p.called and not s.called


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


UID = 'b145766c-92ae-4a5e-948a-9a13cd98dcc2'


def test_webhook_accepts_order_uid_and_dedupes_with_full_sync():
    """GoPOS names the order by its uid; the sync stores the numeric id. Same order, counted once."""
    from app.main import app
    order = dict(_order(44607), uid=UID)
    _sync([order])
    from app.routes import webhook
    with patch.object(ga, 'api_get', return_value={'data': order}) as api, \
            patch.object(ga, 'get_item_tax_rates', return_value={1: 8}):
        webhook._process_order_webhook(UID)
    api.assert_called_once()
    assert api.call_args.args[0] == f'orders/{UID}'
    assert get_db().execute("SELECT quantity FROM sales WHERE product_name='Latte'").fetchone()[0] == 1
    c = TestClient(app)
    with patch('app.routes.webhook._process_order_webhook') as p:
        r = c.post('/api/webhook/gopos', json={'type': 'ORDER', 'resource_id': UID, 'event_type': 'CLOSED'})
    assert r.status_code == 200 and r.json()['status'] == 'ok' and p.call_args.args[0] == UID


def test_webhook_by_uid_then_sync_does_not_double_count():
    order = dict(_order(44608), uid=UID)
    from app.routes import webhook
    with patch.object(ga, 'api_get', return_value={'data': order}), \
            patch.object(ga, 'get_item_tax_rates', return_value={1: 8}):
        webhook._process_order_webhook(UID)
    assert get_db().execute("SELECT order_id FROM processed_orders").fetchone()[0] == '44608'
    _sync([order])
    assert get_db().execute("SELECT quantity FROM sales WHERE product_name='Latte'").fetchone()[0] == 1


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


# ─── sub-recipe cost cascade ───

def test_sub_recipe_cost_updates_when_ingredient_price_changes():
    """Updating a raw ingredient price must cascade to sub-recipes that use it."""
    from app.services.recipes import recalc_sub_recipes_for_ingredient
    db = get_db()
    # Set up: ingredient Молоко priced at 2.00 per L
    db.execute("INSERT INTO ingredients (id, name, unit, quantity, unit_price) VALUES (1, 'Молоко', 'L', 10, 2.0)")
    # Sub-recipe: Вершки (yield 0.5 L from 1 L молока)
    db.execute("INSERT INTO ingredients (id, name, unit, quantity, unit_price) VALUES (2, 'Вершки', 'L', 0, 0)")
    db.execute("INSERT INTO sub_recipes (id, ingredient_id, yield_amount, yield_unit) VALUES (1, 2, 0.5, 'L')")
    db.execute("INSERT INTO sub_recipe_items (sub_recipe_id, ingredient_id, amount) VALUES (1, 1, 1.0)")
    db.commit()
    # Recalc initial cost: 1.0 L × 2.0 / 0.5 yield = 4.0 per L
    recalc_sub_recipes_for_ingredient(db, 1)
    db.commit()
    cost_before = db.execute("SELECT unit_price FROM ingredients WHERE id=2").fetchone()['unit_price']
    assert cost_before == 4.0

    # Supplier raises price to 3.00
    db.execute("UPDATE ingredients SET unit_price = 3.0 WHERE id = 1")
    recalc_sub_recipes_for_ingredient(db, 1)
    db.commit()
    cost_after = db.execute("SELECT unit_price FROM ingredients WHERE id=2").fetchone()['unit_price']
    # Expected: 1.0 × 3.0 / 0.5 = 6.0
    assert cost_after == 6.0


def test_sub_recipe_not_affected_when_unrelated_ingredient_changes():
    """Changing price of an ingredient not in a sub-recipe must not alter it."""
    from app.services.recipes import recalc_sub_recipes_for_ingredient
    db = get_db()
    db.execute("INSERT INTO ingredients (id, name, unit, quantity, unit_price) VALUES (1, 'Молоко', 'L', 10, 2.0)")
    db.execute("INSERT INTO ingredients (id, name, unit, quantity, unit_price) VALUES (2, 'Вершки', 'L', 0, 4.0)")
    db.execute("INSERT INTO ingredients (id, name, unit, quantity, unit_price) VALUES (3, 'Цукор', 'kg', 5, 1.0)")
    db.execute("INSERT INTO sub_recipes (id, ingredient_id, yield_amount, yield_unit) VALUES (1, 2, 0.5, 'L')")
    db.execute("INSERT INTO sub_recipe_items (sub_recipe_id, ingredient_id, amount) VALUES (1, 1, 1.0)")
    db.commit()
    # Update unrelated ingredient
    db.execute("UPDATE ingredients SET unit_price = 5.0 WHERE id=3")
    recalc_sub_recipes_for_ingredient(db, 3)
    db.commit()
    cost = db.execute("SELECT unit_price FROM ingredients WHERE id=2").fetchone()['unit_price']
    assert cost == 4.0  # unchanged


# ─── stock count clears deductions ───

def test_stock_count_clears_todays_deductions():
    """After saving a stock count the deductions for today must be cleared
    so the next 04:30 re-sync deducts from the counted (real) stock."""
    from app.gopos_api import warsaw_today
    from app.gopos_sync import deduct_inventory
    from app.main import app
    today = warsaw_today()  # same business day the route uses
    db = get_db()
    db.execute("INSERT INTO users (id, username, password_hash, role) VALUES (1,'admin','x:y','admin')")
    db.execute("INSERT INTO ingredients (id, name, unit, quantity, min_quantity) VALUES (1,'Молоко','L',5,0)")
    db.execute("INSERT INTO recipes (product_name, ingredient_id, amount) VALUES ('Latte', 1, 0.3)")
    db.execute("INSERT INTO sales (date, product_name, quantity) VALUES (?, 'Latte', 2)", (today,))
    db.commit()
    deduct_inventory(today)
    before = db.execute("SELECT COUNT(*) FROM inventory_deductions WHERE date=?", (today,)).fetchone()[0]
    assert before == 1

    client = TestClient(app)
    from app.auth import sign_cookie
    # POST the stock count with the actual quantity
    r = client.post(
        '/inventory/count',
        data={'qty_1': '4.4'},
        cookies={'session': sign_cookie('admin')},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)
    after = db.execute("SELECT COUNT(*) FROM inventory_deductions WHERE date=?", (today,)).fetchone()[0]
    assert after == 0, "deductions for today must be wiped after a stock count"
    stock = db.execute("SELECT quantity FROM ingredients WHERE id=1").fetchone()['quantity']
    assert stock == 4.4


def test_register_webhook_never_logs_the_token(caplog):
    import logging
    from unittest.mock import MagicMock
    caplog.set_level(logging.INFO)
    get = MagicMock(status_code=200, json=lambda: {'data': []})
    post = MagicMock(status_code=500, text='bad url http://h/api/webhook/gopos?token=TOPSECRET')
    with patch.object(ga, 'get_token', return_value='t'), patch.object(ga.requests, 'get', return_value=get), \
            patch.object(ga.requests, 'post', return_value=post):
        ga.register_webhook('http://h/api/webhook/gopos?token=TOPSECRET')
    post.status_code = 201
    post.json = lambda: {}
    with patch.object(ga, 'get_token', return_value='t'), patch.object(ga.requests, 'get', return_value=get), \
            patch.object(ga.requests, 'post', return_value=post):
        ga.register_webhook('http://h/api/webhook/gopos?token=TOPSECRET')
    assert 'TOPSECRET' not in caplog.text and 'token=***' in caplog.text


def test_day_sync_reads_gopos_orders_once_for_sales_and_writeoffs():
    """One fetch with promotions serves both the sales sync and the write-off log."""
    wo = dict(_order(91), promotions=[{'name': 'write-off'}],
              items=[{**_it('Croissant', 0), 'sub_total_price': {'amount': 12}}])
    calls = []

    def fake_fetch(day, include):
        calls.append((day, include))
        return [_order(90), wo]

    with patch.object(ga, 'fetch_closed_orders', side_effect=fake_fetch), \
            patch.object(ga, 'sync_products'), patch.object(ga, 'get_item_tax_rates', return_value={1: 8}):
        ga.sync_date('2026-11-05')
    assert calls == [('2026-11-05', 'items,items.product,promotions')]
    db = get_db()
    assert db.execute("SELECT quantity FROM sales WHERE product_name='Latte'").fetchone()[0] == 1
    assert db.execute("SELECT quantity FROM wastage WHERE product_name='Croissant' AND source='gopos'").fetchone()[0] == 1


def test_single_steps_still_fetch_on_their_own():
    with patch.object(ga, 'fetch_closed_orders', return_value=[]) as f:
        ga.sync_writeoffs_for_date('2026-11-05')
    assert f.call_args.args == ('2026-11-05', 'items,items.product,promotions')
