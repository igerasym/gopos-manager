"""Interactive Telegram chat: allowlist, tool loop, finance gating, privacy, limits."""
import logging
import time
from unittest.mock import MagicMock, patch

import pytest

from app import telegram_chat as tc
from app import telegram_tools as tt
from app.db import init_db, get_db

OWNER, PARTNER, STRANGER = '111', '222', '999'
TODAY = '2026-11-25'   # Wednesday


@pytest.fixture(autouse=True)
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr('app.db.DB_PATH', tmp_path / 'test.db')
    monkeypatch.setattr(tc.tb, 'TOKEN', 'x')
    monkeypatch.setattr(tc.tb, 'CHAT_IDS', [OWNER, PARTNER])
    monkeypatch.setattr(tc.tb, 'ADMIN_CHAT_ID', OWNER)
    monkeypatch.delenv('TELEGRAM_FINANCE_CHAT_IDS', raising=False)
    monkeypatch.setattr(tc, '_today', lambda: TODAY)
    monkeypatch.setattr(tc, '_api', lambda *a, **k: {})        # typing indicator
    monkeypatch.setattr(tc, 'HOURLY_LIMIT', 30)
    tc._memory.clear()
    tc._asked.clear()
    init_db()
    db = get_db()
    db.execute("INSERT INTO sales (date, product_name, quantity, total_money, net_total, discount, net_profit) "
               "VALUES ('2026-11-24', 'Latte <b>', 10, 250, 231, 0, 250)")
    db.execute("INSERT INTO sales (date, product_name, quantity, total_money, net_total, discount, net_profit) "
               "VALUES ('2026-11-24', 'Espresso', 20, 200, 185, 0, 200)")
    db.execute("INSERT INTO ingredients (name, unit, quantity, min_quantity) VALUES ('Mleko', 'L', 2, 5)")
    db.execute("INSERT INTO expenses (name, category, amount, month) VALUES ('Rent', 'Оренда', 14000, '2026-11')")
    db.commit()
    db.close()
    yield


def _update(chat_id, text, chat_type='private', age=0):
    return {'update_id': 1, 'message': {'chat': {'id': int(chat_id), 'type': chat_type},
                                        'text': text, 'date': int(time.time()) - age}}


def _tool_use(name, args, uid='t1'):
    return {'output': {'message': {'role': 'assistant', 'content': [
        {'toolUse': {'toolUseId': uid, 'name': name, 'input': args}}]}},
        'stopReason': 'tool_use', 'usage': {'inputTokens': 100, 'outputTokens': 10}}


def _final(text):
    return {'output': {'message': {'role': 'assistant', 'content': [{'text': text}]}},
            'stopReason': 'end_turn', 'usage': {'inputTokens': 150, 'outputTokens': 20}}


def _client(*responses):
    c = MagicMock()
    c.converse.side_effect = list(responses)
    return c


# ── allowlist ──

def test_unknown_chat_is_ignored_without_reply_or_model_call():
    client = _client()
    with patch.object(tc.tb, 'send_message') as send:
        tc.handle_update(_update(STRANGER, 'скільки виручка?'), client)
    send.assert_not_called()
    client.converse.assert_not_called()


def test_group_chat_is_ignored_even_if_id_is_allowed():
    client = _client()
    with patch.object(tc.tb, 'send_message') as send:
        tc.handle_update(_update(OWNER, 'hi', chat_type='group'), client)
    send.assert_not_called()


def test_stale_update_is_dropped():
    client = _client()
    with patch.object(tc.tb, 'send_message') as send:
        tc.handle_update(_update(OWNER, 'hi', age=3600), client)
    send.assert_not_called()


# ── answering ──

