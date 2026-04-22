"""Parse supplier invoices using PDF text extraction + AWS Bedrock Claude."""
import json
import logging
import pdfplumber

log = logging.getLogger(__name__)


def extract_text_from_pdf(file_bytes: bytes) -> str:
    """Extract text from PDF bytes."""
    import io
    text = ''
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            text += page.extract_text() or ''
            text += '\n'
    return text


def parse_invoice_with_llm(pdf_text: str, existing_ingredients: list[dict]) -> dict:
    """Send invoice text to Claude Haiku on Bedrock, get structured items back."""
    import boto3

    ingredient_list = '\n'.join(
        f'- id={i["id"]}: {i["name"]} ({i["unit"]}, price={i["unit_price"]:.3f})'
        for i in existing_ingredients
    )

    prompt = f"""You are parsing a supplier invoice for a cafe. Extract all FOOD items (skip packaging, cleaning supplies, recycling fees, paper products, gloves, bags, foil).

For each food item return:
- "invoice_name": original name from invoice
- "ingredient_id": ID from existing ingredients list (or null if new)
- "ingredient_name": matched name from list, or suggested short name for new item
- "quantity": total units delivered (calculate: if pack has 20 eggs and 5 packs ordered, quantity = 100)
- "unit": unit of measurement (g, kg, ml, L, szt, pcs)
- "price_brutto": total price BRUTTO (with VAT) for the line
- "price_per_unit_brutto": price per single unit BRUTTO
- "vat_pct": VAT percentage (5, 8, or 23)
- "is_new": true if not matched to existing ingredient

IMPORTANT:
- Use BRUTTO prices (the "brutto" column from invoice, or calculate: netto × (1 + vat/100))
- For items sold by weight (KG), price_per_unit is per kg
- Match to existing ingredients by meaning, not exact name. E.g. "MC JAJA WOLNY WYBIEG L 20 SZT" = "Jaja L"
- If multiple invoice lines map to same ingredient, combine them

Existing ingredients:
{ingredient_list}

Invoice text:
{pdf_text}

Return ONLY valid JSON array, no markdown, no explanation:
[{{"invoice_name": "...", "ingredient_id": ..., "ingredient_name": "...", "quantity": ..., "unit": "...", "price_brutto": ..., "price_per_unit_brutto": ..., "vat_pct": ..., "is_new": false}}]"""

    try:
        client = boto3.client('bedrock-runtime', region_name='us-west-2')
        resp = client.invoke_model(
            modelId='anthropic.claude-3-5-haiku-20241022-v1:0',
            contentType='application/json',
            accept='application/json',
            body=json.dumps({
                'anthropic_version': 'bedrock-2023-05-31',
                'max_tokens': 4096,
                'messages': [{'role': 'user', 'content': prompt}]
            })
        )
        result = json.loads(resp['body'].read())
        text = result['content'][0]['text']

        # Parse JSON from response
        items = json.loads(text)
        return {'success': True, 'items': items}

    except json.JSONDecodeError as e:
        log.error(f'Failed to parse LLM response: {e}')
        return {'success': False, 'error': f'Invalid JSON from LLM: {str(e)}', 'raw': text}
    except Exception as e:
        log.error(f'Bedrock invoke failed: {e}')
        return {'success': False, 'error': str(e)}
