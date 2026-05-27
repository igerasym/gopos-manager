"""GoPos webhook endpoint — real-time order sync."""
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.db import get_db

log = logging.getLogger(__name__)

router = APIRouter()


@router.post('/api/webhook/gopos')
async def gopos_webhook(request: Request):
    """Handle GoPos webhook notifications.

    GoPos sends: {occurred_at, type, organization_id, resource_id, event_type}
    We care about ORDER type with CLOSED status.
    """
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({'error': 'invalid json'}, status_code=400)

    event_type = payload.get('event_type', '')
    resource_type = payload.get('type', '')
    resource_id = payload.get('resource_id', '')

    log.info(f"GoPos webhook: {resource_type}/{event_type} id={resource_id}")

    # Process order events
    if resource_type == 'ORDER':
        import threading
        threading.Thread(
            target=_process_order_webhook,
            args=(resource_id,),
            daemon=True
        ).start()

    # Process item/category changes (product catalog updates)
    elif resource_type in ('ITEM', 'CATEGORY'):
        import threading
        threading.Thread(target=_sync_products_background, daemon=True).start()

    return JSONResponse({'status': 'ok'})


def _process_order_webhook(order_id: str):
    """Fetch order from API and update sales if closed."""
    try:
        from app.gopos_api import api_get

        # Fetch order with items
        data = api_get(f'orders/{order_id}', {'include': 'items,items.product'})
        order = data.get('data', data) if isinstance(data, dict) else data

        status = order.get('status', '')
        if status != 'CLOSED':
            return  # Only process closed orders

        # Determine date from closed_at
        closed_at = order.get('closed_at', '')
        if not closed_at:
            return
        # GoPos business day: if closed before 05:00, it belongs to previous day
        from datetime import datetime
        dt = datetime.fromisoformat(closed_at)
        if dt.hour < 5:
            from datetime import timedelta
            dt = dt - timedelta(days=1)
        date_str = dt.strftime('%Y-%m-%d')

        # Extract items and update sales
        db = get_db()
        for item in order.get('items', []):
            if item.get('status') != 'ACTIVE':
                continue
            name = item.get('name')
            if not name:
                continue

            qty = item.get('quantity', 1)
            total = item.get('total_price', {}).get('amount', 0)
            sub_total = item.get('sub_total_price', {}).get('amount', 0)
            discount = total - sub_total if total > sub_total else 0

            # Upsert: add to existing sales for this date+product
            db.execute('''
                INSERT INTO sales (date, product_name, quantity, total_money, net_total, discount, net_profit)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date, product_name) DO UPDATE SET
                    quantity = quantity + excluded.quantity,
                    total_money = total_money + excluded.total_money,
                    net_total = net_total + excluded.net_total,
                    discount = discount + excluded.discount,
                    net_profit = net_profit + excluded.net_profit
            ''', (date_str, name, qty, total, sub_total, discount, sub_total))

        db.commit()
        db.close()
        log.info(f"Webhook: processed order {order_id} for {date_str}")

    except Exception as e:
        log.error(f"Webhook order processing failed: {e}")


def _sync_products_background():
    """Sync products catalog when items/categories change."""
    try:
        from app.gopos_api import sync_products
        sync_products()
        log.info("Webhook: products synced after catalog change")
    except Exception as e:
        log.error(f"Webhook products sync failed: {e}")
