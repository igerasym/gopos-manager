"""Cafe Manager — FastAPI backend."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app.db import init_db
from app.auth import get_current_user, can_access, create_default_admin
from app.routes import auth, dashboard, inventory, invoice, recipes, sub_recipes, stock_count, users, sync

BASE_DIR = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    create_default_admin()
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    scheduler = AsyncIOScheduler()

    async def scheduled_sync():
        import threading
        def run():
            import asyncio as aio
            loop = aio.new_event_loop()
            aio.set_event_loop(loop)
            try:
                from app.gopos_sync import sync_today
                loop.run_until_complete(sync_today())
                from app.telegram_bot import daily_report
                daily_report()
            except Exception:
                pass
            finally:
                loop.close()
        threading.Thread(target=run, daemon=True).start()

    scheduler.add_job(scheduled_sync, CronTrigger(hour=21, minute=0))
    scheduler.start()
    yield
    scheduler.shutdown()


app = FastAPI(title='The Frame Manager', lifespan=lifespan)
app.mount('/static', StaticFiles(directory=BASE_DIR / 'static'), name='static')


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in ('/login', '/logout', '/static') or path.startswith('/static/'):
            return await call_next(request)
        user = get_current_user(request)
        if not user:
            return RedirectResponse('/login', status_code=302)
        if not can_access(user, path):
            if path == '/':
                return RedirectResponse('/inventory', status_code=302)
            return HTMLResponse(
                '<h2>Доступ заборонено</h2><p>Ваша роль не має доступу до цієї сторінки.</p>'
                '<a href="/inventory">← Назад</a>', status_code=403)
        request.state.user = user
        return await call_next(request)

app.add_middleware(AuthMiddleware)

for r in (auth, dashboard, inventory, invoice, recipes, sub_recipes, stock_count, users, sync):
    app.include_router(r.router)

if __name__ == '__main__':
    import uvicorn
    uvicorn.run('main:app', host='0.0.0.0', port=8000, reload=True)
