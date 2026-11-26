"""'Гроші': month result, missing recurring costs, invoice states, sheets' endpoints, users, upload preview."""
import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password, sign_cookie
from app.db import get_db, init_db
from app.main import app
from app.routes import expenses as money

TODAY = '2026-11-25'


@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    monkeypatch.setattr('app.db.DB_PATH', tmp_path / 'test.db')
    monkeypatch.setattr('app.routes.expenses.warsaw_today', lambda: TODAY)
    init_db()
    db = get_db()
    for name, role in (('admin', 'admin'), ('chef', 'chef')):
        db.execute('INSERT INTO users (username, password_hash, role, display_name) VALUES (?, ?, ?, ?)',
                   (name, hash_password('x'), role, name.title()))
    # one product with a recipe (cost 4 zł), one without
    db.execute("INSERT INTO ingredients (id, name, unit, unit_price) VALUES (1, 'Mleko', 'L', 4)")
    db.execute("INSERT INTO recipes (product_name, ingredient_id, amount) VALUES ('Latte', 1, 1)")
    for d, qty in (('2026-11-03', 100), ('2026-10-03', 80)):
        db.execute('INSERT INTO sales (date, product_name, quantity, total_money, net_total, discount) VALUES (?,?,?,?,?,?)',
                   (d, 'Latte', qty, qty * 15, qty * 14, 10))
    db.execute("INSERT INTO sales (date, product_name, quantity, total_money, net_total) VALUES ('2026-11-04', 'Brownie', 10, 100, 90)")
    rows = [
        ('Czynsz', 'Оренда', 5000, '2026-10', 1), ('Wynagrodzenia', 'Зарплати', 8000, '2026-10', 1),
        ('ZUS', 'Податки і ZUS', 1500, '2026-10', 1), ('Makro', 'Продукти', 900, '2026-10', 0),
        ('Czynsz', 'Оренда', 5000, '2026-11', 1), ('Makro', 'Продукти', 700, '2026-11', 0),
    ]
    db.executemany('INSERT INTO expenses (name, category, amount, month, recurring) VALUES (?,?,?,?,?)', rows)
    db.commit()
    db.close()
    yield


def _client(user='admin'):
    c = TestClient(app, follow_redirects=False)
    c.cookies.set('session', sign_cookie(user))
    return c


def test_month_result_profit_is_revenue_minus_all_expenses():
    db = get_db()
    r = money.month_result(db, '2026-11', TODAY)
    db.close()
    assert r['revenue'] == 1600            # 1500 Latte + 100 Brownie
    assert r['cogs'] == 400                # 100 Latte x 4 zł; Brownie has no recipe
    assert r['other'] == 5000              # everything except product purchases
    assert r['food_purchases'] == 700
    # recipe cost covers only part of the menu, so profit uses what was really paid
    assert r['profit'] == 1600 - 700 - 5000
    assert r['coverage_incomplete'] and r['without_recipe'] == 1


def test_missing_recurring_and_forecast():
    db = get_db()
    r = money.month_result(db, '2026-11', TODAY)
    db.close()
    assert [m['name'] for m in r['missing']] == ['Wynagrodzenia', 'ZUS']
    assert r['missing_total'] == 9500
    assert r['forecast'] == r['profit'] - 9500
    assert r['missing_categories'] == ['Зарплати', 'Податки і ZUS']
    # same days of the previous month
    assert r['prev_revenue'] == 1200 and r['prev_partial']


def test_copy_recurring_is_idempotent_and_keeps_flag():
    c = _client()
    r = c.post('/expenses/copy-recurring', data={'month': '2026-11'})
    assert r.status_code == 303 and 'done=copied' in r.headers['location']
    c.post('/expenses/copy-recurring', data={'month': '2026-11'})
    db = get_db()
    rows = db.execute("SELECT name, amount, recurring FROM expenses WHERE month = '2026-11' AND name IN ('Wynagrodzenia', 'ZUS')").fetchall()
    db.close()
    assert sorted((r['name'], r['amount'], r['recurring']) for r in rows) == [('Wynagrodzenia', 8000, 1), ('ZUS', 1500, 1)]


def test_dismiss_missing():
    _client().post('/expenses/dismiss-missing', data={'name': 'ZUS', 'month': '2026-11'})
    db = get_db()
    assert [m['name'] for m in money.missing_recurring(db, '2026-11')] == ['Wynagrodzenia']
    db.close()


def test_add_expense_comma_decimal_and_recurring():
    r = _client().post('/expenses/add', data={'name': ' Prąd ', 'category': 'Комунальні', 'amount': '1 234,50',
                                               'month': '2026-11', 'recurring': '1'})
    assert r.headers['location'] == '/expenses?month=2026-11&done=added'
    db = get_db()
    row = db.execute("SELECT * FROM expenses WHERE name = 'Prąd'").fetchone()
    db.close()
    assert row['amount'] == 1234.5 and row['recurring'] == 1


def test_update_and_delete_expense():
    c = _client()
    db = get_db()
    eid = db.execute("SELECT id FROM expenses WHERE month = '2026-11' AND name = 'Czynsz'").fetchone()[0]
    db.close()
    r = c.post(f'/expenses/update/{eid}', data={'amount': '5100,5', 'name': 'Czynsz lokal', 'category': 'Оренда'})
    assert 'done=updated' in r.headers['location']
    db = get_db()
    assert tuple(db.execute('SELECT name, amount FROM expenses WHERE id = ?', (eid,)).fetchone()) == ('Czynsz lokal', 5100.5)
    db.close()
    assert 'done=deleted' in c.post(f'/expenses/delete/{eid}').headers['location']
    db = get_db()
    assert db.execute('SELECT 1 FROM expenses WHERE id = ?', (eid,)).fetchone() is None
    db.close()


