"""'Що потребує уваги': the owner's to-do list on the dashboard, computed from existing tables.

get_attention_items(user) -> list of dicts, most urgent first:
    kind, key     rule id and a short signature of what the row is about; a dismissal hides
                  (kind, key) only, so a NEW invoice / ingredient brings the row back
    severity      'danger' | 'warn' | 'info'
    text, sub     plain-Ukrainian sentence and secondary line (names inside are untrusted: autoescape)
    amount        money in zł or None; amount_note / amount_expense say it is money going out
    count, href, action_label   how many things, where the button goes ('' href = no button)

Admin only: other roles get []. Every rule is read-only, capped and wrapped: a failing rule
is logged and skipped, it never breaks the dashboard.
"""
from __future__ import annotations

import calendar
import hashlib
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app import db as db_module
from app.auth import is_admin as _is_admin

log = logging.getLogger(__name__)

NBSP = '\u00a0'
MONTHS_UA_SHORT = ['січ.', 'лют.', 'бер.', 'квіт.', 'трав.', 'черв.', 'лип.', 'серп.', 'вер.', 'жовт.', 'лист.', 'груд.']
WEEKDAYS_UA = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'нд']


def plural(n, one: str, few: str, many: str) -> str:
    """Ukrainian plural form: 1 -> one, 2-4 -> few, 0 / 5-20 / 11-14 -> many."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def pct(v) -> str:
    return f'{round(v)}%'


def zl(v, decimals: int = 0) -> str:
    """1690.02 -> '1 690 zł' (non-breaking spaces)."""
    s = f'{v:,.{decimals}f}'.replace(',', NBSP)
    return f'{s}{NBSP}zł'


def shortdate(v, weekday: bool = True) -> str:
    """'2026-11-13' -> '13 лист., пт'."""
    d = v if isinstance(v, date) and not isinstance(v, datetime) else (
        v.date() if isinstance(v, datetime) else datetime.strptime(str(v)[:10], '%Y-%m-%d').date())
    out = f'{d.day} {MONTHS_UA_SHORT[d.month - 1]}'
    return f'{out}, {WEEKDAYS_UA[d.weekday()]}' if weekday else out


WARSAW = ZoneInfo('Europe/Warsaw')
SEVERITY_ORDER = {'danger': 0, 'warn': 1, 'info': 2}
MAX_ROWS = 500           # cap for any per-rule row scan
STALE_AFTER_HOURS = 2    # intraday refresh older than this during opening hours = stale
OPEN_HOURS = range(8, 23)
COUNT_OVERDUE_DAYS = 7
COSTS_CHECK_FROM_DAY = 10
WASTAGE_FACTOR = 1.3
WASTAGE_MIN_ZL = 20.0
DEARER_PP = 5.0          # food-cost share rise (percentage points) that triggers a row

# Where each action button goes (the original pages).
HREF = {
    'invoices_waiting': '/expenses#pending-section',
    'invoice_failed': '/expenses#pending-section',
    'items_unmatched': '/expenses#pending-section',
    'low_stock': '/inventory',
    'no_recipe': '/recipes',
    'unclassified_product': '/recipes',
    'costs_incomplete': '/expenses',
    'count_overdue': '/inventory/count',
    'high_wastage': '/wastage',
    'dish_dearer': '/recipes',
    'sales_stale': '',   # no page to open: the «Синк GoPOS» button is in the top bar
}


def _sig(*parts) -> str:
    raw = '|'.join(str(p) for p in parts)
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def _today() -> date:
    from app.gopos_api import warsaw_today
    return datetime.strptime(warsaw_today(), '%Y-%m-%d').date()


def _now_warsaw() -> datetime:
    return datetime.now(WARSAW)


def _local_to_warsaw(ts: str) -> datetime | None:
    """sync_log stores naive server-local time (UTC in the container)."""
    try:
        dt = datetime.strptime(ts[:19], '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None
    return dt.astimezone().astimezone(WARSAW)  # naive -> assume server-local


def _first_names(names, limit=3) -> str:
    names = [n for n in names if n]
    out = ', '.join(names[:limit])
    if len(names) > limit:
        out += f' і ще {len(names) - limit}'
    return out


class _Ctx:
    """Per-call shared state: today's date and lazily computed lookups."""

    def __init__(self, db, today: date, now: datetime):
        self.db, self.today, self.now = db, today, now
        self._cost = None
        self._prices = None

    @property
    def cost_lookup(self) -> dict:
        if self._cost is None:
            from app.services.recipes import get_cost_lookup
            self._cost = get_cost_lookup()
        return self._cost

    @property
    def selling_prices(self) -> dict:
        if self._prices is None:
            from app.services.recipes import get_selling_prices
            self._prices = get_selling_prices()
        return self._prices


