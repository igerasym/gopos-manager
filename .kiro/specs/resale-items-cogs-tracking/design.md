# Design Document: Resale Items & COGS Tracking

## Overview

Розширення системи The Frame Manager для коректного обліку resale-товарів (кава в пачках, drip bags, вино), виключення контейнерів з рецептів, класифікації POS-продуктів та розбивки COGS по категоріях на дашборді.

## Architecture

### Поточний стан

```
GoPos CSV → sales table → get_cost_lookup() → Dashboard COGS
                                ↑
                    recipes (prepared) + direct name match (resale)
```

**Проблеми:**
1. Resale працює через "магічний" name match (`ingredient.name = sales.product_name`) — крихкий, немає явного зв'язку
2. Немає класифікації POS-продуктів — 103 з 156 без рецепта, невідомо чи це resale чи просто missing recipe
3. Контейнери (To-go cup) сидять в рецептах і спотворюють food cost
4. Dashboard показує один загальний Food Cost % без розбивки

### Цільовий стан

```
GoPos CSV → sales table ──┐
                          ↓
                    pos_products table (kind: prepared/resale/ignore/unclassified)
                          ↓
              ┌───────────┴───────────┐
              ↓                       ↓
    prepared: recipe cost      resale: ingredient.unit_price (kind='resale')
              ↓                       ↓
              └───────────┬───────────┘
                          ↓
                Dashboard COGS (by POS_Category)
```

## Database Changes

### New table: `pos_products`

```sql
CREATE TABLE pos_products (
    product_name TEXT PRIMARY KEY,          -- matches sales.product_name exactly
    pos_kind TEXT NOT NULL DEFAULT 'unclassified',  -- prepared/resale/ignore/unclassified
    resale_ingredient_id INTEGER,           -- FK → ingredients.id (only for resale)
    category TEXT,                          -- POS_Category for dashboard grouping
    classified_at TEXT,                     -- ISO timestamp
    classified_by TEXT,                     -- 'auto' | 'user' | NULL
    llm_suggestion TEXT,                    -- JSON: {pos_kind, confidence, reasoning}
    FOREIGN KEY (resale_ingredient_id) REFERENCES ingredients(id)
);
```

### Alter table: `ingredients`

```sql
ALTER TABLE ingredients ADD COLUMN kind TEXT NOT NULL DEFAULT 'raw';
-- Values: 'raw' (used in recipes by weight/volume) | 'resale' (sold as-is, 1:1 with POS product)
```

### Migration data

Existing resale ingredients (auto-detected by name match with sales):
```sql
UPDATE ingredients SET kind = 'resale'
WHERE name IN (SELECT DISTINCT product_name FROM sales)
  AND name NOT IN (SELECT DISTINCT product_name FROM recipes);
```

Container items to remove from recipes + ingredients:
```sql
-- Identify by pattern: kubek, cup, lid, straw, servetka, papier kasowy
-- Remove from recipes first, then from ingredients
-- Log removed items to migration report
```

## Component Design

### 1. Migration Script (`app/migrations/resale_cogs.py`)

**Responsibilities:**
- Add `kind` column to ingredients
- Create `pos_products` table
- Classify existing ingredients (raw vs resale) based on name-match heuristic
- Remove container items from recipes + ingredients
- Populate `pos_products` from all unique `sales.product_name`
- Set `pos_kind` for products that already have recipes → `prepared`
- Set `pos_kind` for products with resale ingredient match → `resale`
- Rest → `unclassified`
- Generate JSON report to `data/migration_reports/`

**Execution:** One-time, run via `docker exec` on prod. Idempotent (safe to re-run).

### 2. GoPos Sync Extension (`app/gopos_sync.py`)

**Change:** After importing sales CSV, check for new product names not in `pos_products`:
```python
# After CSV import
new_products = db.execute('''
    SELECT DISTINCT product_name FROM sales
    WHERE product_name NOT IN (SELECT product_name FROM pos_products)
''').fetchall()
for p in new_products:
    db.execute('INSERT INTO pos_products (product_name) VALUES (?)', (p['product_name'],))
```

No LLM call here — just register as `unclassified`. Classification happens in Inbox.

**Category from GoPos:** GoPos groups products into categories (Coffee, Kitchen, Bakery, Bar, Beans, Ice Cream, Soft Drinks, Brewware). If GoPos CSV includes category info, store it in `pos_products.category`. Otherwise, populate from `recipe_cards.category` during migration.

### 3. POS Classifier (`app/services/pos_classifier.py`)

