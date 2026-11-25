---
inclusion: auto
---

# Design system "Ранкова кава" — rules for every page

The Frame Manager is a café owner's tool used mostly on a phone. Every screen answers one
question with a sentence and one big number; forms live in sheets; status is never colour alone.
Follow this file literally when you migrate or build a page. The owner-approved visual spec is
direction A (warm, calm, card-per-question) plus inline action buttons in the attention rows.

Files (owned by the foundation, page work should not need to edit them):

| File | What |
|---|---|
| `app/static/style.css` | tokens, shell, all components, `LEGACY ALIASES` section at the end |
| `app/static/app.js` | theme, sheets/dialogs, `toast()`, `confirmDialog()`, data-attribute API, `esc()` |
| `app/templates/base.html` | shell: sidebar (≥1024 px), bottom bar (<1024 px), icon sprite, confirm dialog, toasts |
| `app/templates/_ui.html` | macros: `attention_block`, `trend_chip`, `empty_state`, `sparkline`, `page_header` |
| `app/templating.py` | filters `zl num qty pct shortdate trend plural unit_ua`, globals `icon()`, `nav_counts()` |
| `app/services/attention.py` | `get_attention_items(user)`, `nav_counts(user)`, `HREF` targets, `dismiss()` |
| `app/routes/more.py` | `/more` ("Ще") and `POST /attention/dismiss` |

## 1. Migrating a page: the checklist

1. Keep `{% extends "base.html" %}`. Set `{% block page_title %}Склад{% endblock %}`.
2. Opt out of the legacy styles: `{% block main_class %}{% endblock %}` (base puts `legacy` on
   `<main>` by default). Without it old element styles (`table`, `.card` margins…) still apply.
3. Replace old classes with the components below; no inline colours (`#fff`, `#999`…) and no
   emoji as icons (`📦 ✅ ⚠️ 🔄 ✕`): use `{{ icon('name') }}` and a status chip with text.
4. Page CSS, if really needed, goes in `{% block extra_style %}` (inside a `<style>`) or
   `{% block extra_css %}` (raw tags). Use tokens only (`var(--surface)`), never hex values.
5. Page JS goes in `{% block extra_js %}`. Prefer the data-attribute API (section 6) first.
6. Forms move out of the read view into a sheet opened by a "+" button / FAB.
7. Numbers through the filters (section 4). Words from the glossary (section 5).
8. If your page renders its own sub-navigation, override `{% block section_tabs %}{% endblock %}`
   (base renders "Запаси · Інвентаризація · Списання" tabs for admins on `/inventory*` and `/wastage`).
9. Grep your template for legacy class names and remove them (list in section 9).
10. Run `/Users/ygerasym/.kiro/crew/scratch/venv-gopos/bin/python -m pytest -q`; check 390 px and
    1440 px, light and dark (serve with `run_wt.sh`, section 10).

Do not change role gating: `app/auth.py ROLE_ACCESS` stays as is. Chef/barista must never see
prices, costs or `zł` (tests in `tests/test_role_visibility.py`): wrap money in
`{% if is_admin %}` (or `request.state.user.role == 'admin'`) exactly as the page does today.

## 2. Tokens

All colours are CSS custom properties with light and dark values. Dark mode follows the system
(`prefers-color-scheme`) unless the user picked one (`<html data-theme="dark|light">`, saved in
`localStorage['tfm-theme']`).

