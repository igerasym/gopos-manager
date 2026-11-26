"""Google Drive invoices scanning routes."""
import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.gdrive_invoices import list_invoices, download_file, parse_invoice_textract, update_ingredient_prices

templates = Jinja2Templates(directory=Path(__file__).parent.parent / 'templates')
log = logging.getLogger(__name__)

router = APIRouter()


@router.get('/invoices', response_class=HTMLResponse)
async def invoices_page(request: Request):
    """Show list of invoices from Google Drive."""
    try:
        files = list_invoices()
    except Exception as e:
        log.error(f"Error listing invoices: {e}")
        files = []

    return templates.TemplateResponse(request, 'invoices.html', context={
        'files': files,
        'error': str(e) if not files and 'e' in dir() else '',
    })


@router.post('/invoices/parse/{file_id}')
async def parse_single_invoice(file_id: str):
    """Parse a single invoice and return results."""
    try:
        file_bytes = download_file(file_id)
        result = parse_invoice_textract(file_bytes)
        result['file_id'] = file_id
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=500)


@router.get('/api/invoices/list')
async def api_list_invoices():
    """API: list all invoice files."""
    try:
        files = list_invoices()
        return JSONResponse({'files': files})
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=500)
