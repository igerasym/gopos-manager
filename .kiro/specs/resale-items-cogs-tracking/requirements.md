# Requirements Document

## Introduction

Сьогодні система The Frame Manager не розрізняє два принципово різних типи продажів: prepared items (напої та страви, які готуються з інгредієнтів за рецептом) і resale items (товари, що продаються "як є": пачки кави Foundation 250 g, drip bag, ретейл-вино). Через це COGS і Food Cost % обчислюються некоректно (на дашборді спостерігаються значення 13.8% і 133.3% у різних періодах), 103 з 156 POS-продуктів не мають рецепта, а контейнери (To-go cup, паперові стакани) додаються в кожен напій рецепта і спотворюють собівартість.

Ця функція вводить чітку класифікацію інгредієнтів і POS-продуктів (prepared / resale / container), окремий потік обробки для resale-товарів у флоу затвердження інвойсів, inbox для класифікації нових і існуючих POS-продуктів через LLM-пропозиції, виключення контейнерів з інвентарю та рецептів (тільки expense), а також розбивку COGS на дашборді по POS-категоріях з окремою категорією "Resale".

## Glossary

- **Prepared_Item**: POS-продукт, який готується з інгредієнтів через рецепт (напр. Cappuccino, Шакшука, Batch Brew).
- **Resale_Item**: POS-продукт, який продається без приготування "як є" (напр. Foundation Kenia 250g пачка, drip bag, пляшка вина).
- **Container_Item**: предмет упаковки, що використовується при видачі замовлення (To-go cup, паперовий стакан, кришка). Не входить ані в Recipe, ані в Inventory.
- **Raw_Ingredient**: інгредієнт, який витрачається граммами/мл/штуками за рецептом для приготування Prepared_Item (напр. Foundation Kenia 1 kg пачка, що використовується по 18 g на чашку Batch Brew).
- **Resale_Ingredient**: інгредієнт, що відповідає одному Resale_Item; одна одиниця = один проданий Resale_Item (напр. Foundation Kenia 250g пачка). Має тип `kind = 'resale'`.
- **Ingredient_Kind**: атрибут інгредієнта, одне з: `raw`, `resale`. (Контейнери виключаються з інгредієнтів повністю.)
- **POS_Kind**: атрибут POS-продукту, одне з: `prepared`, `resale`, `ignore`, `unclassified`.
- **POS_Product**: рядок з таблиці `sales` (унікальна назва продукту в GoPos).
- **Inbox**: UI-список POS-продуктів зі статусом `unclassified`, з LLM-пропозиціями для швидкого підтвердження.
- **Classifier**: компонент, який запитує LLM (Claude Haiku 4.5 на Bedrock) для класифікації нового POS-продукту або інвойс-рядка.
- **Invoice_Approval_Flow**: існуючий процес: parsed_invoices → invoice_items_pending → approve.
- **Resale_Link**: однозначний зв'язок один-до-одного: `resale_ingredient.id ↔ pos_product.product_name`. Зберігається в `pos_products` (нова таблиця).
- **Confidence**: число [0, 1] від LLM, що відображає впевненість у класифікації або матчі.
- **Auto_Approve_Threshold**: значення confidence (0.85), при якому система створює resale-інгредієнт без ручного підтвердження.
- **COGS**: cost of goods sold — собівартість проданих товарів за період.
- **Food_Cost_Percent**: COGS / Revenue × 100 за категорією або загалом.
- **POS_Category**: категорія POS-продукту з GoPos терміналу: Coffee, Kitchen, Bakery, Bar, Beans, Ice Cream, Soft Drinks, Brewware, Інше. Зберігається в `pos_products.category`.

## Requirements

### Requirement 1: Класифікація інгредієнтів за призначенням

**User Story:** Як власник кафе, я хочу мати чітку різницю між інгредієнтом для рецепту та resale-товаром, щоб COGS і дашборд правильно враховували обидва типи.

#### Acceptance Criteria

