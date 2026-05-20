"""LLM integration via AWS Bedrock for invoice classification and ingredient mapping."""
import json
import logging
import os

import boto3
import botocore.config

log = logging.getLogger(__name__)

MODEL_ID = os.getenv('BEDROCK_MODEL_ID', 'us.anthropic.claude-haiku-4-5-20251001-v1:0')
REGION = os.getenv('BEDROCK_REGION', 'us-west-2')

_client = None


def get_client():
    global _client
    if _client is None:
        config = botocore.config.Config(retries={'max_attempts': 3, 'mode': 'standard'})
        _client = boto3.client('bedrock-runtime', region_name=REGION, config=config)
    return _client


def call_llm(prompt: str, max_tokens: int = 1000) -> str:
    """Call Bedrock Claude with a prompt, return text response."""
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


def classify_invoice(vendor: str, file_name: str, items: list) -> dict:
    """Classify invoice into category and suggest expense name.
    Returns: {'category': str, 'expense_name': str, 'reasoning': str}
    """
    categories = ['Оренда', 'Зарплати', 'Бухгалтерія', 'Комунальні', 'Побут',
                  'Податки і ZUS', 'Логістика', 'Продукти', 'Інше']

    items_preview = '\n'.join([f"- {it.get('name', '')}" for it in items[:10]])
    if len(items) > 10:
        items_preview += f"\n...({len(items) - 10} more)"

    prompt = f"""You are classifying invoices for a Polish coffee shop.

Vendor name: {vendor}
File name: {file_name}
Items on invoice (first 10):
{items_preview}

Classify into ONE category from this list:
{', '.join(categories)}

Categories meanings:
- Продукти: food/drinks ingredients (Makro, coffee suppliers like Foundation/Coffee Plant, bakeries, milk, fruits)
- Комунальні: utilities (electricity PGE/Energa/Tauron, water, gas)
- Побут: household items (cleaning, packaging, paper towels, dishes, internet)
- Оренда: rent
- Зарплати: salaries
- Бухгалтерія: accounting services
- Податки і ZUS: taxes and social security (ZUS, PIT, VAT)
- Логістика: delivery, taxi
- Інше: other

Also suggest a short expense name (in Ukrainian, max 30 chars), like:
- "Закупка Makro" for Makro Cash & Carry
- "Кава Foundation" for Foundation Coffee
- "Електрика" for PGE
- "Інтернет Play" for Play S.A.

Reply with ONLY valid JSON, no markdown:
{{"category": "...", "expense_name": "...", "reasoning": "brief explanation"}}"""

    response = call_llm(prompt, max_tokens=300)
    # Extract JSON from response
    response = response.strip()
    if response.startswith('```'):
        response = response.split('```')[1]
        if response.startswith('json'):
            response = response[4:]
        response = response.strip()
    return json.loads(response)


def map_items_to_ingredients(invoice_items: list, ingredients: list, existing_mappings: dict = None) -> list:
    """Map invoice items to existing ingredients using LLM.
    
    Args:
        invoice_items: [{'name': str, 'quantity': float, 'unit_price': float}, ...]
        ingredients: [{'id': int, 'name': str, 'unit': str}, ...]
        existing_mappings: {invoice_name: ingredient_id} for already-mapped items

    Returns:
        [{'invoice_name': str, 'ingredient_id': int|None, 'action': 'match'|'new'|'skip',
          'confidence': float, 'suggested_name': str}, ...]
    """
    existing_mappings = existing_mappings or {}

    # Pre-filter: items already mapped
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

    # Build ingredient list for prompt
    ing_list = '\n'.join([f"  {i['id']}: {i['name']} ({i['unit']})" for i in ingredients])

    # Build invoice items list
    items_list = '\n'.join([f"  {idx}: {it.get('name', '')} ({it.get('quantity', 0)} szt, {it.get('unit_price', 0):.2f} zł/szt)"
                            for idx, it in enumerate(items_to_map)])

    prompt = f"""You map invoice items from a Polish coffee shop supplier to existing inventory ingredients.

EXISTING INGREDIENTS (id: name (unit)):
{ing_list}

INVOICE ITEMS TO MAP:
{items_list}

For each invoice item, decide:
1. "match" — if there's a clear match in existing ingredients (e.g. "LACIATE MLEKO UHT 3,2% 1L" matches "Mleko 3.2%")
2. "new" — if it's a real food/drink item but NOT in our list (suggest a clean short name in Polish)
3. "skip" — if it's not a food ingredient (cash register paper "SIGMA ROLKA", cleaning products, packaging)

Confidence: 0.0-1.0. Only use "match" with confidence >= 0.7.

Reply with ONLY valid JSON array, no markdown. One object per item, in same order:
[
  {{"item_index": 0, "action": "match", "ingredient_id": 5, "confidence": 0.95, "suggested_name": ""}},
  {{"item_index": 1, "action": "new", "ingredient_id": null, "confidence": 0.0, "suggested_name": "Mleko migdałowe Barista"}},
  {{"item_index": 2, "action": "skip", "ingredient_id": null, "confidence": 1.0, "suggested_name": ""}}
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
