"""Invoice prices are per invoice unit (carton, crate, can): store them per ingredient unit."""
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password, sign_cookie
from app.db import get_db, init_db
from app.main import app
from app.services import pack_pricing as pp
from app.services.invoice_processing import _handle_match_mapping


@pytest.fixture(autouse=True)
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr('app.db.DB_PATH', tmp_path / 'test.db')
    init_db()
    db = get_db()
    db.execute("INSERT INTO users (username, password_hash, role, display_name) VALUES ('admin', ?, 'admin', 'A')",
               (hash_password('x'),))
    db.execute("INSERT INTO ingredients (id, name, unit, quantity, unit_price) VALUES (5, 'Mleko 3.2%', 'L', 0, 3.29)")
    db.execute("INSERT INTO ingredients (id, name, unit, quantity, unit_price) VALUES (98, 'Pomarańcza', 'kg', 0, 7.6)")
    db.execute("INSERT INTO ingredients (id, name, unit, quantity, unit_price) VALUES (7, 'Masło', 'kg', 0, 40)")
    db.execute("INSERT INTO ingredients (id, name, unit, quantity, unit_price) VALUES (8, 'Jajka', 'szt', 0, 1)")
    db.execute("INSERT INTO ingredients (id, name, unit, quantity, unit_price) VALUES (9, 'Olej rzepakowy', 'L', 0, 6.6)")
    db.commit()
    db.close()
    yield


# ── which factor wins (per line, by fit with the current price) ──

def test_candidates_order_and_dedupe():
    db = get_db()
    pp.save_mapping(db, 'M', 5, 'match', pack_factor=12)
    assert pp.candidates(db, 'M', llm_factor=12) == [12.0, 1.0]
    assert pp.candidates(db, 'M', llm_factor=3) == [12.0, 3.0, 1.0]
    assert pp.candidates(db, 'NONE') == [1.0]


def test_the_candidate_that_fits_the_current_price_wins():
    # one name sold both as a carton (39.48) and as single litres (4.42); current price 3.29 per litre
    assert pp.choose(39.48, 3.29, [12.0, 1.0]) == (pytest.approx(3.29), 12.0)
    assert pp.choose(4.42, 3.29, [12.0, 1.0]) == (4.42, 1.0)


def test_no_fit_or_no_current_price_uses_the_first_candidate():
    assert pp.choose(39.48, 3.29, [1.0]) == (39.48, 1.0)
    assert pp.choose(39.48, 0, [12.0, 1.0])[1] == 12.0
    assert pp.choose(0, 3.29, [12.0, 1.0]) == (0.0, 12.0)


def test_a_size_in_the_name_is_never_read():
    db = get_db()
    assert pp.candidates(db, 'MASLO EXTRA 200 G') == [1.0]
    assert pp.price_per_unit(db, 'MASLO EXTRA 200 G', 7, 42.49) == 42.49
    assert pp.price_per_unit(db, 'POMARANCZE NA SOK 15KG', 98, 113.99) == 113.99


def test_price_per_unit_uses_the_stored_factor():
    db = get_db()
    pp.save_mapping(db, 'POMARANCZE NA SOK 15KG EG', 98, 'match', pack_factor=15)
    assert pp.price_per_unit(db, 'POMARANCZE NA SOK 15KG EG', 98, 113.99) == pytest.approx(7.599, abs=1e-3)


def test_save_mapping_keeps_an_existing_factor_and_never_stores_one():
    db = get_db()
    pp.save_mapping(db, 'A', 5, 'match', pack_factor=12)
    pp.save_mapping(db, 'A', 5, 'match')                    # confirmed again with no factor
    pp.save_mapping(db, 'A', 5, 'match', pack_factor=3)     # or with another
    assert pp.stored_factor(db, 'A') == 12
    pp.save_mapping(db, 'B', 5, 'match', pack_factor=1.0)   # a factor of 1 is "unknown", not a pack size
    assert pp.stored_factor(db, 'B') is None


# ── the automatic import ──

def _match(db, name, qty, price, ing_id, **extra):
    m = {'invoice_name': name, 'quantity': qty, 'unit_price': price, 'action': 'match', **extra}
    _handle_match_mapping(db, m, ing_id, 1)
    db.commit()
    return db.execute('SELECT unit_price FROM ingredients WHERE id = ?', (ing_id,)).fetchone()[0]


def test_known_mapping_with_a_stored_factor_prices_per_litre():
    db = get_db()
    pp.save_mapping(db, 'LACIATE MLEKO UHT 3,2% 1L', 5, 'match', pack_factor=12)
    assert _match(db, 'LACIATE MLEKO UHT 3,2% 1L', 4, 39.48, 5) == pytest.approx(3.29)


def test_new_mapping_uses_the_models_conversion_and_remembers_it():
    db = get_db()
    # 2 crates of 15 kg, 113.99 each: the model says 30 kg
    price = _match(db, 'POMARANCZE NA SOK 15KG', 2, 113.99, 98, pack_factor=15.0, converted_qty=30)
    assert price == pytest.approx(7.599, abs=1e-3)
    assert pp.stored_factor(db, 'POMARANCZE NA SOK 15KG') == 15.0
    # the next invoice has no model answer for this known name: the stored factor is used
    assert _match(db, 'POMARANCZE NA SOK 15KG', 1, 119.0, 98) == pytest.approx(7.933, abs=1e-3)


def test_delivery_total_equals_the_invoice_line_total():
    db = get_db()
    _match(db, 'ARO OLEJ RZEPAKOWY 10L', 3, 66.35, 9, pack_factor=10.0, converted_qty=30)
    row = db.execute('SELECT quantity, price FROM deliveries').fetchone()
    assert row['quantity'] == 30 and row['price'] == pytest.approx(3 * 66.35)


