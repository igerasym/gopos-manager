"""Склад redesign: order list, decimals with a comma, stock-count threshold and loss, wastage summary."""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.db import init_db, get_db
from app.auth import hash_password, sign_cookie
from app.routes.inventory import round_order, build_order_list, order_text, parse_decimal, stock_status
from app.routes.stock_count import diff_status, item_loss
from app.routes.wastage import shift_month, month_label, summarize

TODAY = '2026-11-25'


@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    monkeypatch.setattr('app.db.DB_PATH', tmp_path / 'test.db')
    for mod in ('inventory', 'stock_count', 'wastage'):
        monkeypatch.setattr(f'app.routes.{mod}.warsaw_today', lambda: TODAY)
    init_db()
    db = get_db()
    for name, role in (('admin', 'admin'), ('chef', 'chef')):
        db.execute('INSERT INTO users (username, password_hash, role, display_name) VALUES (?, ?, ?, ?)',
                   (name, hash_password('x'), role, name))
    db.execute("INSERT INTO suppliers (id, name) VALUES (1, 'Makro'), (2, 'Kawa Hurt')")
    db.execute("INSERT INTO ingredients (id, name, unit, quantity, min_quantity, unit_price, supplier_id) VALUES "
               "(1, 'Mleko', 'L', 6, 20, 4, 1),"
               "(2, 'Kawa', 'kg', 1.4, 5, 90, 2),"
               "(3, 'Kubek', 'szt', 180, 300, 0.5, 1),"
               "(4, 'Cukier', 'kg', 9, 5, 4, 1),"
               "(5, 'Sól', 'kg', 0, 1, 0, NULL)")
    db.commit()
    db.close()
    yield


def _client(user='admin'):
    c = TestClient(app, follow_redirects=False)
    c.cookies.set('session', sign_cookie(user))
    return c


# ── order list ──

def test_round_order_steps():
    assert round_order(0.33, 'kg') == 0.4
    assert round_order(3.6, 'kg') == 4.0
    assert round_order(34, 'L') == 34
    assert round_order(7, 'szt') == 7
    assert round_order(23, 'szt') == 25
    assert round_order(420, 'szt') == 450
    assert round_order(0, 'kg') == 0


def test_order_list_refills_to_twice_the_minimum_grouped_by_supplier():
    from app.services.inventory import get_all_ingredients
    groups = build_order_list(get_all_ingredients())
    by = {g['supplier']: g for g in groups}
    assert set(by) == {'Makro', 'Kawa Hurt', 'Без постачальника'}
    assert groups[-1]['supplier'] == 'Без постачальника'
    mleko = next(ln for ln in by['Makro']['lines'] if ln['name'] == 'Mleko')
    assert mleko['suggested'] == 34                # 2×20 − 6
    assert mleko['cost'] == pytest.approx(136)     # 34 × 4
    kubek = next(ln for ln in by['Makro']['lines'] if ln['name'] == 'Kubek')
    assert kubek['suggested'] == 450               # 600 − 180 = 420 → 450
    assert by['Kawa Hurt']['lines'][0]['suggested'] == 9.0  # 10 − 1.4 = 8.6 → 9
    assert 'Cukier' not in [ln['name'] for g in groups for ln in g['lines']]  # above minimum
    sol = by['Без постачальника']
    assert sol['lines'][0]['cost'] is None and sol['unpriced'] == 1 and sol['total'] == 0
    assert order_text(by['Kawa Hurt']) == 'Kawa Hurt:\n- Kawa: 9\u00a0кг'


def test_stock_status():
    assert stock_status({'quantity': 0, 'min_quantity': 1}) == 'out'
    assert stock_status({'quantity': 1, 'min_quantity': 1}) == 'low'
    assert stock_status({'quantity': 2, 'min_quantity': 1}) == 'ok'
    assert stock_status({'quantity': 0, 'min_quantity': 0}) == 'ok'


def test_inventory_defaults_to_order_tab_for_admin_when_something_is_low():
    html = _client().get('/inventory').text
    assert 'Що треба замовити сьогодні' in html
    assert 'Скопіювати список' in html and 'Kawa Hurt' in html
    assert '/inventory/upload' in html


