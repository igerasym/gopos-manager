/* Universal confirmation modal */
function openModal(options) {
    const modal = document.getElementById('confirm-modal');
    const form = document.getElementById('modal-form');
    document.getElementById('modal-title').textContent = options.title || '⚠️ Підтвердження';
    document.getElementById('modal-name').textContent = options.name || '';
    const details = document.getElementById('modal-details');
    const warning = document.getElementById('modal-warning');
    const submitBtn = document.getElementById('modal-submit-btn');
    details.style.display = 'none';
    warning.style.display = 'none';
    if (options.details) {
        details.innerHTML = options.details;
        details.style.display = 'block';
    }
    if (options.warning) {
        warning.textContent = options.warning;
        warning.style.display = 'block';
    }
    submitBtn.textContent = options.buttonText || 'Видалити';
    form.action = options.action;
    modal.classList.add('open');
}

function closeModal() {
    document.getElementById('confirm-modal').classList.remove('open');
}

document.getElementById('confirm-modal').addEventListener('click', function(e) {
    if (e.target === this) closeModal();
});

/* Delete ingredient modal (with dependency loading) */
function openDeleteModal(ingredientId) {
    openModal({
        title: '⚠️ Видалити інгредієнт?',
        name: 'Завантаження...',
        action: '/ingredients/delete/' + ingredientId,
    });
    fetch('/api/ingredient/' + ingredientId + '/deps')
        .then(r => r.json())
        .then(d => {
            document.getElementById('modal-name').textContent = d.name;
            const all = [...d.recipes, ...d.sub_recipes.map(s => '🔧 ' + s)];
            if (all.length > 0) {
                document.getElementById('modal-details').innerHTML =
                    '<strong>Використовується в:</strong><br>' + all.map(n => '<span>' + n + '</span>').join(' ');
                document.getElementById('modal-details').style.display = 'block';
                document.getElementById('modal-warning').textContent =
                    'Цей інгредієнт буде видалений з усіх рецептів і напівфабрикатів!';
                document.getElementById('modal-warning').style.display = 'block';
            }
        });
}

/* Simple confirm modal for any delete action */
function confirmDelete(action, name) {
    openModal({
        title: '⚠️ Видалити?',
        name: name || '',
        action: action,
        buttonText: 'Видалити',
    });
}

/* Sync polling (admin only) */
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
