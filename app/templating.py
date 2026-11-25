"""Shared Jinja setup: number/date filters, the SVG icon set and nav badge counts.

Every route module keeps its own `Jinja2Templates` instance (historical); `main.py`
calls `configure_env()` on each of them at import time, so the filters and globals
below exist in every template. New route modules can simply use
`from app.templating import templates`.

Filters:  zl, num, qty, pct, shortdate, trend (see the docstrings, and
          .kiro/steering/design-system.md for usage).
Globals:  icon(name, size=20, label=None, cls=''), icon_sprite(), nav_counts(),
          ICON_NAMES.
"""
from __future__ import annotations

import math
from datetime import date, datetime
from pathlib import Path

from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from markupsafe import Markup, escape

# U+202F NARROW NO-BREAK SPACE: the thin thousands separator used in Polish/Ukrainian
# formatting that never wraps a number onto two lines. U+00A0 glues number and unit.
THIN = '\u202f'
NBSP = '\u00a0'
MINUS = '\u2212'
DASH = '—'

UNIT_UA = {
    'kg': 'кг', 'кг': 'кг',
    'g': 'г', 'г': 'г', 'gr': 'г',
    'l': 'л', 'L': 'л', 'л': 'л',
    'ml': 'мл', 'мл': 'мл',
    'szt': 'шт', 'szt.': 'шт', 'шт': 'шт', 'pcs': 'шт', 'pc': 'шт', 'шт.': 'шт',
}

MONTHS_UA_SHORT = ['січ.', 'лют.', 'бер.', 'квіт.', 'трав.', 'черв.',
                   'лип.', 'серп.', 'вер.', 'жовт.', 'лист.', 'груд.']
WEEKDAYS_UA = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'нд']


def _to_float(v):
    if v is None or v == '':
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _group(int_part: str) -> str:
    out = []
    while len(int_part) > 3:
        out.insert(0, int_part[-3:])
        int_part = int_part[:-3]
    out.insert(0, int_part)
    return THIN.join(out)


def num(v, decimals: int = 0, strip: bool = False, sign: bool = False) -> str:
    """1690.5 -> '1 690' (decimals=0) / '1 690,50' (decimals=2). Comma decimal,
    narrow-space thousands, real minus sign. strip=True drops trailing zeros
    ('12,50' -> '12,5', '3,00' -> '3'). sign=True prefixes '+' for positives.
    None / non-numbers -> '—'."""
    f = _to_float(v)
    if f is None:
        return DASH
    decimals = max(0, int(decimals))
    s = f'{abs(f):.{decimals}f}'
    if s.strip('0.') == '':  # rounds to zero: never print -0
        f = 0.0
    int_part, _, frac = s.partition('.')
    if strip and frac:
        frac = frac.rstrip('0')
    body = _group(int_part) + (',' + frac if frac else '')
    if f < 0:
        return MINUS + body
    if sign and f > 0:
        return '+' + body
    return body


def zl(v, decimals: int = 2, sign: bool = False) -> str:
    """1690.02 -> '1 690,02 zł'. None -> '—'."""
    s = num(v, decimals, sign=sign)
    return s if s == DASH else f'{s}{NBSP}zł'


def unit_ua(unit) -> str:
    """'kg' -> 'кг', 'L' -> 'л', 'szt' -> 'шт'. Unknown units pass through."""
    if unit is None:
        return ''
    u = str(unit).strip()
    return UNIT_UA.get(u, UNIT_UA.get(u.lower(), u))


def qty(v, unit='', decimals: int = 3) -> str:
    """(12.5, 'kg') -> '12,5 кг'; (3, 'szt') -> '3 шт'. Up to `decimals` digits,
    trailing zeros dropped, tiny noise like -0.0004 shown as 0."""
    s = num(v, decimals, strip=True)
    if s == DASH:
        return s
    u = unit_ua(unit)
    return f'{s}{NBSP}{u}' if u else s


def pct(v, decimals: int = 0, sign: bool = False) -> str:
    """38.2 -> '38%'; (12.46, 1) -> '12,5%'. The value is already a percentage."""
    s = num(v, decimals, strip=decimals > 0, sign=sign)
    return s if s == DASH else f'{s}%'


def plural(n, one: str, few: str, many: str) -> str:
    """Ukrainian plural form: (1,'фактура','фактури','фактур') -> 'фактура';
    2-4 -> few; 0, 5-20, 11-14 -> many. Fractions take `few`."""
    f = _to_float(n)
    if f is None:
        return many
    if f != int(f):
        return few
    n = abs(int(f))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _to_date(v):
    if v is None or v == '':
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return datetime.fromisoformat(str(v)[:19]).date()
    except ValueError:
        try:
            return datetime.strptime(str(v)[:10], '%Y-%m-%d').date()
        except ValueError:
            return None


