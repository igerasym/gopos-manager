"""LLM integration via AWS Bedrock for invoice classification and ingredient mapping."""
import base64
import json
import logging
import os

import boto3
import botocore.config

log = logging.getLogger(__name__)

MODEL_ID = os.getenv('BEDROCK_MODEL_ID', 'us.amazon.nova-2-lite-v1:0')
REGION = os.getenv('BEDROCK_REGION', 'us-west-2')

_client = None


def get_client():
    global _client
    if _client is None:
        config = botocore.config.Config(retries={'max_attempts': 3, 'mode': 'standard'})
        _client = boto3.client('bedrock-runtime', region_name=REGION, config=config)
    return _client


def call_llm(prompt: str, max_tokens: int = 1000) -> str:
    """Call Bedrock LLM with a prompt, return text response."""
    client = get_client()
    try:
        response = client.converse(
            modelId=MODEL_ID,
            messages=[{'role': 'user', 'content': [{'text': prompt}]}],
            inferenceConfig={'maxTokens': max_tokens, 'temperature': 0.1}
        )
        return response['output']['message']['content'][0]['text']
    except Exception as e:
        log.error(f"LLM call failed: {e}")
        raise


def parse_invoice_with_vision(file_bytes: bytes, file_name: str = '') -> dict:
    """Parse invoice PDF/image directly with LLM vision.

    Returns: {
        'vendor': str, 'date': str, 'invoice_number': str,
        'total': float, 'items': [{name, quantity, unit_price, total}, ...]
    }
    """
    import io
    # Convert PDF to images first (vision models need images)
    is_pdf = file_name.lower().endswith('.pdf') or file_bytes[:4] == b'%PDF'

    images = []
    if is_pdf:
        try:
            import pdfplumber
            pdf = pdfplumber.open(io.BytesIO(file_bytes))
            for page in pdf.pages[:5]:  # Max 5 pages
                img = page.to_image(resolution=150)
                buf = io.BytesIO()
                img.original.save(buf, format='PNG')
                images.append(('png', buf.getvalue()))
            pdf.close()
        except Exception as e:
            raise Exception(f"PDF to image conversion failed: {e}")
    else:
        # Detect format from bytes
        fmt = 'png'
        if file_bytes[:3] == b'\xff\xd8\xff':
            fmt = 'jpeg'
        images.append((fmt, file_bytes))

    if not images:
        raise Exception("No images extracted from file")

    # Build content with all images + prompt
    content = []
    for fmt, img_bytes in images:
        content.append({
            'image': {
                'format': fmt,
                'source': {'bytes': img_bytes}
            }
        })

    prompt = """You are extracting data from a Polish invoice (faktura) for a coffee shop.

Extract:
- vendor: company name (e.g. "MAKRO", "Foundation Coffee", "PGE")
- invoice_number: faktura number (e.g. "FV/2026/123")
- date: invoice date (YYYY-MM-DD format)
- total: TOTAL BRUTTO amount in PLN (the "Razem do zapłaty" / "Wartość brutto" — final amount with VAT)
- items: array of line items, each with:
  - name: short product name (clean, no codes)
  - quantity: numeric quantity
  - unit_price: BRUTTO unit price in PLN (with VAT)
  - total: BRUTTO line total in PLN

IMPORTANT:
- Use BRUTTO (with VAT) prices, NOT netto
- Total must match sum of line totals (verify yourself!)
- For multi-page invoices: extract ALL items from all pages
- If you see "Razem" or "Suma" — that's the total
- Numbers may use comma as decimal separator (1.234,56 → 1234.56)

Reply with ONLY valid JSON (no markdown, no explanation):
{
  "vendor": "...",
  "invoice_number": "...",
  "date": "YYYY-MM-DD",
  "total": 0.00,
  "items": [
    {"name": "...", "quantity": 1.0, "unit_price": 10.00, "total": 10.00}
  ]
}"""

    content.append({'text': prompt})

    client = get_client()
    response = client.converse(
        modelId=MODEL_ID,
        messages=[{'role': 'user', 'content': content}],
        inferenceConfig={'maxTokens': 4000, 'temperature': 0.01}
    )

    text = response['output']['message']['content'][0]['text'].strip()
    if text.startswith('```'):
        text = text.split('```')[1]
        if text.startswith('json'):
            text = text[4:]
        text = text.strip()

    return json.loads(text)