| Token | Light | Dark | Use |
|---|---|---|---|
| `--bg` | `#FAF7F2` | `#1C1714` | page background |
| `--surface` / `--surface-2` | `#FFF` / `#F3EEE6` | `#26201B` / `#312922` | cards / insets, hovers, tracks |
| `--text` / `--text-muted` | `#2B2118` / `#6F655B` | `#F4ECE2` / `#B3A698` | body / secondary (≥4.5:1 on surface) |
| `--border` / `--border-strong` | `#E6DED2` / `#CFC3B3` | `#3A3128` / `#54473B` | dividers / inputs, outlines |
| `--accent` `--accent-text` `--accent-soft` | terracotta `#C2410C` | `#F08A5D` | primary action, active nav |
| `--ok` `--ok-bg` | sage | | good, "OK", growth |
| `--warn` `--warn-bg` | amber | | needs a look, "Мало", "Чекає" |
| `--danger` `--danger-bg` | brick | | broken, destructive, out of stock |
| `--info` `--info-bg` | blue | | neutral info |
| `--focus` | `#1D4ED8` | `#8DB4FF` | focus ring (2 px) |
| `--bar` `--ghost` | | | chart bar / last-period ghost bar |
| `--chart-1…5` | | | category colours (classes `.c-1`…`.c-5`) |

Type: headings `--f-serif` (Fraunces → Lora → Georgia; no web font is loaded, the system serif
is used), UI `--f-ui` (Inter → system). Scale: `--fs-caption .75` · `--fs-small .875` ·
`--fs-body 1` (16 px, also on phone) · `--fs-lead 1.125` · `--fs-h3 1.375` · `--fs-h2 1.75` ·
`--fs-hero 2.5rem` (3.5 on desktop). All numbers are tabular (`body` sets `tabular-nums`).

Spacing `--s1…--s8` = 4 8 12 16 24 32 48 64 px. Radius `--r-s 8` (inputs, chips) · `--r-m 16`
(cards) · `--r-l 20` (sheets) · `--r-pill`. Shadows `--shadow`, `--shadow-sheet`, `--shadow-pop`.
Tap target `--tap` = 44 px. Motion 150–200 ms ease-out; everything respects `prefers-reduced-motion`.

Breakpoints: phone `<600`, tablet `600–1023` (still bottom bar), desktop `≥1024` (sidebar).
Helpers: `.hide-phone`, `.only-phone`, `.only-desktop`, `.hide-desktop`.

## 3. Components (copy-paste)

Icons: `{{ icon('box') }}`, size `{{ icon('box', 16) }}`, meaningful icon
`{{ icon('alert', label='Увага') }}`. Names: `ICON_NAMES` in templates, or `ICONS` in
`app/templating.py` (sun moon wallet box book dots file file-x alert info chef clipboard link
trend-up trend-down minus check check-circle clock x x-circle copy cart plus chevron-right
chevron-left chevron-down coin pencil trash search filter refresh download upload users user
logout eye eye-off undo calendar chart truck scale tag cup menu settings). Need a new one? Add it
to `ICONS` (24×24 stroke path, Lucide style) in the same PR. Unknown names render `info`.

### Page header
```jinja
{% from "_ui.html" import page_header %}
{% call page_header('Склад', eyebrow='оновлено о 14:15') %}
  <button class="btn btn-primary" type="button" data-sheet-open="sheet-add">{{ icon('plus') }}Додати</button>
{% endcall %}
{# without actions: {{ page_header('Склад') }} #}
```

### Layout
```html
<div class="cols-2">            <!-- 1 column on phone, 2 on desktop (1.15fr / 1fr) -->
  <div class="stack"> …cards… </div>
  <div class="stack"> …cards… </div>
</div>
<div class="cols-auto"> …cards min 240px… </div>
```

### Card
```html
<section class="card" aria-labelledby="wk-h">
  <header class="card-head"><h2 id="wk-h">Тиждень</h2><span class="small muted">до 25 лист., ср</span></header>
  <p class="sentence">З понеділка: <b class="num">{{ week_total | zl }}</b>.</p>
</section>
```
`.card-flush` removes padding (for lists that touch the edges, then `.card-body`, `.card-foot`).
`.qlabel` = small muted question label; `.sentence` = the one-sentence answer (17 px).

