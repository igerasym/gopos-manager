"""GoPos webhook endpoint — real-time order sync.

Security: the URL is public (GoPOS calls it), so it carries a secret token:
    https://<host>/api/webhook/gopos?token=<GOPOS_WEBHOOK_TOKEN>
If GOPOS_WEBHOOK_TOKEN is unset the endpoint still works (legacy registration)
but logs a warning on every call.

Idempotency: each order id is recorded in processed_orders together with its
sales increment, in one transaction, so a GoPOS retry or a replayed request
can't count the same order twice. The full daily sync rewrites both tables.
"""
import hmac
import logging
import os
import re
import threading
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.db import get_db

log = logging.getLogger(__name__)

router = APIRouter()

_ORDER_ID = re.compile(r'^\d{1,20}$')
_PRODUCT_SYNC_LOCK = threading.Lock()
_PRODUCT_SYNC_MIN_INTERVAL = 300  # s; a menu edit fires many ITEM events
_last_product_sync = 0.0


def _token_ok(request: Request) -> bool:
    expected = os.getenv('GOPOS_WEBHOOK_TOKEN', '')
    if not expected:
        log.warning('GOPOS_WEBHOOK_TOKEN not set: webhook accepted without auth')
        return True
    given = request.query_params.get('token', '')
    return hmac.compare_digest(given.encode(), expected.encode())


@router.post('/api/webhook/gopos')
async def gopos_webhook(request: Request):
    """Handle GoPos webhook notifications.

    GoPos sends: {occurred_at, type, organization_id, resource_id, event_type}
    We care about ORDER type with CLOSED status.
    """
    if not _token_ok(request):
        return JSONResponse({'error': 'forbidden'}, status_code=403)

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({'error': 'invalid json'}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({'error': 'invalid payload'}, status_code=400)

    event_type = str(payload.get('event_type', ''))[:40]
    resource_type = str(payload.get('type', ''))[:40]
    resource_id = str(payload.get('resource_id', ''))

    log.info(f"GoPos webhook: {resource_type}/{event_type} id={resource_id[:20]}")

    if resource_type == 'ORDER':
        # resource_id goes into a GoPOS API path: digits only
        if not _ORDER_ID.match(resource_id):
            return JSONResponse({'error': 'invalid resource_id'}, status_code=400)
        threading.Thread(target=_process_order_webhook, args=(resource_id,), daemon=True).start()

    elif resource_type in ('ITEM', 'CATEGORY'):
        threading.Thread(target=_sync_products_background, daemon=True).start()

    return JSONResponse({'status': 'ok'})


def _process_order_webhook(order_id: str):
    """Fetch order from API and add it to sales once, if closed."""
    try:
        from app.gopos_api import api_get, business_date, get_item_tax_rates, order_item_amounts

        data = api_get(f'orders/{order_id}', {'include': 'items,items.product'})
        order = data.get('data', data) if isinstance(data, dict) else data

        if order.get('status', '') != 'CLOSED':
            return
        closed_at = order.get('closed_at', '')
        if not closed_at:
            return
        date_str = business_date(closed_at)

        tax_rates = get_item_tax_rates()
        rows = [(item, a) for item, a in order_item_amounts(order, tax_rates) if item.get('name')]

        db = get_db()
        try:
            db.execute('BEGIN IMMEDIATE')
            cur = db.execute('INSERT OR IGNORE INTO processed_orders (order_id, date) VALUES (?, ?)',
                             (order_id, date_str))
            if cur.rowcount == 0:
                db.rollback()
                log.info(f'Webhook: order {order_id} already counted, skipped')
                return
            for item, a in rows:
                db.execute('''
                    INSERT INTO sales (date, product_name, quantity, total_money, net_total, discount, net_profit)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(date, product_name) DO UPDATE SET
                        quantity = quantity + excluded.quantity,
                        total_money = total_money + excluded.total_money,
                        net_total = net_total + excluded.net_total,
                        discount = discount + excluded.discount,
                        net_profit = net_profit + excluded.net_profit
                ''', (date_str, item['name'], item.get('quantity', 1), a['total_money'],
                      a['net_total'], a['discount'], a['net_profit']))
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        log.info(f"Webhook: processed order {order_id} for {date_str}")

    except Exception as e:
        log.error(f"Webhook order processing failed: {e}")


def _sync_products_background():
    """Sync products catalog when items/categories change — at most one at a time
    and at most once per _PRODUCT_SYNC_MIN_INTERVAL."""
    global _last_product_sync
    if not _PRODUCT_SYNC_LOCK.acquire(blocking=False):
        return
    try:
        if time.monotonic() - _last_product_sync < _PRODUCT_SYNC_MIN_INTERVAL:
            return
        _last_product_sync = time.monotonic()
        from app.gopos_api import sync_products
        sync_products()
        log.info("Webhook: products synced after catalog change")
    except Exception as e:
        log.error(f"Webhook products sync failed: {e}")
    finally:
        _PRODUCT_SYNC_LOCK.release()
