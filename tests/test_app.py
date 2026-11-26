"""Integration tests for the FastAPI app."""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.db import init_db, get_db
from app.auth import hash_password, sign_cookie


@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    """Use a temporary database for each test."""
    db_path = tmp_path / 'test.db'
    # Patch DB_PATH in all modules that import it
    monkeypatch.setattr('app.db.DB_PATH', db_path)
    init_db()
    db = get_db()
    db.execute(
        'INSERT INTO users (username, password_hash, role, display_name) VALUES (?, ?, ?, ?)',
        ('admin', hash_password('admin'), 'admin', 'Admin')
    )
    db.commit()
    db.close()
    yield


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def admin_cookies():
    return {'session': sign_cookie('admin')}


def test_login_page(client):
    r = client.get('/login')
    assert r.status_code == 200
    assert 'Увійти' in r.text


def test_login_wrong_password(client):
    r = client.post('/login', data={'username': 'admin', 'password': 'wrong'})
    assert r.status_code == 200
    assert 'Невірний' in r.text


def test_login_success(client):
    r = client.post('/login', data={'username': 'admin', 'password': 'admin'}, follow_redirects=False)
    assert r.status_code == 302
    assert 'session' in r.cookies


def test_redirect_to_login_without_auth(client):
    r = client.get('/', follow_redirects=False)
    assert r.status_code == 302
    assert '/login' in r.headers['location']


def test_dashboard_with_auth(client, admin_cookies):
    r = client.get('/', cookies=admin_cookies)
    assert r.status_code == 200
    assert 'Дашборд' in r.text or 'Виручка' in r.text


def test_inventory_page(client, admin_cookies):
    r = client.get('/inventory', cookies=admin_cookies)
    assert r.status_code == 200
    assert 'Склад' in r.text


def test_recipes_page(client, admin_cookies):
    r = client.get('/recipes', cookies=admin_cookies)
    assert r.status_code == 200
    assert 'Кухня' in r.text


def test_users_page(client, admin_cookies):
    r = client.get('/users', cookies=admin_cookies)
    assert r.status_code == 200
    assert 'admin' in r.text


def test_sync_status_api(client, admin_cookies):
    r = client.get('/api/sync-status', cookies=admin_cookies)
    assert r.status_code == 200
    assert r.json()['status'] in ('none', 'done', 'running', 'error')


def test_add_ingredient(client, admin_cookies):
    r = client.post('/inventory/add', data={
        'name': 'Test Ingredient', 'unit': 'kg', 'quantity': '5', 'min_quantity': '1'
    }, cookies=admin_cookies, follow_redirects=False)
    assert r.status_code == 303
    # Verify it was added
    db = get_db()
    ing = db.execute('SELECT * FROM ingredients WHERE name = ?', ('Test Ingredient',)).fetchone()
    db.close()
    assert ing is not None
    assert ing['quantity'] == 5.0


def test_logout(client, admin_cookies):
    r = client.get('/logout', cookies=admin_cookies, follow_redirects=False)
    assert r.status_code == 302
    assert '/login' in r.headers['location']
