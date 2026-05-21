# Implementation Tasks

## Task 1: Database Migration — Add `kind` to ingredients + Create `pos_products` table
- [ ] Create migration script `app/migrations/resale_cogs.py`
- [ ] Add `kind TEXT NOT NULL DEFAULT 'raw'` column to `ingredients` table
- [ ] Create `pos_products` table with schema from design
- [ ] Auto-classify existing ingredients: set `kind = 'resale'` for those matching sales product names (without recipe)
- [ ] Populate `pos_products` from all unique `sales.product_name`
- [ ] Set `pos_kind = 'prepared'` for products with existing recipes
- [ ] Set `pos_kind = 'resale'` for products with matching resale ingredient, link `resale_ingredient_id`
- [ ] Rest stays `unclassified`
- [ ] Generate migration report JSON to `data/migration_reports/`
- [ ] Test: run migration on local DB copy, verify counts

## Task 2: Remove containers from recipes and ingredients
- [ ] Define CONTAINER_PATTERNS list in migration script
- [ ] Find all container ingredients by name pattern matching
- [ ] Delete recipe rows referencing container ingredients (log which recipes affected)
- [ ] Delete container ingredients from `ingredients` table
- [ ] Add container patterns to `map_items_to_ingredients` LLM prompt (already has `skip` action)
- [ ] Include removed containers in migration report
- [ ] Test: verify no orphan recipe references

## Task 3: Update `get_cost_lookup()` to use `pos_products`
- [ ] Modify `app/services/recipes.py` → `get_cost_lookup()`
- [ ] Add resale cost path: `pos_products.resale_ingredient_id → ingredients.unit_price`
- [ ] Add fallback: if `pos_products` table doesn't exist, use old logic (backward compat)
- [ ] Remove old "direct name match" logic (replaced by explicit resale link)
- [ ] Test: compare old vs new cost_lookup output on same data

## Task 4: GoPos Sync — auto-register new products in `pos_products`
- [ ] After CSV import in `gopos_sync.py`, insert new product names into `pos_products` as `unclassified`
- [ ] No LLM call during sync — just registration
- [ ] Test: simulate new product in CSV, verify it appears in pos_products

## Task 5: POS Classifier service (`app/services/pos_classifier.py`)
- [ ] Create new module with `classify_pos_product()` and `batch_classify()` functions
- [ ] LLM prompt: product name + sales stats + ingredient list + recipe categories
- [ ] Return: `{pos_kind, suggested_resale_ingredient_id, suggested_category, confidence, reasoning}`
- [ ] Batch: group up to 20 products per LLM call
- [ ] Cache result in `pos_products.llm_suggestion` — never re-call for same product
- [ ] Test: mock LLM response, verify parsing

## Task 6: Inbox UI for classifying POS products
- [ ] New route: `GET /inventory/classify` (admin only)
- [ ] Template: table of unclassified products with LLM suggestions
- [ ] Columns: назва, продажі 30д, виручка 30д, LLM пропозиція, confidence, дії
- [ ] Action buttons: ✓ Prepared, ✓ Resale, ✓ Ignore
- [ ] "Resale" action: create resale ingredient (name=product_name, unit=szt, kind=resale) + link
- [ ] "Prepared" action: set pos_kind + ask for category
- [ ] Batch button: "🤖 Класифікувати всі" → runs batch_classify → shows results
- [ ] Batch confirm: checkboxes (auto-checked if confidence ≥ 0.85) + "Застосувати" button
- [ ] Dashboard warning: "{N} продуктів не класифіковано" with link to inbox

## Task 7: Invoice flow — wire `kind` and `pos_products` into resale creation
- [ ] In `_process_invoices_sync`: when creating resale ingredient, set `kind = 'resale'`
- [ ] After creating resale ingredient, upsert `pos_products` row with `pos_kind = 'resale'` + `resale_ingredient_id`
- [ ] In `confirm_item_mapping`: same logic for manual resale confirmation
- [ ] Test: process invoice with resale item, verify pos_products updated

## Task 8: Recipe editor guard — prevent adding resale ingredients
- [ ] In `add_recipe` route: check `ingredients.kind`, reject if `resale`
- [ ] In recipe template: filter ingredient dropdown to show only `kind = 'raw'`
- [ ] Show validation error message in Ukrainian: "Resale-товар не можна додати у рецепт"
- [ ] Test: try adding resale ingredient to recipe, verify rejection

## Task 9: Dashboard COGS breakdown by POS category
- [ ] In `dashboard.py`: group sales by POS_Category (from pos_products + recipe_cards)
- [ ] Build category breakdown table: Revenue, COGS, Gross Profit, Food Cost %
- [ ] Categories: Бар, Кухня, Морозиво, Resale, Інше, ⚠️ Без рецепту
- [ ] Exclude `pos_kind = 'ignore'` from totals
- [ ] Show warning row for unclassified products with lost revenue sum
- [ ] Add to dashboard template below KPI cards
- [ ] Test: verify category totals sum to overall total

## Task 10: Stock count page — separate Resale section
- [ ] In stock count template: split ingredients into "Raw" and "Resale" sections
- [ ] Resale section header: "Resale товари"
- [ ] No auto-deduction for resale (already works — no recipe = no deduction)
- [ ] Show expected vs actual for resale items
- [ ] Test: verify resale items appear in separate section

## Task 11: Deploy and run migration on prod
- [ ] Commit all changes, push to git
- [ ] SSH to prod, pull, rebuild container
- [ ] Run migration script via docker exec
- [ ] Verify migration report
- [ ] Check dashboard COGS numbers make sense
- [ ] Run batch classification for existing unclassified products
- [ ] Confirm results in inbox UI
