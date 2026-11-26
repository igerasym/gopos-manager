"""Google Drive invoice scanner + AWS Textract parser."""
import io
import logging
import os
from pathlib import Path

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
    """Parse invoice using AWS Textract AnalyzeExpense.
    Handles multi-page PDFs by converting to images first.
    """
    client = boto3.client('textract', region_name='us-west-2')

    # Try direct parsing first (works for single-page and some PDFs)
    try:
        response = client.analyze_expense(Document={'Bytes': file_bytes})
        return _extract_textract_result(response)
    except client.exceptions.UnsupportedDocumentException:
        pass

    # Fallback: convert PDF pages to images and parse each
    try:
        import pdfplumber
        pdf = pdfplumber.open(io.BytesIO(file_bytes))
        all_items = []
        vendor = ''
        date = ''
        total = 0.0

        for page in pdf.pages[:5]:  # Max 5 pages
            img = page.to_image(resolution=200)
            img_buffer = io.BytesIO()
            img.original.save(img_buffer, format='PNG')
            img_bytes = img_buffer.getvalue()

            try:
                response = client.analyze_expense(Document={'Bytes': img_bytes})
                page_result = _extract_textract_result(response)
                if page_result['vendor'] and not vendor:
                    vendor = page_result['vendor']
                if page_result['date'] and not date:
                    date = page_result['date']
                if page_result['total'] > total:
                    total = page_result['total']
                all_items.extend(page_result['items'])
            except Exception:
                continue

        pdf.close()
        return {'vendor': vendor, 'date': date, 'total': total, 'items': all_items}
    except Exception as e:
        raise Exception(f"Could not parse PDF: {e}")


def _extract_textract_result(response: dict) -> dict:
    """Extract structured data from Textract AnalyzeExpense response."""
    result = {
        'vendor': '',
        'date': '',
        'total': 0.0,
        'invoice_number': '',
        'items': [],
    }

    for doc in response.get('ExpenseDocuments', []):
        for field in doc.get('SummaryFields', []):
            field_type = field.get('Type', {}).get('Text', '')
            value = field.get('ValueDetection', {}).get('Text', '')

            if field_type == 'VENDOR_NAME':
                result['vendor'] = value
            elif field_type == 'INVOICE_RECEIPT_DATE':
                result['date'] = value
            elif field_type in ('TOTAL', 'AMOUNT_PAID', 'AMOUNT_DUE', 'SUBTOTAL'):
                v = _parse_number(value)
                if v > result['total']:
                    result['total'] = v
            elif field_type == 'INVOICE_RECEIPT_ID':
                result['invoice_number'] = value

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

    # Fallback: if no total found, sum from line items
    if result['total'] == 0 and result['items']:
        line_totals = sum((it.get('total', 0) or 0) for it in result['items'])
        qty_price = sum((it.get('quantity', 0) or 0) * (it.get('unit_price', 0) or 0) for it in result['items'])
        # PRICE field is usually brutto on each line, prefer that.
        # qty*unit_price can mix netto/brutto, use only if line_totals missing
        if line_totals > 0:
            result['total'] = line_totals
        else:
            result['total'] = qty_price

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


# Default vendor rules (used if DB table is empty or as fallback)
DEFAULT_VENDOR_RULES = [
    ('makro', 'Продукти', 'Закупка Makro'),
    ('foundation', 'Продукти', 'Кава Foundation'),
    ('coffee plant', 'Продукти', 'Кава Coffee Plant'),
    ('coffeeplant', 'Продукти', 'Кава Coffee Plant'),
    ('fresh black', 'Продукти', 'Кава Fresh Black'),
    ('coffeedesk', 'Продукти', 'Закуп Coffeedesk'),
    ('coffee desk', 'Продукти', 'Закуп Coffeedesk'),
    ('ferment', 'Продукти', 'Випічка Ferment'),
    ('bakers house', 'Продукти', 'Випічка Bakers House'),
    ('merchant', 'Продукти', 'Лимонади Merchant'),
    ('mr.pops', 'Продукти', 'Mr Pops морозиво'),
    ('mr pops', 'Продукти', 'Mr Pops морозиво'),
    ('natural rascal', 'Продукти', 'Natural Rascal'),
    ('matcha bros', 'Продукти', 'Matcha Bros'),
    ('pge', 'Комунальні', 'Електрика'),
    ('energa', 'Комунальні', 'Електрика'),
    ('tauron', 'Комунальні', 'Електрика дистрибуція'),
    ('play', 'Побут', 'Інтернет Play'),
    ('santander', 'Побут', 'Santander'),
    ('allegro', 'Побут', 'Allegro'),
    ('hulvanska', 'Зарплати', 'Зарплати'),
]


def classify_vendor(vendor: str, file_name: str = '') -> tuple[str, str]:
    """Classify vendor into category and expense name using rules.
    Returns (category, expense_name).
    """
    search_text = f"{vendor} {file_name}".lower()

    # Try DB rules first
    try:
        db = get_db()
        rules = db.execute('SELECT vendor_pattern, category, expense_name FROM vendor_rules').fetchall()
        db.close()
        for r in rules:
            if r['vendor_pattern'].lower() in search_text:
                return r['category'], r['expense_name']
    except Exception as e:  # custom rules are optional; the defaults below still apply
        log.warning(f'vendor_rules lookup failed: {type(e).__name__}')

    # Fallback to default rules
    for pattern, category, name in DEFAULT_VENDOR_RULES:
        if pattern in search_text:
            return category, name

    return 'Інше', vendor or file_name


def check_price_alerts(ingredient_id: int, new_price: float, old_price: float) -> str | None:
    """Check if price change exceeds threshold (10%). Returns alert message or None."""
    if old_price <= 0 or new_price <= 0:
        return None
    change_pct = ((new_price - old_price) / old_price) * 100
    if abs(change_pct) >= 10:
        db = get_db()
        ing = db.execute('SELECT name, unit FROM ingredients WHERE id = ?', (ingredient_id,)).fetchone()
        db.close()
        if ing:
            direction = '📈' if change_pct > 0 else '📉'
            from html import escape  # Telegram parse_mode=HTML rejects a raw '&' or '<'
            return (f"{direction} <b>{escape(ing['name'])}</b>: {old_price:.2f} → {new_price:.2f} "
                    f"zł/{escape(ing['unit'])} ({change_pct:+.0f}%)")
    return None


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    files = list_invoices()
    print(f"Found {len(files)} files:")
    for f in files:
        print(f"  {f['path']} ({f['mimeType']})")
