"""'Ще' hub (/more) and the attention dismiss endpoint."""
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.db import get_db
from app.services import attention
from app.templating import templates

router = APIRouter()


def _last_sync():
    """Last sync_log row as {'status', 'at' (Warsaw datetime or None), 'message'} or None."""
    db = get_db()
    try:
        row = db.execute('SELECT status, message, started_at, finished_at FROM sync_log '
                         'ORDER BY id DESC LIMIT 1').fetchone()
    except Exception:
        row = None
    finally:
        db.close()
    if not row:
        return None
    at = attention._local_to_warsaw(row['finished_at'] or row['started_at'] or '')
    return {'status': row['status'], 'at': at, 'message': row['message'] or ''}


@router.get('/more', response_class=HTMLResponse)
async def more_page(request: Request):
    return templates.TemplateResponse(request, 'more.html', context={
        'last_sync': _last_sync(),
        'exports': [
            ('Продажі', '/export/sales.csv'),
            ('Витрати', '/export/expenses.csv'),
            ('Списання', '/export/wastage.csv'),
        ],
    })


def _safe_next(value: str) -> str:
    """Only same-site paths: '/x' yes, '//evil' / 'https://…' no."""
    if value and value.startswith('/') and not value.startswith('//') and '\\' not in value:
        return value
    return '/'


@router.post('/attention/dismiss')
async def attention_dismiss(request: Request, kind: str = Form(...), key: str = Form(''),
                            next: str = Form('/')):
    user = request.state.user
    if kind not in attention.HREF:
        return JSONResponse({'ok': False, 'error': 'unknown kind'}, status_code=400)
    until = attention.dismiss(user, kind, key)
    wants_json = 'application/json' in request.headers.get('accept', '') or \
        request.headers.get('x-requested-with') == 'fetch'
    if wants_json:
        return JSONResponse({'ok': True, 'until': until})
    return RedirectResponse(_safe_next(next), status_code=303)

