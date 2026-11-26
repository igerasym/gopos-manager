"""Меню page: dish economics, 'Без рецепту' banner, tabs, sheets' endpoints, role visibility."""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password, sign_cookie
from app.db import get_db, init_db
from app.main import app
from app.services.recipes import (
    COST_SHARE_ALERT, dish_economics, get_products_without_recipe, get_sub_recipe_usage,
    parse_num, safe_next, tab_for_category,
)

TODAY = date(2026, 11, 25)


@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    monkeypatch.setattr('app.db.DB_PATH', tmp_path / 'test.db')
    monkeypatch.setattr('app.gopos_api.warsaw_today', lambda: TODAY.isoformat())
    init_db()
    db = get_db()
    for name, role in (('admin', 'admin'), ('chef', 'chef')):
        db.execute('INSERT INTO users (username, password_hash, role, display_name) VALUES (?, ?, ?, ?)',
                   (name, hash_password('x'), role, name))
    db.executemany('INSERT INTO ingredients (id, name, unit, quantity, unit_price) VALUES (?, ?, ?, 10, ?)', [
        (1, 'Mleko', 'L', 4.0), (2, 'Kawa', 'kg', 100.0), (3, 'Mąka', 'kg', 3.0),
        (4, 'Ciasto (półprodukt)', 'kg', 0),
    ])
    # Latte: 0.2 L milk + 0.018 kg coffee = 0.8 + 1.8 = 2.6 zł, price 10 zł -> 26%
    db.execute("INSERT INTO recipe_cards (product_name, category) VALUES ('Latte', 'Бар')")
    db.execute("INSERT INTO recipes (product_name, ingredient_id, amount) VALUES ('Latte', 1, 0.2)")
    db.execute("INSERT INTO recipes (product_name, ingredient_id, amount) VALUES ('Latte', 2, 0.018)")
    db.execute("INSERT INTO recipe_cards (product_name, category) VALUES ('Cynamonka', 'Випічка')")
    db.execute("INSERT INTO recipes (product_name, ingredient_id, amount) VALUES ('Cynamonka', 4, 0.1)")
    db.execute("INSERT INTO recipe_cards (product_name, category) VALUES ('Zupa dnia', 'Кухня')")  # no lines
    db.execute("INSERT INTO sub_recipes (id, ingredient_id, yield_amount, yield_unit) VALUES (1, 4, 2, 'kg')")
    db.execute("INSERT INTO sub_recipe_items (sub_recipe_id, ingredient_id, amount) VALUES (1, 3, 1.0)")
    for name, kind, price in (('Latte', 'prepared', 10), ('Brownie', 'prepared', 12),
                              ('Zupa dnia', 'prepared', 20), ('Cynamonka', 'prepared', 9),
                              ('New thing', 'unclassified', 5)):
        db.execute('INSERT INTO pos_products (product_name, pos_kind, gopos_price) VALUES (?, ?, ?)',
                   (name, kind, price))
    d = (TODAY - timedelta(days=3)).isoformat()
    old = (TODAY - timedelta(days=60)).isoformat()
    for name, money, day in (('Latte', 600, d), ('Brownie', 300, d), ('Zupa dnia', 100, d),
                             ('New thing', 50, d), ('Old dish', 999, old)):
        db.execute('INSERT INTO sales (date, product_name, quantity, total_money) VALUES (?, ?, 1, ?)',
                   (day, name, money))
    db.commit()
    db.close()
    yield


def _client(user):
    c = TestClient(app, follow_redirects=False)
    c.cookies.set('session', sign_cookie(user))
    return c


# ── pure helpers ──

def test_dish_economics_threshold():
    ok = dish_economics(2.6, 10)
    assert ok['share'] == pytest.approx(26) and not ok['alert']
    assert ok['earn'] == pytest.approx(7.4) and ok['earn_pct'] == pytest.approx(74)
    bad = dish_economics(COST_SHARE_ALERT / 100 * 10 + 0.01, 10)
    assert bad['alert']
    assert dish_economics(0, 10)['share'] is None
    assert dish_economics(2, 0)['share'] is None and dish_economics(2, 0)['earn'] is None


def test_parse_num_accepts_comma_and_dot():
    assert parse_num('1,5') == 1.5
    assert parse_num(' 2.25 ') == 2.25
    assert parse_num('1\u202f000') == 1000
    assert parse_num('abc') is None
    assert parse_num('', 1.0) == 1.0


