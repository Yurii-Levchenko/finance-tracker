"""Імпорт банківських виписок у Beancount.

Читає всі CSV з data/ і правила з rules.csv, повністю перезаписує
transactions.beancount: операції за категоріями і balance-твердження
із залишку банку на кінець кожного місяця. Наприкінці дописує ручні дані
з accounts.csv і manual.csv (див. manual.py).

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

BANK_ACCOUNT = "Assets:Банк:Поточний"
UNCATEGORIZED_EXPENSE = "Expenses:Без-Категорії"
UNCATEGORIZED_INCOME = "Income:Інше"
CURRENCY = "PLN"


def load_rules():
    """Правила у порядку файлу: (шаблон у нижньому регістрі, рахунок, фіксована)."""
    with RULES_FILE.open(encoding="utf-8-sig", newline="") as f:
        return [
            (row["шаблон"].strip().lower(), row["рахунок"].strip(), row["фіксована"].strip().lower() == "так")
            for row in csv.DictReader(f)
            if row["шаблон"].strip()
        ]


def categorize(description, amount, rules):
    """Перше правило, шаблон якого є в описі, перемагає."""
    text = description.lower()
    for pattern, account, fixed in rules:
        if pattern in text:
            return account, fixed
    return (UNCATEGORIZED_INCOME if amount > 0 else UNCATEGORIZED_EXPENSE), False


def load_rows():
    """Усі завершені операції з усіх CSV у порядку файлів, без повторів."""
    seen = set()
    rows = []
    for path in sorted(DATA_DIR.glob("*.csv")):
        with path.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if row["State"] != "COMPLETED":
                    continue
                if row["Currency"] != CURRENCY:
                    raise SystemExit(f"{path.name}: валюта {row['Currency']} не підтримується: {row}")
                if Decimal(row["Fee"]) != 0:
                    raise SystemExit(f"{path.name}: ненульова комісія, імпорт її ще не вміє: {row}")
                key = (row["Completed Date"], row["Description"], row["Amount"], row["Balance"])
                if key in seen:
                    continue
                seen.add(key)
                rows.append({
                    "date": dt.date.fromisoformat(row["Completed Date"][:10]),
                    "description": row["Description"].strip(),
                    "amount": Decimal(row["Amount"]),
                    "balance": Decimal(row["Balance"]),
                })
    return rows


def quote(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def transaction(row, rules):
    account, fixed = categorize(row["description"], row["amount"], rules)
    tag = " #fixed" if fixed else ""
    return (
        f"{row['date']} * {quote(row['description'])}{tag}\n"
        f"  {BANK_ACCOUNT}  {row['amount']:.2f} {CURRENCY}\n"
        f"  {account}\n"
    )


def balance_assertions(rows):
    """Залишок останнього рядка кожного місяця (у порядку банку).

    Для кожного місяця, крім останнього, твердження стоїть на перше число
    наступного місяця; для останнього — на день після останньої операції.
    """
    last_in_month = {}
    for row in rows:
        last_in_month[(row["date"].year, row["date"].month)] = row
    months = sorted(last_in_month)
    lines = []
    for i, (year, month) in enumerate(months):
        row = last_in_month[(year, month)]
        if i < len(months) - 1:
            date = dt.date(year + month // 12, month % 12 + 1, 1)
        else:
            date = max(r["date"] for r in rows) + dt.timedelta(days=1)
        lines.append(f"{date} balance {BANK_ACCOUNT}  {row['balance']:.2f} {CURRENCY}\n")
    return lines


def main():
    rules = load_rules()
    rows = load_rows()
    parts = [
        "; Згенеровано import.py — руками не правити, файл перезаписується при кожному імпорті.\n\n",
        *(transaction(row, rules) + "\n" for row in rows),
        "; Звірка з банком: залишок з колонки Balance на кінець кожного місяця.\n",
        *balance_assertions(rows),
        "\n",
        manual.beancount_text(manual.read_accounts(), manual.read_operations()),
    ]
    OUTPUT_FILE.write_text("".join(parts), encoding="utf-8", newline="\n")
    uncategorized = sum(
        1 for r in rows
        if categorize(r["description"], r["amount"], rules)[0] in (UNCATEGORIZED_EXPENSE, UNCATEGORIZED_INCOME)
    )
    print(f"Операцій з виписки: {len(rows)}, без категорії: {uncategorized}; "
          f"ручних: {len(manual.read_operations())}. Записано {OUTPUT_FILE.name}.")


if __name__ == "__main__":
    main()
