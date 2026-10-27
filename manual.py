"""Ручні дані Олі: власні рахунки, джерела доходу й операції поза банківською випискою.

accounts.csv — рахунки (готівка, інша картка) зі стартовим залишком і джерела доходу.
manual.csv   — ручні операції: витрати й доходи з цих рахунків.

Обидва файли править сторінка app.py (або Excel); import.py перетворює їх на Beancount
і дописує в transactions.beancount разом з випискою.
"""

import csv
import datetime as dt
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).parent
ACCOUNTS_FILE = ROOT / "accounts.csv"
MANUAL_FILE = ROOT / "manual.csv"

# «власник» / «хто» — тег власника (olya / marko): хто додав рахунок чи операцію.
ACCOUNT_FIELDS = ["рахунок", "тип", "назва", "відкрито", "стартовий залишок", "власник"]
MANUAL_FIELDS = ["id", "дата", "тип", "опис", "сума", "категорія", "рахунок", "фіксована", "хто", "спільна"]
OWNERS = ("olya", "marko")

BANK_ACCOUNTS = ("Assets:Банк:Поточний", "Assets:Банк:Марко")
OPENING_ACCOUNT = "Equity:Початковий-Залишок"
CURRENCY = "PLN"
# Тип рахунку в accounts.csv → корінь у Beancount.
ROOTS = {"рахунок": "Assets", "дохід": "Income"}


# ---------- Читання і запис CSV ----------

def _read(path, fields):
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return [{k: (row.get(k) or "").strip() for k in fields} for row in csv.DictReader(f)]


def _write(path, fields, rows):
    # utf-8-sig — щоб Excel відкривав українські літери без «кракозябр».
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def read_accounts():
    return _read(ACCOUNTS_FILE, ACCOUNT_FIELDS)


def write_accounts(rows):
    _write(ACCOUNTS_FILE, ACCOUNT_FIELDS, rows)


def read_operations():
    return _read(MANUAL_FILE, MANUAL_FIELDS)


def write_operations(rows):
    _write(MANUAL_FILE, MANUAL_FIELDS, sorted(rows, key=lambda r: (r["дата"], int(r["id"]))))


# ---------- Перевірки ----------

def account_name(kind, title):
    """«Картка Mono» + рахунок → Assets:Картка-Mono (слова через дефіс, без апострофів)."""
    words = re.sub(r"[ʼ'’`\"]", "", title).replace(":", " ").split()
    words = [re.sub(r"[^\w-]", "", w) for w in words]
    words = [w[0].upper() + w[1:] for w in words if w]
    if not words:
        raise ValueError("Назва порожня.")
    return f"{ROOTS[kind]}:{'-'.join(words)}"


def parse_date(text):
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"Дата «{text}» має бути у форматі РРРР-ММ-ДД.") from None


def parse_amount(text, allow_zero=False):
    try:
        value = Decimal(text.replace(",", ".").replace(" ", "").replace(" ", ""))
    except InvalidOperation:
        raise ValueError(f"Сума «{text}» — не число.") from None
    if value < 0 or (value == 0 and not allow_zero):
        raise ValueError("Сума має бути більшою за нуль.")
    return value.quantize(Decimal("0.01"))


def readable(account):
    """Assets:Готівка-Вдома → «Готівка Вдома» — для повідомлень людині."""
    return "«" + account.split(":", 1)[-1].replace(":", " · ").replace("-", " ") + "»"


def validate(accounts, operations, known):
    """Перевіряє обидва списки. known — рахунки з main.beancount: {назва: дата відкриття}.

    Повертає текст першої помилки або None.
    """
    opened = dict(known)
    try:
        for a in accounts:
            if a["тип"] not in ROOTS:
                return f"Невідомий тип рахунку «{a['тип']}»."
            if a["рахунок"] in known:
                return f"{readable(a['рахунок'])} уже є в основному обліку."
            opened[a["рахунок"]] = parse_date(a["відкрито"])
            if a["тип"] == "рахунок":
                parse_amount(a["стартовий залишок"] or "0", allow_zero=True)
        names = [a["рахунок"] for a in accounts]
        if len(names) != len(set(names)):
            return "Дві однакові назви рахунків."
        for op in operations:
            date = parse_date(op["дата"])
            parse_amount(op["сума"])
            if op["тип"] not in ("витрата", "дохід"):
                return f"Невідомий тип операції «{op['тип']}»."
            root = "Expenses" if op["тип"] == "витрата" else "Income"
            if not op["категорія"].startswith(root + ":") or op["категорія"] not in opened:
                kind = "витрати" if root == "Expenses" else "доходу"
                return f"{readable(op['категорія'])} не підходить як категорія {kind}."
            if not op["рахунок"].startswith("Assets:") or op["рахунок"] not in opened:
                return f"Рахунок {readable(op['рахунок'])} не знайдено — оберіть інший."
            if op["рахунок"] in BANK_ACCOUNTS:
                return "Операції банківського рахунку беруться з виписки — вручну їх не додаємо."
            for account in (op["категорія"], op["рахунок"]):
                if date < opened[account]:
                    return f"{readable(account)} відкрито {opened[account]}, а операція датована {date}."
            if not op["опис"]:
                return "Опис операції порожній."
            if op.get("хто") and op["хто"] not in OWNERS:
                return f"Невідомий власник «{op['хто']}»."
    except ValueError as e:
        return str(e)
    return None


# ---------- Beancount ----------

def quote(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def owner_tag(owner):
    return f" #{owner}" if owner in OWNERS else ""


def beancount_text(accounts, operations):
    """Відкриття ручних рахунків, їхні стартові залишки й ручні операції (тег #manual і тег власника)."""
    if not accounts and not operations:
        return ""
    parts = ["; Ручні дані з accounts.csv і manual.csv (сторінка app.py).\n\n"]
    for a in accounts:
        parts.append(f"{a['відкрито']} open {a['рахунок']}  {CURRENCY}\n")
        start = parse_amount(a["стартовий залишок"] or "0", allow_zero=True) if a["тип"] == "рахунок" else 0
        if start:
            parts.append(
                f"{a['відкрито']} * {quote('Стартовий залишок: ' + a['назва'])} #manual{owner_tag(a.get('власник'))}\n"
                f"  {a['рахунок']}  {start:.2f} {CURRENCY}\n"
                f"  {OPENING_ACCOUNT}\n"
            )
        parts.append("\n")
    for op in operations:
        amount = parse_amount(op["сума"])
        sign = -1 if op["тип"] == "витрата" else 1
        tags = (" #manual" + owner_tag(op.get("хто")) + (" #fixed" if op["фіксована"] == "так" else "")
                + (" #shared" if op.get("спільна") == "так" else ""))
        parts.append(
            f"{op['дата']} * {quote(op['опис'])}{tags}\n"
            f"  {op['рахунок']}  {sign * amount:.2f} {CURRENCY}\n"
            f"  {op['категорія']}\n\n"
        )
    return "".join(parts)