def shortdate(v, weekday: bool = True, year: bool | None = None) -> str:
    """'2026-11-13' -> '13 лист., чт'. weekday=False -> '13 лист.'.
    year=None adds the year only when it is not the current one ('13 лист. 2025, чт')."""
    d = _to_date(v)
    if d is None:
        return DASH if v in (None, '') else str(v)
    s = f'{d.day} {MONTHS_UA_SHORT[d.month - 1]}'
    if year or (year is None and d.year != date.today().year):
        s += f' {d.year}'
    if weekday:
        s += f', {WEEKDAYS_UA[d.weekday()]}'
    return s


def trend(cur, prev, good: str = 'up', flat_below: float = 2.0) -> dict | None:
    """Change of `cur` vs `prev` for a trend chip.

    Returns None when there is nothing to compare (prev missing or 0). Otherwise:
      pct        absolute change in % (float, rounded to 1 digit)
      direction  'up' | 'down' | 'flat'   (flat when |change| < flat_below %)
      sign       '+' | '−' | ''
      css        chip modifier: 'ok' | 'danger' | 'neutral'. good='up' means growth is
                 good (revenue); good='down' means a drop is good (costs, write-offs).
      icon       'trend-up' | 'trend-down' | 'minus'
      text       '12%' (no sign, the arrow icon carries the direction)
      word       'більше' | 'менше' | 'так само'
    """
    c, p = _to_float(cur), _to_float(prev)
    if c is None or p is None or p == 0:
        return None
    change = (c - p) / abs(p) * 100
    if abs(change) < flat_below:
        direction = 'flat'
    else:
        direction = 'up' if change > 0 else 'down'
    if direction == 'flat':
        css = 'neutral'
    elif (direction == 'up') == (good == 'up'):
        css = 'ok'
    else:
        css = 'danger'
    return {
        'pct': round(abs(change), 1),
        'direction': direction,
        'sign': {'up': '+', 'down': MINUS, 'flat': ''}[direction],
        'css': css,
        'icon': {'up': 'trend-up', 'down': 'trend-down', 'flat': 'minus'}[direction],
        'text': pct(abs(change)),
        'word': {'up': 'більше', 'down': 'менше', 'flat': 'так само'}[direction],
    }


# ── Icons: Lucide-style 24x24 stroke icons, drawn for this app (no external files) ──
ICONS: dict[str, str] = {
    'sun': '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    'moon': '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>',
    'wallet': '<path d="M3 7a2 2 0 0 1 2-2h13v4"/><path d="M3 7v11a2 2 0 0 0 2 2h14a1 1 0 0 0 1-1V9a1 1 0 0 0-1-1H5a2 2 0 0 1-2-1z"/><circle cx="16.5" cy="14" r="1"/>',
    'box': '<path d="M21 8l-9-5-9 5 9 5 9-5z"/><path d="M3 8v8l9 5 9-5V8"/><path d="M12 13v8"/>',
    'book': '<path d="M4 5a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 2V5z"/><path d="M19 19v2H6a2 2 0 0 1-2-2"/><path d="M9 8h6"/>',
    'dots': '<circle cx="5" cy="12" r="1.2"/><circle cx="12" cy="12" r="1.2"/><circle cx="19" cy="12" r="1.2"/>',
    'file': '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h4"/>',
    'file-x': '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M10 12l4 4M14 12l-4 4"/>',
    'alert': '<path d="M12 3l10 18H2z"/><path d="M12 10v5M12 18v.5"/>',
    'info': '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/>',
    'chef': '<path d="M7 14a4 4 0 1 1 1.5-7.7A4 4 0 0 1 16 5.5a4 4 0 0 1 1 8.5v6H7z"/><path d="M7 17h10"/>',
    'clipboard': '<rect x="5" y="4" width="14" height="17" rx="2"/><path d="M9 4V3h6v1M9 11h6M9 15h4"/>',
    'link': '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
    'trend-up': '<path d="M12 19V5M5 12l7-7 7 7"/>',
    'trend-down': '<path d="M12 5v14M5 12l7 7 7-7"/>',
    'minus': '<path d="M5 12h14"/>',
    'check': '<path d="M5 12l5 5 9-10"/>',
    'check-circle': '<circle cx="12" cy="12" r="9"/><path d="M8 12l3 3 5-6"/>',
    'clock': '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    'x': '<path d="M6 6l12 12M18 6L6 18"/>',
    'x-circle': '<circle cx="12" cy="12" r="9"/><path d="M9 9l6 6M15 9l-6 6"/>',
    'copy': '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h8"/>',
    'cart': '<circle cx="9" cy="20" r="1.3"/><circle cx="18" cy="20" r="1.3"/><path d="M3 4h2.5l2.2 11h10.6l2-8H6.5"/>',
    'plus': '<path d="M12 5v14M5 12h14"/>',
    'chevron-right': '<path d="M9 6l6 6-6 6"/>',
    'chevron-left': '<path d="M15 6l-6 6 6 6"/>',
    'chevron-down': '<path d="M6 9l6 6 6-6"/>',
    'coin': '<circle cx="12" cy="12" r="9"/><path d="M14.5 9.2c-.5-.8-1.4-1.2-2.5-1.2-1.5 0-2.5.8-2.5 2s1 1.7 2.5 2 2.5.8 2.5 2-1 2-2.5 2c-1.1 0-2-.4-2.5-1.2M12 6.5V8M12 16v1.5"/>',
    'pencil': '<path d="M4 20l4-1 11-11-3-3L5 16z"/><path d="M14 6l3 3"/>',
    'trash': '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
    'search': '<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>',
    'filter': '<path d="M4 5h16l-6 8v6l-4-2v-4z"/>',
    'refresh': '<path d="M20 11a8 8 0 0 0-14.6-4.5L4 8"/><path d="M4 4v4h4"/><path d="M4 13a8 8 0 0 0 14.6 4.5L20 16"/><path d="M20 20v-4h-4"/>',
    'download': '<path d="M12 4v11M7 10l5 5 5-5"/><path d="M5 20h14"/>',
    'upload': '<path d="M12 20V9M7 14l5-5 5 5"/><path d="M5 4h14"/>',
    'users': '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M16 4.5a3.5 3.5 0 0 1 0 7M18 14a6 6 0 0 1 3.5 6"/>',
    'user': '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
    'logout': '<path d="M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3"/><path d="M10 17l-5-5 5-5M5 12h11"/>',
    'eye': '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    'eye-off': '<path d="M3 3l18 18"/><path d="M10.6 5.1A10 10 0 0 1 12 5c6.5 0 10 7 10 7a17 17 0 0 1-2.6 3.5M6.3 6.3A17 17 0 0 0 2 12s3.5 7 10 7a9.6 9.6 0 0 0 4.7-1.2"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>',
    'undo': '<path d="M9 14L4 9l5-5"/><path d="M4 9h11a5 5 0 0 1 0 10h-3"/>',
    'calendar': '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>',
    'chart': '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    'truck': '<path d="M3 6h11v10H3zM14 10h4l3 3v3h-7"/><circle cx="7" cy="18" r="1.8"/><circle cx="17" cy="18" r="1.8"/>',
    'scale': '<path d="M12 3v18M7 21h10M5 7h14"/><path d="M5 7l-3 7a3 3 0 0 0 6 0zM19 7l-3 7a3 3 0 0 0 6 0z"/>',
    'tag': '<path d="M3 12V4h8l10 10-8 8z"/><circle cx="7.5" cy="8.5" r="1.3"/>',
    'cup': '<path d="M4 8h13v5a6 6 0 0 1-6 6h-1a6 6 0 0 1-6-6z"/><path d="M17 10h1.5a2.5 2.5 0 0 1 0 5H17"/><path d="M8 2v3M12 2v3"/>',
    'menu': '<path d="M4 6h16M4 12h16M4 18h16"/>',
    'settings': '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
}
ICON_NAMES = tuple(sorted(ICONS))


