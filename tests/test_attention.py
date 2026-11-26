"""'Що потребує уваги' rules, dismissal, nav badges and the /more + /attention/dismiss routes."""
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import gopos_api
from app.auth import hash_password, sign_cookie
from app.db import get_db, init_db
from app.main import app
from app.services import attention

TODAY = date(2026, 11, 25)
NOW = datetime(2026, 11, 25, 14, 15, tzinfo=attention.WARSAW)
ADMIN = {'id': 1, 'username': 'admin', 'role': 'admin', 'display_name': 'Оля'}
CHEF = {'id': 2, 'username': 'chef', 'role': 'chef', 'display_name': 'Тарас'}


@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    monkeypatch.setattr('app.db.DB_PATH', tmp_path / 'test.db')
    monkeypatch.setattr(gopos_api, 'LAST_INTRADAY_SYNC', None)
    # the tests' clock is 2026-11-25: refreshes only count when they are from that business day
    monkeypatch.setattr(gopos_api, 'warsaw_today', lambda: '2026-11-25')
    monkeypatch.setattr(gopos_api, 'LAST_INTRADAY_DAY', '2026-11-25')
    init_db()
    db = get_db()
    db.execute("INSERT INTO users (id, username, password_hash, role, display_name) VALUES (1, 'admin', ?, 'admin', 'Оля')",
               (hash_password('x'),))
    db.execute("INSERT INTO users (id, username, password_hash, role, display_name) VALUES (2, 'chef', ?, 'chef', 'Тарас')",
               (hash_password('x'),))
    # a recent stock count so the "never counted" row stays out of unrelated tests
    db.execute("INSERT INTO stock_counts (date) VALUES (?)", (TODAY.isoformat(),))
    db.commit()
    db.close()
    yield


def _run(sql_rows=(), **kw):
    db = get_db()
    for sql, args in sql_rows:
        db.execute(sql, args)
    db.commit()
    db.close()
    return attention.get_attention_items(ADMIN, today=TODAY, now=NOW, **kw)


def _kinds(items):
    return [i['kind'] for i in items]


def _by_kind(items, kind):
    return next(i for i in items if i['kind'] == kind)


def test_empty_db_has_nothing():
    assert _run() == []


def test_non_admin_gets_nothing():
    _run([("INSERT INTO ingredients (name, quantity, min_quantity) VALUES ('Mleko', 1, 5)", ())])
    assert attention.get_attention_items(CHEF, today=TODAY, now=NOW) == []


def test_invoices_waiting():
    items = _run([
        ("INSERT INTO parsed_invoices (file_id, file_name, vendor, total, parse_status, expense_status) VALUES ('a','a.pdf','Makro',100.5,'parsed','pending')", ()),
        ("INSERT INTO parsed_invoices (file_id, file_name, vendor, total, parse_status, expense_status) VALUES ('b','b.pdf','Kawa',50,'parsed','pending')", ()),
        ("INSERT INTO parsed_invoices (file_id, file_name, vendor, total, parse_status, expense_status) VALUES ('c','c.pdf','X',70,'parsed','added')", ()),
        ("INSERT INTO parsed_invoices (file_id, file_name, vendor, total, parse_status, expense_status) VALUES ('d','d.pdf','Y',0,'parsed','pending')", ()),
    ])
    it = _by_kind(items, 'invoices_waiting')
    assert it['text'] == '2 фактури чекають підтвердження'
    assert it['amount'] == 150.5 and it['count'] == 2
    assert 'Makro' in it['sub'] and it['action_label'] == 'Переглянути'
    assert it['href'] == attention.HREF['invoices_waiting']


def test_invoice_failed():
    items = _run([("INSERT INTO parsed_invoices (file_id, file_name, parse_status, expense_status) VALUES ('e','skan.pdf','error','pending')", ()),
                  ("INSERT INTO parsed_invoices (file_id, file_name, parse_status, expense_status) VALUES ('f','old.pdf','error','skipped')", ())])
    it = _by_kind(items, 'invoice_failed')
    assert it['text'] == '1 фактуру не вдалося розпізнати' and it['severity'] == 'danger'
    assert it['sub'] == 'skan.pdf'


