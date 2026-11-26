"""Behaviour / security / data-integrity fixes ported back from the redesign onto the original UI."""
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
    for name, role in (('admin', 'admin'), ('chef', 'chef'), ('barista', 'barista')):
        db.execute(
            'INSERT INTO users (username, password_hash, role, display_name) VALUES (?, ?, ?, ?)',
            (name, hash_password('x'), role, name),
        )
    db.execute("INSERT INTO ingredients (id, name, unit, quantity, unit_price) VALUES (1, 'Mleko', 'L', 10, 4.2)")
    db.execute("INSERT INTO ingredients (id, name, unit, quantity, unit_price) VALUES (2, 'Sól', 'kg', 5, 0)")
    db.commit()
    db.close()
    yield


def _client(user):
    c = TestClient(app, follow_redirects=False)
    c.cookies.set('session', sign_cookie(user))
    return c


def _one(sql, params=()):
    db = get_db()
    row = db.execute(sql, params).fetchone()
    db.close()
    return row


# 1. Delivery-note preview: result['items'], not dict.items
def test_invoice_preview_renders_items(monkeypatch):
    import app.invoice_parser as ip
    monkeypatch.setattr(ip, 'extract_text_from_pdf', lambda b: 'text')
    monkeypatch.setattr(ip, 'parse_invoice_with_llm', lambda text, ings: {
        'success': True,
        'items': [{'invoice_name': 'MLEKO 3.2%', 'ingredient_id': 1, 'ingredient_name': 'Mleko',
                   'is_new': False, 'quantity': 6, 'unit': 'L', 'price_brutto': 25.2,
                   'price_per_unit_brutto': 4.2}],
    })
    r = _client('admin').post('/inventory/upload', files={'file': ('wz.pdf', b'%PDF', 'application/pdf')})
    assert r.status_code == 200
    assert 'MLEKO 3.2%' in r.text
    assert 'name="item_count" value="1"' in r.text


# 8. Confirm tolerates a cleared field and comma decimals instead of a 500
def test_invoice_confirm_blank_and_comma_values():
    r = _client('admin').post('/inventory/upload/confirm', data={
        'item_count': '1', 'ingredient_id_0': '1', 'ingredient_name_0': 'Mleko',
        'quantity_0': '1,5', 'unit_0': 'L', 'price_brutto_0': '', 'price_per_unit_0': '4,40',
        'is_new_0': 'false',
    })
    assert r.status_code == 303
    d = _one('SELECT quantity, price FROM deliveries')
    assert d['quantity'] == 1.5 and d['price'] == 0
    assert _one('SELECT unit_price FROM ingredients WHERE id = 1')[0] == 4.4


# 2. /invoices shows the listing error
def test_invoices_page_shows_error(monkeypatch):
    def boom():
        raise RuntimeError('Drive is down')
    monkeypatch.setattr('app.routes.invoices_gdrive.list_invoices', boom)
    html = _client('admin').get('/invoices').text
    assert 'Помилка: Drive is down' in html


# 3. Заготовки without prices for chef/barista
def _seed_sub_recipe():
    db = get_db()
    db.execute("INSERT INTO ingredients (id, name, unit, quantity, unit_price) VALUES (3, 'Sos', 'kg', 0, 12.34)")
    db.execute("INSERT INTO sub_recipes (id, ingredient_id, yield_amount, yield_unit) VALUES (1, 3, 1, 'kg')")
    db.execute("INSERT INTO sub_recipe_items (sub_recipe_id, ingredient_id, amount) VALUES (1, 1, 0.5)")
    db.commit()
    db.close()


@pytest.mark.parametrize('user', ['chef', 'barista'])
def test_sub_recipes_hide_prices(user):
    _seed_sub_recipe()
    r = _client(user).get('/recipes/sub')
    assert r.status_code == 200
    assert 'Sos' in r.text and 'Mleko' in r.text
    assert 'zł' not in r.text
    assert 'Ціна/од.' not in r.text


