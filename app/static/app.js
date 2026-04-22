/* Delete confirmation modal */
function openDeleteModal(ingredientId) {
    const modal = document.getElementById('delete-modal');
    const form = document.getElementById('modal-form');
    const nameEl = document.getElementById('modal-name');
    const depsEl = document.getElementById('modal-deps');
    const warnEl = document.getElementById('modal-warning');

    form.action = '/ingredients/delete/' + ingredientId;
    nameEl.textContent = 'Завантаження...';
    depsEl.style.display = 'none';
    warnEl.style.display = 'none';
    modal.classList.add('open');

    fetch('/api/ingredient/' + ingredientId + '/deps')
        .then(r => r.json())
        .then(d => {
            nameEl.textContent = d.name;
            const all = [...d.recipes, ...d.sub_recipes.map(s => '🔧 ' + s)];
            if (all.length > 0) {
                depsEl.innerHTML = '<strong>Використовується в:</strong><br>' + all.map(n => '<span>' + n + '</span>').join(' ');
                depsEl.style.display = 'block';
                warnEl.style.display = 'block';
            }
        });
}

function closeDeleteModal() {
    document.getElementById('delete-modal').classList.remove('open');
}

document.getElementById('delete-modal').addEventListener('click', function(e) {
    if (e.target === this) closeDeleteModal();
});

/* Sync polling (admin only — checks for #sync-btn existence instead of Jinja2) */
if (document.getElementById('sync-btn')) {
    (function() {
        const el = document.getElementById('sync-status');
        const btn = document.getElementById('sync-btn');
        if (!el) return;
        let polling = null;
        let syncing = false;

        function check() {
            fetch('/api/sync-status').then(r => r.json()).then(d => {
                if (d.status === 'running') {
                    el.innerHTML = '<span style="color:#fbbf24;">⏳ Синхронізація...</span>';
                    btn.disabled = true;
                    btn.style.opacity = '.5';
                    if (!polling) polling = setInterval(check, 3000);
                } else if (d.status === 'done' && syncing) {
                    syncing = false;
                    el.innerHTML = '<span style="color:#34d399;">✓ ' + d.message + '</span>';
                    btn.disabled = false;
                    btn.style.opacity = '1';
                    if (polling) { clearInterval(polling); polling = null; }
                    window.location.reload();
                } else if (d.status === 'done' && !syncing) {
                    if (d.finished_at) {
                        el.innerHTML = '<span style="color:rgba(255,255,255,.4); font-size:.75em;">Останній синк: ' + d.finished_at.substring(5, 16) + '</span>';
                    }
                } else if (d.status === 'error') {
                    syncing = false;
                    el.innerHTML = '<span style="color:#f87171;">✗ ' + d.message.substring(0, 60) + '</span>';
                    btn.disabled = false;
                    btn.style.opacity = '1';
                    if (polling) { clearInterval(polling); polling = null; }
                } else {
                    btn.disabled = false;
                    btn.style.opacity = '1';
                }
            }).catch(() => {});
        }

        // Check last sync on page load
        check();

        document.querySelector('form[action="/sync"]').addEventListener('submit', function(e) {
            e.preventDefault();
            syncing = true;
            el.innerHTML = '<span style="color:#fbbf24;">⏳ Запуск синку...</span>';
            btn.disabled = true;
            btn.style.opacity = '.5';
            fetch('/sync', { method: 'POST', redirect: 'manual' }).then(() => {
                polling = setInterval(check, 3000);
            });
        });
    })();
}