def test_items_unmatched():
    db_rows = [("INSERT INTO parsed_invoices (id, file_id, file_name) VALUES (9, 'g', 'g.pdf')", ())]
    db_rows += [("INSERT INTO invoice_items_pending (parsed_invoice_id, invoice_name, status) VALUES (9, ?, 'pending')", (f'p{i}',))
                for i in range(6)]
    it = _by_kind(_run(db_rows), 'items_unmatched')
    assert it['text'] == '6 позицій з фактур ще не звірені зі складом'


def test_low_stock_grouped_by_supplier():
    items = _run([
        ("INSERT INTO suppliers (id, name) VALUES (1, 'Makro'), (2, 'Mleczarnia')", ()),
        ("INSERT INTO ingredients (name, quantity, min_quantity, supplier_id) VALUES ('A', 1, 5, 1), ('B', 0, 2, 1), ('C', 3, 3, 2), ('D', 9, 2, 2), ('E', 0, 0, 1), ('F', 0, 1, NULL)", ()),
    ])
    it = _by_kind(items, 'low_stock')
    assert it['text'] == '4 інгредієнти закінчуються'   # E has min 0, D is above min
    assert it['sub'].startswith('Makro (2)')
    assert 'без постачальника' in it['sub']


def _sale(name, rev, d=TODAY):
    return ("INSERT INTO sales (date, product_name, quantity, total_money) VALUES (?, ?, 1, ?)", (d.isoformat(), name, rev))


def test_no_recipe_share_of_revenue():
    items = _run([
        ("INSERT INTO ingredients (id, name, unit_price) VALUES (1, 'Kawa', 50)", ()),
        ("INSERT INTO recipes (product_name, ingredient_id, amount) VALUES ('Latte', 1, 0.02)", ()),
        _sale('Latte', 800), _sale('Brownie', 150), _sale('Omlet', 50),
        _sale('Old', 999, TODAY - timedelta(days=40)),
        ("INSERT INTO pos_products (product_name, pos_kind) VALUES ('Woda', 'ignore')", ()), _sale('Woda', 100),
    ])
    it = _by_kind(items, 'no_recipe')
    assert it['text'] == '2 страви без рецепту'
    assert it['sub'].startswith('18%')   # 200 of 1100
    assert it['severity'] == 'warn'


def test_unclassified_product():
    items = _run([("INSERT INTO pos_products (product_name, pos_kind) VALUES ('Pumpkin spice latte', 'unclassified'), ('Ghost', 'unclassified')", ()),
                  _sale('Pumpkin spice latte', 20)])
    it = _by_kind(items, 'unclassified_product')
    assert it['text'] == 'Нова позиція в касі: Pumpkin spice latte'
    assert 'no_recipe' not in _kinds(items)    # not double-counted as "without recipe"


def test_costs_incomplete_after_day_10():
    rows = [
        ("INSERT INTO expenses (name, category, amount, month, recurring) VALUES ('Wynagrodzenia', 'Зарплати', 38000, '2026-10', 1)", ()),
        ("INSERT INTO expenses (name, category, amount, month, recurring) VALUES ('ZUS', 'Податки і ZUS', 3400, '2026-10', 1)", ()),
        ("INSERT INTO expenses (name, category, amount, month, recurring) VALUES ('Czynsz', 'Оренда', 14500, '2026-10', 1)", ()),
        ("INSERT INTO expenses (name, category, amount, month, recurring) VALUES ('Czynsz', 'Оренда', 14500, '2026-11', 1)", ()),
        ("INSERT INTO expenses_dismissed (name, month) VALUES ('ZUS', '2026-11')", ()),
    ]
    it = _by_kind(_run(rows), 'costs_incomplete')
    assert it['text'] == 'Ще не внесено: Зарплати'
    assert it['amount'] == 38000
    early = attention.get_attention_items(ADMIN, today=date(2026, 11, 5), now=NOW)
    assert 'costs_incomplete' not in _kinds(early)