### Hero / stat card with trend chip
```jinja
{% from "_ui.html" import trend_chip %}
{% set t = trend(today_rev, same_day_last_week) %}
<section class="card hero" aria-labelledby="hero-h">
  <div class="hero-top"><h2 id="hero-h" class="qlabel">Виручка сьогодні</h2> …segmented control… </div>
  <div>
    <div class="big num">{{ today_rev | num(2) }}<small>zł</small></div>
    {% if t %}<p class="sentence mt-2">{{ trend_chip(t) }} {{ t.word }}, ніж минулого вівторка до 14:00 ({{ same_day_last_week | zl }})</p>{% endif %}
  </div>
  <div class="stat-row">
    <div class="stat"><span class="stat-label">Чеки</span><span class="stat-value num">{{ orders | num }}</span></div>
    <div class="stat"><span class="stat-label">Середній чек</span><span class="stat-value num">{{ avg | num(2) }} <small>zł</small></span></div>
  </div>
  <div class="stat-foot">{{ icon('clock', 16) }}оновлено о 14:15 <span class="incomplete">{{ icon('alert', 14) }}неповні дані</span></div>
</section>
```
`trend(cur, prev, good='up'|'down', flat_below=2)` returns `None` when there is nothing to compare
(prev 0/None): always guard with `{% if t %}`. `good='down'` for costs and write-offs.
Fields: `pct direction(up|down|flat) sign css(ok|danger|neutral) icon text('12%') word(більше|менше|так само)`.

### Status chip (never colour alone: always text, icon optional)
```html
<span class="chip chip-ok">{{ icon('check', 14) }}OK</span>
<span class="chip chip-warn">{{ icon('alert', 14) }}Мало</span>
<span class="chip chip-danger">{{ icon('x-circle', 14) }}Не розпізнано</span>
<span class="chip chip-info">{{ icon('clock', 14) }}Чекає</span>
<span class="chip chip-neutral">Без рецепту</span>
<span class="chip chip-accent">з каси</span>
```
Invoice statuses: Чекає (info) · Додано (ok) · Не розпізнано (danger). Stock: OK (ok) · Мало (warn) ·
Немає (danger).

### Attention block ("Що потребує уваги") — home page, owner only
```jinja
{% from "_ui.html" import attention_block %}
{{ attention_block(attention_items, limit=5, next='/') }}
```
Route: `from app.services.attention import get_attention_items` →
`'attention_items': get_attention_items(request.state.user)` (returns `[]` for non-admins).
The macro renders "Все гаразд" when the list is empty and a card otherwise. Each row: severity
icon, sentence, optional amount, the action button (`it.href`, `it.action_label`) and an icon
button "Сховати до завтра" that POSTs `/attention/dismiss` (JS removes the row without a reload,
updates the badge, shows a toast; without JS it redirects to `next`). Item keys:
`kind key severity icon text sub amount count href action_label`. To point a button at a new
page/tab, change `HREF` in `app/services/attention.py` (one place).

Hand-made row (same markup):
```html
<li class="attention-row" data-severity="warn">
  <span class="attention-icon">{{ icon('file') }}</span>
  <div class="flex-1"><div class="attention-text">3 фактури чекають підтвердження</div><div class="attention-sub">Makro, Kawa Hurt</div></div>
  <div class="attention-actions"><span class="attention-amount num">{{ 4931 | zl }}</span><a class="btn btn-secondary" href="/expenses#invoices">Переглянути</a></div>
</li>
```

### Notice (inline callout)
```html
<div class="notice notice-warn">{{ icon('alert') }}
  <div class="notice-body"><div class="notice-title">Ще не внесено: Зарплати, ZUS</div>Прибуток за листопад поки що неповний
    <div class="notice-actions"><a class="btn btn-secondary" href="#">Внести</a></div></div>
</div>
```
Variants `notice-warn notice-danger notice-ok notice-info` (plain = neutral).

