"""Recalculate total from items_json for parsed invoices with total=0."""
import sqlite3, json
db = sqlite3.connect("/app/data/cafe.db")

rows = db.execute("SELECT id, file_name, items_json FROM parsed_invoices WHERE parse_status = 'parsed' AND total = 0").fetchall()
print(f"Found {len(rows)} zero-total invoices")

fixed = 0
for r in rows:
    items = json.loads(r[2]) if r[2] else []
    if not items:
        continue
    line_totals = sum((it.get('total', 0) or 0) for it in items)
    qty_price = sum((it.get('quantity', 0) or 0) * (it.get('unit_price', 0) or 0) for it in items)
    new_total = max(line_totals, qty_price)
    if new_total > 0:
        db.execute("UPDATE parsed_invoices SET total = ? WHERE id = ?", (new_total, r[0]))
        print(f"  {r[1][:35]}: 0 -> {new_total:.2f}")
        fixed += 1

db.commit()
print(f"Fixed {fixed} invoices")
db.close()