1. THE Inventory_System SHALL зберігати атрибут `kind` для кожного інгредієнта зі значенням `raw` або `resale`.
2. WHEN адміністратор створює новий інгредієнт через UI або API, THE Inventory_System SHALL вимагати явне значення `kind`.
3. WHERE інгредієнт створюється автоматично з інвойс-флоу як resale, THE Invoice_Approval_Flow SHALL встановити `kind = 'resale'`.
4. WHERE інгредієнт створюється автоматично з інвойс-флоу як новий raw-інгредієнт, THE Invoice_Approval_Flow SHALL встановити `kind = 'raw'`.
5. IF користувач намагається додати інгредієнт з `kind = 'resale'` у Recipe, THEN THE Recipe_Editor SHALL повернути помилку валідації з повідомленням "Resale-товар не можна додати у рецепт".
6. THE Inventory_System SHALL не дозволяти зміну `kind` інгредієнта, якщо для нього існує хоча б один Recipe рядок (для `raw`) або активний Resale_Link (для `resale`).

### Requirement 2: Виключення контейнерів з інвентарю та рецептів

**User Story:** Як власник кафе, я хочу, щоб одноразові контейнери (To-go cup, паперові стакани, кришки) не сиділи у рецептах напоїв і не псували Food Cost, бо це упаковка, а не інгредієнт.

#### Acceptance Criteria

1. WHEN розгортається міграція цієї функції, THE Migration_Tool SHALL видалити з таблиці `recipes` усі рядки, де `ingredient_id` посилається на існуючі контейнерні інгредієнти (визначений список: To-go cup, паперові стакани, кришки, серветки, паперові пакети).
2. WHEN розгортається міграція цієї функції, THE Migration_Tool SHALL видалити з таблиці `ingredients` усі контейнерні інгредієнти зі визначеного списку, попередньо вивівши користувачу список того, що буде видалено, для одноразового підтвердження.
3. THE Invoice_Parser SHALL класифікувати позиції інвойсу, що відповідають контейнерним патернам (To-go cup, paper cup, kubek, lid, papier kasowy), як `skip` для інвентарю та як категорію `Побут` для expense.
4. THE Recipe_Editor SHALL показувати в селекторі інгредієнта тільки інгредієнти з `kind = 'raw'`.

### Requirement 3: Класифікація POS-продуктів

**User Story:** Як власник кафе, я хочу, щоб кожен POS-продукт був явно позначений як prepared, resale або ignore, щоб система знала, як рахувати його COGS.

#### Acceptance Criteria

1. THE Inventory_System SHALL зберігати таблицю `pos_products` з полями `product_name` (UNIQUE), `pos_kind` (`prepared` | `resale` | `ignore` | `unclassified`), `resale_ingredient_id` (NULL або FK на ingredients), `category` (POS_Category, NULL дозволено), `classified_at` (timestamp, NULL для unclassified), `classified_by` (`auto` | `user` | NULL).
2. WHEN GoPos_Sync виконується і виявляє продукт у CSV, що ще не існує в `pos_products`, THE GoPos_Sync SHALL вставити рядок з `pos_kind = 'unclassified'`.
3. WHILE POS_Product має `pos_kind = 'unclassified'`, THE Dashboard SHALL виключати його з обчислення Food_Cost_Percent і відображати кількість таких продуктів як попередження.
4. WHEN POS_Product має `pos_kind = 'resale'`, THE COGS_Calculator SHALL використовувати `unit_price` пов'язаного Resale_Ingredient як unit cost.
5. WHEN POS_Product має `pos_kind = 'prepared'` і існує Recipe для нього, THE COGS_Calculator SHALL використовувати суму `recipe.amount × ingredient.unit_price` як unit cost.
6. WHEN POS_Product має `pos_kind = 'prepared'` і Recipe не існує, THE COGS_Calculator SHALL встановити unit cost у NULL і Dashboard SHALL рахувати такі продажі в окремому показнику "no recipe".
7. WHEN POS_Product має `pos_kind = 'ignore'`, THE COGS_Calculator SHALL виключити його з усіх обчислень COGS і Food_Cost_Percent.

### Requirement 4: LLM-класифікатор POS-продуктів та inbox для підтвердження

