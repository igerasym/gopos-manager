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
