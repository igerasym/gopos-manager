"""Cafe Manager — FastAPI backend."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app.db import init_db
from app.auth import get_current_user, can_access, create_default_admin
from app.routes import auth, dashboard, inventory, invoice, recipes, sub_recipes, stock_count, users, sync, expenses, invoices_gdrive, webhook

BASE_DIR = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    create_default_admin()
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    scheduler = AsyncIOScheduler()

    async def scheduled_daily():
        """Daily at 21:00: full sync + report + process invoices."""
        import threading
        def run():
            # Full API sync (overwrites webhook data — ensures nothing missed)
            try:
                from app.gopos_api import sync_today
                sync_today()
            except Exception:
                pass
            # Daily report
            try:
                from app.telegram_bot import daily_report
                daily_report()
            except Exception:
                pass
            # Sync invoices from Google Drive
            try:
                from app.services.invoice_processing import process_invoices_for_month
                from datetime import datetime
                process_invoices_for_month(datetime.now().strftime('%Y-%m'))
            except Exception:
                pass
        threading.Thread(target=run, daemon=True).start()

    scheduler.add_job(scheduled_daily, CronTrigger(hour=19, minute=0))
    scheduler.start()

    # Sync products catalog on startup (categories, prices from API)
    import threading
    def startup_sync():
        import time
        time.sleep(10)
        try:
            from app.gopos_api import sync_products
            sync_products()
        except Exception:
            pass
    threading.Thread(target=startup_sync, daemon=True).start()

    yield
    scheduler.shutdown()


app = FastAPI(title='The Frame Manager', lifespan=lifespan)
app.mount('/static', StaticFiles(directory=BASE_DIR / 'static'), name='static')


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in ('/login', '/logout', '/static') or path.startswith('/static/') or path.startswith('/api/webhook/'):
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

for r in (auth, dashboard, inventory, invoice, recipes, sub_recipes, stock_count, users, sync, expenses, invoices_gdrive, webhook):
    app.include_router(r.router)

if __name__ == '__main__':
    import uvicorn
    uvicorn.run('main:app', host='0.0.0.0', port=8000, reload=True)
