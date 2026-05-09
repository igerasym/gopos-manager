"""Google Drive invoice scanner + AWS Textract parser."""
import io
import json
import logging
import os
from pathlib import Path
from typing import Optional

import boto3
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from app.db import get_db

log = logging.getLogger(__name__)

CREDENTIALS_PATH = Path(__file__).parent.parent / 'data' / 'google-credentials.json'
FOLDER_ID = os.getenv('GDRIVE_FOLDER_ID', '1f9UJ0-BskYgC_dppr7G8bLQrECg67fH6')
SCOPES = ['https://www.googleapis.com/auth/drive.readonly']


def get_drive_service():
    """Create Google Drive API service."""
    creds = service_account.Credentials.from_service_account_file(
        str(CREDENTIALS_PATH), scopes=SCOPES
    )
    return build('drive', 'v3', credentials=creds)


MONTH_NAMES_UK = {
    '01': 'січень', '02': 'лютий', '03': 'березень',
    '04': 'квітень', '05': 'травень', '06': 'червень',
    '07': 'липень', '08': 'серпень', '09': 'вересень',
    '10': 'жовтень', '11': 'листопад', '12': 'грудень',
}


def list_invoices(folder_id: str = None) -> list[dict]:
    """List all files in the invoices folder (recursive)."""
    folder_id = folder_id or FOLDER_ID
    service = get_drive_service()
    all_files = []
    _list_recursive(service, folder_id, all_files, path='')
    return all_files


def list_invoices_for_month(month: str) -> list[dict]:
    """List invoices for a specific month (e.g. '2026-05').
    Matches folder names containing month name in Ukrainian + year.
    E.g. 'Фактури (травень 2026)' matches '2026-05'.
    """
    all_files = list_invoices()
    year = month[:4]  # '2026'
    month_num = month[5:7]  # '05'
    month_name = MONTH_NAMES_UK.get(month_num, '')

    # Strict match: month name + year
    matched = []
    for f in all_files:
        path = f.get('path', '')
        path_lower = path.lower()
        if month_name and month_name in path_lower and year in path:
            matched.append(f)

    return matched


def _list_recursive(service, folder_id, all_files, path=''):
    """Recursively list files in folder."""
    query = f"'{folder_id}' in parents and trashed = false"
    results = service.files().list(
        q=query,
        fields='files(id, name, mimeType, size, createdTime)',
        pageSize=100
    ).execute()
    files = results.get('files', [])

    for f in files:
        if f['mimeType'] == 'application/vnd.google-apps.folder':
            # Recurse into subfolders
            _list_recursive(service, f['id'], all_files, path=f"{path}/{f['name']}")
        else:
            f['path'] = f"{path}/{f['name']}" if path else f['name']
            all_files.append(f)


def download_file(file_id: str) -> bytes:
    """Download a file from Google Drive."""
    service = get_drive_service()
    request = service.files().get_media(fileId=file_id)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buffer.getvalue()


def parse_invoice_textract(file_bytes: bytes) -> dict:
    """Parse invoice using AWS Textract AnalyzeExpense."""
    client = boto3.client('textract', region_name='us-west-2')

    response = client.analyze_expense(
        Document={'Bytes': file_bytes}
    )

    result = {
        'vendor': '',
        'date': '',
        'total': 0.0,
        'items': [],
    }

    for doc in response.get('ExpenseDocuments', []):
        # Extract summary fields (vendor, date, total)
        for field in doc.get('SummaryFields', []):
            field_type = field.get('Type', {}).get('Text', '')
            value = field.get('ValueDetection', {}).get('Text', '')

            if field_type == 'VENDOR_NAME':
                result['vendor'] = value
            elif field_type == 'INVOICE_RECEIPT_DATE':
                result['date'] = value
            elif field_type == 'TOTAL':
                result['total'] = _parse_number(value)

        # Extract line items
        for group in doc.get('LineItemGroups', []):
            for item in group.get('LineItems', []):
                line = {}
                for expense_field in item.get('LineItemExpenseFields', []):
                    ft = expense_field.get('Type', {}).get('Text', '')
                    val = expense_field.get('ValueDetection', {}).get('Text', '')
                    if ft == 'ITEM':
                        line['name'] = val
                    elif ft == 'QUANTITY':
                        line['quantity'] = _parse_number(val)
                    elif ft == 'UNIT_PRICE':
                        line['unit_price'] = _parse_number(val)
                    elif ft == 'PRICE':
                        line['total'] = _parse_number(val)
                if line.get('name'):
                    result['items'].append(line)

    return result


def _parse_number(text: str) -> float:
    """Parse number from text (handles Polish format: 1 234,56)."""
    if not text:
        return 0.0
    try:
        cleaned = text.replace(' ', '').replace(',', '.').replace('zł', '').replace('PLN', '').strip()
        return float(cleaned)
    except (ValueError, TypeError):
        return 0.0