**User Story:** Як власник кафе, я хочу швидко підтверджувати тип нових POS-продуктів через LLM-пропозиції, щоб не витрачати час на ручний розбір.

#### Acceptance Criteria

1. WHEN POS_Product зі статусом `unclassified` потрапляє в Classifier, THE Classifier SHALL отримати від LLM пропозицію формату `{pos_kind: prepared|resale|ignore, suggested_resale_ingredient_id: int|null, suggested_category: str, confidence: float, reasoning: str}` за один виклик Bedrock.
2. THE Inbox SHALL відображати усі POS_Product зі статусом `unclassified` у таблиці зі стовпцями: назва, кількість продажів за останні 30 днів, виручка за 30 днів, LLM-пропозиція, confidence, кнопки "✓ Prepared", "✓ Resale (вибрати інгредієнт)", "✓ Ignore".
3. WHEN користувач натискає кнопку підтвердження в Inbox, THE Inbox SHALL оновити `pos_products.pos_kind`, встановити `classified_at = NOW()` і `classified_by = 'user'`.
4. WHERE користувач підтверджує POS_Product як `resale` без вибраного інгредієнта, THE Inbox SHALL створити новий Resale_Ingredient з `name = product_name`, `unit = 'szt'`, `quantity = 0`, `kind = 'resale'` і встановити `resale_ingredient_id` у `pos_products`.
5. THE Classifier SHALL мати ліміт виклику LLM не більше одного разу на унікальний `product_name` для уникнення дублюючих витрат.

### Requirement 5: Одноразова batch-міграція 103 існуючих unclassified продуктів

**User Story:** Як власник кафе, я хочу, щоб система за один пробіг класифікувала всі 103 існуючі POS-продукти без рецепта, і я зміг підтвердити список одним кліком.

#### Acceptance Criteria

1. THE Migration_Tool SHALL надавати CLI або UI кнопку "Класифікувати всі unclassified продукти", доступну тільки користувачу з role = admin.
2. WHEN адміністратор запускає batch-класифікацію, THE Migration_Tool SHALL передати усі POS_Product зі статусом `unclassified` у Classifier і отримати пропозиції для кожного.
3. WHEN batch-класифікація завершується, THE Migration_Tool SHALL відобразити таблицю результатів зі стовпцями: назва, поточний стан, LLM-пропозиція, confidence, чекбокс "застосувати".
4. THE Migration_Tool SHALL встановити чекбокс "застосувати" у `true` за замовчуванням для рядків з `confidence >= 0.85` і у `false` для рядків з `confidence < 0.85`.
5. WHEN адміністратор підтверджує batch-форму, THE Migration_Tool SHALL застосувати тільки відмічені рядки одною транзакцією і встановити `classified_by = 'auto'` для авто-створених записів та `classified_by = 'user'` для тих, що були переключені вручну.

### Requirement 6: Resale у флоу затвердження інвойсів

**User Story:** Як власник кафе, я хочу, щоб при затвердженні інвойсу від Foundation/Coffeedesk resale-товари автоматично прив'язувались до POS-продукту і ціна оновлювалась, без зайвих ручних кліків.

#### Acceptance Criteria

1. WHEN Invoice_Parser обробляє рядок інвойсу і LLM повертає `action = 'resale'` з `confidence >= 0.85` і однозначним `suggested_pos_product_name`, THE Invoice_Approval_Flow SHALL автоматично створити або оновити Resale_Ingredient з полем `kind = 'resale'`, оновити Resale_Link для відповідного POS_Product і зберегти рядок зі статусом `confirmed`.
2. WHEN Invoice_Parser обробляє рядок інвойсу і LLM повертає `action = 'resale'` з `confidence < 0.85` або без однозначного `suggested_pos_product_name`, THE Invoice_Approval_Flow SHALL зберегти рядок у `invoice_items_pending` зі статусом `review` для ручного підтвердження.
3. WHEN адміністратор підтверджує `review` рядок як resale через UI, THE Invoice_Approval_Flow SHALL дозволити вибрати існуючий POS_Product зі списку або створити новий resale-інгредієнт.
4. WHEN Resale_Ingredient створюється або оновлюється через Invoice_Approval_Flow, THE Invoice_Approval_Flow SHALL обчислити `unit_price = invoice_line.brutto_price / invoice_line.quantity` і зберегти у `ingredients.unit_price`.
5. WHEN `unit_price` Resale_Ingredient змінюється на ≥ 10% порівняно з попереднім значенням, THE Price_Alert_Service SHALL надіслати Telegram-сповіщення у форматі "📈 {ingredient_name}: {old_price} → {new_price} zł/szt ({+/-N%})".