def test_question_runs_tool_and_replies_only_to_the_asker():
    client = _client(_tool_use('sales_summary', {'date_from': '2026-11-24'}),
                     _final('Вчора 450 zł'))
    with patch.object(tc.tb, 'send_message') as send:
        tc.handle_update(_update(PARTNER, 'скільки вчора?'), client)
    send.assert_called_once()
    text, chat = send.call_args.args
    assert chat == PARTNER and 'Вчора 450 zł' in text
    # the tool really ran against the DB and its result went back to the model
    # (the mock keeps a reference to the list the code keeps appending to: look for the result)
    sent = client.converse.call_args_list[1].kwargs['messages']
    result = next(b['toolResult']['content'][0]['json'] for m in sent for b in m['content'] if 'toolResult' in b)
    assert result['revenue_gross_zl'] == 450.0 and result['items_sold'] == 30


def test_model_text_is_html_escaped():
    client = _client(_final('<b>x</b> & <a href="evil">y</a>'))
    with patch.object(tc.tb, 'send_message') as send:
        tc.handle_update(_update(PARTNER, 'q'), client)
    text = send.call_args.args[0]
    assert '<b>' not in text and '&lt;b&gt;' in text and '&amp;' in text


def test_admin_sees_tools_and_tokens_partner_does_not():
    for chat, expect in ((OWNER, True), (PARTNER, False)):
        tc._memory.clear()
        client = _client(_tool_use('low_stock', {}), _final('Мало молока'))
        with patch.object(tc.tb, 'send_message') as send:
            tc.handle_update(_update(chat, 'що закінчується?'), client)
        text = send.call_args.args[0]
        assert ('low_stock' in text and '250 in / 30 out' in text) is expect


def test_memory_carries_the_previous_exchange_but_only_text():
    c1 = _client(_tool_use('low_stock', {}), _final('Мало молока'))
    with patch.object(tc.tb, 'send_message'):
        tc.handle_update(_update(PARTNER, 'що закінчується?'), c1)
    c2 = _client(_final('ok'))
    with patch.object(tc.tb, 'send_message'):
        tc.handle_update(_update(PARTNER, 'а ще?'), c2)
    msgs = c2.converse.call_args.kwargs['messages'][:3]   # the list also gets the new answer appended
    assert [m['role'] for m in msgs] == ['user', 'assistant', 'user']
    assert all('toolUse' not in b and 'toolResult' not in b for m in msgs for b in m['content'])


def test_memory_is_per_chat():
    with patch.object(tc.tb, 'send_message'):
        tc.handle_update(_update(OWNER, 'secret question'), _client(_final('a')))
        c2 = _client(_final('b'))
        tc.handle_update(_update(PARTNER, 'other'), c2)
    flat = str(c2.converse.call_args.kwargs['messages'])
    assert 'secret question' not in flat


def test_tool_loop_is_bounded():
    client = _client(*[_tool_use('low_stock', {}, uid=f't{i}') for i in range(tc.MAX_TOOL_ROUNDS)])
    with patch.object(tc.tb, 'send_message') as send:
        tc.handle_update(_update(PARTNER, 'loop'), client)
    assert client.converse.call_count == tc.MAX_TOOL_ROUNDS
    assert 'Не вдалося' in send.call_args.args[0]


def test_hourly_limit():
    tc.HOURLY_LIMIT = 2
    with patch.object(tc.tb, 'send_message') as send:
        for _ in range(3):
            tc.handle_update(_update(PARTNER, 'q'), _client(_final('a')))
    assert 'Забагато запитів' in send.call_args.args[0]


def test_model_failure_tells_user_and_alerts_admin_without_error_text():
    client = MagicMock()
    client.converse.side_effect = RuntimeError('secret-detail-from-aws')
    with patch.object(tc.tb, 'send_message') as send, patch.object(tc.tb, 'send_admin') as admin:
        tc.handle_update(_update(PARTNER, 'q'), client)
    assert 'Щось пішло не так' in send.call_args.args[0]
    assert 'secret-detail' not in admin.call_args.args[0]


# ── privacy ──