def _item(kind, severity, icon, text, sub='', amount=None, count=1, key='', action_label='',
          amount_note='', amount_expense=False):
    return {'kind': kind, 'key': key or kind, 'severity': severity, 'icon': icon,
            'text': text, 'sub': sub, 'amount': amount, 'count': count,
            'href': HREF[kind], 'action_label': action_label,
            'amount_note': amount_note, 'amount_expense': amount_expense}


# ── rules ─────────────────────────────────────────────────────────────────────

def rule_invoices_waiting(c: _Ctx):
    rows = c.db.execute(
        "SELECT id, vendor, total FROM parsed_invoices WHERE expense_status = 'pending' "
        "AND parse_status = 'parsed' AND total > 0 ORDER BY total DESC LIMIT ?", (MAX_ROWS,)).fetchall()
    if not rows:
        return None
    n = len(rows)
    vendors = list(dict.fromkeys(r['vendor'] for r in rows if r['vendor']))
    return _item('invoices_waiting', 'warn', 'file',
                 f"{n} {plural(n, 'фактура чекає', 'фактури чекають', 'фактур чекають')} підтвердження",
                 sub=_first_names(vendors), amount=round(sum(r['total'] for r in rows), 2), count=n,
                 key=_sig(*sorted(r['id'] for r in rows)), action_label='Переглянути')


def rule_invoice_failed(c: _Ctx):
    rows = c.db.execute(
        "SELECT id, file_name FROM parsed_invoices WHERE parse_status = 'error' "
        "AND COALESCE(expense_status, '') NOT IN ('skipped', 'added') ORDER BY id LIMIT ?",
        (MAX_ROWS,)).fetchall()
    if not rows:
        return None
    n = len(rows)
    return _item('invoice_failed', 'danger', 'file-x',
                 f"{n} {plural(n, 'фактуру', 'фактури', 'фактур')} не вдалося розпізнати",
                 sub=_first_names([r['file_name'] for r in rows], 2), count=n,
                 key=_sig(*[r['id'] for r in rows]), action_label='Відкрити')


def rule_items_unmatched(c: _Ctx):
    row = c.db.execute(
        "SELECT COUNT(*) AS n, COALESCE(MAX(id), 0) AS mx FROM invoice_items_pending WHERE status = 'pending'"
    ).fetchone()
    n = row['n'] if row else 0
    if not n:
        return None
    return _item('items_unmatched', 'info', 'link',
                 f"{n} {plural(n, 'позиція', 'позиції', 'позицій')} з фактур ще не "
                 f"{plural(n, 'звірена', 'звірені', 'звірені')} зі складом",
                 sub='Без цього склад і ціни не оновляться', count=n, key=_sig(n, row['mx']),
                 action_label='Звірити зі складом')


def rule_low_stock(c: _Ctx):
    rows = c.db.execute(
        'SELECT i.id, i.name, s.name AS supplier FROM ingredients i '
        'LEFT JOIN suppliers s ON s.id = i.supplier_id '
        'WHERE i.min_quantity > 0 AND i.quantity <= i.min_quantity ORDER BY i.name LIMIT ?',
        (MAX_ROWS,)).fetchall()
    if not rows:
        return None
    n = len(rows)
    by_sup: dict[str, int] = {}
    for r in rows:
        by_sup[r['supplier'] or 'без постачальника'] = by_sup.get(r['supplier'] or 'без постачальника', 0) + 1
    parts = [f'{s} ({k})' if k > 1 else s for s, k in sorted(by_sup.items(), key=lambda x: -x[1])]
    return _item('low_stock', 'danger', 'box',
                 f"{n} {plural(n, 'інгредієнт закінчується', 'інгредієнти закінчуються', 'інгредієнтів закінчуються')}",
                 sub=_first_names(parts, 3), count=n, key=_sig(*[r['id'] for r in rows]),
                 action_label='Що замовити')