def test_count_overdue():
    db = get_db(); db.execute('DELETE FROM stock_counts'); db.commit(); db.close()
    items = _run([("INSERT INTO ingredients (name) VALUES ('A')", ()),
                  ("INSERT INTO stock_counts (date) VALUES ('2026-11-13')", ())])
    it = _by_kind(items, 'count_overdue')
    assert it['text'] == 'Інвентаризацію не робили 12 днів'
    assert it['sub'] == 'Остання була 13 лист., пт'


def test_count_never_done():
    db = get_db(); db.execute('DELETE FROM stock_counts'); db.commit(); db.close()
    it = _by_kind(_run([("INSERT INTO ingredients (name) VALUES ('A')", ())]), 'count_overdue')
    assert it['text'] == 'Інвентаризацію ще не робили'


def test_recent_count_is_fine():
    assert 'count_overdue' not in _kinds(_run([("INSERT INTO ingredients (name) VALUES ('A')", ())]))


def test_high_wastage_vs_same_day_last_month():
    rows = [("INSERT INTO ingredients (id, name, unit_price) VALUES (1, 'Mleko', 10)", ()),
            ("INSERT INTO wastage (date, ingredient_id, quantity, source) VALUES ('2026-11-03', 1, 10, 'manual')", ()),   # 100
            ("INSERT INTO wastage (date, ingredient_id, quantity, source) VALUES ('2026-10-04', 1, 5, 'manual')", ()),    # 50
            ("INSERT INTO wastage (date, ingredient_id, quantity, source) VALUES ('2026-10-28', 1, 50, 'manual')", ())]   # after day 25: ignored
    it = _by_kind(_run(rows), 'high_wastage')
    assert it['amount'] == 100
    assert 'минулого місяця' in it['text']


def test_wastage_not_flagged_when_similar():
    rows = [("INSERT INTO ingredients (id, name, unit_price) VALUES (1, 'Mleko', 10)", ()),
            ("INSERT INTO wastage (date, ingredient_id, quantity, source) VALUES ('2026-11-03', 1, 10, 'manual')", ()),
            ("INSERT INTO wastage (date, ingredient_id, quantity, source) VALUES ('2026-10-04', 1, 9, 'manual')", ())]
    assert 'high_wastage' not in _kinds(_run(rows))


def test_dish_got_dearer():
    rows = [("INSERT INTO ingredients (id, name, unit_price) VALUES (1, 'Łosoś', 120)", ()),
            ("INSERT INTO recipes (product_name, ingredient_id, amount) VALUES ('Tost z łososiem', 1, 0.08)", ()),
            ("INSERT INTO pos_products (product_name, pos_kind, gopos_price) VALUES ('Tost z łososiem', 'prepared', 30)", ()),
            ("INSERT INTO ingredient_price_history (ingredient_id, price, date) VALUES (1, 100, '2026-09-01')", ()),
            ("INSERT INTO ingredient_price_history (ingredient_id, price, date) VALUES (1, 120, '2026-11-20')", ())]
    it = _by_kind(_run(rows), 'dish_dearer')   # 8/30 = 26.7% -> 9.6/30 = 32%
    assert '«Tost z łososiem»' in it['text']
    assert it['sub'] == 'Продукти тепер 32% ціни (було 27%)'


def test_sales_stale_after_failed_sync():
    rows = [("INSERT INTO sync_log (started_at, finished_at, status) VALUES ('2026-11-25 09:00:00', '2026-11-25 09:15:00', 'done')", ()),
            ("INSERT INTO sync_log (started_at, finished_at, status) VALUES ('2026-11-25 12:00:00', '2026-11-25 12:00:05', 'error')", ())]
    it = _by_kind(_run(rows), 'sales_stale')
    assert it['text'] == 'Дані з каси давно не оновлювались'
    assert it['severity'] == 'danger'
    assert _run()[0]['kind'] == 'sales_stale'   # danger first


