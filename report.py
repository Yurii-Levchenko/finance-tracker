"""Звіт Олі: виконує збережені запити 1–3 з main.beancount і генерує report.html.

Сторінка українською, без зовнішніх файлів і бібліотек: графіки — вбудований SVG.

Запуск: uv run python report.py
"""

import calendar
import datetime as dt
import html
import sys
from decimal import Decimal
from pathlib import Path

import beanquery
from beancount import loader
from beancount.core import data

# Консоль Windows за замовчуванням не друкує українські літери.
sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).parent
LEDGER = ROOT / "main.beancount"
OUTPUT = ROOT / "report.html"

Q_CATEGORIES = "1 Витрати за категоріями"
Q_SAVINGS = "2 Норма заощаджень"
Q_FIXED = "3 Фіксовані і змінні"

MONTHS = ["", "січень", "лютий", "березень", "квітень", "травень", "червень",
          "липень", "серпень", "вересень", "жовтень", "листопад", "грудень"]


# ---------- Дані ----------

def run_saved_queries():
    """Виконує збережені запити з main.beancount; повертає {назва: [рядки як dict]}."""
    entries, errors, options = loader.load_file(str(LEDGER))
    if errors:
        raise SystemExit(f"Облік має помилки, спершу запустіть bean-check: {errors[0].message}")
    saved = {e.name: e.query_string for e in entries if isinstance(e, data.Query)}
    conn = beanquery.connect("beancount:", entries=entries, errors=errors, options=options)
    results = {}
    for name in (Q_CATEGORIES, Q_SAVINGS, Q_FIXED):
        if name not in saved:
            raise SystemExit(f"У main.beancount немає збереженого запиту «{name}».")
        cursor = conn.execute(saved[name])
        columns = [c.name for c in cursor.description]
        results[name] = [dict(zip(columns, row)) for row in cursor.fetchall()]
    return results


def build_months(results):
    """Збирає дані по місяцях у порядку часу."""
    months = {}
    for row in results[Q_SAVINGS]:
        month = row["month"]
        last_day = dt.date(month.year, month.month, calendar.monthrange(month.year, month.month)[1])
        months[month] = {
            "income": row["income"],
            "expenses": row["expenses"],
            "saved": row["saved"],
            "rate": row["rate_pct"],
            "last_operation": row["last_operation"],
            "categories": [],
            "fixed": Decimal(0),
            "variable": Decimal(0),
            "incomplete": False,
            "last_day": last_day,
        }
    for row in results[Q_CATEGORIES]:
        months[row["month"]]["categories"].append((label(row["account"]), row["amount"]))
    for row in results[Q_FIXED]:
        months[row["month"]]["fixed"] = row["fixed"]
        months[row["month"]]["variable"] = row["variable"]
    ordered = sorted(months)
    # Неповний — останній місяць, у якому дані закінчуються раніше за кінець місяця.
    last = months[ordered[-1]]
    last["incomplete"] = last["last_operation"] < last["last_day"]
    return [(m, months[m]) for m in ordered]


# ---------- Форматування ----------

def label(account):
    return account.split(":")[-1].replace("-", " ")


def money(value):
    """5703.7 → «5 703,70 зл» (тонкий нерозривний пробіл між тисячами)."""
    text = f"{value:,.2f}".replace(",", " ").replace(".", ",")
    return f"{text} зл"


def pct(value):
    return f"{value:.1f}".replace(".", ",") + " %"


def share(part, total):
    return part * 100 / total if total else Decimal(0)


def month_title(month, info):
    title = f"{MONTHS[month.month]} {month.year}"
    if info["incomplete"]:
        d = info["last_operation"]
        title += f" · дані до {d.day:02d}.{d.month:02d}"
    return title


def esc(text):
    return html.escape(str(text))


# ---------- Графіки (вбудований SVG) ----------

# Смуги малюємо SVG, а підписи — звичайним текстом, щоб на телефоні вони не дрібнішали.

