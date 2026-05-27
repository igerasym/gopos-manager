"""Invoice processing service — parse, classify, map, auto-approve."""
import hashlib
import json
import logging

from app.db import get_db

log = logging.getLogger(__name__)


def process_invoices_for_month(current_month: str):
    """Process all pending invoices for a given month.

    Flow: Download from Drive → Claude vision parse → classify → map items → auto-approve.
    """
    from app.gdrive_invoices import list_invoices_for_month, download_file, parse_invoice_textract, classify_vendor
    from app.llm import parse_invoice_with_vision, classify_invoice, map_items_to_ingredients

    db = get_db()

    # Load known state for deduplication
    fully_parsed_ids = set(
        r['file_id'] for r in db.execute("SELECT file_id FROM parsed_invoices WHERE parse_status = 'parsed'").fetchall()
    )
    all_known = {r['file_id']: r['id']
                 for r in db.execute('SELECT file_id, id FROM parsed_invoices').fetchall()}
    known_hashes = set(
        r['file_hash'] for r in db.execute("SELECT file_hash FROM parsed_invoices WHERE file_hash != ''").fetchall()
    )
    known_inv_numbers = set(
        r['invoice_number'] for r in db.execute(
            "SELECT invoice_number FROM parsed_invoices WHERE invoice_number != ''"
        ).fetchall()
    )
    known_amounts = set(
        (round(r['total'], 2), r['month'])
        for r in db.execute(
            "SELECT total, month FROM parsed_invoices WHERE total > 0 AND parse_status = 'parsed'"
        ).fetchall()
    )

    try:
        invoices = list_invoices_for_month(current_month)
    except Exception:
        db.close()
        return

    supported = ['application/pdf', 'image/jpeg', 'image/png', 'image/tiff']
    new_parsed = []
    auto_approved = []
    errors = []

    for f in invoices:
        if f['id'] in fully_parsed_ids or f['mimeType'] not in supported:
            continue
        try:
            _process_single_invoice(
                db, f, current_month, all_known, known_hashes,
                known_inv_numbers, known_amounts, new_parsed, auto_approved
            )
        except Exception as e:
            errors.append(f"{f['name']}: {str(e)[:80]}")
            if f['id'] in all_known:
                db.execute("UPDATE parsed_invoices SET parse_status = 'error' WHERE id = ?", (all_known[f['id']],))
                db.commit()

    db.close()
    _send_summary_notification(current_month, new_parsed, auto_approved, errors)


