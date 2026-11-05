"""Monthly report: previous month vs the month before, numbers + AI summary, to Telegram.

Numbers are computed here (DB + GoPos API); the LLM only writes the narrative from them,
so it can't invent figures.
"""
import json
import logging
import os
import time
from collections import Counter, defaultdict
from datetime import date, timedelta
from html import escape

from app.db import get_db

log = logging.getLogger(__name__)

SUMMARY_MODEL_ID = os.getenv('MONTHLY_REPORT_MODEL_ID', 'us.anthropic.claude-sonnet-4-5-20250929-v1:0')
MONTHS_UA = ['', 'Січень', 'Лютий', 'Березень', 'Квітень', 'Травень', 'Червень', 'Липень',
             'Серпень', 'Вересень', 'Жовтень', 'Листопад', 'Грудень']
WEEKDAYS_UA = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Нд']
HOUR_BUCKETS = [('ранок до 10', 0, 10), ('обід 10–15', 10, 15), ('вечір 15+', 15, 24)]
TG_LIMIT = 4000


# ─── period helpers ───

def month_bounds(month: str):
    y, m = map(int, month.split('-'))
    first = date(y, m, 1)
    last = (date(y + (m == 12), m % 12 + 1, 1)) - timedelta(days=1)
    return first, last


def prev_month(month: str) -> str:
    first, _ = month_bounds(month)
    return (first - timedelta(days=1)).strftime('%Y-%m')


def month_title(month: str) -> str:
    y, m = map(int, month.split('-'))
    return f'{MONTHS_UA[m]} {y}'


def pct(new, old):
    return None if not old else (new / old - 1) * 100


def fmt_pct(p):
    return '—' if p is None else f'{p:+.1f}%'


def zl(x):
    return f'{x:,.0f}'.replace(',', ' ')


# ─── data collection ───

def collect_db(month: str) -> dict:
    a, b = (d.isoformat() for d in month_bounds(month))
    db = get_db()
    try:
        cat = {r[0]: (r[1] or 'Other') for r in db.execute('SELECT product_name, category FROM pos_products')}
    except Exception:
        cat = {}
    t = db.execute('''SELECT COALESCE(SUM(total_money),0), COALESCE(SUM(net_total),0),
                             COALESCE(SUM(discount),0), COALESCE(SUM(quantity),0), COUNT(DISTINCT date)
                      FROM sales WHERE date BETWEEN ? AND ?''', (a, b)).fetchone()
    daily = {d: rev for d, rev in db.execute(
        'SELECT date, SUM(total_money) FROM sales WHERE date BETWEEN ? AND ? GROUP BY date', (a, b))}
    products, categories = {}, defaultdict(float)
    for name, q, rev in db.execute('''SELECT product_name, SUM(quantity), SUM(total_money) FROM sales
                                      WHERE date BETWEEN ? AND ? GROUP BY product_name''', (a, b)):
        products[name] = {'qty': q, 'rev': rev}
        categories[cat.get(name, 'Other')] += rev
    expenses = {c: v for c, v in db.execute(
        'SELECT category, SUM(amount) FROM expenses WHERE month = ? GROUP BY category', (month,))}
    db.close()
    weekday = defaultdict(list)
    for d, rev in daily.items():
        weekday[date.fromisoformat(d).weekday()].append(rev)
    return {
        'revenue': t[0], 'net': t[1], 'discount': t[2], 'items': t[3], 'days': t[4],
        'daily': daily, 'weekday_avg': {k: sum(v) / len(v) for k, v in weekday.items()},
        'weekday_n': {k: len(v) for k, v in weekday.items()},
        'products': products, 'categories': dict(categories), 'expenses': expenses,
    }


def collect_gopos(month: str) -> dict:
    from app.gopos_api import fetch_closed_orders
    first, last = month_bounds(month)
    n, paid = 0, 0.0
    hours, emp = Counter(), Counter()
    disc = defaultdict(lambda: {'orders': 0, 'value': 0.0})
    d = first
    while d <= last:
        for o in fetch_closed_orders(d.isoformat(), 'employee,promotions'):
            n += 1
            paid += (o.get('total_price') or {}).get('amount', 0)
            h = int((o.get('closed_at') or '00T00')[11:13] or 0)
            hours[next(lbl for lbl, lo, hi in HOUR_BUCKETS if lo <= h < hi)] += 1
            emp[(o.get('employee') or {}).get('name') or '—'] += 1
            for p in o.get('promotions') or []:
                e = disc[p.get('name') or '?']
                e['orders'] += 1
                e['value'] += (p.get('used') or {}).get('amount', 0)
        d += timedelta(days=1)
    return {'orders': n, 'avg_ticket': paid / n if n else 0, 'hours': dict(hours),
            'employees': dict(emp), 'discounts': {k: dict(v) for k, v in disc.items()}}


