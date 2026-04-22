"""Sync trigger and status routes."""
from fastapi import APIRouter, Form
from fastapi.responses import JSONResponse

from app.db import get_db

router = APIRouter()


@router.post('/sync')
async def trigger_sync(
    date_from: str = Form(''), date_to: str = Form(''),
):
    import threading

    def run_sync():
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            if date_from and date_to:
                from app.gopos_sync import sync_range
                loop.run_until_complete(sync_range(date_from, date_to))
            elif date_from:
                from app.gopos_sync import sync_date
                loop.run_until_complete(sync_date(date_from))
            else:
                from app.gopos_sync import sync_today
                loop.run_until_complete(sync_today())
        finally:
            loop.close()

    threading.Thread(target=run_sync, daemon=True).start()
    return JSONResponse({'status': 'started'})


@router.get('/api/sync-status')
async def sync_status():
    db = get_db()
    row = db.execute(
        'SELECT status, message, started_at, finished_at FROM sync_log ORDER BY id DESC LIMIT 1'
    ).fetchone()
    db.close()
    if not row:
        return JSONResponse({'status': 'none'})
    return JSONResponse({
        'status': row['status'],
        'message': row['message'],
        'started_at': row['started_at'],
        'finished_at': row['finished_at'],
    })