### List rows (phone-first)
```html
<ul class="rows">
  <li><a class="row" href="/inventory/12">
    <span class="row-lead">{{ icon('box') }}</span>
    <span class="row-main"><span class="row-title">{{ name }}</span><span class="row-sub">{{ supplier }}</span></span>
    <span class="row-trail"><span class="row-value num">{{ q | qty(unit) }}</span><span class="chip chip-warn">Мало</span></span>
    <span class="row-chev">{{ icon('chevron-right') }}</span>
  </a></li>
</ul>
```
Row is ≥56 px. `.row-rank` for top-N numbers. `button.row` for a row that opens a sheet. A row
can be a plain `<li class="row">` when it is not clickable.

### Table (desktop) that becomes card rows below 600 px
```html
<div class="table-wrap">
<table class="table table-stack">
  <thead><tr><th scope="col">Інгредієнт</th><th scope="col" class="num">Залишок</th><th scope="col">Статус</th><th scope="col"><span class="sr-only">Дії</span></th></tr></thead>
  <tbody>
  {% for i in items %}
    <tr>
      <td class="cell-primary" data-label="Інгредієнт">{{ i.name }}</td>
      <td class="num" data-label="Залишок">{{ i.quantity | qty(i.unit) }}</td>
      <td data-label="Статус"><span class="chip chip-ok">OK</span></td>
      <td class="cell-actions"><button class="btn btn-quiet btn-icon" type="button" aria-label="Змінити {{ i.name }}" data-sheet-open="sheet-edit-{{ i.id }}">{{ icon('pencil') }}</button></td>
    </tr>
  {% endfor %}
  </tbody>
</table>
</div>
```
Every `td` needs `data-label` (shown as the label on phone). Money/quantities: `class="num"`
(right aligned, nowrap). Options: `.table-dense` (36 px rows, desktop "Детальніше" pages),
`.table-sticky` (sticky first column), sortable header: `<th aria-sort="descending"><button class="sort">Сума {{ icon('chevron-down', 14) }}</button></th>`.
No zebra stripes.

### Segmented control and tabs
```html
<div class="seg" role="group" aria-label="Період">
  <a href="?p=day" aria-current="true">День</a><a href="?p=week">Тиждень</a><a href="?p=month">Місяць</a>
</div>
<nav class="tabs" aria-label="Розділи">
  <a href="/inventory" aria-current="page">Запаси</a><a href="/inventory?tab=order">Що замовити <span class="count">3</span></a>
</nav>
```
Buttons instead of links: `<button type="button" aria-pressed="true">`.

### Filter chips (scrollable row)
```html
<div class="filter-chips" role="group" aria-label="Фільтр">
  <a class="filter-chip" href="?f=all" aria-current="true">Усі <span class="count">33</span></a>
  <a class="filter-chip" href="?f=low">Мало <span class="count">9</span></a>
  <a class="filter-chip" href="?supplier=Makro">Makro</a>
</div>
```
Multi-select with JS: `<button class="filter-chip" type="button" aria-pressed="false">`.

### Buttons (all ≥44 px tall)
```html
<button class="btn btn-primary" type="submit">Зберегти</button>      <!-- one per screen/sheet -->
<a class="btn btn-secondary" href="…">Переглянути</a>                  <!-- default; plain .btn looks the same -->
<button class="btn btn-ghost" type="button">Скасувати</button>         <!-- accent text, no border -->
<button class="btn btn-quiet" type="button">Сховати</button>           <!-- muted text -->
<button class="btn btn-danger" type="submit">Видалити</button>
<button class="btn btn-secondary btn-icon" type="button" aria-label="Змінити">{{ icon('pencil') }}</button>
<a class="link" href="…">Усі продукти{{ icon('chevron-right', 18) }}</a>
```
`.btn-sm` (36 px, 44 px on touch screens) only for dense desktop tables. `.btn-block` = full
width. `.btn-row` lays out a wrapping group. Destructive actions never sit next to an amount:
put them in the edit sheet and confirm (data-confirm).