def scan_and_parse(folder_id: str = None) -> list[dict]:
    """Scan folder, download and parse all invoices."""
    files = list_invoices(folder_id)
    results = []

    # Filter to supported formats
    supported_types = [
        'application/pdf',
        'image/jpeg', 'image/png', 'image/tiff',
    ]

    for f in files:
        if f['mimeType'] not in supported_types:
            log.info(f"Skipping {f['name']} (unsupported type: {f['mimeType']})")
            continue

        try:
            log.info(f"Processing: {f['path']}")
            file_bytes = download_file(f['id'])
            parsed = parse_invoice_textract(file_bytes)
            parsed['file_name'] = f['name']
            parsed['file_path'] = f['path']
            parsed['file_id'] = f['id']
            results.append(parsed)
            log.info(f"  Vendor: {parsed['vendor']}, Items: {len(parsed['items'])}, Total: {parsed['total']}")
        except Exception as e:
            log.error(f"  Error parsing {f['name']}: {e}")
            results.append({
                'file_name': f['name'],
                'file_path': f['path'],
                'file_id': f['id'],
                'error': str(e),
            })

    return results


def update_ingredient_prices(parsed_invoices: list[dict]) -> dict:
    """Match parsed invoice items to ingredients and update prices."""
    db = get_db()
    ingredients = db.execute('SELECT id, name, unit, unit_price FROM ingredients').fetchall()

    updates = []
    unmatched = []

    for invoice in parsed_invoices:
        if 'error' in invoice:
            continue
        for item in invoice.get('items', []):
            item_name = item.get('name', '').strip()
            if not item_name:
                continue

            # Try to match with existing ingredient (case-insensitive partial match)
            matched = None
            for ing in ingredients:
                if ing['name'].lower() in item_name.lower() or item_name.lower() in ing['name'].lower():
                    matched = ing
                    break

            if matched and item.get('unit_price', 0) > 0:
                updates.append({
                    'ingredient_id': matched['id'],
                    'ingredient_name': matched['name'],
                    'invoice_item': item_name,
                    'old_price': matched['unit_price'],
                    'new_price': item['unit_price'],
                    'vendor': invoice.get('vendor', ''),
                })
            else:
                unmatched.append({
                    'name': item_name,
                    'quantity': item.get('quantity', 0),
                    'unit_price': item.get('unit_price', 0),
                    'total': item.get('total', 0),
                    'vendor': invoice.get('vendor', ''),
                })

    db.close()
    return {'updates': updates, 'unmatched': unmatched}


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    files = list_invoices()
    print(f"Found {len(files)} files:")
    for f in files:
        print(f"  {f['path']} ({f['mimeType']})")


def sync_invoices_for_current_month():
    """Auto-sync: parse all new invoices for current month, notify via Telegram."""
    import json
    from datetime import datetime

    current_month = datetime.now().strftime('%Y-%m')
    db = get_db()

    # Ensure parsed_invoices table exists
    db.execute('''CREATE TABLE IF NOT EXISTS parsed_invoices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_id TEXT NOT NULL UNIQUE,
        file_name TEXT NOT NULL,
        folder TEXT DEFAULT '',
        month TEXT DEFAULT '',
        vendor TEXT DEFAULT '',
        total REAL DEFAULT 0,
        items_json TEXT DEFAULT '[]',
        parsed_at TEXT DEFAULT (datetime('now')),
        added_to_expenses INTEGER DEFAULT 0
    )''')
    db.commit()

    # Get already parsed
    parsed_ids = set(r['file_id'] for r in db.execute('SELECT file_id FROM parsed_invoices').fetchall())

    # Get invoices for current month
    try:
        invoices = list_invoices_for_month(current_month)
    except Exception as e:
        log.error(f"Could not list invoices: {e}")
        db.close()
        return

    supported = ['application/pdf', 'image/jpeg', 'image/png', 'image/tiff']
    new_parsed = []
    errors = []

    for f in invoices:
        if f['id'] in parsed_ids:
            continue
        if f['mimeType'] not in supported:
            continue
        try:
            file_bytes = download_file(f['id'])
            result = parse_invoice_textract(file_bytes)
            db.execute('''
                INSERT OR REPLACE INTO parsed_invoices (file_id, file_name, folder, month, vendor, total, items_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (f['id'], f['name'], f.get('path', ''), current_month,
                  result.get('vendor', ''), result.get('total', 0),
                  json.dumps(result.get('items', []), ensure_ascii=False)))
            new_parsed.append({'name': f['name'], 'vendor': result.get('vendor', ''), 'total': result.get('total', 0)})
        except Exception as e:
            errors.append(f"{f['name']}: {str(e)[:80]}")

    db.commit()
    db.close()

    # Telegram notification
    if new_parsed or errors:
        try:
            from app.telegram_bot import send_message
            msg = f"📄 <b>Фактури sync ({current_month})</b>\n\n"
            if new_parsed:
                msg += f"✅ Оброблено: {len(new_parsed)}\n"
                for p in new_parsed[:10]:
                    msg += f"  • {p['name']} — {p['vendor']} ({p['total']:.0f} zł)\n"
            if errors:
                msg += f"\n❌ Помилки: {len(errors)}\n"
                for e in errors[:5]:
                    msg += f"  • {e}\n"
            send_message(msg)
        except Exception:
            pass

    log.info(f"Invoice sync done: {len(new_parsed)} new, {len(errors)} errors")
