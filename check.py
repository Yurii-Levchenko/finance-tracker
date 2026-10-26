"""Одна команда перевірки: імпорт → bean-check → контрольні цифри Олі й Марка.

Запуск: uv run python check.py

1. Заново генерує transactions.beancount з виписок у data/ (import.py).
2. Запускає bean-check: облік має пройти без жодного повідомлення.
3. Виконує збережений запит «3 Фіксовані і змінні» окремо для кожного власника
   і порівнює змінні витрати за серпень і вересень з контрольними цифрами з умови.
Код виходу 0 — усе збіглося, 1 — є розбіжність або помилка.
"""

import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import beanquery
from beancount import loader
from beancount.core import data

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).parent
LEDGER = ROOT / "main.beancount"
QUERY = "3 Фіксовані і змінні"

# Контрольні змінні витрати з умови ДЗ #3: (тег власника, ім'я) → {місяць: сума}.
EXPECTED = {
    ("olya", "Оля"): {"2026-08": Decimal("2195.21"), "2026-09": Decimal("2582.42")},
    ("marko", "Марко"): {"2026-08": Decimal("1855.57"), "2026-09": Decimal("2432.83")},
}


def for_owner(query, owner):
    """Додає до запиту фільтр «лише операції цього власника» (тег #olya / #marko)."""
    return query.replace("WHERE ", f"WHERE '{owner}' IN tags AND ", 1)


def main():
    ok = True

    print("1. Імпорт виписок", flush=True)
    subprocess.run([sys.executable, str(ROOT / "import.py")], check=True, cwd=ROOT)

    print("\n2. bean-check main.beancount")
    bean_check = Path(sys.executable).with_name("bean-check.exe" if sys.platform == "win32" else "bean-check")
    result = subprocess.run([str(bean_check), str(LEDGER)], capture_output=True, text=True, encoding="utf-8")
    messages = (result.stdout + result.stderr).strip()
    if result.returncode == 0 and not messages:
        print("   тиша — помилок немає ✓")
    else:
        ok = False
        print("   ✗ bean-check повідомляє:\n" + messages)

    print(f"\n3. Контрольні цифри: змінні витрати (запит «{QUERY}»)")
    entries, errors, options = loader.load_file(str(LEDGER))
    query = next(e.query_string for e in entries if isinstance(e, data.Query) and e.name == QUERY)
    conn = beanquery.connect("beancount:", entries=entries, errors=errors, options=options)
    print(f"   {'Хто':<7}{'Місяць':<10}{'Очікується':>12}{'Є':>12}")
    for (owner, name), months in EXPECTED.items():
        cursor = conn.execute(for_owner(query, owner))
        cols = [c.name for c in cursor.description]
        actual = {f"{r['month']:%Y-%m}": r["variable"] for r in (dict(zip(cols, row)) for row in cursor.fetchall())}
        for month, expected in months.items():
            got = actual.get(month, Decimal(0))
            mark = "✓" if got == expected else "✗"
            ok &= got == expected
            print(f"   {name:<7}{month:<10}{expected:>12,.2f}{got:>12,.2f}  {mark}".replace(",", " ").replace(".", ","))

    print("\nУсе збігається ✓" if ok else "\nЄ розбіжності ✗")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
