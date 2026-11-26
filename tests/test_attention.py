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
    attention.clear_cache()
    yield
    attention.clear_cache()


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
    assert attention.nav_counts(CHEF) == {}


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
    assert it['text'].startswith('Дані з каси не оновлювались з ')
    assert it['severity'] == 'danger'
    assert _run()[0]['kind'] == 'sales_stale'   # danger first


def test_sales_stale_intraday(monkeypatch):
    monkeypatch.setattr(gopos_api, 'LAST_INTRADAY_SYNC', '11:15')
    it = _by_kind(_run(), 'sales_stale')
    assert it['text'] == 'Дані з каси не оновлювались з 11:15'
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


def test_nav_counts_cached(monkeypatch):
    _run([("INSERT INTO ingredients (name, quantity, min_quantity) VALUES ('A', 0, 1)", ()),
          ("INSERT INTO parsed_invoices (file_id, file_name, total, parse_status, expense_status) VALUES ('a','a',5,'parsed','pending')", ())])
    first = attention.nav_counts(ADMIN)
    assert first['money'] == 1 and first['today'] >= 2
    calls = []
    monkeypatch.setattr(attention, 'get_attention_items', lambda *a, **k: calls.append(1) or [])
    assert attention.nav_counts(ADMIN) == first and calls == []
    attention.clear_cache()
    assert attention.nav_counts(ADMIN)['today'] == 0 and calls == [1]


# ── routes ──
def _client(user):
    c = TestClient(app, follow_redirects=False)
    c.cookies.set('session', sign_cookie(user))
    return c


def test_more_page_for_admin():
    r = _client('admin').get('/more')
    assert r.status_code == 200
    assert 'Оновити дані з каси' in r.text and 'href="/users"' in r.text and '/export/sales.csv' in r.text


def test_more_page_forbidden_for_chef_but_sheet_in_nav():
    c = _client('chef')
    assert c.get('/more').status_code == 403
    html = c.get('/inventory').text
    assert 'id="more-sheet"' in html and 'href="/logout"' in html
    assert 'Сьогодні' not in html and 'Гроші' not in html


def test_nav_badges_rendered_for_admin():
    _run([("INSERT INTO parsed_invoices (file_id, file_name, total, parse_status, expense_status) VALUES ('a','a',5,'parsed','pending')", ())])
    html = _client('admin').get('/inventory').text
    assert 'lang="uk"' in html
    assert 'class="nav-badge"' in html and 'Сьогодні' in html and 'href="/more"' in html


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
    # the 'Гроші' page uses the same rule
    from app.routes.expenses import missing_recurring
    db = get_db()
    try:
        assert [m['name'] for m in missing_recurring(db, '2026-11')] == ['ZUS']
    finally:
        db.close()


def test_attention_block_shows_all_behind_a_button():
    from app.templating import templates
    items = [attention._item('low_stock', 'warn', 'box', f'Рядок {i}', key=str(i)) for i in range(7)]
    html = templates.env.from_string(
        '{% from "_ui.html" import attention_block %}{{ attention_block(items, limit=5) }}').render(items=items)
    assert html.count('data-attention-extra') == 2
    assert 'Показати всі (7)' in html


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