def test_message_text_and_answer_are_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    client = _client(_tool_use('sales_summary', {'date_from': '2026-11-24'}), _final('ANSWER-MARKER'))
    with patch.object(tc.tb, 'send_message'):
        tc.handle_update(_update(PARTNER, 'QUESTION-MARKER'), client)
    assert 'QUESTION-MARKER' not in caplog.text and 'ANSWER-MARKER' not in caplog.text
    assert 'sales_summary' in caplog.text      # tool names are metadata and fine


# ── finance gating ──

def test_finance_tools_hidden_from_partner_and_refused_if_called():
    names = lambda fin: {s['toolSpec']['name'] for s in tt.tool_specs(fin)}
    assert 'expenses_summary' in names(True) and 'food_cost' in names(True)
    assert not names(False) & {'expenses_summary', 'food_cost'}
    assert 'error' in tt.run_tool('expenses_summary', {}, TODAY, finance=False)
    assert 'expenses_total_zl' in tt.run_tool('expenses_summary', {}, TODAY, finance=True)


def test_finance_chats_default_to_both_owners_and_can_be_narrowed(monkeypatch):
    assert tc.finance_chats() == {OWNER, PARTNER}
    monkeypatch.setenv('TELEGRAM_FINANCE_CHAT_IDS', OWNER)
    assert tc.finance_chats() == {OWNER}
    monkeypatch.setenv('TELEGRAM_FINANCE_CHAT_IDS', f'{OWNER}, {STRANGER}')
    assert tc.finance_chats() == {OWNER}       # an id outside the allowlist never gains access


def test_wastage_costs_only_for_finance():
    db = get_db()
    db.execute("INSERT INTO ingredients (id, name, unit, quantity, unit_price) VALUES (50, 'Cream', 'L', 5, 10)")
    db.execute("INSERT INTO wastage (date, ingredient_id, quantity, reason, source) "
               "VALUES ('2026-11-20', 50, 2, 'expired', 'manual')")
    db.commit()
    db.close()
    plain = tt.run_tool('wastage_summary', {'month': '2026-11'}, TODAY, finance=False)
    fin = tt.run_tool('wastage_summary', {'month': '2026-11'}, TODAY, finance=True)
    assert 'cost_zl' not in str(plain) and 'total_cost_zl' not in plain
    assert fin['total_cost_zl'] == 20.0


# ── tools ──

def test_tool_input_validation_returns_errors_not_exceptions():
    for bad in ({'date_from': '24-11-2026'}, {'date_from': '2026-02-31'},
                {'date_from': '2026-11-25', 'date_to': '2026-11-01'},
                {'date_from': '2020-01-01', 'date_to': '2026-11-25'}):
        assert 'error' in tt.run_tool('sales_summary', bad, TODAY, finance=True), bad
    assert 'error' in tt.run_tool('nope', {}, TODAY, finance=True)
    assert 'error' in tt.run_tool('top_products', {'date_from': TODAY, 'by': 'profit'}, TODAY, True)


def test_product_search_is_literal_not_a_wildcard():
    out = tt.run_tool('product_sales', {'date_from': '2026-11-01', 'date_to': '2026-11-30', 'product': '%'}, TODAY, False)
    assert out['matches'] == []
    out = tt.run_tool('product_sales', {'date_from': '2026-11-01', 'date_to': '2026-11-30', 'product': 'latte'}, TODAY, False)
    assert [m['product'] for m in out['matches']] == ['Latte <b>']


def test_top_products_limit_is_capped():
    out = tt.run_tool('top_products', {'date_from': '2026-11-24', 'limit': 9999}, TODAY, False)
    assert len(out['products']) <= tt.MAX_PRODUCTS


def test_after_midnight_the_prompt_says_the_business_day_is_still_yesterday():
    from datetime import datetime
    p = tc.system_prompt(TODAY, False, datetime(2026, 11, 26, 0, 20))
    assert 'BUSINESS DAY IS STILL 2026-11-25' in p and '25.11' in p and '00:20' in p
    day = tc.system_prompt(TODAY, False, datetime(2026, 11, 25, 14, 0))
    assert 'STILL' not in day and '14:00' in day