def icon(name: str, size: int = 20, label: str | None = None, cls: str = '') -> Markup:
    """<svg class="i"> referencing the sprite in base.html. Decorative (aria-hidden)
    unless `label` is given. Unknown names fall back to 'info' (never raw input)."""
    if name not in ICONS:
        name = 'info'
    try:
        size = int(size)
    except (TypeError, ValueError):
        size = 20
    a11y = (f'role="img" aria-label="{escape(label)}"' if label else 'aria-hidden="true"')
    extra = f' {escape(cls)}' if cls else ''
    return Markup(f'<svg class="i{extra}" width="{size}" height="{size}" {a11y} focusable="false">'
                  f'<use href="#i-{name}"/></svg>')


def icon_sprite() -> Markup:
    """Hidden <svg> with one <symbol id="i-NAME"> per icon. Rendered once in base.html."""
    symbols = ''.join(f'<symbol id="i-{n}" viewBox="0 0 24 24">{p}</symbol>' for n, p in ICONS.items())
    return Markup('<svg width="0" height="0" style="position:absolute" aria-hidden="true" '
                  f'focusable="false"><defs>{symbols}</defs></svg>')


@pass_context
def _nav_counts(context) -> dict:
    """Badge counts for the navigation ({'today': n, 'money': n}); {} for non-admins
    or on any error. Cached 60 s per user in app.services.attention."""
    request = context.get('request')
    user = getattr(getattr(request, 'state', None), 'user', None) if request is not None else None
    if not user:
        return {}
    try:
        from app.services.attention import nav_counts
        return nav_counts(user)
    except Exception:  # a badge must never break a page
        return {}


FILTERS = {
    'zl': zl, 'num': num, 'qty': qty, 'pct': pct, 'shortdate': shortdate,
    'trend': trend, 'unit_ua': unit_ua, 'plural': plural,
}


def configure_env(env) -> None:
    """Register filters and globals on a Jinja environment (idempotent)."""
    env.filters.update(FILTERS)
    env.globals.update({
        'icon': icon, 'icon_sprite': icon_sprite, 'ICON_NAMES': ICON_NAMES,
        'nav_counts': _nav_counts, 'trend': trend,
    })


templates = Jinja2Templates(directory=Path(__file__).parent / 'templates')
configure_env(templates.env)