def test_chef_order_tab_has_no_money():
    html = _client('chef').get('/inventory?tab=order').text
    assert 'Що треба замовити сьогодні' in html
    assert 'zł' not in html


def test_chef_stock_tab_default_and_no_price_field():
    html = _client('chef').get('/inventory').text
    assert 'Пошук інгредієнта' in html
    assert 'name="unit_price"' not in html and 'name="price"' not in html


def test_stock_filter_low_and_supplier():
    c = _client()
    low = c.get('/inventory?tab=stock&f=low').text
    assert 'data-name="cukier"' not in low and 'data-name="mleko"' in low
    makro = c.get('/inventory?tab=stock&supplier=Makro').text
    assert 'data-name="kawa"' not in makro and 'data-name="mleko"' in makro


def test_deliveries_tab_lists_recent_deliveries():
    c = _client()
    c.post('/inventory/delivery', data={'ingredient_id': '1', 'quantity': '12,5', 'price': '48,75', 'supplier_id': '1'})
    html = c.get('/inventory?tab=deliveries').text
    assert '+12,5' in html and '48,75' in html


# ── decimals with a comma ──

def test_parse_decimal():
    assert parse_decimal('1,5') == 1.5
    assert parse_decimal(' 2.25 ') == 2.25
    assert parse_decimal('') is None
    assert parse_decimal(None, 0) == 0


def test_delivery_accepts_comma_and_updates_stock():
    r = _client().post('/inventory/delivery', data={'ingredient_id': '2', 'quantity': '2,6', 'price': '200,50', 'supplier_id': ''})
    assert r.status_code == 303
    db = get_db()
    assert db.execute('SELECT quantity FROM ingredients WHERE id = 2').fetchone()[0] == pytest.approx(4.0)
    row = db.execute('SELECT price, date, supplier_id FROM deliveries').fetchone()
    db.close()
    assert row['price'] == 200.5 and row['date'] == TODAY and row['supplier_id'] is None


def test_edit_ingredient_accepts_comma_and_keeps_supplier_clearable():
    c = _client()
    c.post('/ingredients/edit/1', data={'quantity': '7,5', 'min_quantity': '10', 'unit_price': '3,9', 'supplier_id': ''})
    db = get_db()
    row = db.execute('SELECT quantity, min_quantity, unit_price, supplier_id FROM ingredients WHERE id = 1').fetchone()
    db.close()
    assert (row['quantity'], row['min_quantity'], row['unit_price'], row['supplier_id']) == (7.5, 10, 3.9, None)


def test_edit_ingredient_redirect_stays_local():
    r = _client().post('/ingredients/edit/1', data={'quantity': '1', 'redirect': 'https://evil.example'})
    assert r.headers['location'] == '/inventory'


def test_add_ingredient_with_price_only_for_admin():
    _client().post('/inventory/add', data={'name': 'Masło', 'unit': 'kg', 'quantity': '1,5', 'min_quantity': '2', 'unit_price': '38', 'supplier_id': '1'})
    _client('chef').post('/inventory/add', data={'name': 'Mąka', 'unit': 'kg', 'unit_price': '9'})
    db = get_db()
    maslo = db.execute("SELECT quantity, unit_price, supplier_id FROM ingredients WHERE name = 'Masło'").fetchone()
    maka = db.execute("SELECT unit_price FROM ingredients WHERE name = 'Mąka'").fetchone()
    db.close()
    assert (maslo['quantity'], maslo['unit_price'], maslo['supplier_id']) == (1.5, 38, 1)
    assert maka['unit_price'] == 0


# ── stock count ──

def test_diff_threshold_and_loss():
    assert diff_status(-0.01) == 'match'
    assert diff_status(-0.05) == 'short'
    assert diff_status(0.2) == 'over'
    assert item_loss(-0.5, 10) == 5
    assert item_loss(-0.01, 10) == 0
    assert item_loss(0.5, 10) == 0