def test_implausible_jump_still_skips_the_update():
    db = get_db()
    # milk at 39.48 per litre against 3.29 now: factor unknown (1), so the 0.2-5x guard holds
    assert _match(db, 'UNKNOWN MLEKO 1L', 4, 39.48, 5) == 3.29


def test_same_name_sold_as_carton_and_as_single_litres():
    db = get_db()
    pp.save_mapping(db, 'LAC.MLEKO UHT 3,2% 1L', 5, 'match', pack_factor=12)
    assert _match(db, 'LAC.MLEKO UHT 3,2% 1L', 4, 39.48, 5) == pytest.approx(3.29)
    assert _match(db, 'LAC.MLEKO UHT 3,2% 1L', 12, 4.42, 5) == pytest.approx(4.42)


# ── the model mapping ──

def test_llm_mapping_returns_the_factor_for_new_and_known_lines():
    from app import llm
    reply = json.dumps([{'item_index': 0, 'action': 'match', 'ingredient_id': 5, 'confidence': 0.95,
                         'suggested_name': '', 'converted_qty': 48}])
    items = [{'name': 'NEW MLEKO 1L', 'quantity': 4, 'unit_price': 39.48},
             {'name': 'KNOWN MLEKO 1L', 'quantity': 2, 'unit_price': 39.48}]
    with patch.object(llm, 'call_llm', return_value=reply) as call:
        out = llm.map_items_to_ingredients(items, [{'id': 5, 'name': 'Mleko 3.2%', 'unit': 'L', 'unit_price': 3.29}],
                                           {'KNOWN MLEKO 1L': 5}, [], {'KNOWN MLEKO 1L': 12.0})
    by_name = {r['invoice_name']: r for r in out}
    assert by_name['NEW MLEKO 1L']['pack_factor'] == 12.0           # 48 L from 4 cartons
    assert by_name['KNOWN MLEKO 1L']['pack_factor'] == 12.0         # the stored one
    prompt = call.call_args.args[0]
    assert 'now 3.29 zł per L' in prompt and 'PRICE IS PER INVOICE UNIT' in prompt


# ── the manual paths ──

def _client():
    c = TestClient(app, follow_redirects=False)
    c.cookies.set('session', sign_cookie('admin'))
    return c


def _pending(factor=None):
    db = get_db()
    db.execute("INSERT INTO parsed_invoices (id, file_id, file_name, vendor) VALUES (1, 'f', 'f.pdf', 'Makro')")
    db.execute("INSERT INTO invoice_items_pending (id, parsed_invoice_id, invoice_name, quantity, unit_price, status, pack_factor) "
               "VALUES (1, 1, 'POMARANCZE NA SOK MC 15KG EG', 2, 113.99, 'pending', ?)", (factor,))
    db.commit()
    db.close()


def test_a_typed_pack_size_wins_over_the_models_guess():
    _pending(factor=10)
    r = _client().post('/expenses/items/confirm/1', data={'action': 'match', 'ingredient_id': '98', 'pack_factor': '15'})
    assert r.status_code == 200
    db = get_db()
    assert db.execute('SELECT unit_price FROM ingredients WHERE id=98').fetchone()[0] == pytest.approx(7.599, abs=1e-3)
    assert pp.stored_factor(db, 'POMARANCZE NA SOK MC 15KG EG') == 15


def test_without_any_pack_size_the_price_is_kept_as_printed():
    _pending(factor=None)
    _client().post('/expenses/items/confirm/1', data={'action': 'match', 'ingredient_id': '98'})
    db = get_db()
    assert db.execute('SELECT unit_price FROM ingredients WHERE id=98').fetchone()[0] == 113.99


def test_confirming_a_pending_item_converts_the_price_and_stores_the_factor():
    _pending(factor=15.0)
    r = _client().post('/expenses/items/confirm/1', data={'action': 'match', 'ingredient_id': '98'})
    assert r.status_code == 200
    db = get_db()
    assert db.execute('SELECT unit_price FROM ingredients WHERE id=98').fetchone()[0] == pytest.approx(7.599, abs=1e-3)
    assert pp.stored_factor(db, 'POMARANCZE NA SOK MC 15KG EG') == 15


def test_approving_an_invoice_converts_prices_of_mapped_items():
    db = get_db()
    pp.save_mapping(db, 'LACIATE MLEKO UHT 3,2% 1L', 5, 'match', pack_factor=12)
    items = [{'name': 'LACIATE MLEKO UHT 3,2% 1L', 'quantity': 4, 'unit_price': 39.48, 'total': 157.92}]
    db.execute("INSERT INTO parsed_invoices (id, file_id, file_name, vendor, month, total, items_json, parse_status) "
               "VALUES (1, 'f', 'f.pdf', 'Makro', '2026-11', 157.92, ?, 'done')", (json.dumps(items),))
    db.commit()
    db.close()
    routes = [r.path for r in app.routes if 'approve' in r.path]
    assert routes, 'approve route not found'
    r = _client().post(routes[0].replace('{invoice_id}', '1'), data={'category': 'Продукти'})
    assert r.status_code in (200, 302, 303)
    db = get_db()
    assert db.execute('SELECT unit_price FROM ingredients WHERE id=5').fetchone()[0] == pytest.approx(3.29)
    assert db.execute('SELECT price FROM ingredient_price_history WHERE ingredient_id=5').fetchone()[0] == pytest.approx(3.29)
