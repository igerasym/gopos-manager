"""Takeaway split stored at sync time and served to the Telegram bot (tool + /takeaway skill)."""
from unittest.mock import patch

import pytest

from app import gopos_api as ga
from app import telegram_skills as skills
from app import telegram_tools as tt
from app.db import init_db, get_db

TODAY = '2026-11-25'  # Wednesday


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    monkeypatch.setattr('app.db.DB_PATH', tmp_path / 'test.db')
    init_db()
    yield


def _it(name, paid=10):
    return {'item_id': 1, 'name': name, 'quantity': 1, 'status': 'ACTIVE',
            'sub_total_price': {'amount': paid}, 'total_price': {'amount': paid}}


def _order(oid, paid, *names):
    return {'id': oid, 'status': 'CLOSED', 'closed_at': '2026-11-05T12:00:00',
            'total_price': {'amount': paid}, 'total_price_net': {'amount': paid / 1.1},
            'items': [_it(n, paid / len(names)) for n in names]}


def _put(day, orders, revenue, t_orders, t_revenue):
    db = get_db()
    db.execute('INSERT INTO daily_takeaway VALUES (?, ?, ?, ?, ?)', (day, orders, revenue, t_orders, t_revenue))
    db.commit()
    db.close()


def test_sync_stores_the_takeaway_split():
    orders = [_order(1, 20, 'Latte', 'To-go cup'), _order(2, 40, 'Flat White', 'Brownie'),
              _order(3, 30, 'Cookie', 'To-go box')]
    with patch.object(ga, 'api_get', side_effect=[{'data': orders}, {'data': []}]), \
            patch.object(ga, 'get_item_tax_rates', return_value={1: 8}):
        ga.sync_orders_for_date('2026-11-05')
    row = get_db().execute("SELECT * FROM daily_takeaway WHERE date='2026-11-05'").fetchone()
    assert (row['orders'], row['revenue'], row['takeaway_orders'], row['takeaway_revenue']) == (3, 90.0, 2, 50.0)


def test_resync_replaces_the_day_instead_of_adding():
    for _ in range(2):
        with patch.object(ga, 'api_get', side_effect=[{'data': [_order(1, 20, 'Latte', 'To-go cup')]}, {'data': []}]), \
                patch.object(ga, 'get_item_tax_rates', return_value={1: 8}):
            ga.sync_orders_for_date('2026-11-05')
    assert get_db().execute('SELECT COUNT(*), SUM(orders) FROM daily_takeaway').fetchone()[:] == (1, 1)


def test_backfill_touches_only_the_takeaway_table():
    with patch.object(ga, 'fetch_closed_orders', return_value=[_order(1, 20, 'Latte', 'To-go cup')]):
        assert ga.backfill_takeaway('2026-11-01', '2026-11-02') == 2
    db = get_db()
    assert db.execute('SELECT COUNT(*) FROM daily_takeaway').fetchone()[0] == 2
    assert db.execute('SELECT COUNT(*) FROM sales').fetchone()[0] == 0
    with patch.object(ga, 'fetch_closed_orders', return_value=[]):  # empty answer: keep what we have
        assert ga.backfill_takeaway('2026-11-01', '2026-11-01') == 0
    assert db.execute('SELECT COUNT(*) FROM daily_takeaway').fetchone()[0] == 2


def test_tool_summary_math():
    _put('2026-11-23', 100, 3000.0, 48, 1100.0)
    _put('2026-11-24', 100, 3200.0, 50, 1300.0)
    r = tt.takeaway_summary({'date_from': '2026-11-23', 'date_to': '2026-11-24'}, TODAY)
    assert r['orders'] == 200 and r['takeaway_orders'] == 98
    assert r['takeaway_share_of_orders_pct'] == 49.0
    assert r['takeaway_revenue_zl'] == 2400.0 and r['dine_in_revenue_zl'] == 3800.0
    assert r['takeaway_share_of_revenue_pct'] == 38.7
    assert r['avg_check_takeaway_zl'] == 24.49 and r['avg_check_dine_in_zl'] == 37.25
    assert r['days_with_data'] == 2 and 'To-go cup' in r['note']


def test_tool_without_data_and_with_bad_dates():
    r = tt.run_tool('takeaway_summary', {'date_from': '2026-11-01', 'date_to': '2026-11-02'}, TODAY, False)
    assert r['no_data'] is True and 'orders' not in r
    assert 'error' in tt.run_tool('takeaway_summary', {'date_from': 'nope'}, TODAY, False)


def test_tool_is_available_without_finance():
    assert any(s['toolSpec']['name'] == 'takeaway_summary' for s in tt.tool_specs(False))


def test_skill_compares_two_weeks_and_warns_about_gaps():
    # today Wed 11-25: last complete week 11-18..11-24, week before 11-11..11-17
    for d in ('2026-11-18', '2026-11-19', '2026-11-20'):
        _put(d, 100, 3000.0, 48, 1100.0)
    _put('2026-11-12', 100, 3000.0, 40, 900.0)
    out = skills.takeaway_week(TODAY, False)
    assert '2026-11-18 – 2026-11-24' in out
    assert 'Замовлень: 48% (144 з 300)' in out and 'Чек: 23 zł на виніс vs 37 zł в залі' in out
    assert 'Тиждень до того: 40% замовлень' in out
    assert 'Дані лише за 3 з 7 днів' in out and 'стаканчик' in out


def test_skill_without_data():
    assert 'даних ще немає' in skills.takeaway_week(TODAY, False)


def test_skill_is_registered_for_every_chat():
    assert 'takeaway' in skills.available(False)