def test_stock_count_accepts_comma_and_ignores_rounding_noise():
    c = _client()
    r = c.post('/inventory/count', data={'qty_1': '5,99', 'qty_2': '1,0', 'qty_4': ''})
    assert r.status_code == 303
    count_id = r.headers['location'].split('/')[-1].split('?')[0]
    db = get_db()
    items = {row['ingredient_id']: row['difference'] for row in db.execute('SELECT * FROM stock_count_items')}
    assert set(items) == {1, 2}                               # empty field = not counted
    assert db.execute('SELECT quantity FROM ingredients WHERE id = 2').fetchone()[0] == 1.0
    db.close()
    html = c.get(f'/inventory/count/{count_id}').text
    # Mleko −0,01 L is a match; Kawa −0,4 kg × 90 = 36 zł lost
    assert html.count('Не вистачило</span>') >= 1
    assert '36,00' in html
    hist = c.get('/inventory/history').text
    assert 'не вистачило 1 з 2' in hist and '36,00' in hist


def test_history_warns_when_last_count_is_old():
    db = get_db()
    db.execute("INSERT INTO stock_counts (date, time, user_id) VALUES ('2026-11-13', '21:40', 1)")
    db.commit()
    db.close()
    html = _client().get('/inventory/history').text
    assert 'Остання інвентаризація 12 днів тому' in html


def test_chef_count_detail_has_no_money():
    c = _client('chef')
    r = c.post('/inventory/count', data={'qty_2': '1'})
    html = c.get(r.headers['location']).text
    assert 'zł' not in html and 'Втрати' not in html


# ── wastage ──

def test_month_helpers():
    assert shift_month('2026-01', -1) == '2025-12'
    assert shift_month('2026-12', 1) == '2027-01'
    assert month_label('2026-11') == 'листопад 2026'


def test_summarize_keeps_unknown_cost_out_of_total_and_top():
    rows = [
        {'label': 'Latte', 'quantity': 2, 'unit': 'szt', 'cost': 5.0, 'cost_known': True, 'source': 'gopos', 'reason': 'write-off'},
        {'label': 'Brownie', 'quantity': 3, 'unit': 'szt', 'cost': 0, 'cost_known': False, 'source': 'gopos', 'reason': 'write-off'},
        {'label': 'Mleko', 'quantity': 1, 'unit': 'L', 'cost': 4.0, 'cost_known': True, 'source': 'manual', 'reason': 'expired'},
    ]
    s = summarize(rows)
    assert s['total_cost'] == 9.0
    assert s['unpriced'] == ['Brownie']
    assert [n for n, _ in s['top_losers']] == ['Latte', 'Mleko']
    reasons = dict(s['by_reason'])
    assert reasons['write-off']['count'] == 2 and reasons['write-off']['cost'] == 5.0


def test_wastage_add_accepts_comma_and_rejects_writeoff_reason():
    r = _client('chef').post('/wastage/add', data={'ingredient_id': '1', 'quantity': '1,5', 'reason': 'write-off', 'date': TODAY})
    assert r.status_code == 303 and 'saved=add' in r.headers['location']
    db = get_db()
    row = db.execute('SELECT quantity, reason, source FROM wastage').fetchone()
    stock = db.execute('SELECT quantity FROM ingredients WHERE id = 1').fetchone()[0]
    db.close()
    assert (row['quantity'], row['reason'], row['source']) == (1.5, 'other', 'manual')
    assert stock == 4.5


def test_wastage_page_month_switch_and_share_of_revenue():
    db = get_db()
    db.execute("INSERT INTO sales (date, product_name, quantity, total_money) VALUES ('2026-11-10', 'Latte', 100, 1000)")
    db.execute("INSERT INTO wastage (date, ingredient_id, quantity, reason, source) VALUES ('2026-11-10', 1, 5, 'expired', 'manual')")
    db.execute("INSERT INTO wastage (date, ingredient_id, quantity, reason, source) VALUES ('2026-10-10', 1, 10, 'expired', 'manual')")
    db.commit()
    db.close()
    html = _client().get('/wastage').text
    assert 'листопад 2026' in html and 'month=2026-10' in html
    assert 'Це 2% виручки' in html                  # 20 zł of 1000 zł
    assert 'менше, ніж за жовтень 2026' in html      # 20 vs 40
    assert 'month=2026-12' not in html               # no future months
    assert 'листопад 2026' in _client().get('/wastage?month=bad').text
