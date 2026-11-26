"""Sync trigger and status routes."""
from datetime import datetime

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
        try:
            from app.gopos_api import sync_date, sync_range, sync_today
            if date_from and date_to:
                sync_range(date_from, date_to)
            elif date_from:
                sync_date(date_from)
            else:
                sync_today()
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f'Sync failed: {e}')

    threading.Thread(target=run_sync, daemon=True).start()
    return JSONResponse({'status': 'started'})


def _refreshed() -> dict:
    """When sales were last refreshed, in Warsaw time, from the same source as the bot and the
    attention block (hourly refresh in RAM, else the last finished sync). 'short' is the label
    shown next to the sync button: 'оновлено о 14:15' / 'оновлено 25 лист. о 04:30'."""
    from app.services.attention import data_freshness, shortdate
    f = data_freshness()
    at = f['at']
    if at is None:
        return {'refreshed_at': None, 'refreshed_short': 'ще не оновлювалось'}
    when = f"о {at.strftime('%H:%M')}"
    if at.date() != datetime.now(at.tzinfo).date():
        when = f"{shortdate(at.date(), weekday=False)} {when}"
    return {'refreshed_at': at.isoformat(), 'refreshed_short': f'оновлено {when}'}


@router.get('/api/sync-status')
async def sync_status():
    db = get_db()
    row = db.execute(
        'SELECT status, message, started_at, finished_at FROM sync_log ORDER BY id DESC LIMIT 1'
    ).fetchone()
    db.close()
    fresh = _refreshed()
    if not row:
        return JSONResponse({'status': 'none', **fresh})
    return JSONResponse({
        'status': row['status'],
        'message': row['message'],
        'started_at': row['started_at'],
        'finished_at': row['finished_at'],
        **fresh,
    })