### Charts
Week bars with last-week ghost (height in % of the max of both weeks):
```html
<div class="bars" role="group" aria-label="Виручка по днях">
  <a class="bar-col is-today" href="/?date=2026-11-25" aria-label="ср: цього тижня 3 120 zł, минулого 4 410 zł">
    <span class="bar-pair"><span class="bar-ghost" style="height:68%"></span>
      <span class="bar-now is-partial" style="height:48%"><span class="bar-value num">3 120</span></span></span>
    <span class="bar-label">ср</span></a>
  …7 columns (set style="--bars: 12" on .bars for another count)…
</div>
<div class="legend"><span><i class="swatch-bar"></i>Цей тиждень</span><span><i class="swatch-ghost"></i>Минулий тиждень</span></div>
```
`is-partial` = the day is still going (striped). Horizontal meter (top lists, stock level):
`<div class="meter"><i class="is-warn" style="width:40%"></i></div>` (`is-ok|is-warn|is-danger`, default accent).
Stacked bar + legend list:
```html
<div class="stacked" aria-hidden="true"><i class="c-1" style="width:45%"></i><i class="c-2" style="width:25%"></i></div>
<ul class="rows"><li class="row"><span class="swatch c-1"></span><span class="row-main">Оренда</span><span class="row-value num">{{ 14500 | zl }}</span></li></ul>
```
Sparkline: `{% from "_ui.html" import sparkline %}{{ sparkline(values, label='Втрати по інвентаризаціях') }}`.
Inline styles are allowed only for computed `width`/`height` percentages.

### Empty state
```jinja
{% from "_ui.html" import empty_state %}
{{ empty_state('box', 'Усе в нормі', 'Нічого не треба замовляти', '/inventory', 'До запасів') }}
```

### Forms (persistent labels, unit suffix, decimal keypad)
```html
<form class="form" method="post" action="/inventory/delivery">
  <div class="field">
    <label class="field-label" for="d-ing">Інгредієнт</label>
    <select class="select" id="d-ing" name="ingredient_id" required>…</select>
  </div>
  <div class="form-grid">
    <div class="field">
      <label class="field-label" for="d-q">Кількість</label>
      <div class="input-group"><input class="input" id="d-q" name="quantity" inputmode="decimal" required><span class="input-suffix">кг</span></div>
      <span class="field-help">Як у накладній</span>
    </div>
    <div class="field has-error">
      <label class="field-label" for="d-p">Ціна <span class="optional">(необовʼязково)</span></label>
      <div class="input-group"><input class="input" id="d-p" name="price" inputmode="decimal" aria-invalid="true" aria-describedby="d-p-err"><span class="input-suffix">zł</span></div>
      <span class="field-error" id="d-p-err">{{ icon('alert', 16) }}Введіть число</span>
    </div>
  </div>
  <label class="check"><input type="checkbox" name="recurring" value="1"> Постійна витрата</label>
  <div class="form-actions"><button class="btn btn-primary" type="submit">Додати</button></div>
</form>
```
No placeholder-as-label, no uppercase labels. Server parsing of decimals: accept both `,` and `.`
(`float(v.replace(',', '.'))`) when you add `inputmode="decimal"` inputs. Search: `<div class="search">{{ icon('search') }}<input class="input" type="search" …></div>`.