def collect(month: str) -> dict:
    s = collect_db(month)
    try:
        s.update(collect_gopos(month))
        s['gopos_ok'] = True
    except Exception as e:
        log.warning(f'Monthly report: GoPOS stats for {month} failed: {e!r}')
        s['gopos_ok'] = False
    return s


# ─── comparison ───

def compare(cur_m: str, cur: dict, prev: dict) -> dict:
    # like-for-like: what the current month's calendar would earn at last month's weekday pace
    first, last = month_bounds(cur_m)
    expected, d = 0.0, first
    while d <= last:
        expected += prev['weekday_avg'].get(d.weekday(), 0)
        d += timedelta(days=1)
    pp, cp = prev['products'], cur['products']
    names = set(pp) | set(cp)
    delta = sorted(((n, pp.get(n, {}).get('rev', 0), cp.get(n, {}).get('rev', 0)) for n in names),
                   key=lambda x: x[2] - x[1])
    def pcts(c: dict, p: dict):
        return {k: (round(v), round(pct(v, p.get(k, 0)), 1) if p.get(k) else None) for k, v in c.items()}

    weekends = [(dd, WEEKDAYS_UA[date.fromisoformat(dd).weekday()], round(rev))
                for dd, rev in sorted(cur['daily'].items()) if date.fromisoformat(dd).weekday() >= 5]
    half = len(weekends) // 2
    avg = lambda rows: sum(r[2] for r in rows) / len(rows) if rows else 0
    return {
        'lfl_expected': expected,
        'lfl_pct': pct(cur['revenue'], expected),
        # ready-made [value, % vs previous month] so the summary never computes its own numbers
        'categories_pct': pcts(cur['categories'], prev['categories']),
        'weekday_pct': {WEEKDAYS_UA[k]: (round(v), round(pct(v, prev['weekday_avg'].get(k, 0)), 1)
                                         if prev['weekday_avg'].get(k) else None)
                        for k, v in sorted(cur['weekday_avg'].items())},
        'hours_pct': pcts(cur.get('hours', {}), prev.get('hours', {})),
        'weekend_days': weekends,
        'weekend_avg_first_half': round(avg(weekends[:half])),
        'weekend_avg_second_half': round(avg(weekends[half:])),
        'prev_weekend_avg': round(sum(prev['weekday_avg'].get(k, 0) for k in (5, 6)) / 2),
        'per_day_cur': cur['revenue'] / cur['days'] if cur['days'] else 0,
        'per_day_prev': prev['revenue'] / prev['days'] if prev['days'] else 0,
        'up': [x for x in reversed(delta[-6:]) if x[2] > x[1] and x[1] > 0],
        'down': [x for x in delta[:6] if x[2] < x[1] and x[2] > 0],
        'new': sorted(((n, cp[n]['rev']) for n in cp if n not in pp), key=lambda x: -x[1])[:5],
        'gone': sorted(((n, pp[n]['rev']) for n in pp if n not in cp), key=lambda x: -x[1])[:5],
    }


# ─── formatting ───