def test_sales_stale_intraday(monkeypatch):
    monkeypatch.setattr(gopos_api, 'LAST_INTRADAY_SYNC', '11:15')
    it = _by_kind(_run(), 'sales_stale')
    assert it['text'] == 'Дані з каси давно не оновлювались' and 'сьогодні о 11:15' in it['sub']
    monkeypatch.setattr(gopos_api, 'LAST_INTRADAY_SYNC', '13:15')
    assert 'sales_stale' not in _kinds(_run())


def test_failing_rule_does_not_break_the_list(monkeypatch):
    def boom(c):
        raise RuntimeError('db exploded')
    monkeypatch.setattr(attention, 'RULES', [boom, attention.rule_low_stock])
    items = _run([("INSERT INTO ingredients (name, quantity, min_quantity) VALUES ('A', 0, 1)", ())])
    assert _kinds(items) == ['low_stock']


def test_dismiss_hides_until_tomorrow_and_new_items_come_back():
    rows = [("INSERT INTO ingredients (name, quantity, min_quantity) VALUES ('A', 0, 1)", ())]
    it = _by_kind(_run(rows), 'low_stock')
    until = attention.dismiss(ADMIN, 'low_stock', it['key'], today=TODAY)
    assert until == '2026-11-26'
    assert 'low_stock' not in _kinds(_run())
    assert 'low_stock' in _kinds(attention.get_attention_items(ADMIN, today=date(2026, 11, 26), now=NOW))
    # another ingredient runs low: different key -> visible again today
    assert 'low_stock' in _kinds(_run([("INSERT INTO ingredients (name, quantity, min_quantity) VALUES ('B', 0, 1)", ())]))




# ── routes ──
def _client(user):
    c = TestClient(app, follow_redirects=False)
    c.cookies.set('session', sign_cookie(user))
    return c








def test_dismiss_endpoint_json_and_redirect():
    c = _client('admin')
    r = c.post('/attention/dismiss', data={'kind': 'low_stock', 'key': 'abc'}, headers={'Accept': 'application/json'})
    assert r.status_code == 200 and r.json()['ok'] is True
    db = get_db()
    row = db.execute('SELECT user_id, kind, key FROM attention_dismissed').fetchone()
    db.close()
    assert tuple(row) == (1, 'low_stock', 'abc')
    r = c.post('/attention/dismiss', data={'kind': 'low_stock', 'key': 'abc', 'next': '//evil.example'})
    assert r.status_code == 303 and r.headers['location'] == '/'
    r = c.post('/attention/dismiss', data={'kind': 'low_stock', 'key': 'abc', 'next': '/inventory'})
    assert r.headers['location'] == '/inventory'
    assert c.post('/attention/dismiss', data={'kind': 'nope'}).status_code == 400


def test_dismiss_endpoint_admin_only():
    assert _client('chef').post('/attention/dismiss', data={'kind': 'low_stock'}).status_code == 403


def test_costs_incomplete_labels_the_amount_as_last_months_expense():
    it = _by_kind(_run([("INSERT INTO expenses (name, category, amount, month, recurring) "
                         "VALUES ('Wynagrodzenia', 'Зарплати', 38000, '2026-10', 1)", ())]), 'costs_incomplete')
    assert it['amount_expense'] is True
    assert it['amount_note'] == 'витрати минулого місяця'