def _process_single_invoice(db, f, current_month, all_known, known_hashes,
                            known_inv_numbers, known_amounts, new_parsed, auto_approved):
    """Process a single invoice file."""
    from app.gdrive_invoices import download_file, parse_invoice_textract, classify_vendor
    from app.llm import parse_invoice_with_vision, classify_invoice, map_items_to_ingredients

    # Mark as processing
    if f['id'] in all_known:
        db.execute("UPDATE parsed_invoices SET parse_status = 'processing' WHERE id = ?",
                   (all_known[f['id']],))
        db.commit()

    file_bytes = download_file(f['id'])
    file_hash = hashlib.md5(file_bytes).hexdigest()

    # Dedup by hash
    if file_hash in known_hashes:
        log.info(f"Skipping duplicate by hash: {f['name']}")
        if f['id'] in all_known:
            db.execute("UPDATE parsed_invoices SET parse_status = 'duplicate' WHERE id = ?",
                       (all_known[f['id']],))
            db.commit()
        return

    # Parse with Claude vision, fallback to Textract
    result = None
    try:
        result = parse_invoice_with_vision(file_bytes, f['name'])
        log.info(f"Parsed with Claude vision: {f['name']}")
    except Exception as vision_err:
        log.warning(f"Claude vision failed for {f['name']}, fallback to Textract: {vision_err}")
        try:
            result = parse_invoice_textract(file_bytes)
        except Exception as textract_err:
            raise Exception(f"Both vision and Textract failed: {textract_err}")

    # Dedup by invoice number
    inv_num = result.get('invoice_number', '')
    if inv_num and inv_num in known_inv_numbers:
        log.info(f"Skipping duplicate by invoice number {inv_num}: {f['name']}")
        if f['id'] in all_known:
            db.execute("UPDATE parsed_invoices SET parse_status = 'duplicate' WHERE id = ?",
                       (all_known[f['id']],))
            db.commit()
        return

    # Dedup by amount + month
    invoice_total = result.get('total', 0)
    if invoice_total > 0 and (round(invoice_total, 2), current_month) in known_amounts:
        log.info(f"Skipping fuzzy duplicate by amount {invoice_total}: {f['name']}")
        if f['id'] in all_known:
            db.execute("UPDATE parsed_invoices SET parse_status = 'duplicate' WHERE id = ?",
                       (all_known[f['id']],))
            db.commit()
        return

    # Classify vendor
    category, expense_name = classify_vendor(result.get('vendor', ''), f['name'])

    # Save/update parsed invoice
    invoice_db_id = all_known.get(f['id'])
    if invoice_db_id:
        db.execute('''
            UPDATE parsed_invoices SET
                file_hash = ?, invoice_number = ?, vendor = ?, category = ?, total = ?,
                items_json = ?, parse_status = 'parsed'
            WHERE id = ?
        ''', (file_hash, inv_num,
              expense_name or result.get('vendor', ''), category,
              result.get('total', 0),
              json.dumps(result.get('items', []), ensure_ascii=False),
              invoice_db_id))
    else:
        cur = db.execute('''
            INSERT OR IGNORE INTO parsed_invoices
            (file_id, file_name, file_hash, invoice_number, folder, month, vendor, category, total, items_json, parse_status, expense_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'parsed', 'pending')
        ''', (f['id'], f['name'], file_hash, inv_num,
              f.get('path', ''), current_month,
              expense_name or result.get('vendor', ''), category,
              result.get('total', 0),
              json.dumps(result.get('items', []), ensure_ascii=False)))
        invoice_db_id = cur.lastrowid

    known_hashes.add(file_hash)
    if inv_num:
        known_inv_numbers.add(inv_num)
    if invoice_total > 0:
        known_amounts.add((round(invoice_total, 2), current_month))
    db.commit()

    # LLM classification + item mapping
    llm_classification = None
    if invoice_db_id and result.get('items'):
        llm_classification = _classify_and_map_items(
            db, invoice_db_id, result, f['name'], category, expense_name
        )

    # Auto-approve to expenses
    invoice_vendor = (llm_classification.get('expense_name') if llm_classification else None) or expense_name
    invoice_category = (llm_classification.get('category') if llm_classification else None) or category
    vendor_known = bool(invoice_vendor and invoice_vendor.lower() not in ('', '—', 'інше'))

    if vendor_known and invoice_total > 0:
        cur = db.execute(
            'INSERT INTO expenses (name, category, amount, month, recurring, note) VALUES (?, ?, ?, ?, 0, ?)',
            (invoice_vendor, invoice_category, invoice_total, current_month,
             f"Фактура #{inv_num or f['name']}")
        )
        expense_id = cur.lastrowid
        db.execute(
            "UPDATE parsed_invoices SET expense_status = 'added', expense_id = ? WHERE id = ?",
            (expense_id, invoice_db_id)
        )
        db.commit()
        auto_approved.append({'name': f['name'], 'vendor': invoice_vendor, 'total': invoice_total})

    new_parsed.append({'name': f['name'], 'vendor': expense_name, 'total': invoice_total})


def _classify_and_map_items(db, invoice_db_id, result, file_name, category, expense_name) -> dict:
    """Run LLM classification and item mapping for an invoice."""
    from app.llm import classify_invoice, map_items_to_ingredients

    try:
        items = result.get('items', [])
        ingredients = [dict(r) for r in db.execute('SELECT id, name, unit FROM ingredients').fetchall()]
        existing = {r['invoice_name']: r['ingredient_id']
                    for r in db.execute('SELECT invoice_name, ingredient_id FROM ingredient_mappings').fetchall()}

        llm_classification = classify_invoice(result.get('vendor', ''), file_name, items)

        # Get POS products for resale matching
        pos_products = [r['product_name'] for r in db.execute(
            'SELECT DISTINCT product_name FROM sales ORDER BY product_name'
        ).fetchall()]

        llm_mappings = map_items_to_ingredients(items, ingredients, existing, pos_products)

        db.execute(
            'UPDATE parsed_invoices SET category = ?, vendor = ? WHERE id = ?',
            (llm_classification.get('category', category),
             llm_classification.get('expense_name', expense_name) or result.get('vendor', ''),
             invoice_db_id)
        )

        # Process mappings
        db.execute('DELETE FROM invoice_items_pending WHERE parsed_invoice_id = ?', (invoice_db_id,))
        for m in llm_mappings:
            _process_item_mapping(db, m, invoice_db_id)
        db.commit()

        return llm_classification
    except Exception as llm_err:
        log.warning(f"LLM processing failed for {file_name}: {llm_err}")
        return {}