def classify_invoice(vendor: str, file_name: str, items: list) -> dict:
    """Classify invoice into category and suggest expense name."""
    categories = ['Оренда', 'Зарплати', 'Бухгалтерія', 'Комунальні', 'Побут',
                  'Податки і ZUS', 'Логістика', 'Продукти', 'Інше']

    items_preview = '\n'.join([f"- {it.get('name', '')}" for it in items[:15]])
    if len(items) > 15:
        items_preview += f"\n...({len(items) - 15} more)"

    prompt = f"""You are classifying invoices for "The Frame" coffee shop in Warsaw, Poland.

Vendor: {vendor}
File name: {file_name}
Items on invoice (first 15):
{items_preview}

Categories:
- Продукти: food/drinks for sale (Makro grocery, coffee suppliers, bakeries, milk, fruits, wine for sale)
- Комунальні: utilities (electricity PGE/Energa/Tauron, water, gas, internet)
- Побут: operational items (cleaning, packaging, dishes, instax cassettes, printing menus/posters, flowers, decorations, equipment)
- Оренда: rent only
- Зарплати: salaries
- Бухгалтерія: accounting services
- Податки і ZUS: taxes (ZUS, PIT, VAT)
- Логістика: delivery, taxi, fuel
- Інше: only if truly cannot classify

Generate a SHORT meaningful expense name in Ukrainian (max 30 chars).

CONSISTENCY: Use the same expense_name for the same vendor:
- Coffeedesk → "Закуп Coffeedesk"
- Makro → "Закупка Makro"
- Foundation → "Кава Foundation"
- Coffee Plant → "Кава Coffee Plant"
- Fresh Black → "Кава Fresh Black"
- Ferment bakery → "Випічка Ferment"
- Bakers House → "Випічка Bakers House"
- PGE/Tauron/Energa → "Електрика"
- Orange/Play → "Інтернет [provider]"

EXAMPLES:
- Items "MLEKO, JAJA" → "Закупка Makro"
- Items "Foundation Kawa" → "Кава Foundation"
- Items "Energia czynna" → "Електрика" (Комунальні)
- Items "WKLAD INSTAX" → "Касети Instax" (Побут)
- Items "Wydruk menu" → "Друк меню" (Побут)
- Items "Wino", "Vinos" → "Закупка вина" (Продукти)

NEVER use "KSeF", "Krajowy System", "Faktura VAT" as expense_name — these are headers, not vendors!

Reply with ONLY valid JSON:
{{"category": "...", "expense_name": "...", "reasoning": "brief explanation in Ukrainian"}}"""

    response = call_llm(prompt, max_tokens=300)
    response = response.strip()
    if response.startswith('```'):
        response = response.split('```')[1]
        if response.startswith('json'):
            response = response[4:]
        response = response.strip()
    return json.loads(response)


