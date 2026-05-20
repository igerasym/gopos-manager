import sqlite3, json
db = sqlite3.connect("/app/data/cafe.db")
rows = db.execute("SELECT file_name, vendor, total, items_json FROM parsed_invoices WHERE parse_status = 'parsed' AND total = 0").fetchall()
print(f'Zero total parsed: {len(rows)}')
for r in rows[:5]:
    items = json.loads(r[3]) if r[3] else []
    items_total = sum((i.get('total', 0) or 0) for i in items)
    items_qty_price = sum((i.get('quantity', 0) or 0) * (i.get('unit_price', 0) or 0) for i in items)
    print(f"{r[0][:35]} items={len(items)} total_sum={items_total:.2f} qty*price={items_qty_price:.2f}")
db.close()
