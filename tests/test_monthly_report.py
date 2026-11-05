"""Monthly report: period math, like-for-like, formatting, send routing."""
from unittest.mock import patch

from app import monthly_report as mr


def _stats(revenue, days, weekday_avg, products, discount=0, expenses=None, gopos=True):
    s = {'revenue': revenue, 'net': revenue / 1.15, 'discount': discount, 'items': 0, 'days': days,
         'daily': {}, 'weekday_avg': weekday_avg, 'weekday_n': {}, 'products': products,
         'categories': {'Coffee': revenue * 0.6, 'Kitchen': revenue * 0.4}, 'expenses': expenses or {},
         'gopos_ok': gopos}
    if gopos:
        s.update(orders=days * 100, avg_ticket=revenue / (days * 100),
                 hours={'ранок до 10': 10, 'обід 10–15': 50, 'вечір 15+': 20},
                 employees={'Alina': 70, 'Barista': 30},
                 discounts={'Staff': {'orders': 10, 'value': 500.0}, 'write-off': {'orders': 2, 'value': 40.0}},
                 vat={'23%': {'net': revenue * 0.5 / 1.23, 'tax': revenue * 0.5 * 0.23 / 1.23, 'gross': revenue * 0.5},
                      '8%': {'net': revenue * 0.5 / 1.08, 'tax': revenue * 0.5 * 0.08 / 1.08, 'gross': revenue * 0.5}})
    return s


def test_month_math():
    assert mr.month_bounds('2026-10')[1].day == 31
    assert mr.month_bounds('2026-02')[1].day == 28
    assert mr.prev_month('2026-01') == '2025-12'
    assert mr.month_title('2026-10') == 'Жовтень 2026'


def test_like_for_like_uses_previous_weekday_pace():
    # Every weekday earned 1000 last month → October (31 days) at the same pace = 31 000
    prev = _stats(30000, 30, {wd: 1000 for wd in range(7)}, {})
    cur = _stats(31000, 31, {wd: 1000 for wd in range(7)}, {})
    cmp = mr.compare('2026-10', cur, prev)
    assert cmp['lfl_expected'] == 31000 and abs(cmp['lfl_pct']) < 1e-9


def test_movers_new_gone():
    prev = _stats(1, 30, {}, {'Iced Latte': {'qty': 10, 'rev': 500}, 'Latte': {'qty': 10, 'rev': 100},
                               'Cake': {'qty': 1, 'rev': 50}})
    cur = _stats(1, 31, {}, {'Iced Latte': {'qty': 2, 'rev': 100}, 'Latte': {'qty': 30, 'rev': 300},
                              'PSL': {'qty': 5, 'rev': 200}})
    cmp = mr.compare('2026-10', cur, prev)
    assert cmp['up'][0][0] == 'Latte' and cmp['down'][0][0] == 'Iced Latte'
    assert cmp['new'] == [('PSL', 200)] and cmp['gone'] == [('Cake', 50)]


def test_format_has_key_lines_and_expense_note():
    prev = _stats(130000, 30, {wd: 4300 for wd in range(7)}, {'A': {'qty': 1, 'rev': 10}}, discount=2000)
    cur = _stats(137000, 31, {wd: 4400 for wd in range(7)}, {'A': {'qty': 1, 'rev': 20}}, discount=1800)
    text = mr.format_numbers('2026-10', '2026-09', cur, prev, mr.compare('2026-10', cur, prev))
    assert 'Жовтень 2026' in text and '137 000 zł' in text and 'З поправкою на календар' in text
    assert 'Staff: 10×' in text and 'Barista 30' in text
    # 137 000: 68 500 @23% → 12 809 tax, 68 500 @8% → 5 074 tax
    assert '🏛 ПДВ з продажів: <b>17 883 zł</b>' in text and 'за ставками: 23% 12 809 · 8% 5 074' in text
    assert 'немає даних за Жовтень 2026, Вересень 2026' in text
    assert '<1' not in text  # no raw '<' that would break Telegram HTML


def test_format_without_gopos_skips_order_sections():
    prev = _stats(100, 30, {}, {}, gopos=False)
    cur = _stats(110, 31, {}, {}, gopos=False)
    text = mr.format_numbers('2026-10', '2026-09', cur, prev, mr.compare('2026-10', cur, prev))
    assert 'Замовлень' not in text and 'Години' not in text and 'Виручка' in text


def test_weekend_trend_and_ready_percentages():
    prev = _stats(1, 30, {5: 6000, 6: 6000}, {})
    cur = _stats(1, 31, {5: 5500, 6: 5500}, {})
    cur['daily'] = {'2026-10-03': 6500, '2026-10-04': 6400, '2026-10-24': 5000, '2026-10-25': 5100,
                    '2026-10-26': 3000}  # 26th is a Monday — must not count as weekend
    cmp = mr.compare('2026-10', cur, prev)
    assert [d for d, _, _ in cmp['weekend_days']] == ['2026-10-03', '2026-10-04', '2026-10-24', '2026-10-25']
    assert cmp['weekend_avg_first_half'] == 6450 and cmp['weekend_avg_second_half'] == 5050
    assert cmp['weekday_pct']['Сб'] == (5500, -8.3)
    text = mr.format_numbers('2026-10', '2026-09', cur, prev, cmp)
    assert 'Вихідні: 1-ша половина 6 450 → 2-га 5 050 (минулий місяць 6 000)' in text


def test_chunks_respect_limit():
    parts = mr._chunks('\n\n'.join(['x' * 1500] * 5), limit=4000)
    assert all(len(p) <= 4000 for p in parts) and len(parts) == 3


def test_send_routes_and_survives_llm_failure():
    from app import telegram_bot as tb
    with patch.object(mr, 'build', return_value=('numbers', None, {'month': '2026-10', 'llm_error': 'boom'})), \
            patch.object(tb, 'send_message') as send, patch.object(tb, 'send_admin') as admin, \
            patch.object(tb, 'ADMIN_CHAT_ID', '111'):
        mr.send_monthly_report('2026-10')
        assert send.call_args_list[0].args == ('numbers',)   # numbers to everyone
        assert 'AI summary failed' in admin.call_args.args[0]  # failure to admin only
        send.reset_mock()
        mr.send_monthly_report('2026-10', admin_only=True)
        assert send.call_args.args == ('numbers', '111')
