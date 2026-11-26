"""Sync button label + 'data updated at' text next to it."""
from datetime import datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.auth import sign_cookie
from app.db import init_db
from app.main import app
from app.services.attention import WARSAW


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    monkeypatch.setattr('app.db.DB_PATH', tmp_path / 'test.db')
    init_db()
    yield


def _status(fresh_at):
    c = TestClient(app)
    c.cookies.set('session', sign_cookie('admin'))
    with patch('app.services.attention.data_freshness', return_value={'at': fresh_at, 'error': False, 'text': ''}):
        return c.get('/api/sync-status').json()


def test_today_shows_only_the_time():
    now = datetime.now(WARSAW).replace(hour=14, minute=15, second=0, microsecond=0)
    d = _status(now)
    assert d['refreshed_short'] == 'оновлено о 14:15' and d['refreshed_at']


def test_older_refresh_shows_the_date_too():
    old = datetime(2026, 11, 20, 4, 30, tzinfo=WARSAW)
    assert _status(old)['refreshed_short'] == 'оновлено 20 лист. о 04:30'


def test_never_refreshed():
    d = _status(None)
    assert d['refreshed_at'] is None and d['refreshed_short'] == 'ще не оновлювалось'


def test_button_is_small_and_named_plainly():
    c = TestClient(app)
    c.cookies.set('session', sign_cookie('admin'))
    html = c.get('/').text
    assert '↻ Оновити' in html and 'Синк GoPOS' not in html


# ── the label must not fall back to the night sync after a restart ──

def _it(name='Latte', paid=20):
    return {'item_id': 1, 'name': name, 'quantity': 1, 'status': 'ACTIVE',
            'sub_total_price': {'amount': paid}, 'total_price': {'amount': paid}}


def _order(oid=101):
    return {'id': oid, 'status': 'CLOSED', 'closed_at': '2026-11-26T10:30:00',
            'total_price': {'amount': 20}, 'total_price_net': {'amount': 18.52}, 'items': [_it()]}


def _state():
    from app.db import get_db
    row = get_db().execute("SELECT value FROM app_state WHERE key = 'sales_refreshed_at'").fetchone()
    return row['value'] if row else None


def test_webhook_order_marks_sales_as_refreshed():
    from app import gopos_api as ga
    from app.routes import webhook
    assert _state() is None
    with patch.object(ga, 'api_get', return_value={'data': _order()}), \
            patch.object(ga, 'get_item_tax_rates', return_value={1: 8}):
        webhook._process_order_webhook('101')
    assert _state()


def test_a_replayed_webhook_order_does_not_move_the_time():
    from app import gopos_api as ga
    from app.db import get_db
    from app.routes import webhook
    with patch.object(ga, 'api_get', return_value={'data': _order()}), \
            patch.object(ga, 'get_item_tax_rates', return_value={1: 8}):
        webhook._process_order_webhook('101')
        first = _state()
        db = get_db()
        db.execute("UPDATE app_state SET value = '2000-01-01 00:00:00'")
        db.commit()
        webhook._process_order_webhook('101')   # already counted: nothing changes
    assert _state() == '2000-01-01 00:00:00' and first


def test_full_sync_marks_sales_as_refreshed():
    from app import gopos_api as ga
    with patch.object(ga, 'api_get', side_effect=[{'data': [_order()]}, {'data': []}]), \
            patch.object(ga, 'get_item_tax_rates', return_value={1: 8}):
        ga.sync_orders_for_date('2026-11-26')
    assert _state()


def test_freshness_survives_a_restart_and_beats_the_night_sync(monkeypatch):
    """RAM is empty (fresh process) and the last sync_log row is the night job: the stored mark
    from a later webhook order must win."""
    from app import gopos_api as ga
    from app.db import get_db
    from app.services.attention import data_freshness
    monkeypatch.setattr(ga, 'LAST_INTRADAY_SYNC', None)
    monkeypatch.setattr(ga, 'LAST_INTRADAY_DAY', None)
    db = get_db()
    db.execute("INSERT INTO sync_log (started_at, finished_at, status, message) VALUES "
               "('2026-11-26 04:30:00', '2026-11-26 04:30:25', 'done', 'Synced 2026-11-25: 69 products via API')")
    db.execute("INSERT INTO app_state (key, value) VALUES ('sales_refreshed_at', '2026-11-26 09:42:10')")
    db.commit()
    now = datetime(2026, 11, 26, 11, 0, tzinfo=WARSAW)
    f = data_freshness(db, now)
    assert f['at'].strftime('%H:%M') == '10:42'            # 09:42 UTC = 10:42 Warsaw, not the night sync
    assert 'о 10:42' in f['text']


def test_without_any_mark_it_falls_back_to_the_last_sync(monkeypatch):
    from app import gopos_api as ga
    from app.db import get_db
    from app.services.attention import data_freshness, _local_to_warsaw
    monkeypatch.setattr(ga, 'LAST_INTRADAY_SYNC', None)
    db = get_db()
    db.execute("INSERT INTO sync_log (started_at, finished_at, status, message) VALUES "
               "('2026-11-26 04:30:00', '2026-11-26 04:30:25', 'done', 'x')")
    db.commit()
    f = data_freshness(db, datetime(2026, 11, 26, 11, 0, tzinfo=WARSAW))
    # the sync_log clock is the server's local time: the machine's zone decides 04:30 vs 05:30
    assert f['at'] == _local_to_warsaw('2026-11-26 04:30:25')
