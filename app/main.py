"""Cafe Manager — FastAPI backend."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app.db import init_db, enable_wal
from app.auth import get_current_user, can_access, create_default_admin, check_session_secret
from app.routes import auth, dashboard, inventory, invoice, recipes, sub_recipes, stock_count, users, sync, expenses, invoices_gdrive, webhook, wastage

BASE_DIR = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    enable_wal()
    check_session_secret()
    init_db()
    create_default_admin()
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    scheduler = AsyncIOScheduler()

    async def scheduled_daily():
        """Daily at 21:00: full sync + report + process invoices."""
        import threading
        def run():
            from app.telegram_bot import alert_admin
            # Full API sync (overwrites webhook data — ensures nothing missed)
            try:
                from app.gopos_api import sync_today
                sync_today()
            except Exception as e:
                alert_admin('Daily GoPOS sync', e)
            # Daily report
            try:
                from app.telegram_bot import daily_report
                daily_report()
            except Exception as e:
                alert_admin('Daily report', e)
            # Sync invoices from Google Drive
            try:
                from app.services.invoice_processing import process_invoices_for_month
                from app.gopos_api import warsaw_today
                process_invoices_for_month(warsaw_today()[:7])
            except Exception as e:
                alert_admin('Invoice processing', e)
        threading.Thread(target=run, daemon=True).start()

    scheduler.add_job(scheduled_daily, CronTrigger(hour=19, minute=0))

    async def scheduled_close_yesterday():
        """After the business day ends (05:00 Warsaw): re-sync yesterday in full.
        The 19:00 UTC sync misses everything sold later that evening, which so far
        only reached `sales` via webhook and never got deducted from stock."""
        import threading
        def run():
            try:
                from datetime import datetime, timedelta
                from app.gopos_api import sync_date, warsaw_today
                yesterday = (datetime.strptime(warsaw_today(), '%Y-%m-%d')
                             - timedelta(days=1)).strftime('%Y-%m-%d')
                sync_date(yesterday)
            except Exception as e:
                from app.telegram_bot import alert_admin
                alert_admin('Morning re-sync of yesterday', e)
        threading.Thread(target=run, daemon=True).start()

    # 04:30 UTC = 05:30/06:30 Warsaw: after the 05:00 cutoff in both CET and CEST
    scheduler.add_job(scheduled_close_yesterday, CronTrigger(hour=4, minute=30))

    intraday_failing = {'on': False}

    async def scheduled_intraday():
        """Hourly while the cafe is open: refresh today's sales (no stock deduction)."""
        import threading
        def run():
            try:
                from app.gopos_api import sync_intraday
                sync_intraday()
                intraday_failing['on'] = False
            except Exception as e:
                # Tell the admin once per outage, not every hour
                if not intraday_failing['on']:
                    from app.telegram_bot import alert_admin
                    alert_admin('Intraday sales refresh', e)
                intraday_failing['on'] = True
        threading.Thread(target=run, daemon=True).start()

    # :15 never coincides with the :00/:30 jobs above; Warsaw 07:15-22:15
    scheduler.add_job(scheduled_intraday,
                      CronTrigger(hour='7-22', minute='15', timezone='Europe/Warsaw'))

    async def scheduled_monthly():
        """1st of the month: last month vs the month before, numbers + AI summary."""
        import threading
        def run():
            try:
                from app.monthly_report import send_monthly_report
                send_monthly_report()
            except Exception as e:
                from app.telegram_bot import alert_admin
                alert_admin('Monthly report', e)
        threading.Thread(target=run, daemon=True).start()

    # 06:30 UTC = 07:30/08:30 Warsaw; last month's sales are complete after the 19:00 daily sync
    scheduler.add_job(scheduled_monthly, CronTrigger(day=1, hour=6, minute=30))
    scheduler.start()

    # Sync products catalog on startup (categories, prices from API)
    import threading
    def startup_sync():
        import time
        time.sleep(10)
        try:
            from app.gopos_api import sync_products
            sync_products()
        except Exception as e:
            from app.telegram_bot import alert_admin
            alert_admin('Startup product sync', e)
    threading.Thread(target=startup_sync, daemon=True).start()

    # Interactive Telegram chat (long polling, outbound only). No-op without a bot token.
    chat_stop = threading.Event()
    from app.telegram_chat import start_polling
    start_polling(chat_stop)

    yield
    chat_stop.set()
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

for r in (auth, dashboard, inventory, invoice, recipes, sub_recipes, stock_count, users, sync, expenses, invoices_gdrive, webhook, wastage):
    app.include_router(r.router)

if __name__ == '__main__':
    import uvicorn
    uvicorn.run('main:app', host='0.0.0.0', port=8000, reload=True)
