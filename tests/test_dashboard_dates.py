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
    assert f'value="{YESTERDAY}"' in html          # the date inputs
    assert '500 zł' in html
    assert '-100' not in html


def test_default_opens_on_today_once_there_are_sales():
    _sale(YESTERDAY, 500)
    _sale(TODAY, 120)
    html = _get()
    assert f'value="{TODAY}"' in html
    assert 'день ще триває' in html                # no half-day vs full-day percentage
    assert '-100' not in html


def test_single_day_compares_with_same_weekday_last_week():
    _sale(WEEK_AGO, 400)
    _sale(YESTERDAY, 500)
    _sale('2026-11-17', 100)  # the day before WEEK_AGO must not be the yardstick
    html = _get(date_from=YESTERDAY, date_to=YESTERDAY)
    assert 'vs вт 17.11' in html                   # 24.11 is a Tuesday -> 17.11
    assert '+400.0%' in html                       # 500 vs 100


def test_multi_day_compares_with_previous_period_of_same_length():
    _sale('2026-11-10', 100)
    _sale('2026-11-11', 100)
    _sale('2026-11-12', 300)
    _sale('2026-11-13', 300)
    html = _get(date_from='2026-11-12', date_to='2026-11-13')
    assert 'vs попередній період' in html
    assert '+200.0%' in html                       # 600 vs 200


def test_empty_period_does_not_show_minus_100():
    _sale('2026-11-01', 300)
    html = _get(date_from='2026-11-20', date_to='2026-11-21')
    assert '-100' not in html
    assert 'немає попередніх даних' in html


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