def test_safe_next_only_allows_menu_paths():
    assert safe_next('/recipes/bar') == '/recipes/bar'
    assert safe_next('https://evil.example/') == '/recipes'
    assert safe_next('//evil.example') == '/recipes'
    assert safe_next('/expenses') == '/recipes'


def test_tab_for_category():
    assert tab_for_category('Бар') == 'bar'
    assert tab_for_category('Випічка') == 'bakery'
    assert tab_for_category('Сніданки') == 'kitchen'
    assert tab_for_category('') == 'kitchen'


def test_products_without_recipe_counts_recent_uncosted_only():
    res = get_products_without_recipe(TODAY)
    names = [i['product_name'] for i in res['items']]
    # Brownie (no card) and Zupa dnia (card, no lines); not Latte (costed), not the
    # unclassified product, not a dish sold only 60 days ago.
    assert names == ['Brownie', 'Zupa dnia']
    total = 600 + 300 + 100 + 50
    assert res['share'] == pytest.approx(400 / total * 100)
    zupa = res['items'][1]
    assert zupa['has_card'] and zupa['tab'] == 'kitchen'
    assert not res['items'][0]['has_card']


def test_sub_recipe_usage():
    db = get_db()
    usage = get_sub_recipe_usage(db)
    db.close()
    assert usage[4] == [{'name': 'Cynamonka', 'kind': 'dish'}]


# ── pages ──

def test_tabs_split_categories():
    c = _client('admin')
    kitchen = c.get('/recipes').text
    assert 'Zupa dnia' in kitchen and 'data-sheet-open="dish-' in kitchen
    bar = c.get('/recipes/bar').text
    assert '>Latte<' in bar
    bakery = c.get('/recipes/bakery').text
    assert '>Cynamonka<' in bakery and 'Заготовки' in bakery
    assert '>Cynamonka<' not in kitchen


def test_admin_sees_economics_and_no_recipe_banner():
    html = _client('admin').get('/recipes/bar').text
    assert 'Без рецепту' in html and 'Brownie' in html
    assert 'заробляємо' in html and 'продукти 26%' in html
    assert 'Немає інгредієнтів' not in html  # Zupa is on the kitchen tab
    assert 'Немає інгредієнтів' in _client('admin').get('/recipes').text


def test_red_chip_above_threshold():
    db = get_db()
    db.execute("UPDATE pos_products SET gopos_price = 5 WHERE product_name = 'Latte'")  # 2.6/5 = 52%
    db.commit()
    db.close()
    html = _client('admin').get('/recipes/bar').text
    assert 'chip chip-danger' in html and 'продукти 52%' in html


def test_chef_sees_recipes_without_money():
    c = _client('chef')
    for path in ('/recipes', '/recipes/bar', '/recipes/bakery', '/recipes/sub'):
        r = c.get(path)
        assert r.status_code == 200, path
        assert 'zł' not in r.text, path
        assert 'собівартість' not in r.text.lower(), path
        assert 'виручки' not in r.text, path
    assert 'Mleko' in c.get('/recipes/bar').text


def test_sub_page_shows_usage_and_cost_per_kg_to_admin():
    html = _client('admin').get('/recipes/sub').text
    assert 'Де використовується' in html and 'Cynamonka' in html
    assert 'за 1 кг' in html


# ── actions ──

def test_new_dish_with_first_ingredient_in_grams():
    r = _client('admin').post('/recipes/card', data={
        'product_name': 'Brownie', 'category': 'Випічка', 'ingredient_id': '3',
        'amount': '120,5', 'display': '1'})
    assert r.status_code == 303
    assert r.headers['location'].startswith('/recipes/bakery?saved=card&open=Brownie')
    db = get_db()
    assert db.execute("SELECT category FROM recipe_cards WHERE product_name='Brownie'").fetchone()[0] == 'Випічка'
    amt = db.execute("SELECT amount FROM recipes WHERE product_name='Brownie'").fetchone()[0]
    db.close()
    assert amt == pytest.approx(0.1205)
    assert 'Brownie' not in [i['product_name'] for i in get_products_without_recipe(TODAY)['items']]


