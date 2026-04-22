"""Tests for unit conversion utilities."""
from app.services.units import to_display, from_display


def test_kg_to_g():
    amount, unit = to_display(0.015, 'kg')
    assert amount == 15.0
    assert unit == 'g'


def test_l_to_ml():
    amount, unit = to_display(0.008, 'L')
    assert amount == 8.0
    assert unit == 'ml'


def test_szt_unchanged():
    amount, unit = to_display(3, 'szt')
    assert amount == 3
    assert unit == 'szt'


def test_zero_kg():
    amount, unit = to_display(0, 'kg')
    assert amount == 0.0
    assert unit == 'g'


def test_from_display_g_to_kg():
    assert from_display(15, 'kg') == 0.015


def test_from_display_ml_to_l():
    assert from_display(8, 'L') == 0.008


def test_from_display_szt():
    assert from_display(3, 'szt') == 3


def test_from_display_zero():
    assert from_display(0, 'kg') == 0.0


def test_roundtrip_kg():
    original = 0.0525
    display_amount, display_unit = to_display(original, 'kg')
    back = from_display(display_amount, 'kg')
    assert abs(back - original) < 0.0001


def test_roundtrip_l():
    original = 0.08
    display_amount, display_unit = to_display(original, 'L')
    back = from_display(display_amount, 'L')
    assert abs(back - original) < 0.0001
