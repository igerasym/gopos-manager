"""Simple cookie-based auth with roles."""
import hashlib
import hmac
import os
import time
from functools import wraps

from fastapi import Request, HTTPException
from fastapi.responses import RedirectResponse

from app.db import get_db

SECRET = os.getenv('SESSION_SECRET', 'the-frame-cafe-secret-2026')

# Simple user cache (username -> (user_dict, timestamp))
_user_cache = {}
_CACHE_TTL = 300  # 5 minutes

# Roles and their allowed paths
ROLE_ACCESS = {
    'admin': None,  # None = access to everything
    'chef': ['/inventory', '/recipes', '/api/sync-status',
             '/ingredients/', '/recipes/'],
    'barista': ['/inventory', '/recipes', '/api/sync-status',
                '/ingredients/', '/recipes/'],
}


def hash_password(password: str) -> str:
    salt = os.urandom(16).hex()
    h = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100_000).hex()
    return f'{salt}:{h}'


def verify_password(password: str, password_hash: str) -> bool:
    salt, h = password_hash.split(':')
    return hmac.compare_digest(
        h, hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100_000).hex()
    )


def sign_cookie(value: str) -> str:
    sig = hmac.new(SECRET.encode(), value.encode(), 'sha256').hexdigest()[:16]
    return f'{value}.{sig}'


def verify_cookie(cookie: str) -> str | None:
    if not cookie or '.' not in cookie:
        return None
    value, sig = cookie.rsplit('.', 1)
    expected = hmac.new(SECRET.encode(), value.encode(), 'sha256').hexdigest()[:16]
    if hmac.compare_digest(sig, expected):
        return value
    return None


def get_current_user(request: Request) -> dict | None:
    cookie = request.cookies.get('session')
    username = verify_cookie(cookie) if cookie else None
    if not username:
        return None

    # Check cache
    now = time.time()
    if username in _user_cache:
        user, ts = _user_cache[username]
        if now - ts < _CACHE_TTL:
            return user

    db = get_db()
    user = db.execute(
        'SELECT id, username, role, display_name FROM users WHERE username = ?',
        (username,)
    ).fetchone()
    db.close()
    result = dict(user) if user else None
    if result:
        _user_cache[username] = (result, now)
    return result


def can_access(user: dict, path: str) -> bool:
    if not user:
        return False
    role = user['role']
    allowed = ROLE_ACCESS.get(role)
    if allowed is None:  # admin
        return True
    return any(path.startswith(p) for p in allowed)


def create_default_admin():
    """Create admin user if no users exist."""
    db = get_db()
    count = db.execute('SELECT COUNT(*) as c FROM users').fetchone()['c']
    if count == 0:
        db.execute(
            'INSERT INTO users (username, password_hash, role, display_name) VALUES (?, ?, ?, ?)',
            ('admin', hash_password('admin'), 'admin', 'Admin')
        )
        db.commit()
    db.close()