def _recent_sales(c: _Ctx, days=30):
    start = (c.today - timedelta(days=days - 1)).isoformat()
    return c.db.execute(
        'SELECT product_name, SUM(total_money) AS rev FROM sales WHERE date >= ? AND date <= ? '
        'GROUP BY product_name ORDER BY rev DESC LIMIT ?', (start, c.today.isoformat(), 2000)).fetchall()


def _pos_kinds(c: _Ctx) -> dict:
    return {r['product_name']: r['pos_kind'] for r in c.db.execute(
        'SELECT product_name, pos_kind FROM pos_products LIMIT 5000').fetchall()}


def rule_no_recipe(c: _Ctx):
    sales = _recent_sales(c)
    if not sales:
        return None
    kinds = _pos_kinds(c)
    total = sum((r['rev'] or 0) for r in sales)
    missing = [r for r in sales
               if kinds.get(r['product_name']) not in ('ignore', 'unclassified')
               and r['product_name'] not in c.cost_lookup]
    if not missing:
        return None
    n = len(missing)
    share = sum((r['rev'] or 0) for r in missing) / total * 100 if total else 0
    return _item('no_recipe', 'warn' if share >= 10 else 'info', 'book',
                 f"{n} {plural(n, 'страва', 'страви', 'страв')} без рецепту",
                 sub=f'{pct(share)} виручки за 30 днів без підрахунку собівартості', count=n,
                 key=_sig(*sorted(r['product_name'] for r in missing)), action_label='Додати рецепти')


def rule_unclassified_product(c: _Ctx):
    start = (c.today - timedelta(days=29)).isoformat()
    rows = c.db.execute(
        "SELECT pp.product_name FROM pos_products pp WHERE pp.pos_kind = 'unclassified' "
        'AND EXISTS (SELECT 1 FROM sales s WHERE s.product_name = pp.product_name AND s.date >= ?) '
        'ORDER BY pp.product_name LIMIT ?', (start, MAX_ROWS)).fetchall()
    if not rows:
        return None
    names = [r['product_name'] for r in rows]
    n = len(names)
    text = (f'Нова позиція в касі: {names[0]}' if n == 1
            else f"{n} {plural(n, 'нова позиція', 'нові позиції', 'нових позицій')} в касі")
    return _item('unclassified_product', 'info', 'tag', text,
                 sub='Вкажіть, що це: страва за рецептом чи готовий товар' if n == 1 else _first_names(names),
                 count=n, key=_sig(*names), action_label='Налаштувати')


def missing_recurring_costs(db, month: str, prev: str) -> list[dict]:
    """Recurring costs of `prev` month still missing in `month` (the one rule shared by the
    'costs_incomplete' attention row and the 'Гроші' page).

    A cost counts as entered when this month already has an expense with the same name OR,
    when it has a category, any expense in the same category (e.g. last month 'Prąd + gaz'
    under 'Комунальні', this month 'Rachunek PGE' under 'Комунальні'). A dismissed name
    ('Цього місяця не буде') never counts as missing."""
    last = db.execute(
        'SELECT name, category, SUM(amount) AS amount FROM expenses WHERE month = ? AND recurring = 1 '
        'GROUP BY name ORDER BY amount DESC LIMIT ?', (prev, MAX_ROWS)).fetchall()
    if not last:
        return []
    rows = db.execute('SELECT DISTINCT name, category FROM expenses WHERE month = ?', (month,)).fetchall()
    names = {r['name'] for r in rows}
    cats = {r['category'] for r in rows if r['category']}
    dismissed = {r['name'] for r in db.execute('SELECT name FROM expenses_dismissed WHERE month = ?', (month,))}
    return [{'name': r['name'], 'category': r['category'], 'amount': round(r['amount'] or 0, 2)}
            for r in last
            if r['name'] not in names and r['name'] not in dismissed
            and not (r['category'] and r['category'] in cats)]


