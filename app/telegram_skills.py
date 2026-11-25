"""Ready-made "skills" of the Telegram chat: slash commands that answer without the model.

Each skill is a plain function over the read-only tools, so it costs nothing, is instant and
always gives the same numbers for the same data. Free-form questions still go to the model.
A skill marked finance=True shows costs or profit and is limited to finance chats.
"""
from datetime import date, timedelta
from html import escape

from app import telegram_tools as tt


def _money(x) -> str:
    return f'{x:,.0f}'.replace(',', ' ') + ' zł'


def _delta(now, before) -> str:
    if not before:
        return ''
    p = (now - before) / before * 100
    return f' ({"▲" if p >= 0 else "▼"} {abs(p):.0f}%)'


def _d(today: str, days: int) -> str:
    return (date.fromisoformat(today) + timedelta(days=days)).isoformat()


def _summary(d_from: str, d_to: str, today: str) -> dict:
    return tt.sales_summary({'date_from': d_from, 'date_to': d_to}, today)


def _week_ranges(today: str) -> tuple:
    """Last 7 complete business days and the 7 before them (today is still running)."""
    end = _d(today, -1)
    return (_d(end, -6), end), (_d(end, -13), _d(end, -7))


# ── skills ──

def today_sales(today: str, finance: bool) -> str:
    s = _summary(today, today, today)
    top = tt.top_products({'date_from': today, 'limit': 3}, today)['products']
    from app import gopos_api
    as_of = gopos_api.LAST_INTRADAY_SYNC
    note = f'станом на {as_of}, оновлюється кожні 30 хв' if as_of else 'оновлюється кожні 30 хв'
    if not s['items_sold']:
        return f'📊 <b>{today}</b>\nПродажів за сьогодні ще не записано ({note}).'
    lines = [f'📊 <b>Сьогодні, {today}</b> ({note})',
             f'Виручка: {_money(s["revenue_gross_zl"])}', f'Позицій: {s["items_sold"]:.0f}']
    if top:
        lines.append('Топ: ' + ', '.join(f'{escape(p["product"])} ×{p["quantity"]:.0f}' for p in top))
    return '\n'.join(lines)


def yesterday_sales(today: str, finance: bool) -> str:
    day = _d(today, -1)
    s = _summary(day, day, today)
    week_ago = _d(day, -7)
    w = _summary(week_ago, week_ago, today)
    weekday = tt.WEEKDAYS_UA[date.fromisoformat(day).weekday()]
    if not s['items_sold']:
        return f'📊 <b>Вчора, {day}</b>\nПродажів немає.'
    top = tt.top_products({'date_from': day, 'limit': 3}, today)['products']
    lines = [f'📊 <b>Вчора, {weekday} {day}</b>',
             f'Виручка: {_money(s["revenue_gross_zl"])}' + _delta(s['revenue_gross_zl'], w['revenue_gross_zl']) +
             (f' проти {weekday} {week_ago}' if w['revenue_gross_zl'] else ''),
             f'Позицій: {s["items_sold"]:.0f}']
    if s['discounts_zl']:
        lines.append(f'Знижки: {_money(s["discounts_zl"])}')
    if top:
        lines.append('Топ: ' + ', '.join(f'{escape(p["product"])} ×{p["quantity"]:.0f}' for p in top))
    return '\n'.join(lines)


def week_sales(today: str, finance: bool) -> str:
    (a_from, a_to), (b_from, b_to) = _week_ranges(today)
    a, b = _summary(a_from, a_to, today), _summary(b_from, b_to, today)
    days = tt.daily_revenue({'date_from': a_from, 'date_to': a_to}, today)['days']
    lines = [f'📅 <b>Тиждень {a_from} – {a_to}</b>',
             f'Виручка: {_money(a["revenue_gross_zl"])}' + _delta(a['revenue_gross_zl'], b['revenue_gross_zl']) +
             f' (попередній тиждень {_money(b["revenue_gross_zl"])})',
             f'Позицій: {a["items_sold"]:.0f}' + _delta(a['items_sold'], b['items_sold'])]
    if days:
        best, worst = max(days, key=lambda x: x['revenue_zl']), min(days, key=lambda x: x['revenue_zl'])
        lines.append(f'Найкращий день: {best["weekday"]} {best["date"][5:]} — {_money(best["revenue_zl"])}')
        lines.append(f'Найслабший: {worst["weekday"]} {worst["date"][5:]} — {_money(worst["revenue_zl"])}')
    return '\n'.join(lines)


