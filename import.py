"""Імпорт банківських виписок у Beancount.

Читає всі CSV з data/ і правила з rules.csv, повністю перезаписує
transactions.beancount: операції за категоріями і balance-твердження
із залишку банку на кінець кожного місяця. Наприкінці дописує ручні дані
з accounts.csv і manual.csv (див. manual.py).

Формат кожного CSV визначається автоматично:
  * банк Олі — CSV з комами, заголовок Type,Product,…, рахунок Assets:Банк:Поточний, тег #olya;
  * банк Марка — польська «Historia operacji»: роздільник «;», кома в сумах, дати dd.mm.yyyy,
    новіші рядки зверху; рахунок Assets:Банк:Марко, тег #marko.

Запуск: uv run python import.py
"""

import csv
import datetime as dt
import sys
from decimal import Decimal
from pathlib import Path

import manual

# Консоль Windows за замовчуванням не друкує українські літери.
sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
RULES_FILE = ROOT / "rules.csv"
OUTPUT_FILE = ROOT / "transactions.beancount"

# Власник → (банківський рахунок, тег). Тег визначає, чия операція, у звіті й перевірці.
OLYA = ("Assets:Банк:Поточний", "olya")
MARKO = ("Assets:Банк:Марко", "marko")
UNCATEGORIZED_EXPENSE = "Expenses:Без-Категорії"
UNCATEGORIZED_INCOME = "Income:Інше"
CURRENCY = "PLN"


def load_rules():
    """Правила у порядку файлу: (шаблон у нижньому регістрі, рахунок, фіксована, спільна)."""
    yes = lambda value: (value or "").strip().lower() == "так"
    with RULES_FILE.open(encoding="utf-8-sig", newline="") as f:
        return [
            (row["шаблон"].strip().lower(), row["рахунок"].strip(), yes(row["фіксована"]), yes(row.get("спільна")))
            for row in csv.DictReader(f)
            if row["шаблон"].strip()
        ]


def categorize(description, amount, rules):
    """Перше правило, шаблон якого є в описі, перемагає. → (рахунок, фіксована, спільна)."""
    text = description.lower()
    for pattern, account, fixed, shared in rules:
        if pattern in text:
            return account, fixed, shared
    return (UNCATEGORIZED_INCOME if amount > 0 else UNCATEGORIZED_EXPENSE), False, False


def read_olya(path):
    """Виписка банку Олі: завершені операції у порядку банку."""
    rows = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if row["State"] != "COMPLETED":
                continue
            if row["Currency"] != CURRENCY:
                raise SystemExit(f"{path.name}: валюта {row['Currency']} не підтримується: {row}")
            if Decimal(row["Fee"]) != 0:
                raise SystemExit(f"{path.name}: ненульова комісія, імпорт її ще не вміє: {row}")
            rows.append({
                "date": dt.date.fromisoformat(row["Completed Date"][:10]),
                "description": row["Description"].strip(),
                "amount": Decimal(row["Amount"]),
                "balance": Decimal(row["Balance"]),
            })
    return rows


def polish_amount(text):
    """«-1 234,56» → Decimal('-1234.56')."""
    return Decimal(text.replace("\u00a0", "").replace(" ", "").replace(",", "."))


def read_marko(path):
    """Виписка банку Марка («Historia operacji»): службові рядки, потім таблиця, новіші зверху."""
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    header = next(i for i, l in enumerate(lines) if l.startswith("Data operacji"))
    meta = dict(l.split(";", 1) for l in lines[:header] if ";" in l)
    if meta.get("Waluta", CURRENCY).strip() != CURRENCY:
        raise SystemExit(f"{path.name}: валюта {meta['Waluta']} не підтримується")
    rows = []
    for line in lines[header + 1:]:
        if not line.strip():
            continue
        _, booked, _, description, amount, balance = line.split(";")
        d, m, y = booked.split(".")
        rows.append({
            "date": dt.date(int(y), int(m), int(d)),
            "description": description.strip(),
            "amount": polish_amount(amount),
            "balance": polish_amount(balance),
        })
    # Залишок «Saldo po operacji» рахується від старих до нових: розвертаємо, не сортуємо.
    rows.reverse()
    return rows


def load_rows():
    """Усі операції з усіх CSV у data/, кожна з власником і рахунком, без повторів."""
    seen = set()
    rows = []
    for path in sorted(DATA_DIR.glob("*.csv")):
        first = path.read_text(encoding="utf-8-sig").splitlines()[0]
        if first.startswith("Historia operacji"):
            source, (account, owner) = read_marko(path), MARKO
        elif first.startswith("Type,"):
            source, (account, owner) = read_olya(path), OLYA
        else:
            raise SystemExit(f"{path.name}: невідомий формат виписки (перший рядок: {first[:40]!r})")
        for row in source:
            key = (owner, row["date"], row["description"], row["amount"], row["balance"])
            if key in seen:
                continue
            seen.add(key)
            rows.append({**row, "account": account, "owner": owner})
    return rows


def quote(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def transaction(row, rules):
    account, fixed, shared = categorize(row["description"], row["amount"], rules)
    # #shared — спільна витрата пари (ділиться навпіл у розрахунку «хто кому винен»; сума в обліку не змінюється).
    tags = f" #{row['owner']}" + (" #fixed" if fixed else "") + (" #shared" if shared else "")
    return (
        f"{row['date']} * {quote(row['description'])}{tags}\n"
        f"  {row['account']}  {row['amount']:.2f} {CURRENCY}\n"
        f"  {account}\n"
    )


def balance_assertions(rows):
    """Залишок останнього рядка кожного місяця (у порядку банку), окремо для кожного рахунку.

    Для кожного місяця, крім останнього, твердження стоїть на перше число
    наступного місяця; для останнього — на день після останньої операції рахунку.
    """
    lines = []
    for account in dict.fromkeys(r["account"] for r in rows):
        own = [r for r in rows if r["account"] == account]
        last_in_month = {}
        for row in own:
            last_in_month[(row["date"].year, row["date"].month)] = row
        months = sorted(last_in_month)
        for i, (year, month) in enumerate(months):
            row = last_in_month[(year, month)]
            if i < len(months) - 1:
                date = dt.date(year + month // 12, month % 12 + 1, 1)
            else:
                date = max(r["date"] for r in own) + dt.timedelta(days=1)
            lines.append(f"{date} balance {account}  {row['balance']:.2f} {CURRENCY}\n")
    return lines


def main():
    rules = load_rules()
    rows = load_rows()
    parts = [
        "; Згенеровано import.py — руками не правити, файл перезаписується при кожному імпорті.\n\n",
        *(transaction(row, rules) + "\n" for row in rows),
        "; Звірка з банком: залишок з виписки на кінець кожного місяця.\n",
        *balance_assertions(rows),
        "\n",
        manual.beancount_text(manual.read_accounts(), manual.read_operations()),
    ]
    OUTPUT_FILE.write_text("".join(parts), encoding="utf-8", newline="\n")
    uncategorized = sum(
        1 for r in rows
        if categorize(r["description"], r["amount"], rules)[0] in (UNCATEGORIZED_EXPENSE, UNCATEGORIZED_INCOME)
    )
    by_owner = ", ".join(f"{o} {sum(1 for r in rows if r['owner'] == o)}" for o in dict.fromkeys(r["owner"] for r in rows))
    print(f"Операцій з виписок: {len(rows)} ({by_owner}), без категорії: {uncategorized}; "
          f"ручних: {len(manual.read_operations())}. Записано {OUTPUT_FILE.name}.")


if __name__ == "__main__":
    main()