def test_card_without_ingredient_still_works():
    _client('admin').post('/recipes/card', data={'product_name': 'Soup', 'category': ''})
    db = get_db()
    assert db.execute("SELECT COUNT(*) FROM recipe_cards WHERE product_name='Soup'").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM recipes WHERE product_name='Soup'").fetchone()[0] == 0
    db.close()


def test_add_line_display_units_and_redirect_back():
    r = _client('admin').post('/recipes/add', data={
        'product_name': 'Zupa dnia', 'ingredient_id': '1', 'amount': '250', 'display': '1',
        'next': '/recipes'})
    assert r.headers['location'] == '/recipes?saved=line&open=Zupa+dnia'
    db = get_db()
    assert db.execute("SELECT amount FROM recipes WHERE product_name='Zupa dnia'").fetchone()[0] == pytest.approx(0.25)
    db.close()


def test_add_line_legacy_base_units_and_bad_next():
    r = _client('admin').post('/recipes/add', data={
        'product_name': 'Zupa dnia', 'ingredient_id': '1', 'amount': '0.3', 'next': 'https://evil.example'})
    assert r.headers['location'].startswith('/recipes?')
    db = get_db()
    assert db.execute("SELECT amount FROM recipes WHERE product_name='Zupa dnia'").fetchone()[0] == pytest.approx(0.3)
    db.close()


def test_update_line_accepts_comma():
    db = get_db()
    rid = db.execute("SELECT id FROM recipes WHERE product_name='Latte' AND ingredient_id=1").fetchone()[0]
    db.close()
    _client('admin').post(f'/recipes/update/{rid}', data={'display_amount': '180,5', 'base_unit': 'L', 'next': '/recipes/bar'})
    db = get_db()
    assert db.execute('SELECT amount FROM recipes WHERE id=?', (rid,)).fetchone()[0] == pytest.approx(0.1805)
    db.close()


def test_delete_dish_removes_card_and_lines():
    r = _client('admin').post('/recipes/card/delete', data={'product_name': 'Latte', 'next': '/recipes/bar'})
    assert r.headers['location'] == '/recipes/bar?saved=deleted'
    db = get_db()
    assert db.execute("SELECT COUNT(*) FROM recipes WHERE product_name='Latte'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM recipe_cards WHERE product_name='Latte'").fetchone()[0] == 0
    db.close()


def test_sub_recipe_add_in_grams_and_edit_yield_with_comma():
    c = _client('admin')
    r = c.post('/recipes/sub/1/add', data={'ingredient_id': '1', 'amount': '500', 'display': '1'})
    assert r.headers['location'] == '/recipes/sub?saved=line&open=prep-1'
    c.post('/recipes/sub/1/edit', data={'yield_amount': '2,5', 'description': ''})
    db = get_db()
    assert db.execute('SELECT amount FROM sub_recipe_items WHERE ingredient_id=1').fetchone()[0] == pytest.approx(0.5)
    assert db.execute('SELECT yield_amount FROM sub_recipes WHERE id=1').fetchone()[0] == 2.5
    # (1 kg flour * 3 + 0.5 L milk * 4) / 2.5 kg = 2.0 zł/kg
    assert db.execute('SELECT unit_price FROM ingredients WHERE id=4').fetchone()[0] == pytest.approx(2.0)
    db.close()


def test_sub_recipe_update_display_amount():
    db = get_db()
    item = db.execute('SELECT id FROM sub_recipe_items WHERE sub_recipe_id=1').fetchone()[0]
    db.close()
    _client('admin').post(f'/recipes/sub/1/update/{item}', data={'display_amount': '1500', 'base_unit': 'kg'})
    db = get_db()
    assert db.execute('SELECT amount FROM sub_recipe_items WHERE id=?', (item,)).fetchone()[0] == pytest.approx(1.5)
    db.close()


def test_sub_recipe_create():
    r = _client('admin').post('/recipes/sub/create', data={'name': 'Sos', 'unit': 'L', 'yield_amount': '1,5'})
    assert r.status_code == 303 and 'saved=created' in r.headers['location']
    db = get_db()
    row = db.execute("SELECT sr.yield_amount, sr.yield_unit FROM sub_recipes sr JOIN ingredients i "
                     "ON i.id = sr.ingredient_id WHERE i.name='Sos'").fetchone()
    db.close()
    assert tuple(row) == (1.5, 'L')
