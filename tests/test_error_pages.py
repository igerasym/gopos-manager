"""Browser requests get styled error pages; API/JSON requests keep JSON."""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.db import init_db, get_db
from app.auth import hash_password, sign_cookie

HTML = {'accept': 'text/html,application/xhtml+xml'}


@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    monkeypatch.setattr('app.db.DB_PATH', tmp_path / 'test.db')
    init_db()
    db = get_db()
    for name, role in (('admin', 'admin'), ('staff', 'barista')):
        db.execute('INSERT INTO users (username, password_hash, role, display_name) VALUES (?, ?, ?, ?)',
                   (name, hash_password('x'), role, name))
    db.commit()
    db.close()


def _client(user):
    c = TestClient(app, follow_redirects=False, raise_server_exceptions=False)
    c.cookies.set('session', sign_cookie(user))
    return c


def test_404_html_page_in_shell():
    r = _client('admin').get('/no-such-page', headers=HTML)
    assert r.status_code == 404
    assert 'text/html' in r.headers['content-type']
    assert 'Такої сторінки немає' in r.text
    assert 'На головну' in r.text and 'href="/"' in r.text
    assert 'class="bottom-nav"' in r.text      # inside the app shell
    assert '"detail"' not in r.text


def test_404_json_for_api_and_json_accept():
    c = _client('admin')
    r = c.get('/api/no-such-thing', headers=HTML)
    assert r.status_code == 404 and r.json() == {'detail': 'Not Found'}
    r = c.get('/no-such-page', headers={'accept': 'application/json'})
    assert r.status_code == 404 and r.json() == {'detail': 'Not Found'}


def test_403_html_for_staff_goes_to_stock():
    r = _client('staff').get('/expenses', headers=HTML)
    assert r.status_code == 403
    assert 'Сюди у вас немає доступу' in r.text
    assert 'href="/inventory"' in r.text
    assert 'zł' not in r.text


def test_403_json_for_api():
    r = _client('staff').get('/api/sync', headers={'accept': 'application/json'})
    assert r.status_code == 403
    assert r.headers['content-type'].startswith('application/json')


def test_500_html_page():
    def boom(*a, **k):
        raise RuntimeError('secret traceback text')
    route = next(r for r in app.routes if getattr(r, 'path', '') == '/more')
    orig = route.endpoint
    try:
        route.dependant.call = boom
        r = _client('admin').get('/more', headers=HTML)
    finally:
        route.dependant.call = orig
    assert r.status_code == 500
    assert 'Щось пішло не так' in r.text
    assert 'secret traceback text' not in r.text
