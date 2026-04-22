"""Tests for auth utilities."""
from app.auth import hash_password, verify_password, sign_cookie, verify_cookie


def test_password_hash_and_verify():
    pw = 'TestPass123!'
    hashed = hash_password(pw)
    assert verify_password(pw, hashed)


def test_wrong_password():
    hashed = hash_password('correct')
    assert not verify_password('wrong', hashed)


def test_cookie_sign_and_verify():
    value = 'admin'
    signed = sign_cookie(value)
    assert '.' in signed
    assert verify_cookie(signed) == value


def test_cookie_tampered():
    signed = sign_cookie('admin')
    tampered = signed[:-1] + 'x'
    assert verify_cookie(tampered) is None


def test_cookie_empty():
    assert verify_cookie('') is None
    assert verify_cookie(None) is None