def _process_item_mapping(db, m: dict, invoice_db_id: int):
    """Process a single item mapping result from LLM."""
    action = m['action']
    confidence = m.get('confidence', 0)
    ing_id = m.get('ingredient_id')
    status = 'pending'

    if action == 'resale' and m.get('suggested_name') and confidence >= 0.85:
        ing_id, status = _handle_resale_mapping(db, m, invoice_db_id)
    elif action == 'match' and ing_id and confidence >= 0.85:
        ing_id, status = _handle_match_mapping(db, m, ing_id, invoice_db_id)

    db.execute('''
        INSERT INTO invoice_items_pending
        (parsed_invoice_id, invoice_name, quantity, unit_price, status, ingredient_id, suggested_ingredient, confidence)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (invoice_db_id, m['invoice_name'], m.get('quantity', 0), m.get('unit_price', 0),
          status, ing_id, m.get('suggested_name', ''), confidence))


def _handle_resale_mapping(db, m: dict, invoice_db_id: int) -> tuple:
    """Handle auto-creation/update of resale ingredient."""
    pos_name = m['suggested_name']
    existing_resale = db.execute('SELECT id FROM ingredients WHERE name = ?', (pos_name,)).fetchone()

    if existing_resale:
        ing_id = existing_resale['id']
        if m.get('unit_price', 0) > 0:
            old_row = db.execute('SELECT unit_price FROM ingredients WHERE id = ?', (ing_id,)).fetchone()
            old_price = old_row['unit_price'] if old_row else 0
            new_price = m['unit_price']
            if (old_price == 0) or (0.2 <= new_price / old_price <= 5.0):
                db.execute('UPDATE ingredients SET unit_price = ? WHERE id = ?', (new_price, ing_id))
                db.execute(
                    'INSERT INTO ingredient_price_history (ingredient_id, price, invoice_id) VALUES (?, ?, ?)',
                    (ing_id, new_price, invoice_db_id)
                )
    else:
        cur = db.execute(
            "INSERT INTO ingredients (name, unit, quantity, min_quantity, unit_price, kind) VALUES (?, 'szt', 0, 1, ?, 'resale')",
            (pos_name, m.get('unit_price', 0))
        )
        ing_id = cur.lastrowid

    db.execute(
        'INSERT OR REPLACE INTO ingredient_mappings (invoice_name, ingredient_id, action) VALUES (?, ?, ?)',
        (m['invoice_name'], ing_id, 'resale')
    )
    return ing_id, 'confirmed'


def _handle_match_mapping(db, m: dict, ing_id: int, invoice_db_id: int) -> tuple:
    """Handle auto-confirmation of matched ingredient — update price + create delivery."""
    db.execute(
        'INSERT OR REPLACE INTO ingredient_mappings (invoice_name, ingredient_id, action) VALUES (?, ?, ?)',
        (m['invoice_name'], ing_id, 'match')
    )
    new_price = m.get('unit_price', 0)

    if new_price > 0:
        old_row = db.execute('SELECT unit_price FROM ingredients WHERE id = ?', (ing_id,)).fetchone()
        old_price = old_row['unit_price'] if old_row else 0
        if (old_price == 0) or (0.2 <= new_price / old_price <= 5.0):
            db.execute('UPDATE ingredients SET unit_price = ? WHERE id = ?', (new_price, ing_id))
            db.execute(
                'INSERT INTO ingredient_price_history (ingredient_id, price, invoice_id) VALUES (?, ?, ?)',
                (ing_id, new_price, invoice_db_id)
            )
        else:
            log.warning(f"Skipping price update for ingredient {ing_id}: {old_price} -> {new_price}")

    # Auto-create delivery with LLM-converted quantity (in ingredient units)
    converted_qty = m.get('converted_qty', 0)
    if converted_qty > 0:
        total_price = new_price * converted_qty if new_price > 0 else 0
        db.execute(
            'INSERT INTO deliveries (ingredient_id, quantity, price, note) VALUES (?, ?, ?, ?)',
            (ing_id, converted_qty, total_price, f'Auto from invoice #{invoice_db_id}')
        )
        db.execute(
            'UPDATE ingredients SET quantity = quantity + ? WHERE id = ?',
            (converted_qty, ing_id)
        )

    return ing_id, 'confirmed'


def _send_summary_notification(current_month, new_parsed, auto_approved, errors):
    """Send Telegram notification with processing summary."""
    try:
        from app.telegram_bot import send_message
        if new_parsed or errors:
            msg = f"📄 <b>Фактури ({current_month})</b>\n\n"
            if auto_approved:
                msg += f"✅ Авто-додано у витрати: {len(auto_approved)}\n"
                for p in auto_approved[:10]:
                    msg += f"  • {p['vendor']} — {p['total']:.0f} zł\n"
            pending_count = len(new_parsed) - len(auto_approved)
            if pending_count > 0:
                msg += f"\n⏳ Чекають підтвердження: {pending_count}\n"
            if errors:
                msg += f"\n❌ Помилки: {len(errors)}\n"
            send_message(msg)
    except Exception:
        pass