def test_sub_recipes_show_prices_to_admin():
    _seed_sub_recipe()
    html = _client('admin').get('/recipes/sub').text
    assert '12.34 zł/kg' in html
    assert 'Ціна/од.' in html


# 4. Price history is owner-only
@pytest.mark.parametrize('user', ['chef', 'barista'])
def test_price_history_forbidden_for_staff(user):
    r = _client(user).get('/api/ingredient/1/price-history')
    assert r.status_code in (302, 303, 403)
    assert '4.2' not in r.text


def test_price_history_route_refuses_non_admin_itself():
    """Defence in depth: the handler itself refuses, not only the role middleware."""
    import asyncio
    from types import SimpleNamespace
    from app.routes.inventory import ingredient_price_history_api
    req = SimpleNamespace(state=SimpleNamespace(user={'role': 'chef'}))
    r = asyncio.run(ingredient_price_history_api(req, 1))
    assert r.status_code == 403


def test_price_history_for_admin():
    r = _client('admin').get('/api/ingredient/1/price-history')
    assert r.status_code == 200
    assert r.json()['current_price'] == 4.2


# 5. Deliveries carry the Warsaw business date
def test_delivery_uses_warsaw_business_date(monkeypatch):
    monkeypatch.setattr('app.routes.inventory.warsaw_today', lambda: '2030-01-02')
    _client('admin').post('/inventory/delivery', data={'ingredient_id': '1', 'quantity': '3', 'price': '12'})
    assert _one('SELECT date FROM deliveries')[0] == '2030-01-02'


def test_invoice_confirm_delivery_uses_warsaw_date(monkeypatch):
    monkeypatch.setattr('app.routes.invoice.warsaw_today', lambda: '2030-01-03')
    _client('admin').post('/inventory/upload/confirm', data={
        'item_count': '1', 'ingredient_id_0': '1', 'quantity_0': '1', 'price_brutto_0': '1',
        'price_per_unit_0': '1', 'is_new_0': 'false'})
    assert _one('SELECT date FROM deliveries')[0] == '2030-01-03'


def test_auto_delivery_from_invoice_uses_warsaw_date(monkeypatch):
    import app.services.invoice_processing as ipr
    monkeypatch.setattr(ipr, 'warsaw_today', lambda: '2030-01-04')
    db = get_db()
    ipr._handle_match_mapping(db, {'invoice_name': 'MLEKO', 'unit_price': 0, 'converted_qty': 2}, 1, None)
    db.commit()
    db.close()
    assert _one('SELECT date, quantity FROM deliveries')[:] == ('2030-01-04', 2)


# 6. Manual wastage cannot use the reserved GoPOS reason
@pytest.mark.parametrize('reason', ['write-off', 'bogus'])
def test_wastage_reserved_or_unknown_reason_stored_as_other(reason):
    r = _client('chef').post('/wastage/add', data={'ingredient_id': '1', 'quantity': '1', 'reason': reason})
    assert r.status_code == 303
    assert _one('SELECT reason, source FROM wastage')[:] == ('other', 'manual')


def test_wastage_valid_reason_kept():
    _client('chef').post('/wastage/add', data={'ingredient_id': '1', 'quantity': '1', 'reason': 'expired'})
    assert _one('SELECT reason FROM wastage')[0] == 'expired'


# 7. Ingredient edit redirects only to local paths
@pytest.mark.parametrize('target', ['https://evil.example/x', '//evil.example', '/\\evil.example', 'evil'])
def test_ingredient_edit_rejects_external_redirect(target):
    r = _client('admin').post('/ingredients/edit/1', data={'quantity': '3', 'redirect': target})
    assert r.status_code == 303
    assert r.headers['location'] == '/inventory'