def test_model_bold_markdown_becomes_telegram_bold_and_stays_escaped():
    assert tc.to_telegram_html('**4277 zł** & <x>') == '<b>4277 zł</b> &amp; &lt;x&gt;'
    assert tc.to_telegram_html('**<script>**') == '<b>&lt;script&gt;</b>'


def test_system_prompt_names_the_date_and_scope():
    from datetime import datetime
    p = tc.system_prompt(TODAY, finance=False, now=datetime(2026, 11, 25, 12, 0))
    assert '2026-11-25 (середа)' in p and 'недоступні' in p
    assert 'можете відповідати і про витрати' in tc.system_prompt(TODAY, True, datetime(2026, 11, 25, 12, 0))


# ── commands and startup ──

def test_help_and_today_commands_need_no_model():
    client = _client()
    with patch.object(tc.tb, 'send_message') as send:
        tc.handle_update(_update(PARTNER, '/help'), client)
        tc.handle_update(_update(PARTNER, '/today'), client)
    client.converse.assert_not_called()
    assert send.call_count == 2


def test_start_polling_is_off_without_token_or_chats(monkeypatch):
    import threading
    monkeypatch.setattr(tc.tb, 'TOKEN', '')
    assert tc.start_polling(threading.Event()) is None
    monkeypatch.setattr(tc.tb, 'TOKEN', 'x')
    monkeypatch.setattr(tc.tb, 'CHAT_IDS', [])
    assert tc.start_polling(threading.Event()) is None
    monkeypatch.setattr(tc.tb, 'CHAT_IDS', [OWNER])
    monkeypatch.setenv('TELEGRAM_CHAT_ENABLED', '0')
    assert tc.start_polling(threading.Event()) is None


def test_poll_loop_uses_offset_and_survives_errors(monkeypatch):
    import threading
    stop = threading.Event()
    calls = []

    def fake_api(method, payload, timeout=15):
        calls.append(dict(payload))
        if len(calls) == 1:
            return {'result': [{'update_id': 41, 'message': {}}]}
        if len(calls) == 2:
            raise OSError('network down')
        stop.set()
        return {'result': []}

    monkeypatch.setattr(tc, '_api', fake_api)
    monkeypatch.setattr(tc, '_register_menu', lambda: None)
    monkeypatch.setattr(stop, 'wait', lambda s: None)
    tc.poll_forever(stop)
    assert 'offset' not in calls[0] and calls[1]['offset'] == 42 and calls[2]['offset'] == 42


def test_wastage_tool_prices_gopos_rows_by_recipe_for_finance_only():
    db = get_db()
    db.execute("INSERT INTO ingredients (id, name, unit, quantity, unit_price) VALUES (60, 'Beans', 'kg', 5, 100)")
    db.execute("INSERT INTO recipes (product_name, ingredient_id, amount) VALUES ('Cup', 60, 0.02)")  # 2 zł
    db.execute("INSERT INTO wastage (date, product_name, quantity, reason, source) "
               "VALUES ('2026-11-20', 'Cup', 3, 'write-off', 'gopos')")
    db.commit()
    db.close()
    fin = tt.run_tool('wastage_summary', {'month': '2026-11'}, TODAY, finance=True)
    assert fin['total_cost_zl'] == 6.0
    assert 'cost' not in str(tt.run_tool('wastage_summary', {'month': '2026-11'}, TODAY, finance=False))


# ── skills (slash commands, no model) ──

def _cmd(chat, text):
    client = _client()
    with patch.object(tc.tb, 'send_message') as send:
        tc.handle_update(_update(chat, text), client)
    client.converse.assert_not_called()
    return send.call_args.args[0]