def bars_chart(items):
    """Горизонтальні смуги за категоріями: [(підпис, сума)]."""
    top = max(v for _, v in items) or 1
    rows = []
    for name, value in items:
        w = max(0.5, float(value / top) * 100)
        rows.append(
            f'<div class="row"><span class="lbl">{esc(name)}</span>'
            f'<svg viewBox="0 0 100 10" preserveAspectRatio="none" aria-hidden="true">'
            f'<rect width="{w:.2f}" height="10" class="bar"/></svg>'
            f'<span class="val">{esc(money(value))}</span></div>'
        )
    return f'<div class="bars" role="img">{"".join(rows)}</div>'


def split_chart(parts_spec):
    """Одна смуга, поділена на частини, і легенда під нею: [(підпис, сума, клас)]."""
    total = sum(v for _, v, _ in parts_spec) or 1
    rects, legend, x = [], [], 0.0
    for name, value, cls in parts_spec:
        w = float(value / total) * 100
        rects.append(f'<rect x="{x:.2f}" width="{w:.2f}" height="10" class="{cls}"/>')
        legend.append(
            f'<li><i class="{cls}"></i>{esc(name)}: {esc(money(value))} · {esc(pct(share(value, total)))}</li>'
        )
        x += w
    return (f'<svg class="split" viewBox="0 0 100 10" preserveAspectRatio="none" aria-hidden="true">'
            f'{"".join(rects)}</svg><ul class="legend">{"".join(legend)}</ul>')


# ---------- Висновки ----------

def categories_conclusion(info):
    cats = info["categories"]
    top_name, top_value = cats[0]
    text = f"Найбільша стаття — {top_name}: {pct(share(top_value, info['expenses']))} усіх витрат"
    if len(cats) > 1:
        second_name, second_value = cats[1]
        text += f", далі {second_name} — {money(second_value)}"
    return text + "."


def savings_conclusion(info, prev):
    text = f"Ви відклали {money(info['saved'])} з {money(info['income'])} доходу"
    if info["incomplete"]:
        text += ", але місяць ще не закінчився, тож норма поки завищена"
    elif prev:
        diff = info["rate"] - prev["rate"]
        if diff:
            more = "більше" if diff > 0 else "менше"
            points = f"{abs(diff):.1f}".replace(".", ",")
            text += f" — норма на {points} п. п. {more}, ніж минулого місяця"
    return text + "."


def fixed_conclusion(info):
    var_share = share(info["variable"], info["expenses"])
    return (f"Змінні витрати — {money(info['variable'])} ({pct(var_share)} від усіх): "
            f"саме їх ви можете скоротити, фіксовані {money(info['fixed'])} лишаються щомісяця.")


# ---------- Сторінка ----------

def month_section(month, info, prev):
    key = month.strftime("%Y-%m")
    return f"""
<section class="month" id="m-{key}" data-month="{key}" hidden>
  <p class="period">{esc(month_title(month, info))}</p>

  <article class="card">
    <h2>1. Скільки грошей іде на кожну категорію?</h2>
    <p class="big">{esc(money(info['expenses']))}</p>
    <p class="sub">усього витрат за місяць</p>
    {bars_chart(info['categories'])}
    <p class="concl">{esc(categories_conclusion(info))}</p>
  </article>

  <article class="card">
    <h2>2. Яку частку доходу я відкладаю?</h2>
    <p class="big">{esc(pct(info['rate']))}</p>
    <p class="sub">норма заощаджень: відкладено {esc(money(info['saved']))}</p>
    {split_chart([("Витрати", info['expenses'], "seg-a"), ("Відкладено", info['saved'], "seg-b")])}
    <p class="concl">{esc(savings_conclusion(info, prev))}</p>
  </article>

  <article class="card">
    <h2>3. Скільки витрат фіксовані, а скільки — під моїм контролем?</h2>
    <p class="big">{esc(money(info['variable']))}</p>
    <p class="sub">змінні витрати, якими можна керувати</p>
    {split_chart([("Фіксовані", info['fixed'], "seg-c"), ("Змінні", info['variable'], "seg-b")])}
    <p class="concl">{esc(fixed_conclusion(info))}</p>
  </article>
</section>"""


