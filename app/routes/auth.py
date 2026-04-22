"""Auth routes — login / logout."""
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from app.db import get_db
from app.auth import get_current_user, verify_password, sign_cookie

templates = Jinja2Templates(directory=Path(__file__).parent.parent / 'templates')

router = APIRouter()


@router.get('/login', response_class=HTMLResponse)
async def login_page(request: Request):
    user = get_current_user(request)
    if user:
        return RedirectResponse('/', status_code=302)
    return templates.TemplateResponse(request, 'login.html', context={'error': None})


@router.post('/login')
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    db = get_db()
    user = db.execute('SELECT username, password_hash FROM users WHERE username = ?', (username,)).fetchone()
    db.close()

    if not user or not verify_password(password, user['password_hash']):
        return templates.TemplateResponse(request, 'login.html', context={'error': 'Невірний логін або пароль'})

    response = RedirectResponse('/', status_code=302)
    response.set_cookie('session', sign_cookie(username), httponly=True, max_age=86400 * 30)
    return response


@router.get('/logout')
async def logout():
    response = RedirectResponse('/login', status_code=302)
    response.delete_cookie('session')
    return response