def rule_costs_incomplete(c: _Ctx):
    if c.today.day <= COSTS_CHECK_FROM_DAY:
        return None
    cur = c.today.strftime('%Y-%m')
    first = c.today.replace(day=1)
    prev = (first - timedelta(days=1)).strftime('%Y-%m')
    missing = missing_recurring_costs(c.db, cur, prev)
    if not missing:
        return None
    cats = list(dict.fromkeys((r['category'] or r['name']) for r in missing))
    amount = round(sum(r['amount'] or 0 for r in missing), 2)
    return _item('costs_incomplete', 'warn', 'coin', f'Ще не внесено: {_first_names(cats, 4)}',
                 sub='Без них прибуток за цей місяць поки що неповний',
                 amount=amount, amount_note='витрати минулого місяця', amount_expense=True, count=len(missing), key=_sig(cur, *sorted(r['name'] for r in missing)),
                 action_label='Внести')


def rule_count_overdue(c: _Ctx):
    row = c.db.execute('SELECT MAX(date) AS d FROM stock_counts').fetchone()
    last = row['d'] if row else None
    if not last:
        has_ing = c.db.execute('SELECT 1 FROM ingredients LIMIT 1').fetchone()
        if not has_ing:
            return None
        return _item('count_overdue', 'info', 'clipboard', 'Інвентаризацію ще не робили',
                     sub='Порахуйте залишки, щоб бачити реальні втрати', key='never', action_label='Почати')
    try:
        days = (c.today - datetime.strptime(last[:10], '%Y-%m-%d').date()).days
    except ValueError:
        return None
    if days <= COUNT_OVERDUE_DAYS:
        return None
    return _item('count_overdue', 'info', 'clipboard',
                 f"Інвентаризацію не робили {days} {plural(days, 'день', 'дні', 'днів')}",
                 sub=f'Остання була {shortdate(last)}', count=days, key=last[:10], action_label='Почати')


def _wastage_cost(c: _Ctx, start: str, end: str) -> float:
    rows = c.db.execute(
        'SELECT w.quantity, w.source, w.product_name, COALESCE(i.unit_price, 0) AS unit_price '
        'FROM wastage w LEFT JOIN ingredients i ON i.id = w.ingredient_id '
        'WHERE w.date >= ? AND w.date <= ? LIMIT 5000', (start, end)).fetchall()
    total = 0.0
    for r in rows:
        q = r['quantity'] or 0
        if r['source'] == 'gopos':
            total += q * (c.cost_lookup.get(r['product_name'], 0) or 0)
        else:
            total += q * (r['unit_price'] or 0)
    return total


