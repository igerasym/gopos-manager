/* The Frame Manager — shared front-end helpers.
   Docs: .kiro/steering/design-system.md
   Public API (globals):
     esc(v)                         escape untrusted text before putting it into HTML
     toast(message, {type, action, onAction, timeout})
     confirmDialog({title, text, confirmText, cancelText, danger}) -> Promise<boolean>
     openSheet(id) / closeSheet(id)
     setTheme('light'|'dark'|null)
   Data-attribute API (no page JS needed):
     data-sheet-open="ID"          open <dialog class="sheet" id="ID"> (or .dialog)
     data-sheet-close              close the dialog it sits in
     data-toast="Текст"            click shows a toast
     data-toast-now="Текст"        on page load shows a toast (e.g. after a redirect)
     data-confirm="Питання?"       on a <form>: confirm before submit; on a link/button: before navigating/clicking
       + data-confirm-title, data-confirm-ok ("Видалити"), data-confirm-tone="danger|primary"
     data-theme-toggle             button that flips light/dark (label in [data-theme-label])
     form[data-attention-dismiss]  "Сховати до завтра" without a page reload
   Legacy (kept for unmigrated pages): openModal, closeModal, openDeleteModal, confirmDelete, sync polling.
*/

// Escape text before inserting it as HTML (server messages, invoice data).
function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

/* ── theme ─────────────────────────────────────────────────────────────── */
(function () {
    const root = document.documentElement;
    const mq = window.matchMedia ? matchMedia('(prefers-color-scheme: dark)') : null;
    function current() { return root.getAttribute('data-theme') || (mq && mq.matches ? 'dark' : 'light'); }
    function sync() {
        const dark = current() === 'dark';
        document.querySelectorAll('[data-theme-toggle]').forEach(b => {
            b.setAttribute('aria-pressed', String(dark));
            const label = b.querySelector('[data-theme-label]');
            if (label) label.textContent = dark ? 'Світла тема' : 'Темна тема';
            const use = b.querySelector('svg.i use');
            if (use) use.setAttribute('href', dark ? '#i-sun' : '#i-moon');
        });
    }
    window.setTheme = function (t) {
        if (t === 'dark' || t === 'light') root.setAttribute('data-theme', t); else root.removeAttribute('data-theme');
        try { t ? localStorage.setItem('tfm-theme', t) : localStorage.removeItem('tfm-theme'); } catch (e) {}
        sync();
    };
    document.addEventListener('click', e => {
        const b = e.target.closest('[data-theme-toggle]');
        if (!b) return;
        setTheme(current() === 'dark' ? 'light' : 'dark');
    });
    if (mq && mq.addEventListener) mq.addEventListener('change', sync);
    if (window.MutationObserver) new MutationObserver(sync).observe(root, { attributes: true, attributeFilter: ['data-theme'] });
    sync();
})();