### Sheet (bottom sheet on phone, centred modal ≥600 px) — holds every create/edit form
```html
<button class="btn btn-primary" type="button" data-sheet-open="sheet-delivery">{{ icon('plus') }}Поставка</button>

<dialog class="sheet" id="sheet-delivery" aria-labelledby="sheet-delivery-h">
  <div class="sheet-grip" aria-hidden="true"></div>
  <div class="sheet-head"><h2 id="sheet-delivery-h">Нова поставка</h2>
    <button class="btn btn-quiet btn-icon" type="button" data-sheet-close aria-label="Закрити">{{ icon('x') }}</button></div>
  <form method="post" action="/inventory/delivery" style="display:contents">
    <div class="sheet-body"> …fields… </div>
    <div class="sheet-foot"><button class="btn btn-ghost" type="button" data-sheet-close>Скасувати</button><button class="btn btn-primary" type="submit">Додати</button></div>
  </form>
</dialog>
```
Native `<dialog>` + `showModal()`: Esc closes, the page behind is inert, focus goes to the first
field and returns to the opener, Tab is trapped. Backdrop click closes. `.sheet-wide` = 880 px on
desktop. A `<form>` that wraps body and foot needs `style="display:contents"` (as above).
JS: `openSheet('sheet-delivery')`, `closeSheet('sheet-delivery')`.

### FAB + action sheet (phone; hidden ≥1024 px, show a header button there instead)
```html
<button class="fab" type="button" data-sheet-open="sheet-add" aria-label="Додати">{{ icon('plus') }}</button>
<dialog class="sheet" id="sheet-add" aria-labelledby="sheet-add-h">
  <div class="sheet-grip" aria-hidden="true"></div>
  <div class="sheet-head"><h2 id="sheet-add-h">Додати</h2><button class="btn btn-quiet btn-icon" type="button" data-sheet-close aria-label="Закрити">{{ icon('x') }}</button></div>
  <div class="sheet-body"><ul class="action-list">
    <li><button type="button" data-sheet-open="sheet-delivery"><span class="row-lead">{{ icon('truck') }}</span>Поставка</button></li>
    <li><button type="button" data-sheet-open="sheet-wastage"><span class="row-lead">{{ icon('trash') }}</span>Списання</button></li>
    <li><button type="button" data-sheet-open="sheet-ingredient"><span class="row-lead">{{ icon('plus') }}</span>Інгредієнт</button></li>
  </ul></div>
</dialog>
```

### Toast (aria-live, bottom above the nav)
```js
toast('Поставку додано');
toast('Поставку додано', { action: 'Скасувати', onAction: () => fetch('/…/undo', {method: 'POST'}) });
toast('Не вдалося зберегти', { type: 'error' });   // role=alert
toast('Збережено', { type: 'ok' });
```
After a POST-redirect, show a toast without JS: render
`<div hidden data-toast-now="Поставку додано"></div>` (e.g. from a `?saved=1` query flag).
Click-to-toast: `data-toast="Скопійовано"`. Toast text is set with `textContent`.

### Confirm dialog (replaces `confirm()` and the old modal)
```html
<form method="post" action="/expenses/delete/{{ e.id }}" data-confirm="Видалити «{{ e.name }}»?" data-confirm-ok="Видалити">
  <button class="btn btn-danger" type="submit">Видалити</button>
</form>
<a href="/x" data-confirm="Перейти без збереження?" data-confirm-ok="Перейти" data-confirm-tone="primary">…</a>
```
```js
if (await confirmDialog({ title: 'Видалити?', text: 'Mleko 3,2%', confirmText: 'Видалити' })) { … }
```
Text is inserted with `textContent`; Jinja autoescapes the attribute.

### Skeleton
```html
<div class="skeleton skeleton-text" style="width:40%"></div>
<div class="skeleton skeleton-chart"></div>
```

### Utilities
`.num .muted .small .caption .strong .ok-text .warn-text .danger-text .right .nowrap .truncate
.sr-only .hidden .mt-2…5 .mb-2…4 .flex .flex-1 .wrap .between .gap-2 .stack .btn-row`.

## 4. Numbers, units, dates (always through the filters)

