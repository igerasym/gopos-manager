/* Склад pages (inventory, stock count, wastage). Uses app.js: openSheet, closeSheet, toast, confirmDialog, esc. */
(function () {
    'use strict';
    const $ = (sel, root) => (root || document).querySelector(sel);
    const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

    // Old attention link '/inventory#order' -> the order tab.
    if (location.pathname === '/inventory' && location.hash === '#order' && !/[?&]tab=/.test(location.search)) {
        location.replace('/inventory?tab=order');
        return;
    }

    // Action sheet: close it before opening the chosen sheet (no stacked modals).
    document.addEventListener('click', e => {
        const b = e.target.closest('[data-sheet-swap]');
        if (!b) return;
        e.preventDefault();
        closeSheet(b.closest('dialog'));
        openSheet(b.getAttribute('data-sheet-swap'), document.querySelector('[data-sheet-open="sheet-add"]'));
    });

    // Copy a plain-text order list.
    document.addEventListener('click', async e => {
        const b = e.target.closest('[data-copy]');
        if (!b) return;
        const src = document.getElementById(b.getAttribute('data-copy'));
        if (!src) return;
        const text = src.value;
        const done = b.getAttribute('data-copy-msg') || 'Список скопійовано';
        try {
            await navigator.clipboard.writeText(text);
            toast(done, { type: 'ok' });
        } catch (err) {
            // Fallback for http / old browsers: select a temporary textarea.
            const t = document.createElement('textarea');
            t.value = text; t.setAttribute('readonly', ''); t.style.position = 'fixed'; t.style.opacity = '0';
            document.body.appendChild(t); t.select();
            const ok = document.execCommand && document.execCommand('copy');
            t.remove();
            toast(ok ? done : 'Не вдалося скопіювати', { type: ok ? 'ok' : 'error' });
        }
    });

    // Search inside a list (rows carry data-name, lower-case).
    function bindListSearch(input, rows) {
        if (!input) return;
        const nothing = $('#stock-nothing');
        input.addEventListener('input', () => {
            const q = input.value.trim().toLowerCase();
            let shown = 0;
            rows.forEach(li => { const hit = !q || li.dataset.name.includes(q); li.hidden = !hit; if (hit) shown++; });
            $$('.group-title').forEach(h => {
                const list = h.nextElementSibling;
                h.hidden = !!list && !$$('li', list).some(li => !li.hidden);
            });
            if (nothing) nothing.classList.toggle('hidden', shown > 0);
        });
    }
    bindListSearch($('#stock-search'), $$('.stock-rows > li'));
    bindListSearch($('#count-search'), $$('.count-rows > li'));

    // Search box that filters a <select>'s options.
    $$('[data-filter-select]').forEach(input => {
        const sel = document.getElementById(input.getAttribute('data-filter-select'));
        if (!sel) return;
        input.addEventListener('input', () => {
            const q = input.value.trim().toLowerCase();
            let first = null;
            Array.from(sel.options).forEach(o => {
                if (!o.value) return;
                const hit = !q || o.textContent.toLowerCase().includes(q);
                o.hidden = !hit; o.disabled = !hit;
                if (hit && !first) first = o;
            });
            const cur = sel.selectedOptions[0];
            if (q && first && (!cur || !cur.value || cur.hidden)) { sel.value = first.value; sel.dispatchEvent(new Event('change')); }
        });
    });

    // Unit suffix follows the chosen ingredient; delivery supplier defaults to the ingredient's.
    $$('[data-unit-source]').forEach(sel => {
        const form = sel.closest('form');
        const target = form && $('[data-unit-target]', form);
        const supTarget = sel.dataset.supplierTarget && document.getElementById(sel.dataset.supplierTarget);
        sel.addEventListener('change', () => {
            const o = sel.selectedOptions[0];
            if (target) target.textContent = (o && o.dataset.unit) || '—';
            if (supTarget && o && o.dataset.supplier) supTarget.value = o.dataset.supplier;
        });
    });

    // Stock list: one shared edit sheet filled from the row's data-* attributes.
    const editForm = $('#edit-form');
    let editing = null;
    if (editForm) {
        document.addEventListener('click', e => {
            const b = e.target.closest('button[data-edit]');
            if (!b) return;
            const d = b.dataset;
            editing = d;
            editForm.action = '/ingredients/edit/' + encodeURIComponent(d.id);
            $('#sheet-edit-h').textContent = d.name;
            $('#e-name').value = d.name;
            $('#e-q').value = d.quantity;
            $('#e-min').value = d.min;
            $('#e-unit').value = d.unit;
            $('#e-sup').value = d.supplier || '';
            const price = $('#e-price');
            if (price) price.value = d.price || '';
            const u = ($('#e-unit').selectedOptions[0] || {}).textContent || '';
            $('#e-q-unit').textContent = u.split(' ')[0];
            $('#e-min-unit').textContent = u.split(' ')[0];
            openSheet('sheet-edit', b);
        });
        $('#e-unit').addEventListener('change', e => {
            const u = (e.target.selectedOptions[0] || {}).textContent.split(' ')[0];
            $('#e-q-unit').textContent = u; $('#e-min-unit').textContent = u;
        });

        $('#e-delete').addEventListener('click', async () => {
            if (!editing) return;
            let text = 'Інгредієнт зникне зі складу, рецептів і заготовок.';
            try {
                const deps = await (await fetch('/api/ingredient/' + encodeURIComponent(editing.id) + '/deps')).json();
                const used = (deps.recipes || []).concat(deps.sub_recipes || []);
                if (used.length) text = 'Його використовують: ' + used.slice(0, 8).join(', ') + (used.length > 8 ? ' та ще ' + (used.length - 8) : '') + '. З цих рецептів він теж зникне.';
            } catch (err) { /* keep the generic text */ }
            const yes = await confirmDialog({ title: 'Видалити «' + editing.name + '»?', text: text, confirmText: 'Видалити', tone: 'danger' });
            if (!yes) return;
            const f = $('#delete-form');
            f.action = '/ingredients/delete/' + encodeURIComponent(editing.id);
            f.submit();
        });

        const hist = $('#e-history');
        if (hist) hist.addEventListener('click', () => { if (editing) showPriceHistory(editing.id, editing.name, hist); });
    }

    function fmtZl(v) {
        return Number(v).toLocaleString('uk-UA', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + '\u00a0zł';
    }

    async function showPriceHistory(id, name, opener) {
        const body = $('#price-body');
        $('#sheet-price-h').textContent = 'Історія цін: ' + name;
        body.innerHTML = '<div class="skeleton skeleton-chart"></div>';
        openSheet('sheet-price', opener);
        try {
            const data = await (await fetch('/api/ingredient/' + encodeURIComponent(id) + '/price-history')).json();
            const h = data.history || [];
            if (!h.length) { body.innerHTML = '<p class="muted">Ціна ще не змінювалась.</p>'; return; }
            const prices = h.map(p => Number(p.price) || 0);
            const lo = Math.min(...prices), hi = Math.max(...prices), span = (hi - lo) || 1;
            const W = 320, H = 80;
            const xy = (p, i) => [(prices.length === 1 ? W / 2 : 8 + i * (W - 16) / (prices.length - 1)), H - 6 - (p - lo) / span * (H - 12)];
            const pts = prices.map((p, i) => xy(p, i).map(n => n.toFixed(1)).join(',')).join(' ');
            const lastXY = xy(prices[prices.length - 1], prices.length - 1);
            let html = '<svg class="price-chart" viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="Зміна ціни"><polyline points="' + pts + '"/><circle cx="' + lastXY[0].toFixed(1) + '" cy="' + lastXY[1].toFixed(1) + '" r="4"/></svg>';
            html += '<p class="small muted between flex"><span>від ' + esc(fmtZl(lo)) + '</span><span>до ' + esc(fmtZl(hi)) + '</span></p>';
            html += '<ul class="rows">';
            h.slice().reverse().slice(0, 10).forEach(p => {
                html += '<li class="row"><span class="row-main"><span class="row-title">' + esc(fmtZl(p.price)) + '</span><span class="row-sub">' + esc(p.source || 'вручну') + '</span></span><span class="row-trail small muted">' + esc(p.date) + '</span></li>';
            });
            body.innerHTML = html + '</ul>';
        } catch (err) {
            body.innerHTML = '<p class="danger-text">Не вдалося завантажити історію цін.</p>';
        }
    }

    // Stock count: progress, Enter -> next field, decimals.
    const countForm = $('#count-form');
    if (countForm) {
        const inputs = $$('.count-rows input');
        const bar = $('[data-count-bar]');
        const update = () => {
            const done = inputs.filter(i => i.value.trim() !== '').length;
            $$('[data-count-done]').forEach(el => { el.textContent = done; });
            if (bar) bar.style.width = (inputs.length ? done / inputs.length * 100 : 0).toFixed(1) + '%';
        };
        inputs.forEach((input, idx) => {
            input.addEventListener('input', update);
            input.addEventListener('keydown', e => {
                if (e.key !== 'Enter') return;
                e.preventDefault();
                const next = inputs.slice(idx + 1).find(i => !i.closest('li').hidden);
                if (next) { next.focus(); next.select(); }
            });
        });
        update();
    }

    // Stock count detail: shared edit sheet.
    document.addEventListener('click', e => {
        const b = e.target.closest('[data-count-edit]');
        if (!b) return;
        $('#ce-item').value = b.dataset.item;
        $('#ce-actual').value = b.dataset.actual;
        $('#ce-unit').textContent = b.dataset.unit;
        $('#sheet-count-edit-h').textContent = b.dataset.name;
        openSheet('sheet-count-edit', b);
    });
})();