def test_yesterday_skill_shows_revenue_and_escapes_names():
    out = _cmd(PARTNER, '/yesterday')
    assert '450 zł' in out and '&lt;b&gt;' in out and '<b>Latte' not in out


def test_yesterday_compares_with_same_weekday_last_week():
    db = get_db()
    db.execute("INSERT INTO sales (date, product_name, quantity, total_money) VALUES ('2026-11-17', 'Espresso', 10, 300)")
    db.commit()
    db.close()
    assert '▲ 50%' in _cmd(PARTNER, '/yesterday')     # 450 vs 300


def test_week_skill_compares_two_complete_weeks():
    db = get_db()
    db.execute("INSERT INTO sales (date, product_name, quantity, total_money) VALUES ('2026-11-14', 'Espresso', 10, 300)")
    db.execute("INSERT INTO sales (date, product_name, quantity, total_money) VALUES ('2026-11-25', 'Espresso', 99, 9999)")
    db.commit()
    db.close()
    out = _cmd(PARTNER, '/week')
    assert '2026-11-18 – 2026-11-24' in out            # today (a running day) is not part of it
    assert '450 zł' in out and '▲ 50%' in out and '9 999' not in out


def test_top_skill():
    out = _cmd(PARTNER, '/top')
    assert 'Топ-5' in out and 'Espresso' in out


def test_order_skill_groups_by_supplier_and_stock_is_an_alias():
    db = get_db()
    db.execute("INSERT INTO suppliers (id, name) VALUES (1, 'Makro')")
    db.execute("UPDATE ingredients SET supplier_id = 1 WHERE name = 'Mleko'")
    db.execute("INSERT INTO ingredients (name, unit, quantity, min_quantity) VALUES ('Cukier', 'kg', 1, 3)")
    db.commit()
    db.close()
    for cmd in ('/order', '/stock'):
        out = _cmd(PARTNER, cmd)
        assert '<b>Makro</b>' in out and 'Mleko' in out and 'бракує до мінімуму 3' in out
        assert 'Без постачальника' in out and 'Cukier' in out


def test_wastage_skill_costs_only_in_finance_chats(monkeypatch):
    db = get_db()
    db.execute("INSERT INTO ingredients (id, name, unit, quantity, unit_price) VALUES (70, 'Cream', 'L', 5, 10)")
    db.execute("INSERT INTO wastage (date, ingredient_id, quantity, reason, source) VALUES ('2026-11-20', 70, 2, 'expired', 'manual')")
    db.commit()
    db.close()
    assert '20 zł' in _cmd(PARTNER, '/wastage')
    monkeypatch.setenv('TELEGRAM_FINANCE_CHAT_IDS', OWNER)
    out = _cmd(PARTNER, '/wastage')
    assert 'Cream' in out and ' zł' not in out


def test_costs_skill_is_finance_only(monkeypatch):
    assert 'Food cost' in _cmd(PARTNER, '/costs')
    monkeypatch.setenv('TELEGRAM_FINANCE_CHAT_IDS', OWNER)
    assert 'недоступна' in _cmd(PARTNER, '/costs')
    assert 'Food cost' in _cmd(OWNER, '/costs')


def test_unknown_command_and_botname_suffix():
    assert 'Невідома команда' in _cmd(PARTNER, '/nope')
    assert 'Топ-5' in _cmd(PARTNER, '/top@TheFrameBot')


def test_help_lists_only_the_skills_the_chat_may_use(monkeypatch):
    assert '/costs' in _cmd(PARTNER, '/help')
    monkeypatch.setenv('TELEGRAM_FINANCE_CHAT_IDS', OWNER)
    assert '/costs' not in _cmd(PARTNER, '/help') and '/week' in _cmd(PARTNER, '/help')


def test_skill_crash_is_reported_without_details(monkeypatch):
    monkeypatch.setitem(tc.skills.SKILLS, 'today', ('x', lambda t, f: 1 / 0, False))
    assert 'Не вдалося виконати' in _cmd(PARTNER, '/today')


