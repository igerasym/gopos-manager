"""Jinja filters/globals from app.templating (number, unit, date formatting, icons)."""
from datetime import date

from markupsafe import Markup

from app.templating import (ICONS, NBSP, THIN, MINUS, configure_env, icon, icon_sprite,
                            num, pct, plural, qty, shortdate, templates, trend, unit_ua, zl)


def test_zl_comma_decimal_and_thin_thousands():
    assert zl(1690.02) == f'1{THIN}690,02{NBSP}zł'
    assert zl(1234567.5) == f'1{THIN}234{THIN}567,50{NBSP}zł'
    assert zl(0) == f'0,00{NBSP}zł'
    assert zl(12.345, 0) == f'12{NBSP}zł'


def test_zl_negative_none_and_junk():
    assert zl(-28.98) == f'{MINUS}28,98{NBSP}zł'
    assert zl(None) == '—'
    assert zl('') == '—'
    assert zl('abc') == '—'
    assert zl(float('nan')) == '—'
    assert zl(5, sign=True) == f'+5,00{NBSP}zł'


def test_num_never_prints_minus_zero():
    assert num(-0.0004, 2) == '0,00'
    assert num(-0.0) == '0'
    assert num(12.5, 2, strip=True) == '12,5'
    assert num(3.0, 2, strip=True) == '3'
    assert num('1500') == f'1{THIN}500'


def test_qty_units_in_ukrainian():
    assert qty(12.5, 'kg') == f'12,5{NBSP}кг'
    assert qty(0.8, 'L') == f'0,8{NBSP}л'
    assert qty(3, 'szt') == f'3{NBSP}шт'
    assert qty(180.0, 'szt') == f'180{NBSP}шт'
    assert qty(250, 'g') == f'250{NBSP}г'
    assert qty(7.825, 'kg') == f'7,825{NBSP}кг'
    assert qty(-0.0001, 'kg') == f'0{NBSP}кг'
    assert qty(5) == '5'
    assert qty(None, 'kg') == '—'
    assert unit_ua('SZT') == 'шт'
    assert unit_ua('box') == 'box'


def test_pct():
    assert pct(38.2) == '38%'
    assert pct(12.46, 1) == '12,5%'
    assert pct(5, sign=True) == '+5%'
    assert pct(None) == '—'


def test_shortdate():
    y = date.today().year
    assert shortdate(f'{y}-11-13') == f'13 лист., {["пн","вт","ср","чт","пт","сб","нд"][date(y, 11, 13).weekday()]}'
    assert shortdate('2026-11-13', year=False) == '13 лист., пт'
    assert shortdate(date(2026, 1, 5), weekday=False, year=False) == '5 січ.'
    assert shortdate(f'{y - 1}-05-01', weekday=False) == f'1 трав. {y - 1}'   # other year: shown
    assert shortdate('2026-11-13 14:15:00', year=False) == '13 лист., пт'
    assert shortdate(None) == '—'
    assert shortdate('garbage') == 'garbage'


def test_trend_directions_and_colours():
    up = trend(112, 100)
    assert up['direction'] == 'up' and up['css'] == 'ok' and up['icon'] == 'trend-up'
    assert up['text'] == '12%' and up['sign'] == '+' and up['word'] == 'більше'
    down = trend(80, 100)
    assert down['direction'] == 'down' and down['css'] == 'danger'
    costs_down = trend(80, 100, good='down')
    assert costs_down['css'] == 'ok'
    flat = trend(101, 100)
    assert flat['direction'] == 'flat' and flat['css'] == 'neutral' and flat['icon'] == 'minus'
    assert trend(5, 0) is None and trend(None, 5) is None


def test_plural_uk():
    forms = ('фактура', 'фактури', 'фактур')
    assert [plural(n, *forms) for n in (1, 2, 4, 5, 11, 12, 21, 22, 25, 111, 0)] == [
        'фактура', 'фактури', 'фактури', 'фактур', 'фактур', 'фактур',
        'фактура', 'фактури', 'фактур', 'фактур', 'фактур']


def test_icon_markup_is_safe_and_known():
    s = icon('box')
    assert isinstance(s, Markup)
    assert '<use href="#i-box"/>' in s and 'aria-hidden="true"' in s
    assert '#i-info' in icon('"><script>')          # unknown name -> fallback, never echoed
    labelled = icon('alert', label='<b>Увага</b>')
    assert 'aria-label="&lt;b&gt;Увага&lt;/b&gt;"' in labelled
    sprite = icon_sprite()
    for name in ICONS:
        assert f'id="i-{name}"' in sprite


def test_filters_available_in_templates():
    env = templates.env
    configure_env(env)
    out = env.from_string("{{ 1690.02|zl }}|{{ 12.5|qty('kg') }}|{{ 38|pct }}|{{ icon('sun') }}").render()
    assert out.startswith(f'1{THIN}690,02{NBSP}zł|12,5{NBSP}кг|38%|<svg')


def test_every_route_env_has_the_filters():
    import app.main  # noqa: F401  (configures every route module)
    from app.routes import dashboard, inventory, wastage, more
    for mod in (dashboard, inventory, wastage, more):
        assert 'zl' in mod.templates.env.filters
        assert 'icon' in mod.templates.env.globals
