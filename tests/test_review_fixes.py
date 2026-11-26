"""Fixes from the new-user review: units never glued to numbers, business-day hint,
takeaway share of revenue, new-dish category defaults."""
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.db import get_db, init_db
from app.routes.dashboard import business_night_hint, checks_stats
from app.routes.recipes import card_categories, default_category
from app.services.recipes import card_category_for_pos

TEMPLATES = Path(__file__).resolve().parent.parent / 'app' / 'templates'
W = ZoneInfo('Europe/Warsaw')


@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    monkeypatch.setattr('app.db.DB_PATH', tmp_path / 'test.db')
    init_db()


def test_no_number_glued_to_a_unit_in_templates():
    """'1 690,02zł' must never happen: a filter output or digit is followed by &nbsp;."""
    glued = re.compile(r'(\}\}|\d)( |<small>|\s?<small>)(zł|кг|л|шт|мл)\b')
    bad = []
    for p in TEMPLATES.glob('*.html'):
        for n, line in enumerate(p.read_text().splitlines(), 1):
            if glued.search(line) and 'input-suffix' not in line:
                bad.append(f'{p.name}:{n}: {line.strip()[:90]}')
    assert not bad, bad


def test_business_night_hint():
    assert business_night_hint(datetime(2026, 11, 26, 1, 40, tzinfo=W)) == 'Робочий день триває до 05:00'
    assert business_night_hint(datetime(2026, 11, 26, 5, 0, tzinfo=W)) == ''
    assert business_night_hint(datetime(2026, 11, 26, 23, 59, tzinfo=W)) == ''


def test_takeaway_share_of_checks_and_revenue():
    db = get_db()
    db.execute("INSERT INTO daily_takeaway VALUES ('2026-11-25', 100, 2000, 33, 560)")
    db.commit()
    s = checks_stats(db, '2026-11-25', '2026-11-25')
    db.close()
    assert round(s['takeaway_pct']) == 33 and round(s['takeaway_rev_pct']) == 28


def test_new_dish_category_defaults():
    assert default_category('bar') == 'Бар'
    assert default_category('bakery') == 'Випічка'
    assert default_category('kitchen') == 'Кухня'
    assert card_category_for_pos('Kawa') == 'Бар'
    assert card_category_for_pos('Desery') == 'Випічка'
    assert card_category_for_pos('Śniadania') == 'Кухня'
    assert card_category_for_pos('') == ''
    assert card_categories(['Бар', 'Супи']) == ['Кухня', 'Бар', 'Випічка', 'Супи']
