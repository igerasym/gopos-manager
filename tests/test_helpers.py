"""Shared helpers that replaced copies spread over several modules."""
from types import SimpleNamespace

import pytest

from app.auth import is_admin
from app.gopos_api import month_bounds


@pytest.mark.parametrize('month,expected', [
    ('2026-11', ('2026-11-01', '2026-12-01')),
    ('2026-12', ('2026-12-01', '2027-01-01')),
    ('2027-01', ('2027-01-01', '2027-02-01')),
])
def test_month_bounds(month, expected):
    assert month_bounds(month) == expected


def test_is_admin_takes_a_user_or_a_request():
    assert is_admin({'role': 'admin'})
    assert not is_admin({'role': 'chef'})
    assert not is_admin(None)
    assert is_admin(SimpleNamespace(state=SimpleNamespace(user={'role': 'admin'})))
    assert not is_admin(SimpleNamespace(state=SimpleNamespace(user={'role': 'barista'})))
    assert not is_admin(SimpleNamespace(state=SimpleNamespace()))   # no user on the request
