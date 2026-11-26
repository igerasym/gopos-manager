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