def rule_high_wastage(c: _Ctx):
    first = c.today.replace(day=1)
    prev_last = first - timedelta(days=1)
    prev_first = prev_last.replace(day=1)
    prev_same = prev_first.replace(day=min(c.today.day, calendar.monthrange(prev_first.year, prev_first.month)[1]))
    cur = _wastage_cost(c, first.isoformat(), c.today.isoformat())
    prev = _wastage_cost(c, prev_first.isoformat(), prev_same.isoformat())
    if prev <= 0 or cur < WASTAGE_MIN_ZL or cur <= prev * WASTAGE_FACTOR:
        return None
    return _item('high_wastage', 'warn', 'trash',
                 'Списали більше, ніж минулого місяця',
                 sub=f'З початку місяця {zl(cur, 0)}, минулого місяця за той самий час {zl(prev, 0)}', amount=round(cur, 2),
                 key=_sig(c.today.strftime('%Y-%m'), int(cur // 50)), action_label='Подивитись')


def rule_dish_dearer(c: _Ctx):
    since = (c.today - timedelta(days=30)).isoformat()
    hist = c.db.execute(
        'SELECT ingredient_id, price, date FROM ingredient_price_history '
        'WHERE ingredient_id IN (SELECT ingredient_id FROM ingredient_price_history WHERE date >= ?) '
        'ORDER BY ingredient_id, date, id LIMIT 5000', (since,)).fetchall()
    by_ing: dict[int, list] = {}
    for h in hist:
        by_ing.setdefault(h['ingredient_id'], []).append(h)
    old_price = {}
    for ing, entries in by_ing.items():
        recent = [e for e in entries if (e['date'] or '') >= since]
        older = [e for e in entries if (e['date'] or '') < since] or entries[:-1]
        if recent and older and older[-1]['price']:
            old_price[ing] = older[-1]['price']
    if not old_price:
        return None
    rows = c.db.execute(
        'SELECT r.product_name, r.ingredient_id, r.amount, COALESCE(i.unit_price, 0) AS unit_price '
        'FROM recipes r JOIN ingredients i ON i.id = r.ingredient_id LIMIT 5000').fetchall()
    dishes: dict[str, list] = {}
    for r in rows:
        dishes.setdefault(r['product_name'], []).append(r)
    worse = []
    for name, lines in dishes.items():
        if not any(l['ingredient_id'] in old_price for l in lines):
            continue
        price = c.selling_prices.get(name) or 0
        if price <= 0:
            continue
        new_cost = sum(l['amount'] * l['unit_price'] for l in lines)
        old_cost = sum(l['amount'] * old_price.get(l['ingredient_id'], l['unit_price']) for l in lines)
        new_s, old_s = new_cost / price * 100, old_cost / price * 100
        if new_s - old_s > DEARER_PP:
            worse.append((new_s - old_s, name, old_s, new_s))
    if not worse:
        return None
    worse.sort(reverse=True)
    _, name, old_s, new_s = worse[0]
    n = len(worse)
    detail = f'{name}: продукти {pct(old_s)} → {pct(new_s)} ціни'
    if n == 1:
        text, sub = f'Інгредієнти для «{name}» подорожчали', f'Продукти тепер {pct(new_s)} ціни (було {pct(old_s)})'
    else:
        text, sub = f"Інгредієнти подорожчали для {n} {plural(n, 'страви', 'страв', 'страв')}", detail
    return _item('dish_dearer', 'warn', 'trend-up', text, sub=sub, count=n,
                 key=_sig(*sorted(w[1] for w in worse)), action_label='Переглянути ціну')


def rule_sales_stale(c: _Ctx):
    last = c.db.execute('SELECT status, finished_at, started_at FROM sync_log ORDER BY id DESC LIMIT 1').fetchone()
    ok = c.db.execute("SELECT finished_at FROM sync_log WHERE status IN ('done', 'ok') "
                      'ORDER BY id DESC LIMIT 1').fetchone()
    ok_at = _local_to_warsaw(ok['finished_at']) if ok and ok['finished_at'] else None

    stale_since = None
    if last and last['status'] == 'error':
        stale_since = ok_at or _local_to_warsaw(last['started_at'] or '')
    else:
        try:
            from app import gopos_api
            # only today's refresh counts: a leftover 'HH:MM' from yesterday would read as fresh
            hhmm = (gopos_api.LAST_INTRADAY_SYNC
                    if gopos_api.LAST_INTRADAY_DAY == gopos_api.warsaw_today() else None)
        except Exception:
            hhmm = None
        if hhmm and c.now.hour in OPEN_HOURS:
            h, m = (int(x) for x in hhmm.split(':'))
            at = c.now.replace(hour=h, minute=m, second=0, microsecond=0)
            if at > c.now:
                at -= timedelta(days=1)
            if c.now - at > timedelta(hours=STALE_AFTER_HOURS):
                stale_since = at
    if stale_since is None:
        return None
    fresh = data_freshness(c.db, c.now)['text']
    return _item('sales_stale', 'danger', 'refresh', 'Дані з каси давно не оновлювались',
                 sub=f'{fresh}. Цифри продажів можуть бути неповні, натисніть «Синк GoPOS» угорі.',
                 key=stale_since.strftime('%Y-%m-%d %H:%M'), action_label='')


def data_freshness(db=None, now: datetime | None = None) -> dict:
    """When sales were last refreshed from the till, one source for every page.

    The in-memory last intraday refresh (gopos_api.LAST_INTRADAY_SYNC, 'HH:MM' Warsaw) wins;
    otherwise the last finished sync_log row. Returns {'at': Warsaw datetime | None,
    'error': bool (the newest sync_log row failed), 'text': one plain sentence}."""
    now = now or _now_warsaw()
    at = None
    try:
        from app import gopos_api
        # only today's refresh counts: a leftover 'HH:MM' from yesterday would read as fresh
        hhmm = (gopos_api.LAST_INTRADAY_SYNC
                if gopos_api.LAST_INTRADAY_DAY == gopos_api.warsaw_today() else None)
    except Exception:
        hhmm = None
    if hhmm:
        try:
            h, m = (int(x) for x in hhmm.split(':'))
            at = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if at > now:
                at -= timedelta(days=1)
        except ValueError:
            at = None
    own = db is None
    error = False
    try:
        db = db or db_module.get_db()
        last = db.execute('SELECT status, finished_at, started_at FROM sync_log ORDER BY id DESC LIMIT 1').fetchone()
        error = bool(last) and last['status'] == 'error'
        # newest of: the in-memory refresh, the stored mark (written by every sync and every
        # webhook order, so it survives a restart) and the last finished sync
        candidates = [at] if at else []
        try:
            from app import gopos_api
            stored = gopos_api.last_refreshed_utc(db)
            if stored:
                candidates.append(stored.replace(tzinfo=ZoneInfo('UTC')).astimezone(WARSAW))
        except Exception as e:
            log.warning('data_freshness (stored mark): %s', e)
        row = db.execute('SELECT finished_at FROM sync_log WHERE finished_at IS NOT NULL '
                         "AND status != 'error' ORDER BY id DESC LIMIT 1").fetchone()
        if row:
            synced = _local_to_warsaw(row['finished_at'])
            if synced:
                candidates.append(synced)
        at = max(candidates) if candidates else None
    except Exception as e:
        log.warning('data_freshness: %s', e)
    finally:
        if own and db is not None:
            db.close()
    if at is None:
        text = 'Дані з каси ще не оновлювались'
    elif at.date() == now.date():
        text = f"Дані з каси оновлено сьогодні о {at.strftime('%H:%M')}"
    else:
        text = f"Дані з каси оновлено {shortdate(at.date(), weekday=False)} о {at.strftime('%H:%M')}"
    return {'at': at, 'error': error, 'text': text}


RULES = [
    rule_sales_stale, rule_invoices_waiting, rule_invoice_failed, rule_items_unmatched,
    rule_low_stock, rule_no_recipe, rule_unclassified_product, rule_costs_incomplete,
    rule_count_overdue, rule_high_wastage, rule_dish_dearer,
]


def _dismissed(db, user_id, today: date) -> set:
    try:
        rows = db.execute('SELECT kind, key FROM attention_dismissed WHERE user_id = ? AND until > ?',
                          (user_id, today.isoformat())).fetchall()
    except Exception as e:  # table missing on an old DB: nothing dismissed
        log.warning('attention_dismissed unreadable: %s', e)
        return set()
    return {(r['kind'], r['key']) for r in rows}


def get_attention_items(user, today: date | None = None, now: datetime | None = None,
                        include_dismissed: bool = False) -> list[dict]:
    """All current attention rows for `user` (owner only), most urgent first.
    The template shows the first 5 and puts the rest behind a button."""
    if not _is_admin(user):
        return []
    try:
        today = today or _today()
        now = now or _now_warsaw()
        db = db_module.get_db()
    except Exception as e:
        log.warning('attention: setup failed: %s', e)
        return []
    items = []
    try:
        c = _Ctx(db, today, now)
        for rule in RULES:
            try:
                it = rule(c)
            except Exception as e:
                log.warning('attention rule %s failed: %s', rule.__name__, e)
                continue
            if it:
                items.append(it)
        if not include_dismissed:
            hidden = _dismissed(db, user['id'], today)
            items = [i for i in items if (i['kind'], i['key']) not in hidden]
    finally:
        db.close()
    items.sort(key=lambda i: (SEVERITY_ORDER.get(i['severity'], 9), -(i['amount'] or 0)))
    return items


def dismiss(user, kind: str, key: str, today: date | None = None) -> str:
    """Hide (kind, key) for this user until the next Warsaw business day. Returns `until`."""
    today = today or _today()
    until = (today + timedelta(days=1)).isoformat()
    db = db_module.get_db()
    try:
        db.execute('INSERT INTO attention_dismissed (user_id, kind, key, until) VALUES (?, ?, ?, ?) '
                   'ON CONFLICT(user_id, kind, key) DO UPDATE SET until = excluded.until',
                   (user['id'], kind[:64], key[:64], until))
        # keep the table small
        db.execute('DELETE FROM attention_dismissed WHERE until < ?', ((today - timedelta(days=30)).isoformat(),))
        db.commit()
    finally:
        db.close()
    return until