def top_week(today: str, finance: bool) -> str:
    (a_from, a_to), _ = _week_ranges(today)
    rows = tt.top_products({'date_from': a_from, 'date_to': a_to, 'limit': 5}, today)['products']
    if not rows:
        return f'🏆 Топ за {a_from} – {a_to}: продажів немає.'
    lines = [f'🏆 <b>Топ-5 за {a_from} – {a_to}</b>']
    for i, p in enumerate(rows, 1):
        lines.append(f'{i}. {escape(p["product"])} — {_money(p["revenue_zl"])}, {p["quantity"]:.0f} шт')
    return '\n'.join(lines)


def order_list(today: str, finance: bool) -> str:
    items = tt.low_stock({}, today)['items_below_minimum']
    if not items:
        return '✅ Усе на складі вище мінімуму.'
    by_supplier = {}
    for it in items:
        by_supplier.setdefault(it['supplier'] or 'Без постачальника', []).append(it)
    lines = ['🛒 <b>Що замовити</b> (на складі не більше мінімуму)']
    for supplier, rows in by_supplier.items():
        lines.append(f'\n<b>{escape(supplier)}</b>')
        for it in rows:
            need = it['minimum'] - it['on_hand']
            lines.append(f'• {escape(it["ingredient"])}: є {it["on_hand"]:g} {escape(it["unit"])}, '
                         f'мінімум {it["minimum"]:g}, бракує до мінімуму {max(need, 0):g}')
    return '\n'.join(lines)


def wastage_month(today: str, finance: bool) -> str:
    w = tt.run_tool('wastage_summary', {'month': today[:7]}, today, finance)
    if not w.get('records'):
        return f'🗑 Списань за {today[:7]} немає.'
    lines = [f'🗑 <b>Списання {w["month"]}</b>: записів {w["records"]}']
    if finance:
        lines[0] += f', збитки {_money(w["total_cost_zl"])}'
    for it in w['top_items'][:5]:
        line = f'• {escape(it["item"])}: {it["quantity"]:g} {escape(it["unit"])}'
        if finance and it.get('cost_zl'):
            line += f' — {_money(it["cost_zl"])}'
        lines.append(line)
    return '\n'.join(lines)


def food_cost_30(today: str, finance: bool) -> str:
    end = _d(today, -1)
    start = _d(end, -29)
    f = tt.food_cost({'date_from': start, 'date_to': end}, today)
    if not f['revenue_gross_zl']:
        return f'🧮 Food cost {start} – {end}: продажів немає.'
    lines = [f'🧮 <b>Food cost {start} – {end}</b>',
             f'Виручка: {_money(f["revenue_gross_zl"])}, собівартість: {_money(f["cogs_zl"])}']
    if f['food_cost_pct_of_priced_revenue'] is not None:
        lines.append(f'Food cost: {f["food_cost_pct_of_priced_revenue"]:.1f}% (ціль 28–35%)')
    lines.append(f'Техкартами покрито {f["recipe_coverage_pct_of_revenue"]:.0f}% виручки')
    miss = f['biggest_products_without_recipe']
    if miss:
        lines.append('Найбільші без техкарти: ' + ', '.join(
            f'{escape(m["product"])} ({_money(m["revenue_zl"])})' for m in miss[:3]))
    return '\n'.join(lines)


# name -> (menu description, function, finance_only)
SKILLS = {
    'today': ('Продажі за сьогодні', today_sales, False),
    'yesterday': ('Вчорашній день і порівняння з минулим тижнем', yesterday_sales, False),
    'week': ('Тиждень проти попереднього', week_sales, False),
    'top': ('Топ-5 позицій за тиждень', top_week, False),
    'order': ('Що замовити: закінчується, по постачальниках', order_list, False),
    'wastage': ('Списання за місяць', wastage_month, False),
    'costs': ('Food cost за 30 днів', food_cost_30, True),
}
ALIASES = {'stock': 'order', 'inventory': 'order', 'report': 'today'}


def available(finance: bool) -> dict:
    return {k: v for k, v in SKILLS.items() if finance or not v[2]}


def menu(finance: bool) -> list:
    """Entries for Telegram's setMyCommands."""
    cmds = [{'command': k, 'description': v[0]} for k, v in available(finance).items()]
    return cmds + [{'command': 'help', 'description': 'Що вміє бот'}]


def help_text(finance: bool) -> str:
    lines = ['☕ <b>The Frame Bot</b>', '', 'Напишіть питання своїми словами, наприклад:',
             '• скільки ми заробили вчора?', '• які дні тижня найслабші цього місяця?',
             '• скільки продали лате за останні 30 днів?', '', 'Або готові команди:']
    lines += [f'/{k} — {v[0]}' for k, v in available(finance).items()]
    return '\n'.join(lines)


def run(name: str, today: str, finance: bool):
    """Text of the skill, 'forbidden' for a finance skill in a non-finance chat, or None."""
    name = ALIASES.get(name, name)
    entry = SKILLS.get(name)
    if not entry:
        return None
    if entry[2] and not finance:
        return 'forbidden'
    return entry[1](today, finance)
