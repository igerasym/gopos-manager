"""Users management routes (admin only)."""
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.db import get_db
from app.auth import hash_password

templates = Jinja2Templates(directory=Path(__file__).parent.parent / 'templates')

router = APIRouter()


@router.get('/users', response_class=HTMLResponse)
async def users_page(request: Request):
    db = get_db()
    users = db.execute('SELECT id, username, role, display_name FROM users ORDER BY username').fetchall()
    db.close()
    return templates.TemplateResponse(request, 'users.html', context={'users': users})


@router.post('/users/add')
async def add_user(
    username: str = Form(...), password: str = Form(...),
    role: str = Form('staff'), display_name: str = Form(''),
):
    db = get_db()
    db.execute(
        'INSERT OR IGNORE INTO users (username, password_hash, role, display_name) VALUES (?, ?, ?, ?)',
        (username, hash_password(password), role, display_name)
    )
    db.commit()
    db.close()
    return RedirectResponse('/users', status_code=303)


@router.post('/users/role/{user_id}')
async def change_role(user_id: int, role: str = Form(...)):
    db = get_db()
    db.execute('UPDATE users SET role = ? WHERE id = ?', (role, user_id))
    db.commit()
    db.close()
    return RedirectResponse('/users', status_code=303)


@router.post('/users/password/{user_id}')
async def change_password(user_id: int, password: str = Form(...)):
    db = get_db()
    db.execute('UPDATE users SET password_hash = ? WHERE id = ?', (hash_password(password), user_id))
    db.commit()
    db.close()
    return RedirectResponse('/users', status_code=303)


@router.post('/users/delete/{user_id}')
async def delete_user(user_id: int):
    db = get_db()
    db.execute('DELETE FROM users WHERE id = ? AND username != "admin"', (user_id,))
    db.commit()
    db.close()
    return RedirectResponse('/users', status_code=303)