CSS = """
:root{--bg:#f6f5f2;--card:#fff;--ink:#1d1d1f;--muted:#6b6b70;--line:#e3e1dc;
--a:#c9c6bf;--b:#2f7d5b;--c:#8a8579;--accent:#2f7d5b;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#21211f;--ink:#f1f0ec;
--muted:#a3a29c;--line:#34332f;--a:#4a4843;--b:#5cbf8f;--c:#8f8a7e;--accent:#5cbf8f}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif}
.wrap{max-width:720px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:26px;margin:0 0 4px}
.lead{color:var(--muted);margin:0 0 20px}
nav{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:8px}
nav button{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--ink);
padding:8px 14px;border-radius:999px;cursor:pointer}
nav button[aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:#fff}
.period{color:var(--muted);margin:8px 0 16px;font-size:15px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;margin:0 0 16px}
h2{font-size:17px;margin:0 0 10px}
.big{font-size:34px;font-weight:700;margin:0;font-variant-numeric:tabular-nums}
.sub{color:var(--muted);margin:0 0 14px;font-size:14px}
.concl{margin:14px 0 0}
.bars{display:grid;gap:6px}
.row{display:grid;grid-template-columns:minmax(90px,30%) 1fr auto;align-items:center;gap:10px;font-size:14px}
.row svg{width:100%;height:14px;display:block}
.val{color:var(--muted);font-variant-numeric:tabular-nums;white-space:nowrap}
.split{width:100%;height:26px;display:block;border-radius:6px}
.legend{list-style:none;padding:0;margin:10px 0 0;font-size:14px;display:grid;gap:4px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:8px}
.bar,.seg-b{fill:var(--b)} .seg-a{fill:var(--a)} .seg-c{fill:var(--c)}
i.seg-a{background:var(--a)} i.seg-b{background:var(--b)} i.seg-c{background:var(--c)}
@media (max-width:420px){.row{grid-template-columns:1fr auto}.row svg{grid-column:1/-1;order:3}}
footer{color:var(--muted);font-size:13px;margin-top:24px}
"""

JS = """
const buttons=[...document.querySelectorAll('nav button')];
function show(key){
  document.querySelectorAll('.month').forEach(s=>s.hidden=s.dataset.month!==key);
  buttons.forEach(b=>b.setAttribute('aria-pressed',b.dataset.month===key));
}
buttons.forEach(b=>b.addEventListener('click',()=>show(b.dataset.month)));
show(buttons[buttons.length-1].dataset.month);
"""


def render(months):
    nav = "".join(
        f'<button type="button" data-month="{m.strftime("%Y-%m")}" aria-pressed="false">'
        f'{esc(MONTHS[m.month].capitalize())}{" *" if info["incomplete"] else ""}</button>'
        for m, info in months
    )
    sections = "".join(
        month_section(m, info, months[i - 1][1] if i else None)
        for i, (m, info) in enumerate(months)
    )
    note = ""
    incomplete = [(m, i) for m, i in months if i["incomplete"]]
    if incomplete:
        m, i = incomplete[0]
        d = i["last_operation"]
        note = f"<p class=\"lead\">* {esc(MONTHS[m.month].capitalize())} неповний: дані до {d.day:02d}.{d.month:02d}.{d.year}.</p>"
    generated = dt.date.today().strftime("%d.%m.%Y")
    return f"""<!doctype html>
<html lang="uk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Фінанси Олі</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <h1>Фінанси Олі</h1>
  <p class="lead">Щомісячний звіт: категорії, норма заощаджень, фіксовані й змінні витрати.</p>
  <nav aria-label="Місяць">{nav}</nav>
  {note}
  {sections}
  <footer>Згенеровано report.py з main.beancount {generated}. Цифри — збережені запити 1–3.</footer>
</div>
<script>{JS}</script>
</body>
</html>
"""


def main():
    months = build_months(run_saved_queries())
    OUTPUT.write_text(render(months), encoding="utf-8")
    for m, info in months:
        mark = " (неповний)" if info["incomplete"] else ""
        print(f"{m:%Y-%m}{mark}: витрати {info['expenses']}, норма {info['rate']} %, "
              f"фіксовані {info['fixed']}, змінні {info['variable']}")
    print(f"Записано {OUTPUT.name}.")


if __name__ == "__main__":
    main()
