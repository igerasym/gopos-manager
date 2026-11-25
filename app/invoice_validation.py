"""Cross-checks for LLM-parsed invoices.

The vision model reads digits off a picture and sometimes drops or swaps one
(a 1 581,77 zł invoice stored as 581,77). The PDF's own text layer holds the exact
digits, so we check what the model returned against it, and against itself
(line totals must add up to the invoice total).
"""
import io
import logging
import re

log = logging.getLogger(__name__)

# 1 234,56 / 1 234.56 (space or NBSP thousands) first, then plain 1234,56 / 2.435,07
_NUM_RE = re.compile(r'\d{1,3}(?:[ \u00a0]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)*')

MAX_TEXT_CHARS = 20000


def pdf_text(file_bytes: bytes) -> str:
    """Text layer of a PDF, '' for scans, photos and anything unreadable."""
    if file_bytes[:4] != b'%PDF':
        return ''
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            text = '\n'.join((p.extract_text() or '') for p in pdf.pages[:8])
    except Exception as e:
        log.warning(f'PDF text extraction failed: {e}')
        return ''
    return text[:MAX_TEXT_CHARS] if len(text.strip()) >= 40 else ''


def parse_number(token: str):
    """'2 174,10' / '2.435,07' / '1581.79' -> float, None if it is not a number."""
    s = re.sub(r'\s+', '', str(token)).replace('zł', '').replace('PLN', '')
    if not s:
        return None
    try:
        if ',' in s and '.' in s:
            s = s.replace(',', '') if s.rfind('.') > s.rfind(',') else s.replace('.', '').replace(',', '.')
        elif ',' in s:
            one_comma = s.count(',') == 1 and len(s.split(',')[-1]) <= 3
            s = s.replace(',', '.') if one_comma else s.replace(',', '')
        elif s.count('.') > 1:
            s = s.replace('.', '')
        return round(float(s), 2)
    except ValueError:
        return None


def numbers_in_text(text: str) -> set:
    out = set()
    for m in _NUM_RE.finditer(text or ''):
        n = parse_number(m.group())
        if n is not None:
            out.add(n)
    return out


def _has(numbers: set, value: float) -> bool:
    return any(abs(value - n) < 0.015 for n in numbers)


def check_invoice(result: dict, text: str = '') -> list:
    """Problems found in a parsed invoice, [] when it looks right.

    Checks the total against the PDF text, the total as printed against the total,
    and the sum of the line totals against the total. The last one is deliberately
    loose (25 % with a PDF text layer, 15 % for scans): on real invoices the lines differ
    from the total by 5-10 % because of deposits, VAT rounding and Makro's discount lines,
    so it only catches gross misses such as a skipped page. The PDF-text check is the
    precise one.
    """
    issues = []
    try:
        total = float(result.get('total') or 0)
    except (TypeError, ValueError):
        return ['total is not a number']
    if total == 0:
        issues.append('total is missing or zero')
        return issues

    if text:
        numbers = numbers_in_text(text)
        if not _has(numbers, abs(total)):
            issues.append(f'total {total:.2f} does not appear in the PDF text')

    printed = result.get('total_printed')
    if printed:
        p = parse_number(printed)
        if p is None or abs(abs(p) - abs(total)) > 0.015:
            issues.append(f'total {total:.2f} differs from the printed total "{printed}"')

    line_totals = []
    for it in result.get('items') or []:
        try:
            line_totals.append(float(it.get('total') or 0))
        except (TypeError, ValueError):
            continue
    if line_totals and any(line_totals):
        s = sum(line_totals)
        # with the PDF text the exact-digit check above already ran, so be looser; scans only have this
        slack = max(2.0, (0.25 if text else 0.15) * abs(total))
        if abs(s - total) > slack:
            issues.append(f'line totals add up to {s:.2f}, invoice total is {total:.2f}')
    return issues
