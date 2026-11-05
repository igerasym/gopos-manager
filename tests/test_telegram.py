"""Telegram recipients: comma-separated TELEGRAM_CHAT_ID fan-out."""
import json
from unittest.mock import patch

from app import telegram_bot as tb


def _sent_ids(mock_urlopen):
    return [json.loads(c.args[0].data)['chat_id'] for c in mock_urlopen.call_args_list]


def test_broadcast_to_all_recipients():
    with patch.object(tb, 'TOKEN', 'x'), patch.object(tb, 'CHAT_IDS', ['111', '222']), \
            patch.object(tb, 'urlopen') as m:
        tb.send_message('hi')
    assert _sent_ids(m) == ['111', '222']


def test_explicit_chat_id_only_that_chat():
    with patch.object(tb, 'TOKEN', 'x'), patch.object(tb, 'CHAT_IDS', ['111', '222']), \
            patch.object(tb, 'urlopen') as m:
        tb.send_message('hi', '333')
    assert _sent_ids(m) == ['333']


def test_one_failure_does_not_stop_others():
    with patch.object(tb, 'TOKEN', 'x'), patch.object(tb, 'CHAT_IDS', ['111', '222']), \
            patch.object(tb, 'urlopen', side_effect=[Exception('blocked'), None]) as m:
        tb.send_message('hi')
    assert _sent_ids(m) == ['111', '222']


def test_get_shift_staff_aggregates_by_employee():
    from app import gopos_api
    pages = [
        {'data': [
            {'employee': {'name': 'Alina'}, 'closed_at': '2026-11-05T07:41:00'},
            {'employee': {'name': 'Nastia'}, 'closed_at': '2026-11-05T13:02:00'},
            {'employee': {'name': 'Alina'}, 'closed_at': '2026-11-05T12:10:00'},
        ]},
        {'data': [{'employee': None, 'closed_at': '2026-11-05T18:55:00'}]},
        {'data': []},
    ]
    with patch.object(gopos_api, 'api_get', side_effect=pages):
        staff = gopos_api.get_shift_staff('2026-11-05')
    assert staff == [
        {'name': 'Alina', 'orders': 2, 'first': '07:41', 'last': '12:10'},
        {'name': 'Nastia', 'orders': 1, 'first': '13:02', 'last': '13:02'},
        {'name': '—', 'orders': 1, 'first': '18:55', 'last': '18:55'},
    ]


def _item(name, base, paid, qty=1, status='ACTIVE'):
    return {'name': name, 'quantity': qty, 'status': status,
            'base_unit_price': {'amount': base},
            'sub_total_price': {'amount': base * qty}, 'total_price': {'amount': paid}}


SAMPLE_ORDERS = [
    {'employee': {'name': 'Alina'}, 'closed_at': '2026-11-05T08:01:00',
     'promotions': [{'name': 'Staff'}],
     'items': [_item('Cappuccino', 18, 0, qty=2), _item('Milk oatmeal', 3, 0), _item('Brownie', 14, 0)]},
    {'employee': {'name': 'Alina'}, 'closed_at': '2026-11-05T17:56:00',
     'promotions': [{'name': 'write-off'}],
     'items': [_item('Bułka z makiem', 12, 0, qty=3)]},
    {'employee': {'name': 'Alina'}, 'closed_at': '2026-11-05T12:00:00', 'promotions': [],
     'items': [_item('Latte', 20, 20)]},
    {'employee': {'name': 'Alina'}, 'closed_at': '2026-11-05T12:30:00',
     'promotions': [{'name': '50% Cookie Countdown'}],
     'items': [_item('Cookie', 16, 8), _item('Latte', 20, 20)]},
]


def test_discounted_items_counts_only_reduced_items():
    from app import gopos_api
    d = gopos_api.discounted_items(SAMPLE_ORDERS)
    assert d['staff']['Cappuccino'] == {'qty': 2, 'value': 36, 'unit_price': 18}
    assert d['write-off']['Bułka z makiem']['qty'] == 3
    assert 'Latte' not in d['50% cookie countdown']  # full price item in a discounted order


def test_daily_report_blocks_and_survives_api_failure(tmp_path, monkeypatch):
    from app import gopos_api
    from app.db import init_db, get_db
    monkeypatch.setattr('app.db.DB_PATH', tmp_path / 'test.db')
    init_db()
    db = get_db()
    db.execute('CREATE TABLE pos_products (product_name TEXT PRIMARY KEY, category TEXT)')
    db.executemany('INSERT INTO pos_products VALUES (?, ?)',
                   [('Cappuccino', 'Coffee'), ('Milk oatmeal', 'Coffee'), ('Brownie', 'Bakery')])
    db.commit()
    db.close()

    with patch.object(tb, 'send_message') as send, \
            patch.object(gopos_api, 'fetch_closed_orders', return_value=SAMPLE_ORDERS):
        tb.daily_report()
    text = send.call_args.args[0]
    assert 'Alina — 08:01–17:56, 4 orders' in text
    assert '☕ <b>Staff drinks:</b> 2 — 53 zł' in text  # 36 coffee + 3 milk + 14 brownie
    assert '  Cappuccino ×2' in text and '  + Brownie' in text and 'Milk oatmeal' not in text
    assert '🗑 <b>Write-off:</b> 3 pcs — 36 zł' in text and 'Bułka z makiem ×3' in text

    with patch.object(tb, 'send_message') as send, \
            patch.object(gopos_api, 'fetch_closed_orders', side_effect=Exception('down')):
        tb.daily_report()
    text = send.call_args.args[0]
    assert send.called and 'Shift' not in text and 'Staff' not in text