| Filter | Example | Output |
|---|---|---|
| `zl` | `{{ 1690.02 \| zl }}`, `{{ x \| zl(0) }}`, `{{ d \| zl(sign=True) }}` | `1 690,02 zł`, `1 690 zł`, `+5,00 zł` |
| `num` | `{{ 1690.5 \| num }}`, `{{ v \| num(2) }}`, `{{ v \| num(2, strip=True) }}` | `1 691`, `1 690,50`, `12,5` |
| `qty` | `{{ 12.5 \| qty('kg') }}`, `{{ 3 \| qty('szt') }}` | `12,5 кг`, `3 шт` (≤3 decimals, zeros dropped) |
| `pct` | `{{ 38.2 \| pct }}`, `{{ v \| pct(1) }}` | `38%`, `12,5%` (input is already a percentage) |
| `shortdate` | `{{ '2026-11-13' \| shortdate }}`, `\| shortdate(weekday=False)` | `13 лист., пт`, `13 лист.` (year only if not current) |
| `trend` | `{% set t = trend(cur, prev) %}` | dict or `None` (section 3) |
| `plural` | `{{ n }} {{ n \| plural('страва', 'страви', 'страв') }}` | Ukrainian forms 1 / 2–4 / 5+ |
| `unit_ua` | `{{ 'kg' \| unit_ua }}` | `кг` |

Rules: comma decimals everywhere; thousands separator is a narrow no-break space (U+202F); a
no-break space between number and unit; real minus `−`; `None` prints `—`; `-0.0` prints `0`.
Units in UI: `кг л г мл шт` (DB keeps `kg L g ml szt`; never change stored values). Money in a
sentence: `zl(0)` when cents do not matter. Dates: `13 лист., чт` in lists, `13.11` only in very
tight chart labels. Business day = Warsaw, 05:00 cutoff: use `app.gopos_api.warsaw_today()`,
never `date.today()` for "сьогодні".

## 5. Glossary: plain Ukrainian instead of jargon

| Don't write | Write |
|---|---|
| COGS, Собівартість (COGS) | Собівартість продуктів (скільки коштували інгредієнти проданого) |
| Food Cost % | Частка продуктів у ціні ("з 100 zł виручки на продукти йде X zł") |
| Gross profit, Валовий прибуток | Заробіток після продуктів |
| Gross margin, маржа | % що лишається після продуктів |
| Net profit, Прибуток (Витрати) | Чистий прибуток (після всіх витрат) |
| Рентабельність | З кожних 100 zł лишається X zł |
| P&L Звіт | Результат місяця |
| ABC аналіз (A/B/C) | Хіти / Середні / Рідко беруть (or drop it) |
| Техкарта, технологічна картка | Рецепт |
| Покриття техкартами | Страви з рецептом |
| Напівфабрикат, ПФ | Заготовка (власного виробництва) |
| resale | Готовий товар (перепродаж) |
| Синк GoPOS, GoPos | Оновити дані з каси; "каса" |
| Списання (GoPos) | Списано на касі (a source chip "з каси", not a reason) |
| Чиста виручка, нетто | Виручка без ПДВ (VAT) |
| Парсинг, розпарсено | Розпізнано |
| Нестачі | Не вистачило |
| Надлишок | Більше, ніж у системі |
| Мін. запас | Мінімум на складі |
| szt / шт, kg, L | шт, кг, л |
| `2026-11-13` | 13 лист., чт |
| Маппінг | Привʼязка до інгредієнтів |
| Дашборд | Сьогодні |
| Витрати (section name) | Гроші |

Product, ingredient and supplier names stay exactly as in GoPOS / invoices (Polish/English).
Sentence case everywhere, no UPPERCASE labels, no "!" in UI text.

## 6. JavaScript API (`app/static/app.js`)