**New module.** Single responsibility: classify POS products via LLM.

```python
def classify_pos_product(product_name: str, sales_data: dict, ingredients: list) -> dict:
    """Ask LLM to classify a POS product.
    
    Returns: {
        'pos_kind': 'prepared' | 'resale' | 'ignore',
        'suggested_resale_ingredient_id': int | None,
        'suggested_category': str,
        'confidence': float,
        'reasoning': str
    }
    """

def batch_classify(product_names: list[str]) -> list[dict]:
    """Classify multiple products in one LLM call (batches of 20)."""
```

**LLM prompt context:**
- Product name
- Sales volume last 30 days + revenue
- List of existing ingredients (for resale matching)
- List of recipe_cards categories (for prepared categorization)

**Cost control:** One LLM call per product, cached in `pos_products.llm_suggestion`. Never re-classify if already has suggestion.

### 4. Inbox UI (`/inventory/classify` or section on Dashboard)

**New page or dashboard section.** Shows unclassified POS products.

**Table columns:**
| Назва | Продажі (30д) | Виручка | LLM пропозиція | Confidence | Дії |
|-------|---------------|---------|----------------|------------|-----|

**Actions per row:**
- ✓ Prepared (sets pos_kind, asks for category)
- ✓ Resale (sets pos_kind, creates/links resale ingredient)
- ✓ Ignore (sets pos_kind='ignore')
- 🤖 Класифікувати (trigger LLM for this product)

**Batch action:** "Класифікувати всі" button → runs batch_classify → shows results with checkboxes → "Застосувати обрані"

### 5. Updated `get_cost_lookup()` (`app/services/recipes.py`)

```python
def get_cost_lookup() -> dict:
    """Get unit cost per product using pos_products classification."""
    db = get_db()
    
    # Prepared items: from recipes
    recipe_costs = db.execute('''
        SELECT r.product_name, SUM(r.amount * COALESCE(i.unit_price, 0)) as unit_cost
        FROM recipes r JOIN ingredients i ON r.ingredient_id = i.id
        GROUP BY r.product_name
    ''').fetchall()
    result = {r['product_name']: r['unit_cost'] for r in recipe_costs}
    
    # Resale items: from pos_products → resale_ingredient_id → unit_price
    resale = db.execute('''
        SELECT pp.product_name, COALESCE(i.unit_price, 0) as unit_cost
        FROM pos_products pp
        JOIN ingredients i ON pp.resale_ingredient_id = i.id
        WHERE pp.pos_kind = 'resale'
    ''').fetchall()
    for r in resale:
        result[r['product_name']] = r['unit_cost']
    
    db.close()
    return result
```

**Backward compatible:** If `pos_products` table doesn't exist yet (pre-migration), falls back to current logic.

### 6. Dashboard COGS by Category

**New section on dashboard** below existing KPI cards:

```
┌─────────────┬──────────┬────────┬─────────────┬────────────┐
│ Категорія   │ Виручка  │ COGS   │ Валовий     │ Food Cost% │
├─────────────┼──────────┼────────┼─────────────┼────────────┤
│ Coffee      │ 47,465   │ 6,170  │ 41,295      │ 13.0%      │
│ Kitchen     │ 22,932   │ 8,026  │ 14,906      │ 35.0%      │
│ Bakery      │ 5,614    │ 1,685  │ 3,929       │ 30.0%      │
│ Bar         │ 150      │ 75     │ 75          │ 50.0%      │
│ Beans       │ 3,904    │ 2,342  │ 1,562       │ 60.0%      │
│ Ice Cream   │ 1,380    │ 552    │ 828         │ 40.0%      │
│ Soft Drinks │ 1,174    │ 587    │ 587         │ 50.0%      │
│ Brewware    │ 41       │ 20     │ 21          │ 49.0%      │
│ ⚠️ Не класиф│ 2,000    │ —      │ —           │ —          │
├─────────────┼──────────┼────────┼─────────────┼────────────┤
│ РАЗОМ       │ 84,660   │ 19,457 │ 65,203      │ 23.0%      │
└─────────────┴──────────┴────────┴─────────────┴────────────┘
```

**Category source — GoPos categories (as-is from POS terminal):**

| GoPos Category | Тип | COGS calculation |
|---|---|---|
| Coffee | prepared | recipe cost |
| Kitchen | prepared | recipe cost |
| Bakery | prepared | recipe cost |
| Bar | mixed (пиво/вино пляшки = resale, коктейлі = prepared) | recipe or unit_price |
| Beans | resale | ingredient unit_price |
| Ice Cream | resale | ingredient unit_price |
| Soft Drinks | resale | ingredient unit_price |
| Brewware | resale | ingredient unit_price |

