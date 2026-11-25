"""Invoice parse checks: the cases found on real invoices (a dropped digit, a section total)."""
from app.invoice_validation import check_invoice, numbers_in_text, parse_number, pdf_text
from app.llm import build_invoice_prompt


def test_parse_number_formats():
    assert parse_number('2 174,10') == 2174.10
    assert parse_number('2\u00a0174,10') == 2174.10
    assert parse_number('2.435,07') == 2435.07
    assert parse_number('1581.79') == 1581.79
    assert parse_number('1,234.56') == 1234.56
    assert parse_number('abc') is None


def test_numbers_do_not_glue_neighbouring_columns():
    # "Razem 4 498,50 1 034,66 5 533,16" is net, VAT, gross: three numbers, not one
    n = numbers_in_text('Razem 4 498,50 1 034,66 5 533,16')
    assert {4498.50, 1034.66, 5533.16} <= n


TEXT = 'Razem wartość faktury 1 285,99 295,78 1 581,77\nDo zapłaty 1 581,77'


def test_dropped_digit_is_caught():
    # real case: electricity invoice of 1 581,77 stored as 581.77
    issues = check_invoice({'total': 581.77, 'items': []}, TEXT)
    assert any('does not appear' in i for i in issues)


def test_correct_total_passes():
    items = [{'total': 1000.0}, {'total': 581.77}]
    assert check_invoice({'total': 1581.77, 'items': items}, TEXT) == []


def test_negative_credit_note_total_matches_text():
    assert check_invoice({'total': -84.36}, 'Korekta razem -84,36 PLN') == []


def test_line_totals_must_add_up():
    items = [{'total': 100.0}, {'total': 100.0}]
    issues = check_invoice({'total': 400.0, 'items': items}, '')
    assert any('add up' in i for i in issues)


def test_normal_gap_from_discounts_and_deposits_is_tolerated():
    # Makro prints discounts and deposits on separate lines: 5-10 % off is normal
    items = [{'total': 1000.0}, {'total': 1000.0}]
    assert check_invoice({'total': 2174.10, 'items': items}, '') == []


def test_skipped_page_is_caught():
    items = [{'total': 1000.0}, {'total': 400.0}]
    assert any('add up' in i for i in check_invoice({'total': 2174.10, 'items': items}, ''))


def test_printed_total_must_match():
    ok = {'total': 2174.10, 'total_printed': '2 174,10', 'items': []}
    assert check_invoice(ok, '') == []
    bad = {'total': 1581.79, 'total_printed': '2 174,10', 'items': []}
    assert any('printed' in i for i in check_invoice(bad, ''))


def test_missing_total_is_flagged():
    assert check_invoice({'total': 0, 'items': []}, TEXT)
    assert check_invoice({'total': None, 'items': []}, TEXT)


def test_scan_without_text_skips_the_text_check():
    # no PDF text layer: only the self-consistency checks run
    assert check_invoice({'total': 50.0, 'items': [{'total': 50.0}]}, '') == []


def test_pdf_text_is_empty_for_non_pdf():
    assert pdf_text(b'\x89PNG....') == ''
    assert pdf_text(b'%PDF-broken') == ''


def test_prompt_includes_text_and_feedback_only_when_given():
    base = build_invoice_prompt()
    assert '<pdf_text>' not in base and 'PREVIOUS ANSWER' not in base
    full = build_invoice_prompt('Razem 1 581,77', '- total 581.77 does not appear')
    assert 'Razem 1 581,77' in full and 'PREVIOUS ANSWER FAILED' in full
    assert 'total_printed' in base and 'Kwota należności ogółem' in base


def test_line_sum_is_looser_when_pdf_text_exists():
    items = [{'total': 800.0}, {'total': 800.0}]  # 1600 vs 1581.77: fine either way
    assert check_invoice({'total': 1581.77, 'items': items}, TEXT) == []
    items = [{'total': 1000.0}, {'total': 0.0}]  # 1000 vs 1581.77 is 37 % off
    assert any('add up' in i for i in check_invoice({'total': 1581.77, 'items': items}, TEXT))