def test_recurring_cost_counts_as_entered_by_category():
    """Last month 'Prąd + gaz' (Комунальні); this month a different name in Комунальні."""
    rows = [
        ("INSERT INTO expenses (name, category, amount, month, recurring) VALUES ('Prąd + gaz', 'Комунальні', 2600, '2026-10', 1)", ()),
        ("INSERT INTO expenses (name, category, amount, month, recurring) VALUES ('ZUS', 'Податки і ZUS', 3400, '2026-10', 1)", ()),
        ("INSERT INTO expenses (name, category, amount, month, recurring) VALUES ('Rachunek PGE', 'Комунальні', 121.5, '2026-11', 0)", ()),
    ]
    it = _by_kind(_run(rows), 'costs_incomplete')
    assert it['text'] == 'Ще не внесено: Податки і ZUS'
    assert it['amount'] == 3400




def test_data_freshness_prefers_intraday_then_sync_log(monkeypatch):
    monkeypatch.setattr(gopos_api, 'LAST_INTRADAY_SYNC', '13:45')
    f = attention.data_freshness(now=NOW)
    assert f['text'] == 'Дані з каси оновлено сьогодні о 13:45'
    monkeypatch.setattr(gopos_api, 'LAST_INTRADAY_SYNC', None)
    db = get_db()
    db.execute("INSERT INTO sync_log (status, started_at, finished_at) VALUES ('done', '2026-11-24 03:29:00', '2026-11-24 03:30:00')")
    db.commit()
    f = attention.data_freshness(db, now=NOW)
    db.close()
    assert f['at'] is not None and f['text'].startswith('Дані з каси оновлено 24 лист. о ')
    assert not f['error']


def test_data_freshness_never_synced(monkeypatch):
    monkeypatch.setattr(gopos_api, 'LAST_INTRADAY_SYNC', None)
    db = get_db(); db.execute('DELETE FROM sync_log'); db.commit()
    f = attention.data_freshness(db, now=NOW)
    db.close()
    assert f == {'at': None, 'error': False, 'text': 'Дані з каси ще не оновлювались'}


def test_dashboard_shows_block_capped_with_show_all():
    db = get_db()
    for i in range(7):
        db.execute("INSERT INTO ingredients (name, quantity, min_quantity) VALUES (?, 0, 1)", (f'I{i}',))
    db.commit(); db.close()
    # 7 low-stock ingredients are ONE row; add more kinds to exceed the cap
    db = get_db()
    db.execute("INSERT INTO parsed_invoices (file_id, file_name, vendor, total, parse_status, expense_status) VALUES ('a','a.pdf','Makro',100,'parsed','pending')")
    db.execute("INSERT INTO parsed_invoices (file_id, file_name, parse_status, expense_status) VALUES ('e','e.pdf','error','pending')")
    db.execute("DELETE FROM stock_counts"); db.execute("INSERT INTO stock_counts (date) VALUES ('2026-01-01')")
    db.execute("INSERT INTO pos_products (product_name, pos_kind) VALUES ('Nowy', 'unclassified')")
    db.execute("INSERT INTO sales (date, product_name, quantity, total_money) VALUES (?, 'Nowy', 1, 20)", (TODAY.isoformat(),))
    db.execute("INSERT INTO sales (date, product_name, quantity, total_money) VALUES (?, 'Bez', 1, 900)", (TODAY.isoformat(),))
    db.execute("INSERT INTO invoice_items_pending (parsed_invoice_id, invoice_name, status) VALUES (1, 'p', 'pending')")
    db.commit(); db.close()
    html = _client('admin').get('/').text
    items = attention.get_attention_items(ADMIN, today=TODAY, now=NOW)
    assert len(items) > 5
    assert 'Що потребує уваги' in html
    assert html.count('data-attention-extra style') == len(items) - 5
    assert f'Показати всі ({len(items)})' in html
    assert 'Сховати до завтра' in html


def test_dashboard_all_clear_line_and_chef_sees_nothing():
    # fresh DB: a recent count exists, nothing else to flag except a never-synced till is not a row
    html = _client('admin').get('/').text
    assert 'Все гаразд' in html and 'attention-card' not in html
    assert 'attention-card' not in _client('chef').get('/inventory').text