def format_numbers(cur_m: str, prev_m: str, cur: dict, prev: dict, cmp: dict) -> str:
    L = [f'📊 <b>The Frame — {month_title(cur_m)}</b> vs {month_title(prev_m).split()[0].lower()}', '']
    L.append(f'💰 Виручка: <b>{zl(cur["revenue"])} zł</b> ({fmt_pct(pct(cur["revenue"], prev["revenue"]))})')
    L.append(f'📅 За день: {zl(cmp["per_day_cur"])} zł ({fmt_pct(pct(cmp["per_day_cur"], cmp["per_day_prev"]))}), '
             f'днів {cur["days"]} vs {prev["days"]}')
    L.append(f'🧮 З поправкою на календар: <b>{fmt_pct(cmp["lfl_pct"])}</b>')
    if cur.get('gopos_ok') and prev.get('gopos_ok'):
        L.append(f'🧾 Замовлень: {zl(cur["orders"])} ({fmt_pct(pct(cur["orders"], prev["orders"]))}) · '
                 f'чек {cur["avg_ticket"]:.2f} zł ({fmt_pct(pct(cur["avg_ticket"], prev["avg_ticket"]))})')
    L.append(f'🏷 Знижки: {zl(cur["discount"])} zł ({fmt_pct(pct(cur["discount"], prev["discount"]))}) · '
             f'нетто без ПДВ {zl(cur["net"])} zł')

    L += ['', '<b>Дні тижня</b> (середнє за день):']
    for wd in range(7):
        c, p = cur['weekday_avg'].get(wd), prev['weekday_avg'].get(wd)
        if c is not None:
            L.append(f'  {WEEKDAYS_UA[wd]} {zl(c)} ({fmt_pct(pct(c, p))})')
    if cmp.get('weekend_days'):
        L.append(f'  Вихідні: 1-ша половина {zl(cmp["weekend_avg_first_half"])} → 2-га '
                 f'{zl(cmp["weekend_avg_second_half"])} (минулий місяць {zl(cmp["prev_weekend_avg"])})')

    if cur.get('gopos_ok') and prev.get('gopos_ok'):
        L += ['', '<b>Години</b> (замовлень):']
        for lbl, _, _ in HOUR_BUCKETS:
            c, p = cur['hours'].get(lbl, 0), prev['hours'].get(lbl, 0)
            L.append(f'  {lbl}: {zl(c)} ({fmt_pct(pct(c, p))})')

    L += ['', '<b>Категорії</b>:']
    for k, v in sorted(cur['categories'].items(), key=lambda kv: -kv[1])[:6]:
        L.append(f'  {escape(k)} {zl(v)} zł ({fmt_pct(pct(v, prev["categories"].get(k, 0)))})')

    def items(rows, fmt):
        return [f'  {fmt(r)}' for r in rows]
    if cmp['up']:
        L += ['', '📈 <b>Росте</b>:'] + items(cmp['up'][:5], lambda r: f'{escape(r[0])} {zl(r[1])} → {zl(r[2])}')
    if cmp['down']:
        L += ['', '📉 <b>Падає</b>:'] + items(cmp['down'][:5], lambda r: f'{escape(r[0])} {zl(r[1])} → {zl(r[2])}')
    if cmp['new']:
        L += ['', '🆕 <b>Нове</b>:'] + items(cmp['new'], lambda r: f'{escape(r[0])} {zl(r[1])} zł')
    if cmp['gone']:
        L += ['', '🚫 <b>Зникло</b>:'] + items(cmp['gone'], lambda r: f'{escape(r[0])} (було {zl(r[1])} zł)')

    if cur.get('gopos_ok') and prev.get('gopos_ok') and cur['discounts']:
        L += ['', '<b>Знижки за типом</b>:']
        for k, v in sorted(cur['discounts'].items(), key=lambda kv: -kv[1]['value']):
            pv = prev['discounts'].get(k, {}).get('value', 0)
            L.append(f'  {escape(k)}: {v["orders"]}× · {zl(v["value"])} zł (було {zl(pv)})')
        L += ['', '<b>Каса</b> (замовлень):',
              '  ' + ', '.join(f'{escape(k)} {v}' for k, v in sorted(cur['employees'].items(), key=lambda kv: -kv[1]))]

    if cur['expenses'] and prev['expenses']:
        ce, pe = sum(cur['expenses'].values()), sum(prev['expenses'].values())
        L += ['', f'💸 Витрати: {zl(ce)} zł ({fmt_pct(pct(ce, pe))}) · виручка − витрати: '
                  f'{zl(cur["revenue"] - ce)} zł (було {zl(prev["revenue"] - pe)})']
    else:
        missing = [month_title(m) for m, s in ((cur_m, cur), (prev_m, prev)) if not s['expenses']]
        L += ['', f'💸 Витрати: немає даних за {", ".join(missing)} — завантаж фактури, щоб був P&L']
    return '\n'.join(L)


