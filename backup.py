"""Резервні копії даних, яких немає в git: ручні витрати, рахунки, ліміти, виписки.

Запуск: python backup.py   → ~/finance-backups/finance-РРРР-ММ-ДД.zip, зберігає 30 останніх.
На PythonAnywhere — щоденна задача (вкладка Tasks). Ту саму копію можна завантажити
зі сторінки «Додати витрату» (кнопка «Завантажити резервну копію»), щоб мати її поза сервером.

Ключі й паролі (~/.tracker.env) у копію не потрапляють свідомо.
Відновлення: розпакувати архів у папку проєкту й запустити python check.py.
"""

import datetime as dt
import io
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent
FILES = ["manual.csv", "accounts.csv", "limits.csv", "rules.csv"]
KEEP = 30


def build_zip():
    """Архів з даними трекера (байти). Порожні або відсутні файли просто пропускаються."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for name in FILES:
            path = ROOT / name
            if path.exists():
                z.write(path, name)
        for path in sorted((ROOT / "data").glob("*.csv")):
            z.write(path, f"data/{path.name}")
    return buffer.getvalue()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    target = Path.home() / "finance-backups"
    target.mkdir(exist_ok=True)
    path = target / f"finance-{dt.date.today()}.zip"
    path.write_bytes(build_zip())
    old = sorted(target.glob("finance-*.zip"))[:-KEEP]
    for p in old:
        p.unlink()
    print(f"Записано {path} ({path.stat().st_size} байт); видалено старих: {len(old)}.")


if __name__ == "__main__":
    main()