def map_items_to_ingredients(invoice_items: list, ingredients: list, existing_mappings: dict = None, pos_products: list = None) -> list:
    """Map invoice items to existing ingredients using LLM.

    Args:
        invoice_items: [{'name': str, 'quantity': float, 'unit_price': float}, ...]
        ingredients: [{'id': int, 'name': str, 'unit': str}, ...]
        existing_mappings: {invoice_name: ingredient_id} for already-mapped items
        pos_products: list of product names from sales (for resale matching)

    Returns:
        [{'invoice_name': str, 'ingredient_id': int|None, 'action': 'match'|'new'|'skip'|'resale',
          'confidence': float, 'suggested_name': str}, ...]
    """
    existing_mappings = existing_mappings or {}
    pos_products = pos_products or []

    results = []
    items_to_map = []
    for item in invoice_items:
        name = item.get('name', '').strip()
        if not name:
            continue
        if name in existing_mappings:
            ing_id = existing_mappings[name]
            results.append({
                'invoice_name': name,
                'ingredient_id': ing_id,
                'action': 'match' if ing_id else 'skip',
                'confidence': 1.0,
                'suggested_name': '',
                'unit_price': item.get('unit_price', 0),
                'quantity': item.get('quantity', 0),
            })
        else:
            items_to_map.append(item)

    if not items_to_map:
        return results

    ing_list = '\n'.join([f"  {i['id']}: {i['name']} ({i['unit']})" for i in ingredients])

    items_list = '\n'.join([f"  {idx}: {it.get('name', '')} ({it.get('quantity', 0)} szt, {it.get('unit_price', 0):.2f} zł/szt)"
                            for idx, it in enumerate(items_to_map)])

    # POS products for resale matching (limit to coffee/wine/drink products to keep prompt smaller)
    resale_candidates = [p for p in pos_products if any(k in p.lower() for k in
                        ['foundation', 'coffee plant', 'drip bag', 'wino', 'wine', 'kawa ziarn', 'mr.pops'])]
    pos_list = '\n'.join(f"  - {p}" for p in resale_candidates[:60]) if resale_candidates else '(none)'

    prompt = f"""You map invoice items from a Polish coffee shop supplier to inventory ingredients.

EXISTING INGREDIENTS (id: name (unit)):
{ing_list}

POS PRODUCTS FOR RESALE (these are sold "as is" — buy 1 pack, sell 1 pack):
{pos_list}

INVOICE ITEMS TO MAP:
{items_list}

For each invoice item, decide:
1. "match" — clear match in existing ingredients (e.g. "LACIATE MLEKO 3,2% 1L" matches "Mleko 3.2%")
2. "resale" — invoice item is a coffee bag / drip bag / wine bottle that we RESELL as-is.
   The suggested_name MUST EXACTLY MATCH a POS product name from the list above.
   Example: invoice "Foundation Kawa ziarnista Kenia Kathakwa (filter) 250 gr" → suggested_name "Foundation filter Kenia Kegwa AB 250g" (find closest match in POS list)
3. "new" — real ingredient but not in our list (suggest clean Polish name for inventory use)
4. "skip" — not a food/inventory item (cash register paper, cleaning, packaging)

For "resale" items: confidence should be high (>0.85). Use POS product list as authoritative source.
For "match" items: confidence >= 0.7.

Reply with ONLY valid JSON array:
[
  {{"item_index": 0, "action": "match", "ingredient_id": 5, "confidence": 0.95, "suggested_name": ""}},
  {{"item_index": 1, "action": "resale", "ingredient_id": null, "confidence": 0.9, "suggested_name": "Foundation filter Kenia Kegwa AB 250g"}},
  {{"item_index": 2, "action": "new", "ingredient_id": null, "confidence": 0.0, "suggested_name": "Mleko migdałowe Barista"}},
  {{"item_index": 3, "action": "skip", "ingredient_id": null, "confidence": 1.0, "suggested_name": ""}}
]"""

    response = call_llm(prompt, max_tokens=2000)
    response = response.strip()
    if response.startswith('```'):
        response = response.split('```')[1]
        if response.startswith('json'):
            response = response[4:]
        response = response.strip()

    try:
        mappings = json.loads(response)
    except json.JSONDecodeError as e:
        log.error(f"Failed to parse LLM response: {e}\nResponse: {response[:500]}")
        return results

    for m in mappings:
        idx = m.get('item_index', 0)
        if idx >= len(items_to_map):
            continue
        item = items_to_map[idx]
        results.append({
            'invoice_name': item.get('name', ''),
            'ingredient_id': m.get('ingredient_id'),
            'action': m.get('action', 'skip'),
            'confidence': m.get('confidence', 0),
            'suggested_name': m.get('suggested_name', ''),
            'unit_price': item.get('unit_price', 0),
            'quantity': item.get('quantity', 0),
        })

    return results