### Requirement 7: COGS і дашборд з розбивкою по POS-категоріях

**User Story:** Як власник кафе, я хочу бачити Food Cost і прибуток окремо по Бар, Кухня, Морозиво, Resale та інших, щоб розуміти, які категорії приносять прибуток.

#### Acceptance Criteria

1. THE Dashboard SHALL відображати таблицю розбивки за POS_Category з колонками: категорія, Revenue, COGS, Gross_Profit, Food_Cost_Percent, кількість проданих одиниць.
2. THE Dashboard SHALL використовувати POS_Category з `pos_products.category` (GoPos categories: Coffee, Kitchen, Bakery, Bar, Beans, Ice Cream, Soft Drinks, Brewware).
3. WHEN POS_Product не має значення `category`, THE Dashboard SHALL відносити продаж до категорії `Інше`.
4. THE Dashboard SHALL відображати `Food_Cost_Percent` загальний (зважений по виручці) і окремо по кожній POS_Category.
5. THE Dashboard SHALL виключати POS_Product зі статусом `pos_kind = 'ignore'` або `pos_kind = 'unclassified'` з обчислення Food_Cost_Percent і додавати окремі попереджувальні рядки "{N} продуктів не класифіковано (втрачена виручка: {sum} zł)".
6. THE Dashboard SHALL показувати окремо рядок Packaging_Cost (сума expense за категорією Побут, що відповідає контейнерним vendor-rules за період), не включаючи його в Food_Cost_Percent.

### Requirement 8: Resale-інвентар без авто-списання при продажу

**User Story:** Як власник кафе, я хочу, щоб resale-товари мали запас в інвентарі, але не списувались автоматично при продажу — я звірятиму їх вручну через stock count.

#### Acceptance Criteria

1. WHEN POS_Product зі статусом `pos_kind = 'resale'` продається, THE Inventory_Deduction_Service SHALL не змінювати `quantity` пов'язаного Resale_Ingredient.
2. WHEN POS_Product зі статусом `pos_kind = 'prepared'` продається, THE Inventory_Deduction_Service SHALL списувати інгредієнти з рецепта (існуюча поведінка).
3. THE Stock_Count_Page SHALL відображати Resale_Ingredient окремою секцією "Resale" з полями: назва, очікуваний залишок (`quantity`), фактичний залишок (input), розрахований витрата за період між інвентаризаціями = (попередній фактичний залишок + delivered) − поточний фактичний залишок.
4. WHEN адміністратор зберігає Stock_Count, THE Stock_Count_Service SHALL оновити `quantity` усіх інгредієнтів (raw і resale) до фактичних значень.

### Requirement 9: Звіт міграції контейнерів та незмінних інгредієнтів

**User Story:** Як власник кафе, я хочу бачити, що саме змінилося після міграції, щоб переконатись, що нічого важливого не пропало.

#### Acceptance Criteria

1. WHEN Migration_Tool виконує міграцію контейнерів і класифікацію інгредієнтів, THE Migration_Tool SHALL вивести підсумковий звіт зі списком: видалені контейнери (id, назва, кількість recipe-рядків що використовували), помічені як `resale` інгредієнти (id, назва), помічені як `raw` інгредієнти (кількість).
2. THE Migration_Tool SHALL зберегти підсумковий звіт міграції у файл `data/migration_reports/resale-cogs-{timestamp}.json` для аудиту.
3. IF Migration_Tool виявляє інгредієнт з `kind = 'resale'`, який присутній у Recipe, THEN THE Migration_Tool SHALL припинити міграцію і вивести помилку зі списком конфліктів для ручного розв'язання.