def test_ingredient_edit_keeps_local_redirect():
    r = _client('admin').post('/ingredients/edit/1', data={'quantity': '3', 'redirect': '/inventory?supplier=Mlekpol'})
    assert r.headers['location'] == '/inventory?supplier=Mlekpol'


# 8. Recipe amount (text input) accepts comma decimals
def test_recipe_update_accepts_comma():
    db = get_db()
    db.execute("INSERT INTO recipes (id, product_name, ingredient_id, amount) VALUES (1, 'Latte', 1, 0.2)")
    db.commit()
    db.close()
    r = _client('admin').post('/recipes/update/1', data={'display_amount': '250,5', 'base_unit': 'L'})
    assert r.status_code == 303
    assert abs(_one('SELECT amount FROM recipes WHERE id = 1')[0] - 0.2505) < 1e-9
    r = _client('admin').post('/recipes/update/1', data={'display_amount': 'abc', 'base_unit': 'L'})
    assert r.status_code == 303
    assert abs(_one('SELECT amount FROM recipes WHERE id = 1')[0] - 0.2505) < 1e-9


def test_parse_num():
    from app.services.recipes import parse_num
    assert parse_num('1 234,50') == 1234.5
    assert parse_num(' 1.5 ') == 1.5
    assert parse_num('') is None
    assert parse_num('x', 0.0) == 0.0


# 9a. Stock count: tiny differences are a match, not a shortage
def _seed_count():
    db = get_db()
    db.execute("INSERT INTO stock_counts (id, date) VALUES (1, '2030-01-01')")
    db.executemany('INSERT INTO stock_count_items (stock_count_id, ingredient_id, expected, actual, difference) '
                   'VALUES (1, ?, ?, ?, ?)', [(1, 10, 9.96, -0.04), (2, 5, 3, -2)])
    db.commit()
    db.close()


def test_stock_count_rounding_noise_is_not_shortage():
    from app.routes.stock_count import diff_status
    assert diff_status(-0.04) == 'match'
    assert diff_status(-0.05) == 'short'
    assert diff_status(0.05) == 'over'
    _seed_count()
    html = _client('admin').get('/inventory/count/1').text
    assert html.count('>Нестача<') == 1
    assert '-0.0' not in html
    assert '0.00 zł' in html  # the only real shortage (Sól) has no price; Mleko noise is not a loss


def test_stock_count_history_counts_only_real_shortages():
    from app.routes import stock_count
    _seed_count()
    captured = {}
    orig = stock_count.templates.TemplateResponse

    def spy(request, name, context):
        captured.update(context)
        return orig(request, name, context=context)
    stock_count.templates.TemplateResponse = spy
    try:
        assert _client('admin').get('/inventory/history').status_code == 200
    finally:
        stock_count.templates.TemplateResponse = orig
    assert captured['counts'][0]['shortage_count'] == 1


def test_stock_count_detail_hides_loss_value_from_chef():
    _seed_count()
    html = _client('chef').get('/inventory/count/1').text
    assert 'Вартість втрат' not in html and 'zł' not in html


# 9b. Wastage top losses: only items with a known cost
def test_wastage_top_losers_only_known_cost():
    from app.routes import wastage
    db = get_db()
    db.execute("INSERT INTO wastage (date, ingredient_id, quantity, reason, source) VALUES ('2030-01-05', 1, 2, 'expired', 'manual')")
    db.execute("INSERT INTO wastage (date, ingredient_id, quantity, reason, source) VALUES ('2030-01-05', 2, 9, 'spoiled', 'manual')")
    db.commit()
    db.close()
    captured = {}
    orig = wastage.templates.TemplateResponse

    def spy(request, name, context):
        captured.update(context)
        return orig(request, name, context=context)
    wastage.templates.TemplateResponse = spy
    try:
        r = _client('admin').get('/wastage?month=2030-01')
    finally:
        wastage.templates.TemplateResponse = orig
    assert r.status_code == 200
    names = [n for n, _ in captured['top_losers']]
    assert names == ['Mleko']
