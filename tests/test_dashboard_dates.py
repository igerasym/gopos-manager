"""Dashboard: Warsaw business day, default view and the period comparison."""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.db import init_db, get_db
from app.auth import hash_password, sign_cookie

TODAY = '2026-11-25'      # Wednesday
YESTERDAY = '2026-11-24'
WEEK_AGO = '2026-11-18'   # Wednesday


@pytest.fixture(autouse=True)
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr('app.db.DB_PATH', tmp_path / 'test.db')
    monkeypatch.setattr('app.routes.dashboard.warsaw_today', lambda: TODAY)
    init_db()
    db = get_db()
    db.execute("INSERT INTO users (username, password_hash, role, display_name) VALUES ('admin', ?, 'admin', 'A')",
               (hash_password('x'),))
    db.commit()
    db.close()
    yield


def _sale(date, money):
    db = get_db()
    db.execute('INSERT INTO sales (date, product_name, quantity, total_money, net_total, net_profit) '
               "VALUES (?, 'Latte', 1, ?, ?, ?)", (date, money, money, money))
    db.commit()
    db.close()


def _get(**params):
    c = TestClient(app, follow_redirects=False)
    c.cookies.set('session', sign_cookie('admin'))
    return c.get('/', params=params).text


def test_default_opens_on_yesterday_when_nothing_sold_today():
    _sale(YESTERDAY, 500)
    html = _get()
    assert 'Виручка за вчора' in html
    assert '500,00' in html
    assert '-100' not in html and '−100' not in html


def test_default_opens_on_today_once_there_are_sales():
    _sale(YESTERDAY, 500)
    _sale(TODAY, 120)
    html = _get()
    assert 'Виручка сьогодні' in html
    assert 'День ще триває' in html                # no half-day vs full-day percentage
    assert '-100' not in html and '−100' not in html


def test_running_day_is_not_compared_with_a_full_day():
    _sale(WEEK_AGO, 400)
    _sale(TODAY, 120)
    html = _get()
    assert 'chip-ok' not in html.split('id="hero-h"')[1].split('stat-row')[0]
    assert '400 zł' in html.replace('\u202f', ' ').replace('\xa0', ' ')   # last Wednesday, as a fact
    assert 'минулої середи' in html


def test_single_day_compares_with_same_weekday_last_week():
    _sale(WEEK_AGO, 400)
    _sale(YESTERDAY, 500)
    _sale('2026-11-17', 100)  # the day before WEEK_AGO must not be the yardstick
    html = _get(date=YESTERDAY)
    assert 'минулого вівторка' in html             # 24.11 is a Tuesday -> 17.11
    assert '400%' in html                          # 500 vs 100


def test_multi_day_compares_with_previous_period_of_same_length():
    _sale('2026-11-10', 100)
    _sale('2026-11-11', 100)
    _sale('2026-11-12', 300)
    _sale('2026-11-13', 300)
    html = _get(date_from='2026-11-12', date_to='2026-11-13')
    assert 'попереднього періоду' in html
    assert '200%' in html                          # 600 vs 200


def test_week_compares_completed_days_only():
    _sale('2026-11-16', 100)   # last Monday
    _sale('2026-11-17', 100)   # last Tuesday
    _sale('2026-11-23', 200)   # Monday
    _sale('2026-11-24', 200)   # Tuesday
    _sale(TODAY, 50)           # running Wednesday: left out of the comparison
    html = _get(p='week')
    assert 'минулого тижня за ті самі дні' in html
    assert '100%' in html                          # 400 vs 200
    assert 'день ще триває' in html


def test_month_period_and_custom_range_sheet():
    _sale('2026-11-02', 100)
    html = _get(p='month')
    assert 'Виручка за місяць' in html
    assert 'id="sheet-range"' in html and 'name="date_from"' in html


def test_empty_period_does_not_show_minus_100():
    _sale('2026-11-01', 300)
    html = _get(date_from='2026-11-20', date_to='2026-11-21')
    assert '-100' not in html and '−100' not in html
    assert 'продажів ще немає' in html


def test_week_bars_have_ghost_and_link_to_day():
    _sale(WEEK_AGO, 400)
    _sale(TODAY, 120)
    html = _get()
    assert 'class="bar-ghost"' in html and 'is-partial' in html
    assert f'href="/?date={YESTERDAY}"' in html


def test_products_page_lists_sales_and_flags_missing_recipes():
    _sale(TODAY, 120)
    c = TestClient(app, follow_redirects=False)
    c.cookies.set('session', sign_cookie('admin'))
    html = c.get('/dashboard/products').text
    assert 'Усі продукти' in html and 'Latte' in html
    assert 'Неповні дані' in html and 'Без рецепту' in html   # never "норма" without a recipe
    assert 'ABC' not in html


def test_products_page_is_admin_only():
    db = get_db()
    db.execute("INSERT INTO users (username, password_hash, role, display_name) VALUES ('chef', ?, 'chef', 'C')",
               (hash_password('x'),))
    db.commit()
    db.close()
    c = TestClient(app, follow_redirects=False)
    c.cookies.set('session', sign_cookie('chef'))
    assert c.get('/dashboard/products').status_code in (302, 303, 403)
    assert c.get('/').status_code in (302, 303, 403)


def test_csv_default_uses_warsaw_business_day():
    _sale(TODAY, 77)
    c = TestClient(app, follow_redirects=False)
    c.cookies.set('session', sign_cookie('admin'))
    body = c.get('/export/sales.csv').text
    assert TODAY in body


def test_stock_count_uses_business_day_and_clears_that_days_deductions(monkeypatch):
    """At 02:00 Warsaw the UTC date is already the next day but the business day is not.
    The count must be stored under the business day and wipe THAT day's deductions."""
    monkeypatch.setattr('app.routes.stock_count.warsaw_today', lambda: '2026-11-24')
    db = get_db()
    db.execute("INSERT INTO ingredients (id, name, unit, quantity, min_quantity) VALUES (1, 'Mleko', 'L', 5, 0)")
    db.execute("INSERT INTO inventory_deductions (date, ingredient_id, amount) VALUES ('2026-11-24', 1, 1)")
    db.execute("INSERT INTO inventory_deductions (date, ingredient_id, amount) VALUES ('2026-11-25', 1, 1)")
    db.commit()
    db.close()

    c = TestClient(app, follow_redirects=False)
    c.cookies.set('session', sign_cookie('admin'))
    assert c.post('/inventory/count', data={'qty_1': '4'}).status_code in (302, 303)

    db = get_db()
    assert db.execute('SELECT date FROM stock_counts').fetchone()[0] == '2026-11-24'
    left = [r[0] for r in db.execute('SELECT date FROM inventory_deductions')]
    assert left == ['2026-11-25']
    db.close()


def test_expenses_and_wastage_default_month_follow_warsaw_day(monkeypatch):
    monkeypatch.setattr('app.routes.expenses.warsaw_today', lambda: '2026-10-31')
    monkeypatch.setattr('app.routes.wastage.warsaw_today', lambda: '2026-10-31')
    c = TestClient(app, follow_redirects=False)
    c.cookies.set('session', sign_cookie('admin'))
    assert 'value="2026-10"' in c.get('/expenses').text
    assert 'value="2026-10"' in c.get('/wastage').text