| API | What |
|---|---|
| `esc(v)` | escape for HTML. **Every** untrusted value (DB/API/user text) that goes into `innerHTML` or a template string must go through `esc()`; prefer `textContent` |
| `toast(msg, opts)` | see Toast |
| `confirmDialog(opts)` | Promise<boolean> |
| `openSheet(id)` / `closeSheet(id)` | dialogs |
| `setTheme('dark'\|'light'\|null)` | null = follow system |
| `data-sheet-open="id"`, `data-sheet-close` | open/close a dialog |
| `data-confirm="…"` (+`-title`, `-ok`, `-tone`) | on form/link/button |
| `data-toast="…"`, `data-toast-now="…"` (+`data-toast-type`) | toasts |
| `data-theme-toggle` (+ child `[data-theme-label]`) | theme switch |
| `form[data-attention-dismiss]` | used by `attention_block` |
| legacy: `openModal`, `closeModal`, `confirmDelete`, `openDeleteModal` | still work on unmigrated pages; replace with `data-confirm` when migrating |

Server side, Jinja autoescape is on: never use `|safe` on DB/user text. `icon()` and the
`_ui.html` macros return safe markup and escape their inputs.

## 7. Shell and navigation (base.html)

Owner (admin): Сьогодні `/` (badge = attention rows) · Гроші `/expenses` (badge = invoices
waiting) · Склад `/inventory` · Меню `/recipes` · Ще `/more`. Staff (chef, barista): Склад ·
Порахувати `/inventory/count` · Списати `/wastage` · Рецепти `/recipes` · Ще (a sheet with theme +
logout; `/more` itself is admin-only by `ROLE_ACCESS`). Active state is computed from the URL
prefix in `base.html`; pages do nothing. Badges come from `nav_counts()` (60 s cache per user;
`attention.clear_cache()` after anything that changes counts, e.g. approving an invoice, if you
want the badge to update at once).

Base template variables are prefixed `tfm_` so they never shadow page context variables; do not
name your context variables `tfm_*`.

Blocks: `page_title`, `extra_css`, `extra_style`, `extra_js`, `main_class`, `section_tabs`, `content`.

## 8. Accessibility rules

Contrast ≥4.5:1 for text (tokens are chosen for it; do not use `--text-muted` on `--surface-2`
for long text), status never by colour alone, tap targets ≥44×44 with ≥8 px gaps, visible focus
ring (do not remove outlines), real `<label for>`, `scope="col"` on table headers, `aria-label`
on icon-only buttons, `lang="uk"` (base), body 16 px on phone, `inputmode="decimal"` for
numbers, `aria-current="page"` for the active tab/nav item, `aria-live` toasts (base).

## 9. Legacy aliases (temporary)

`style.css` ends with `/* LEGACY ALIASES */`: old variables (`--card --red --green --orange
--text-secondary --nav --primary --card-bg --accent-light --radius`) and old classes
(`.card-header .card-title .card-subtitle .section-title .grid-2…5 .kpi* .btn-outline
.btn-success .btn-link .btn-sm(32px) .form-group .form-control .form-row .alert .alert-warning
.alert-success .bar-chart .bar-row .bar-track .bar-fill .badge .badge-green/red/orange .info-tip
.modal-overlay .modal .modal-actions .deps .hide-mobile .text-right .text-center .text-muted
.count-input .inline-edit` and bare `table/th/td`) mapped onto the new look, mostly scoped to
`.legacy`. Migrated pages must not use them. When the last page is migrated: delete the section,
the `legacy` default in `{% block main_class %}`, the `#confirm-modal` markup in `base.html` and
the legacy functions in `app.js`.

## 10. Running a worktree locally

```
/Users/ygerasym/.kiro/crew/scratch/redesign/run_wt.sh <worktree-dir> [port] [--reseed]
/Users/ygerasym/.kiro/crew/scratch/redesign/run_wt.sh stop <worktree-dir>
```
Serves on 127.0.0.1 with invented data (`<worktree>/data/`, git-ignored; "today" in the seed is
2026-11-25). Logins `admin/admin`, `staff/staff`, `chef/chef`. No GoPOS/Telegram/Drive/AWS:
outbound sockets are blocked, so sync/scan buttons fail locally. Restart after Python changes.
Screenshots: open `http://localhost:<port>` (not 127.0.0.1) at 390 and 1440 wide, light and dark.
