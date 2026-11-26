"""Styled error pages (404 / 403 / 500) for browser requests.

A page a person opens in the browser gets a page inside the app shell with a plain
Ukrainian message and a button back to the role's home. API calls (/api/*, or a
request that asks for JSON) keep getting JSON, exactly as before.
"""
from __future__ import annotations

import logging

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.templating import templates

log = logging.getLogger(__name__)

MESSAGES = {
    403: ('Сюди у вас немає доступу',
          'Ця сторінка доступна лише власнику. Якщо вона потрібна вам для роботи, попросіть власника.'),
    404: ('Такої сторінки немає',
          'Можливо, посилання застаріло або в адресі помилка.'),
    500: ('Щось пішло не так',
          'Ми не змогли показати цю сторінку. Спробуйте ще раз за хвилину.'),
}


def wants_html(request: Request) -> bool:
    """True for a page opened in a browser; False for API/JSON/static requests."""
    path = request.url.path
    if path.startswith('/api/') or path.startswith('/static/'):
        return False
    accept = request.headers.get('accept', '')
    if 'application/json' in accept and 'text/html' not in accept:
        return False
    return 'text/html' in accept


def home_for(user) -> str:
    return '/' if user and user.get('role') == 'admin' else '/inventory'


def error_page(request: Request, status: int) -> HTMLResponse:
    code = status if status in MESSAGES else (500 if status >= 500 else 404)
    title, text = MESSAGES[code]
    user = getattr(request.state, 'user', None)
    if user is None:
        request.state.user = None
    return templates.TemplateResponse(request, 'error.html', {
        'title': title, 'text': text, 'home': home_for(user),
        'home_label': 'На головну' if user and user.get('role') == 'admin' else 'До складу',
    }, status_code=status)


async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    if wants_html(request) and exc.status_code in (403, 404, 405, 500):
        return error_page(request, exc.status_code)
    return JSONResponse({'detail': exc.detail}, status_code=exc.status_code,
                        headers=getattr(exc, 'headers', None))


async def server_error_handler(request: Request, exc: Exception):
    log.exception('Unhandled error on %s', request.url.path)
    if wants_html(request):
        try:
            return error_page(request, 500)
        except Exception:  # the error page itself must never fail
            pass
    return JSONResponse({'detail': 'Internal Server Error'}, status_code=500)


def install(app) -> None:
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, server_error_handler)