/* ── sheets / modals (<dialog class="sheet|dialog">) ───────────────────── */
const _openers = new WeakMap();
const FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]):not([type=hidden]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';

function openSheet(id, opener) {
    const d = typeof id === 'string' ? document.getElementById(id) : id;
    if (!d) return null;
    _openers.set(d, opener || document.activeElement);
    if (typeof d.showModal === 'function') { if (!d.open) d.showModal(); } else d.setAttribute('open', '');
    const body = d.querySelector('.sheet-body');
    const first = d.querySelector('[autofocus]') || (body && body.querySelector(FOCUSABLE)) || d.querySelector(FOCUSABLE);
    if (first) setTimeout(() => first.focus(), 0);
    return d;
}

function closeSheet(id) {
    const d = typeof id === 'string' ? document.getElementById(id) : id;
    if (!d) return;
    if (typeof d.close === 'function' && d.open) d.close(); else d.removeAttribute('open');
}

document.addEventListener('click', e => {
    const opener = e.target.closest('[data-sheet-open]');
    if (opener) { e.preventDefault(); openSheet(opener.getAttribute('data-sheet-open'), opener); return; }
    const closer = e.target.closest('[data-sheet-close]');
    if (closer) { e.preventDefault(); closeSheet(closer.closest('dialog')); return; }
    // click on the backdrop (the dialog element itself, outside its content box)
    if (e.target.tagName === 'DIALOG' && (e.target.classList.contains('sheet') || e.target.classList.contains('dialog'))) {
        const r = e.target.getBoundingClientRect();
        const inside = e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom;
        if (!inside) closeSheet(e.target);
    }
});

document.addEventListener('close', e => {
    const d = e.target;
    if (!(d instanceof HTMLElement) || d.tagName !== 'DIALOG') return;
    const back = _openers.get(d);
    if (back && typeof back.focus === 'function' && document.contains(back)) back.focus();
}, true);

// Focus trap (native modal dialogs already make the page inert; this keeps Tab cycling inside).
document.addEventListener('keydown', e => {
    if (e.key !== 'Tab') return;
    const d = document.querySelector('dialog[open]');
    if (!d) return;
    const items = Array.from(d.querySelectorAll(FOCUSABLE)).filter(el => el.offsetParent !== null);
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
});

/* ── toast ─────────────────────────────────────────────────────────────── */
function toast(message, opts) {
    opts = opts || {};
    const box = document.getElementById('toasts');
    if (!box) return null;
    const el = document.createElement('div');
    el.className = 'toast' + (opts.type === 'error' ? ' toast-error' : opts.type === 'ok' ? ' toast-ok' : '');
    if (opts.type === 'error') el.setAttribute('role', 'alert');
    const msg = document.createElement('span');
    msg.className = 'toast-msg';
    msg.textContent = message;               // textContent: never HTML
    el.appendChild(msg);
    let timer = null;
    const remove = () => { clearTimeout(timer); el.remove(); };
    if (opts.action) {
        const b = document.createElement('button');
        b.type = 'button'; b.className = 'btn btn-sm'; b.textContent = opts.action;
        b.addEventListener('click', () => { remove(); if (opts.onAction) opts.onAction(); });
        el.appendChild(b);
    }
    box.appendChild(el);
    timer = setTimeout(remove, opts.timeout || (opts.action ? 7000 : 4000));
    return el;
}

/* ── confirm dialog (replaces window.confirm) ──────────────────────────── */
function confirmDialog(opts) {
    opts = typeof opts === 'string' ? { text: opts } : (opts || {});
    const d = document.getElementById('tfm-confirm');
    if (!d || typeof d.showModal !== 'function') return Promise.resolve(window.confirm(opts.text || opts.title || ''));
    d.querySelector('#tfm-confirm-title').textContent = opts.title || 'Підтвердження';
    d.querySelector('#tfm-confirm-text').textContent = opts.text || '';
    const ok = d.querySelector('[data-confirm-ok]');
    ok.textContent = opts.confirmText || 'Видалити';
    ok.className = 'btn ' + ((opts.danger === false || opts.tone === 'primary') ? 'btn-primary' : 'btn-danger');
    d.querySelector('[data-confirm-cancel]').textContent = opts.cancelText || 'Скасувати';
    d.returnValue = '';
    return new Promise(resolve => {
        d.addEventListener('close', () => resolve(d.returnValue === 'ok'), { once: true });
        openSheet(d);
        setTimeout(() => d.querySelector('[data-confirm-cancel]').focus(), 0);
    });
}

function _confirmOpts(el) {
    return {
        title: el.getAttribute('data-confirm-title') || 'Підтвердження',
        text: el.getAttribute('data-confirm'),
        confirmText: el.getAttribute('data-confirm-ok') || 'Видалити',
        tone: el.getAttribute('data-confirm-tone') || 'danger',
    };
}

document.addEventListener('submit', e => {
    const f = e.target;
    if (!f.hasAttribute || !f.hasAttribute('data-confirm') || f.dataset.confirmed === '1') return;
    e.preventDefault();
    const submitter = e.submitter;
    confirmDialog(_confirmOpts(f)).then(yes => {
        if (!yes) return;
        f.dataset.confirmed = '1';
        if (f.requestSubmit) f.requestSubmit(submitter || undefined); else f.submit();
        setTimeout(() => { delete f.dataset.confirmed; }, 0);
    });
}, true);

document.addEventListener('click', e => {
    const el = e.target.closest('a[data-confirm], button[data-confirm]');
    if (!el || el.dataset.confirmed === '1') return;
    e.preventDefault();
    e.stopPropagation();   // also holds back inline onclick handlers until confirmed
    confirmDialog(_confirmOpts(el)).then(yes => {
        if (!yes) return;
        if (el.tagName === 'A') { window.location.href = el.href; return; }
        if (el.form && el.type !== 'button') {
            const f = el.form;
            f.dataset.confirmed = '1';
            if (f.requestSubmit) f.requestSubmit(el); else f.submit();
            setTimeout(() => { delete f.dataset.confirmed; }, 0);
            return;
        }
        el.dataset.confirmed = '1'; el.click(); delete el.dataset.confirmed;
    });
}, true);

/* ── data-toast ────────────────────────────────────────────────────────── */
document.addEventListener('click', e => {
    const el = e.target.closest('[data-toast]');
    if (el) toast(el.getAttribute('data-toast'));
});
document.querySelectorAll('[data-toast-now]').forEach(el => toast(el.getAttribute('data-toast-now'), { type: el.getAttribute('data-toast-type') || undefined }));

/* ── attention: "Сховати до завтра" without reload ─────────────────────── */
document.addEventListener('submit', e => {
    const f = e.target;
    if (!f.matches || !f.matches('form[data-attention-dismiss]')) return;
    e.preventDefault();
    const row = f.closest('.attention-row');
    fetch(f.action, { method: 'POST', body: new FormData(f), headers: { 'Accept': 'application/json' } })
        .then(r => { if (!r.ok) throw new Error(r.status); return r.json(); })
        .then(() => {
            if (row) {
                const list = row.parentElement;
                row.classList.add('is-hiding');
                setTimeout(() => {
                    row.remove();
                    if (list && !list.querySelector('.attention-row')) {
                        const card = list.closest('.attention');
                        if (card) card.hidden = true;
                        document.querySelectorAll('[data-attention-ok]').forEach(x => { x.hidden = false; });
                    }
                }, 200);
            }
            document.querySelectorAll('a[href="/"] .nav-badge').forEach(b => {
                const n = parseInt(b.textContent, 10) - 1;
                if (n > 0) b.textContent = String(n); else b.remove();
            });
            toast('Сховано до завтра');
        })
        .catch(() => toast('Не вдалося сховати. Спробуйте ще раз', { type: 'error' }));
});

/* ── LEGACY: universal confirmation modal (#confirm-modal) ─────────────── */
function openModal(options) {
    const modal = document.getElementById('confirm-modal');
    const form = document.getElementById('modal-form');
    document.getElementById('modal-title').textContent = options.title || 'Підтвердження';
    document.getElementById('modal-name').textContent = options.name || '';
    const details = document.getElementById('modal-details');
    const warning = document.getElementById('modal-warning');
    const submitBtn = document.getElementById('modal-submit-btn');
    details.style.display = 'none';
    warning.style.display = 'none';
    if (options.details) {
        details.innerHTML = options.details;   // callers pass pre-escaped HTML (esc())
        details.style.display = 'block';
    }
    if (options.warning) {
        warning.textContent = options.warning;
        warning.style.display = 'block';
    }
    submitBtn.textContent = options.buttonText || 'Видалити';
    form.action = options.action;
    modal.classList.add('open');
    const cancel = modal.querySelector('.modal-actions .btn-outline');
    if (cancel) cancel.focus();
}

function closeModal() {
    document.getElementById('confirm-modal').classList.remove('open');
}

(function () {
    const m = document.getElementById('confirm-modal');
    if (!m) return;
    m.addEventListener('click', function (e) { if (e.target === this) closeModal(); });
    document.addEventListener('keydown', e => {
        if (e.key === 'Escape') document.querySelectorAll('.modal-overlay.open').forEach(o => o.classList.remove('open'));
    });
})();

/* Delete ingredient modal (with dependency loading) */
function openDeleteModal(ingredientId) {
    openModal({
        title: 'Видалити інгредієнт?',
        name: 'Завантаження...',
        action: '/ingredients/delete/' + ingredientId,
    });
    fetch('/api/ingredient/' + ingredientId + '/deps')
        .then(r => r.json())
        .then(d => {
            document.getElementById('modal-name').textContent = d.name;
            const all = [...d.recipes, ...d.sub_recipes.map(s => 'заготовка: ' + s)];
            if (all.length > 0) {
                document.getElementById('modal-details').innerHTML =
                    '<strong>Використовується в:</strong><br>' + all.map(n => '<span>' + esc(n) + '</span>').join(' ');
                document.getElementById('modal-details').style.display = 'block';
                document.getElementById('modal-warning').textContent =
                    'Цей інгредієнт буде видалений з усіх рецептів і заготовок!';
                document.getElementById('modal-warning').style.display = 'block';
            }
        });
}

/* Simple confirm modal for any delete action */
function confirmDelete(action, name) {
    openModal({ title: 'Видалити?', name: name || '', action: action, buttonText: 'Видалити' });
}

/* ── "Оновити дані з каси": form[action="/sync"] + #sync-btn + #sync-status ── */
if (document.getElementById('sync-btn')) {
    (function () {
        const el = document.getElementById('sync-status');
        const btn = document.getElementById('sync-btn');
        if (!el) return;
        let polling = null;
        let syncing = false;
        function show(cls, text) { el.innerHTML = '<span class="chip ' + cls + '">' + esc(text) + '</span>'; }
        function busy(on) { btn.disabled = on; btn.setAttribute('aria-busy', String(on)); }

        function check() {
            fetch('/api/sync-status').then(r => r.json()).then(d => {
                if (d.status === 'running') {
                    show('chip-info', 'Оновлюємо…');
                    busy(true);
                    if (!polling) polling = setInterval(check, 3000);
                } else if (d.status === 'done' && syncing) {
                    syncing = false;
                    show('chip-ok', 'Готово');
                    busy(false);
                    if (polling) { clearInterval(polling); polling = null; }
                    toast('Дані з каси оновлено', { type: 'ok' });
                    setTimeout(function () { window.location.reload(); }, 1500);
                } else if (d.status === 'error') {
                    syncing = false;
                    show('chip-danger', 'Не вдалося оновити');
                    busy(false);
                    if (polling) { clearInterval(polling); polling = null; }
                    if (d.message) toast('Помилка: ' + String(d.message).substring(0, 80), { type: 'error' });
                } else {
                    busy(false);
                }
            }).catch(() => {});
        }

        check();

        const form = document.querySelector('form[action="/sync"]');
        if (form) form.addEventListener('submit', function (e) {
            e.preventDefault();
            syncing = true;
            show('chip-info', 'Запускаємо…');
            busy(true);
            fetch('/sync', { method: 'POST', redirect: 'manual' }).then(() => {
                polling = setInterval(check, 3000);
            }).catch(() => { syncing = false; busy(false); show('chip-danger', 'Не вдалося запустити'); });
        });
    })();
}
