"""'Сховати до завтра' for a row of the «Що потребує уваги» block."""
from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.services import attention

router = APIRouter()


def _safe_next(value: str) -> str:
    """Only same-site paths: '/x' yes, '//evil' / 'https://…' no."""
    if value and value.startswith('/') and not value.startswith('//') and '\\' not in value:
        return value
    return '/'


@router.post('/attention/dismiss')
async def attention_dismiss(request: Request, kind: str = Form(...), key: str = Form(''),
                            next: str = Form('/')):
    user = request.state.user
    if not user or user['role'] != 'admin':
        return JSONResponse({'ok': False, 'error': 'forbidden'}, status_code=403)
    if kind not in attention.HREF:
        return JSONResponse({'ok': False, 'error': 'unknown kind'}, status_code=400)
    until = attention.dismiss(user, kind, key)
    if 'application/json' in request.headers.get('accept', ''):
        return JSONResponse({'ok': True, 'until': until})
    return RedirectResponse(_safe_next(next), status_code=303)