- Category comes from `pos_products.category` (synced from GoPos CSV or set manually)
- If category is NULL → "Інше"
- `pos_kind = 'ignore'` → excluded from all calculations
- `pos_kind = 'unclassified'` → warning row with lost revenue

### 7. Invoice Flow Changes

**Current resale detection** (in `map_items_to_ingredients`): Already works — LLM returns `action='resale'` with `suggested_name` matching POS product.

**Enhancement:** When creating resale ingredient from invoice:
1. Set `kind = 'resale'` on new ingredient
2. Create/update `pos_products` row with `resale_ingredient_id`
3. If POS product doesn't exist yet in `pos_products` → create with `pos_kind = 'resale'`

**No breaking changes** to existing invoice flow. Just adds `kind` field and `pos_products` linkage.

### 8. Recipe Editor Guard

**Validation in `add_recipe` route:**
```python
# Check ingredient kind before adding to recipe
ing = db.execute('SELECT kind FROM ingredients WHERE id = ?', (ingredient_id,)).fetchone()
if ing and ing['kind'] == 'resale':
    return JSONResponse({'error': 'Resale-товар не можна додати у рецепт'}, status_code=400)
```

**UI:** Filter ingredient dropdown to show only `kind = 'raw'` ingredients.

### 9. Inventory Deduction (No change for resale)

Current `inventory_deductions` logic only processes products with recipes. Since resale items won't have recipes, they're already excluded from auto-deduction. **No code change needed.**

Stock count page: Add "Resale" section header for `kind = 'resale'` ingredients.

## Container Handling

**Defined container patterns** (for migration + ongoing invoice parsing):
```python
CONTAINER_PATTERNS = [
    'kubek', 'cup', 'to-go', 'lid', 'pokrywka', 'kryshka',
    'straw', 'słomka', 'servetka', 'serwetka', 'papier kasowy',
    'torba papierowa', 'rękawice', 'folia'
]
```

**Migration:** Remove from `recipes` and `ingredients`. Log what was removed.

**Ongoing:** In `map_items_to_ingredients` LLM prompt, containers already get `action = 'skip'`. Invoice expense goes to "Побут" category. No inventory tracking.

## Data Flow Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                    INVOICE PROCESSING                         │
│                                                              │
│  PDF → Claude Vision → items[] → map_items_to_ingredients   │
│                                        │                     │
│                    ┌───────────────────┼──────────────┐      │
│                    ↓                   ↓              ↓      │
│              action=match        action=resale   action=skip │
│                    │                   │              │      │
│              update price        create/update    expense    │
│              (ingredients)       resale ingredient  only     │
│                                  + pos_products link         │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│                    COGS CALCULATION                           │
│                                                              │
│  sales × quantity ──→ pos_products.pos_kind?                │
│                              │                               │
│              ┌───────────────┼───────────────┐               │
│              ↓               ↓               ↓               │
│         prepared          resale          ignore             │
│              │               │               │               │
│     recipe cost      ingredient.unit_price   excluded        │
│     (sum amounts     (1:1 match)                             │
│      × prices)                                               │
└─────────────────────────────────────────────────────────────┘
```

## Rollout Plan

1. **Phase 1: Migration** — Add columns, create table, classify existing data
2. **Phase 2: Cost lookup** — Switch `get_cost_lookup()` to use `pos_products`
3. **Phase 3: Dashboard** — Add category breakdown table
4. **Phase 4: Inbox** — UI for classifying new products
5. **Phase 5: Invoice flow** — Wire `kind` + `pos_products` into auto-approval

Phases 1-3 can ship together (one deploy). Phase 4-5 are incremental improvements.

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Migration breaks existing COGS | Dashboard shows wrong numbers | Fallback: if pos_products empty, use old logic |
| LLM misclassifies product | Wrong food cost for that product | Confidence threshold 0.85 + manual inbox |
| New POS products appear daily | Inbox grows unbounded | Show count on dashboard as warning, batch classify |
| Container removal breaks recipes | Some recipes lose items | Pre-check: log affected recipes, require confirmation |

## Cost Estimate

- LLM calls for batch classification of 103 products: ~$0.10 (one-time)
- Ongoing: 1-2 new products/week × 1 LLM call = negligible
- No new AWS services needed
