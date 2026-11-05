"""GoPos item → sales money fields (sub_total_price = BEFORE discount, total_price = AFTER)."""
from unittest.mock import patch

from app import gopos_api as ga


def _it(item_id, before, paid, qty=1, name='X'):
    return {'item_id': item_id, 'name': name, 'quantity': qty, 'status': 'ACTIVE',
            'sub_total_price': {'amount': before}, 'total_price': {'amount': paid}}


def test_full_price_item_23pct():
    a = ga.item_amounts(_it(1, 20, 20), {}, {1: 23})
    assert a == {'total_money': 20, 'net_total': 16.26, 'discount': 0, 'net_profit': 20}


def test_staff_100pct_discount():
    # real Staff order: Cappuccino 22 zł → paid 0
    a = ga.item_amounts(_it(1, 22, 0), {}, {1: 8})
    assert a == {'total_money': 0, 'net_total': 0, 'discount': 22, 'net_profit': 22}


def test_cookie_countdown_half_price():
    a = ga.item_amounts(_it(2, 16, 8), {}, {2: 5})
    assert a['total_money'] == 8 and a['discount'] == 8 and a['net_profit'] == 16
    assert a['net_total'] == round(8 / 1.05, 2)


def test_unknown_item_uses_order_net_ratio():
    order = {'total_price': {'amount': 100}, 'total_price_net': {'amount': 81.30}}
    a = ga.item_amounts(_it(999, 50, 50), order, {})
    assert a['net_total'] == 40.65


def test_sync_orders_for_date_writes_fixed_fields(tmp_path, monkeypatch):
    from app.db import init_db, get_db
    monkeypatch.setattr('app.db.DB_PATH', tmp_path / 'test.db')
    init_db()
    orders = {'data': [{'total_price': {'amount': 22}, 'total_price_net': {'amount': 20.37},
                        'items': [_it(1, 22, 22, name='Cappuccino'), _it(1, 22, 0, name='Cappuccino')]}]}
    with patch.object(ga, 'api_get', side_effect=[orders, {'data': []}]), \
            patch.object(ga, 'get_item_tax_rates', return_value={1: 8}):
        ga.sync_orders_for_date('2026-11-05')
    r = get_db().execute("SELECT * FROM sales WHERE product_name = 'Cappuccino'").fetchone()
    assert (r['quantity'], r['total_money'], r['discount'], r['net_profit']) == (2, 22, 22, 44)
    assert r['net_total'] == round(22 / 1.08, 2)
    assert r['net_total'] < r['total_money']  # the old bug made net > paid


def test_resync_does_not_touch_inventory():
    with patch.object(ga, 'sync_orders_for_date') as s, patch.object(ga, '_deduct_inventory') as d, \
            patch.object(ga, '_start_sync_log', return_value=1), patch.object(ga, '_finish_sync_log'):
        assert ga.resync_sales('2026-10-30', '2026-11-01') == 3
    assert s.call_count == 3 and not d.called


def test_unknown_items_take_order_residual_net():
    # 23% known coffee 20 zł (net 16.26) + unknown 8%-food 10.80 zł (net 10.00); order net 26.26
    order = {'total_price': {'amount': 30.80}, 'total_price_net': {'amount': 26.26},
             'items': [_it(1, 20, 20), _it(999, 10.80, 10.80), {'status': 'REMOVED', 'item_id': 1}]}
    rows = ga.order_item_amounts(order, {1: 23})
    assert len(rows) == 2
    assert [a['net_total'] for _, a in rows] == [16.26, 10.0]
    assert round(sum(a['net_total'] for _, a in rows), 2) == 26.26