def test_menu_is_registered_per_chat_with_finance_filter(monkeypatch):
    sent = []
    monkeypatch.setattr(tc, '_api', lambda m, p, timeout=15: sent.append((m, p)) or {})
    monkeypatch.setenv('TELEGRAM_FINANCE_CHAT_IDS', OWNER)
    tc._register_menu()
    by_chat = {p['scope']['chat_id']: {c['command'] for c in p['commands']} for m, p in sent if m == 'setMyCommands'}
    assert 'costs' in by_chat[int(OWNER)] and 'costs' not in by_chat[int(PARTNER)]
    assert 'help' in by_chat[int(PARTNER)]


# ── who is on shift (live GoPOS, read-only) ──

def _orders(*rows):
    return [{'employee': {'name': n} if n else None, 'closed_at': f'2026-11-25T{t}:00'} for n, t in rows]


@pytest.fixture
def shift_api(monkeypatch):
    from app import gopos_api
    tt._shift_cache.clear()
    calls = []

    def fake(day, include):
        calls.append((day, include))
        return _orders(('Alina', '08:05'), ('Alina', '09:40'), ('Nastia', '12:10'), ('Barista', '12:30'), (None, '13:00'))

    monkeypatch.setattr(gopos_api, 'fetch_closed_orders', fake)
    return calls


def test_shift_tool_lists_employees_with_first_and_last_order(shift_api):
    out = tt.run_tool('shift_staff', {}, TODAY, finance=False)
    people = {p['name']: p for p in out['staff']}
    assert people['Alina'] == {'name': 'Alina', 'orders': 2, 'first_order': '08:05', 'last_order': '09:40'}
    assert people['Nastia']['orders'] == 1 and out['orders_total'] == 5
    assert shift_api == [(TODAY, 'employee')]          # one live call, only the employee field
    assert 'not a schedule' in out['note'] and 'freshness_note' in out


def test_shift_tool_is_cached_and_validates_date(shift_api):
    tt.run_tool('shift_staff', {}, TODAY, False)
    tt.run_tool('shift_staff', {}, TODAY, False)
    assert len(shift_api) == 1
    assert 'error' in tt.run_tool('shift_staff', {'date': '2026-12-01'}, TODAY, False)   # future
    assert 'error' in tt.run_tool('shift_staff', {'date': 'yesterday'}, TODAY, False)


def test_shift_tool_survives_a_gopos_outage(monkeypatch):
    from app import gopos_api
    tt._shift_cache.clear()
    monkeypatch.setattr(gopos_api, 'fetch_closed_orders', lambda d, i: (_ for _ in ()).throw(ConnectionError('x')))
    out = tt.run_tool('shift_staff', {}, TODAY, False)
    assert 'not reachable' in out['error'] and 'ConnectionError' in out['error']


def test_who_is_on_shift_is_answered_in_chat_through_the_tool_not_a_command(shift_api):
    client = _client(_tool_use('shift_staff', {}), _final('Зараз Alina (з 08:05) і Nastia.'))
    with patch.object(tc.tb, 'send_message') as send:
        tc.handle_update(_update(PARTNER, 'Хто на зміні?'), client)
    assert 'Alina' in send.call_args.args[0]
    sent = client.converse.call_args_list[1].kwargs['messages']
    result = next(b['toolResult']['content'][0]['json'] for m in sent for b in m['content'] if 'toolResult' in b)
    assert {p['name'] for p in result['staff']} >= {'Alina', 'Nastia', 'Barista'}
    assert 'shift' not in {c['command'] for c in tc.skills.menu(True)}    # no /shift command


def test_prompt_routes_shift_questions_to_the_tool_and_is_honest_about_the_rota():
    p = tc.system_prompt(TODAY, True)
    assert 'shift_staff' in p and 'not a rota' in p and 'Barista' in p
    assert 'staff or shift data' not in p