def llm_summary(cur_m: str, prev_m: str, cur: dict, prev: dict, cmp: dict):
    """Narrative from the computed numbers. Returns (text, usage) or raises."""
    from app.llm import get_client

    def slim(s):
        out = {k: v for k, v in s.items() if k not in ('products', 'daily', 'weekday_avg', 'weekday_n')}
        out['daily'] = {d: [WEEKDAYS_UA[date.fromisoformat(d).weekday()], round(v)]
                        for d, v in sorted(s['daily'].items())}
        return out
    top = lambda s: sorted(s['products'].items(), key=lambda kv: -kv[1]['rev'])[:15]
    data = {'month': cur_m, 'previous_month': prev_m, 'current': slim(cur), 'previous': slim(prev),
            'top15_current': top(cur), 'top15_previous': top(prev), 'comparison': cmp}
    prompt = (
        'Ти аналітик кав\'ярні The Frame (Варшава). Нижче — точні цифри за два місяці (JSON). '
        'Напиши українською коротке резюме для власників: що стало краще, що просіло, на що звернути '
        'увагу, і 3–4 конкретні рекомендації.\n'
        'ПРАВИЛА:\n'
        '- Не рахуй відсотки й суми сам. Бери готові: comparison.lfl_pct, comparison.*_pct '
        '(формат [значення, % до минулого місяця]), weekend_avg_first_half/second_half, prev_weekend_avg.\n'
        '- День тижня дивись тільки в полі daily ([день, виручка]) або weekend_days. Вихідні = Сб і Нд.\n'
        '- Якщо lfl_pct близько нуля, прямо скажи, що зростання пояснюється календарем.\n'
        '- Від\'ємний % — це «просіло», не «стабільно». Різниця менше 2% — «без змін».\n'
        '- Помічай сезонність, нові/зниклі позиції, знижки (Staff, write-off), частку акаунта "Barista".\n'
        '- Нічого не вигадуй поза JSON. Якщо даних немає (наприклад витрат) — так і скажи.\n'
        'Формат: чотири блоки з заголовками "Краще:", "Просіло:", "Увага:", "Рекомендації:", у кожному '
        '2–4 пункти через "• ", кожен пункт одне коротке речення. Без markdown, без зірочок. До 1600 символів.\n\n'
        + json.dumps(data, ensure_ascii=False, default=str))
    r = get_client().converse(
        modelId=SUMMARY_MODEL_ID,
        messages=[{'role': 'user', 'content': [{'text': prompt}]}],
        inferenceConfig={'maxTokens': 1500, 'temperature': 0.1})
    return r['output']['message']['content'][0]['text'].strip(), r.get('usage', {})


def _chunks(text: str, limit: int = TG_LIMIT):
    out, cur = [], ''
    for para in text.split('\n\n'):
        if cur and len(cur) + len(para) + 2 > limit:
            out.append(cur)
            cur = para
        else:
            cur = f'{cur}\n\n{para}' if cur else para
    if cur:
        out.append(cur)
    return out


def build(month: str = None):
    """Return (numbers_text, summary_text_or_None, meta) for `month` (default: last month)."""
    if month is None:
        month = (date.today().replace(day=1) - timedelta(days=1)).strftime('%Y-%m')
    pm = prev_month(month)
    cur, prev = collect(month), collect(pm)
    cmp = compare(month, cur, prev)
    numbers = format_numbers(month, pm, cur, prev, cmp)
    meta = {'month': month, 'prev': pm}
    try:
        t0 = time.monotonic()
        text, usage = llm_summary(month, pm, cur, prev, cmp)
        meta.update(usage=usage, llm_secs=round(time.monotonic() - t0, 1))
        summary = '🧠 <b>Резюме</b>\n\n' + escape(text, quote=False)
    except Exception as e:
        log.warning(f'Monthly report summary failed: {e!r}')
        meta['llm_error'] = repr(e)
        summary = None
    return numbers, summary, meta


def send_monthly_report(month: str = None, admin_only: bool = False):
    from app.telegram_bot import send_message, send_admin, ADMIN_CHAT_ID
    numbers, summary, meta = build(month)
    parts = _chunks(numbers) + (_chunks(summary) if summary else [])
    for p in parts:
        send_message(p, ADMIN_CHAT_ID) if admin_only else send_message(p)
    if 'llm_error' in meta:
        send_admin(f'🔧 <b>Monthly report: AI summary failed</b>\n<code>{escape(meta["llm_error"])[:500]}</code>')
    log.info(f'Monthly report {meta["month"]} sent ({len(parts)} msgs), meta={meta}')
    return meta
