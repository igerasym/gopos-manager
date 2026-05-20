"""Auto-approve all existing pending invoices where vendor is recognized."""
import sqlite3
db = sqlite3.connect("/app/data/cafe.db")

# Get all pending invoices with parse_status=parsed, total>0, and recognized vendor
rows = db.execute("""
    SELECT id, file_name, vendor, category, total, month, invoice_number
    FROM parsed_invoices
    WHERE parse_status = 'parsed'
      AND expense_status = 'pending'
      AND total > 0
      AND vendor != ''
      AND vendor != '—'
""").fetchall()

print(f"Found {len(rows)} pending invoices to auto-approve")

approved = 0
for r in rows:
    inv_id, file_name, vendor, category, total, month, inv_num = r
    # Add to expenses
    cur = db.execute(
        'INSERT INTO expenses (name, category, amount, month, recurring, note) VALUES (?, ?, ?, ?, 0, ?)',
        (vendor, category or 'Інше', total, month, f"Фактура #{inv_num or file_name}")
    )
    expense_id = cur.lastrowid
    # Update invoice status
    db.execute(
        "UPDATE parsed_invoices SET expense_status = 'added', expense_id = ? WHERE id = ?",
        (expense_id, inv_id)
    )
    print(f"  ✓ {vendor} — {total:.2f} zł ({category})")
    approved += 1

db.commit()
print(f"\nAuto-approved: {approved}")
db.close()