def _invoice(**kw):
    vals = {'file_id': 'f1', 'file_name': 'skan.pdf', 'month': '2026-11', 'vendor': '', 'total': 0,
            'parse_status': 'error', 'expense_status': 'pending'}
    vals.update(kw)
    db = get_db()
    iid = db.execute('INSERT INTO parsed_invoices (file_id, file_name, month, vendor, total, parse_status, expense_status) '
                     'VALUES (:file_id, :file_name, :month, :vendor, :total, :parse_status, :expense_status)', vals).lastrowid
    db.commit()
    db.close()
    return iid


@pytest.mark.parametrize('ps,es,total,state', [
    ('error', 'pending', 0, 'failed'), ('parsed', 'pending', 0, 'failed'), ('parsed', 'pending', 10, 'waiting'),
    ('pending', 'pending', 0, 'new'), ('processing', 'pending', 0, 'processing'), ('parsed', 'added', 10, 'added'),
    ('parsed', 'skipped', 10, 'skipped'), ('duplicate', 'pending', 0, 'duplicate'),
])
def test_invoice_state(ps, es, total, state):
    assert money.invoice_state({'parse_status': ps, 'expense_status': es, 'total': total}) == state


def test_manual_invoice_adds_expense_once():
    iid = _invoice()
    c = _client()
    r = c.post(f'/expenses/manual-invoice/{iid}', data={'vendor': 'Hala', 'total': '320,40', 'category': 'Продукти'})
    assert 'done=manual' in r.headers['location']
    c.post(f'/expenses/manual-invoice/{iid}', data={'vendor': 'Hala', 'total': '320,40', 'category': 'Продукти'})
    db = get_db()
    inv = db.execute('SELECT * FROM parsed_invoices WHERE id = ?', (iid,)).fetchone()
    n = db.execute("SELECT COUNT(*) FROM expenses WHERE name = 'Hala' AND amount = 320.4").fetchone()[0]
    db.close()
    assert inv['expense_status'] == 'added' and inv['vendor'] == 'Hala' and n == 1


def test_page_renders_month_result_and_invoice_list():
    _invoice()
    _invoice(file_id='f2', vendor='Makro', total=250, parse_status='parsed')
    html = _client().get('/expenses').text
    assert 'Результат листопада' in html and 'type="month"' not in html
    assert 'Ще не внесено' in html and 'Скопіювати з жовтня' in html
    assert 'Не розпізнано' in html and 'Ввести вручну' in html and 'Чекає' in html
    assert 'Собівартість за рецептами неповна' in html
    assert 'Рентабельність' not in html and '✕' not in html
    assert 'href="/expenses?month=2026-10"' in html


def test_empty_month_offers_copy():
    html = _client().get('/expenses?month=2026-12').text  # future-ish month with nothing entered
    assert 'Скопіювати постійні витрати з листопада' in html


def test_untrusted_names_are_escaped():
    _invoice(file_id='f3', vendor='<script>x</script>', total=10, parse_status='parsed')
    html = _client().get('/expenses').text
    assert '<script>x</script>' not in html


def test_money_pages_are_admin_only():
    c = _client('chef')
    for url in ('/expenses', '/suppliers/history', '/users'):
        r = c.get(url)
        assert r.status_code in (302, 303, 403), url
        assert 'zł' not in r.text


def test_users_page_rows_and_confirmed_role_change():
    html = _client().get('/users').text
    assert 'Власник' in html and 'Кухар' in html and 'Що бачить кожна роль' in html
    assert 'data-confirm="Змінити роль для Chef?' in html
    r = _client().post('/users/password/2', data={'password': 'new'})
    assert r.headers['location'] == '/users?done=password'


def test_supplier_history_groups_by_month():
    _invoice(file_id='a', vendor='Makro', total=100, parse_status='parsed', expense_status='added')
    _invoice(file_id='b', vendor='Makro', total=50, parse_status='parsed', expense_status='added', month='2026-10')
    html = _client().get('/suppliers/history?vendor=Makro').text
    assert 'Листопад' in html and 'Жовтень' in html and '150' in html


def test_upload_preview_and_confirm_with_comma_decimals(monkeypatch):
    monkeypatch.setattr('app.invoice_parser.extract_text_from_pdf', lambda b: 'text')
    monkeypatch.setattr('app.invoice_parser.parse_invoice_with_llm', lambda t, ings: {'success': True, 'items': [
        {'invoice_name': 'MLEKO 3,2% 1L', 'ingredient_id': 1, 'ingredient_name': 'Mleko', 'is_new': False,
         'quantity': 12, 'unit': 'L', 'price_brutto': 50.4, 'price_per_unit_brutto': 4.2}]})
    c = _client()
    html = c.post('/inventory/upload', files={'file': ('f.pdf', b'%PDF', 'application/pdf')}).text
    assert 'name="item_count" value="1"' in html and 'Знайдено' in html
    c.post('/inventory/upload/confirm', data={'item_count': '1', 'ingredient_id_0': '1', 'ingredient_name_0': 'Mleko',
                                              'quantity_0': '12', 'unit_0': 'L', 'price_brutto_0': '50,40',
                                              'price_per_unit_0': '4,2', 'is_new_0': 'false'})
    db = get_db()
    assert db.execute('SELECT unit_price FROM ingredients WHERE id = 1').fetchone()[0] == 4.2
    assert db.execute('SELECT price FROM deliveries').fetchone()[0] == 50.4
    db.close()
