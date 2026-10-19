# Прогрес

## Зроблено
- Git-репозиторій і `.gitignore`: `data/`, `transactions.beancount`, `report.html`, `.venv/`, `__pycache__/` не потрапляють у коміти.
- Допит Олі: рахунок, валюта, початковий залишок, фіксовані витрати, нові категорії (Play, Rossmann, Decathlon), визначення заощаджень, неповний жовтень.
- Документи: `PRD.md`, `IMPLEMENTATION_PLAN.md`, `PROGRESS.md`, розділ «Проєкт» у `CLAUDE.md`.
- Крок 1 плану — імпорт і звірка з банком:
  - `import.py` читає всі CSV з `data/` і `rules.csv`, перезаписує `transactions.beancount` (142 операції, тег `#fixed` для фіксованих) і додає balance-твердження: 8 596,30 на 2026-09-01, 12 305,39 на 2026-10-01, 17 127,34 на 2026-10-16;
  - `main.beancount`: опції, `open` рахунків, початковий залишок 4 500 зл на 2026-07-31, `include "transactions.beancount"`;
  - `uv run bean-check main.beancount` мовчить;
  - у `Expenses:Без-Категорії` 6 операцій на 562,98 зл: Rossmann ×2, Play ×2, Decathlon ×2.

## Далі
- Крок 2 плану: чотири збережені запити `query` у `main.beancount` і Fava на порті 5050.
- Правила для Play, Rossmann і Decathlon — на кроці 4, не раніше.
