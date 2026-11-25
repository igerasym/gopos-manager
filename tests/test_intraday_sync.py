"""Today's sales are refreshed during the day; the bot and dashboard say how fresh they are."""
import threading
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import gopos_api, telegram_tools as tt
from app.main import app
from app.db import init_db, get_db
from app.auth import hash_password, sign_cookie

TODAY = '2026-11-25'


@pytest.fixture(autouse=True)
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr('app.db.DB_PATH', tmp_path / 'test.db')
    monkeypatch.setattr(gopos_api, 'LAST_INTRADAY_SYNC', None)
    monkeypatch.setattr('app.routes.dashboard.warsaw_today', lambda: TODAY)
    init_db()
    db = get_db()
    db.execute("INSERT INTO users (username, password_hash, role, display_name) VALUES ('admin', ?, 'admin', 'A')",
               (hash_password('x'),))
    db.commit()
    db.close()
    yield


def test_intraday_sync_refreshes_only_today_and_records_the_time(monkeypatch):
    monkeypatch.setattr(gopos_api, 'warsaw_today', lambda: TODAY)
    with patch.object(gopos_api, 'sync_orders_for_date', return_value=7) as sync, \
            patch.object(gopos_api, '_deduct_inventory') as deduct:
        assert gopos_api.sync_intraday() == 7
    sync.assert_called_once_with(TODAY)
    deduct.assert_not_called()                      # the evening sync owns stock deduction
    assert gopos_api.LAST_INTRADAY_SYNC and len(gopos_api.LAST_INTRADAY_SYNC) == 5


def test_intraday_sync_skips_when_a_run_is_in_progress():
    gopos_api._intraday_lock.acquire()
    try:
        with patch.object(gopos_api, 'sync_orders_for_date') as sync:
            assert gopos_api.sync_intraday() is None
        sync.assert_not_called()
    finally:
        gopos_api._intraday_lock.release()


def test_failed_intraday_sync_keeps_the_previous_refresh_time(monkeypatch):
    monkeypatch.setattr(gopos_api, 'LAST_INTRADAY_SYNC', '10:15')
    with patch.object(gopos_api, 'sync_orders_for_date', side_effect=RuntimeError('api down')):
        with pytest.raises(RuntimeError):
            gopos_api.sync_intraday()
    assert gopos_api.LAST_INTRADAY_SYNC == '10:15'
    assert gopos_api._intraday_lock.acquire(blocking=False)   # lock released after a failure
    gopos_api._intraday_lock.release()


def test_bot_marks_ranges_that_include_today_as_partial(monkeypatch):
    monkeypatch.setattr(gopos_api, 'LAST_INTRADAY_SYNC', '14:45')
    out = tt.sales_summary({'date_from': TODAY}, TODAY)
    assert out['includes_today_still_running'] is True
    assert out['today_last_refreshed_warsaw'] == '14:45'
    assert 'partial' in out['note']
    past = tt.sales_summary({'date_from': '2026-11-24'}, TODAY)
    assert 'includes_today_still_running' not in past
    for name, args in (('daily_revenue', {'date_from': TODAY}), ('top_products', {'date_from': TODAY}),
                       ('product_sales', {'date_from': TODAY, 'product': 'x'})):
        assert tt.run_tool(name, args, TODAY, False)['includes_today_still_running'] is True


def test_today_skill_with_no_sales_does_not_claim_data_is_unavailable(monkeypatch):
    from app import telegram_skills as sk
    monkeypatch.setattr(gopos_api, 'LAST_INTRADAY_SYNC', '09:15')
    out = sk.today_sales(TODAY, False)
    assert 'ще не записано' in out and '09:15' in out


def test_dashboard_shows_refresh_time_for_the_running_day(monkeypatch):
    monkeypatch.setattr(gopos_api, 'LAST_INTRADAY_SYNC', '13:45')
    db = get_db()
    db.execute("INSERT INTO sales (date, product_name, quantity, total_money) VALUES (?, 'Latte', 2, 40)", (TODAY,))
    db.commit()
    db.close()
    c = TestClient(app, follow_redirects=False)
    c.cookies.set('session', sign_cookie('admin'))
    assert 'дані станом на 13:45' in c.get('/').text


def test_scheduler_registers_the_intraday_job():
    import inspect
    src = inspect.getsource(__import__('app.main', fromlist=['x']))
    assert "scheduled_intraday" in src and "minute='15,45'" in src and "timezone='Europe/Warsaw'" in src
