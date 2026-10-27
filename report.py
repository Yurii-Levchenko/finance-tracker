"""Звіт Олі й Марка: виконує збережені запити 1–3 і 5 окремо для Олі, для Марка і разом
і генерує report.html з перемикачем «Оля / Марко / Разом».

Сторінка українською, без зовнішніх файлів і бібліотек: дані вбудовані в сторінку
як JSON, графіки малює вбудований скрипт у SVG під ширину екрана.

Запуск: uv run python report.py
"""

import calendar
import datetime as dt
import json
import math
import sys
from decimal import ROUND_CEILING, Decimal
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
Q_ACCOUNT = "5 Рух по рахунках"
Q_SHARED = "6 Спільні витрати"
Q_SETTLE = "7 Розрахунки між партнерами"

# Подушка безпеки: ціль — стільки місяців середніх витрат, і дата, до якої Оля хоче її мати
# («до літа» — до 1 червня 2027).
CUSHION_MONTHS = 6
CUSHION_DEADLINE = dt.date(2027, 6, 1)

MONTHS = ["", "січень", "лютий", "березень", "квітень", "травень", "червень",
          "липень", "серпень", "вересень", "жовтень", "листопад", "грудень"]
# «ніж у серпні»
MONTHS_LOC = ["", "січні", "лютому", "березні", "квітні", "травні", "червні",
              "липні", "серпні", "вересні", "жовтні", "листопаді", "грудні"]
# «до 1 червня», «на кінець вересня»
MONTHS_GEN = ["", "січня", "лютого", "березня", "квітня", "травня", "червня",
              "липня", "серпня", "вересня", "жовтня", "листопада", "грудня"]


# ---------- Дані ----------

# Чий звіт: тег власника (#olya / #marko) або None — обидва разом.
VIEWS = [("olya", "Оля"), ("marko", "Марко"), (None, "Разом")]


def for_owner(query, owner):
    """Додає до збереженого запиту фільтр «лише операції цього власника»."""
    return query if owner is None else query.replace("WHERE ", f"WHERE '{owner}' IN tags AND ", 1)


def open_ledger():
    """Завантажує облік; повертає (з'єднання для запитів, {назва: текст збереженого запиту})."""
    entries, errors, options = loader.load_file(str(LEDGER))
    if errors:
        raise SystemExit(f"Облік має помилки, спершу запустіть bean-check: {errors[0].message}")
    saved = {e.name: e.query_string for e in entries if isinstance(e, data.Query)}
    conn = beanquery.connect("beancount:", entries=entries, errors=errors, options=options)
    return conn, saved


def run_saved_queries(conn, saved, owner=None):
    """Виконує збережені запити 1–3 і 5 для одного власника (або для обох); {назва: [рядки як dict]}."""
    results = {}
    for name in (Q_CATEGORIES, Q_SAVINGS, Q_FIXED, Q_ACCOUNT):
        if name not in saved:
            raise SystemExit(f"У main.beancount немає збереженого запиту «{name}».")
        cursor = conn.execute(for_owner(saved[name], owner))
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
            "categories": {},
            "fixed": Decimal(0),
            "variable": Decimal(0),
            "incomplete": False,
            "last_day": last_day,
        }
    for row in results[Q_CATEGORIES]:
        months[row["month"]]["categories"][label(row["account"])] = row["amount"]
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


# ---------- Висновки (по одному реченню на питання) ----------

def categories_conclusion(info, prev, prev_month):
    cats = sorted(info["categories"].items(), key=lambda kv: kv[1], reverse=True)
    top_name, top_value = cats[0]
    text = f"Найбільша стаття — {top_name}: {pct(share(top_value, info['expenses']))} усіх витрат"
    if prev and not info["incomplete"]:
        changes = [(name, value - prev["categories"].get(name, Decimal(0))) for name, value in cats]
        name, diff = max(changes, key=lambda kv: kv[1])
        if diff > 0:
            text += f", а найбільше зросли {name}: на {money(diff)} більше, ніж у {MONTHS_LOC[prev_month.month]}"
    elif len(cats) > 1:
        text += f", далі {cats[1][0]} — {money(cats[1][1])}"
    return text + "."


def savings_conclusion(info, prev):
    if info["rate"] is None:
        return (f"Доходу за цей період ще не було (зарплата приходить наприкінці місяця), тому норму "
                f"заощаджень порахувати не можна. Витрачено {money(info['expenses'])}.")
    text = f"Ви відклали {money(info['saved'])} з {money(info['income'])} доходу"
    if info["incomplete"]:
        text += ", але місяць ще не закінчився, тож норма поки завищена"
    elif prev and prev["rate"] is not None:
        diff = info["rate"] - prev["rate"]
        if diff:
            points = f"{abs(diff):.1f}".replace(".", ",")
            more = "вища" if diff > 0 else "нижча"
            text += f" — норма на {points} п. п. {more}, ніж минулого місяця"
    return text + "."


def fixed_conclusion(info):
    var_share = share(info["variable"], info["expenses"])
    return (f"Змінні витрати — {money(info['variable'])} ({pct(var_share)} від усіх): "
            f"саме їх ви можете скоротити, фіксовані {money(info['fixed'])} лишаються щомісяця.")


# ---------- Подушка безпеки ----------

def add_months(first_day, n):
    year, month = divmod(first_day.month - 1 + n, 12)
    return dt.date(first_day.year + year, month + 1, 1)


def plural(n, one, few, many):
    """1 місяць, 2 місяці, 5 місяців."""
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} {one}"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} {few}"
    return f"{n} {many}"


def cushion(results, months):
    """Подушка = гроші на всіх рахунках (Assets) на кінець останнього повного місяця.

    Посеред неповного місяця на рахунку лежить ще не витрачена зарплата, тож його
    залишок до подушки не рахуємо. Ціль і прогноз — за середніми повних місяців.
    """
    balances, total = {}, Decimal(0)
    for row in results[Q_ACCOUNT]:
        total += row["change"]
        balances[row["month"]] = total
    latest_month = max(balances)
    complete = [(m, info) for m, info in months if not info["incomplete"]]
    if not complete:
        raise SystemExit("Немає жодного повного місяця — прогноз подушки рахувати ні з чого.")
    last_full = complete[-1][0]
    avg_expenses = sum(info["expenses"] for _, info in complete) / len(complete)
    avg_saved = sum(info["saved"] for _, info in complete) / len(complete)
    now = balances[last_full]
    target = CUSHION_MONTHS * avg_expenses
    gap = target - now
    # Скільки місяців від кінця останнього повного до дедлайну (вересень → 1 червня = 8).
    months_left = ((CUSHION_DEADLINE.year - last_full.year) * 12
                   + CUSHION_DEADLINE.month - last_full.month - 1)
    if gap <= 0:
        reach_n = 0
    elif avg_saved > 0:
        reach_n = math.ceil(gap / avg_saved)
    else:
        reach_n = None
    on_track = reach_n is not None and reach_n <= months_left
    # Округлюємо вгору до гроша: з такою сумою на місяць ціль точно досягається вчасно.
    needed = ((gap / months_left).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
              if gap > 0 and months_left > 0 else Decimal(0))

    # Точки графіка: фактичний залишок на кінець кожного повного місяця, далі — прогноз.
    points = [{"key": m.strftime("%Y-%m"), "name": MONTHS[m.month].capitalize(), "year": m.year,
               "value": float(b), "projected": False}
              for m, b in sorted(balances.items()) if m <= last_full]
    horizon = max(months_left, min(reach_n or 0, months_left + 12))
    for k in range(1, horizon + 1):
        m = add_months(last_full, k)
        points.append({"key": m.strftime("%Y-%m"), "name": MONTHS[m.month].capitalize(), "year": m.year,
                       "value": float(now + avg_saved * k), "projected": True})
    reach = add_months(last_full, reach_n) if reach_n is not None else None

    first_m, last_m = complete[0][0], complete[-1][0]
    pace = (f"у {MONTHS_LOC[first_m.month]}–{MONTHS_LOC[last_m.month]}" if first_m != last_m
            else f"у {MONTHS_LOC[last_m.month]}")
    deadline = f"{CUSHION_DEADLINE.day} {MONTHS_GEN[CUSHION_DEADLINE.month]} {CUSHION_DEADLINE.year}"
    if gap <= 0:
        text = f"Ціль уже досягнуто: на рахунку {money(now)}, а потрібно {money(target)}."
    elif on_track:
        early = months_left - reach_n
        text = (f"Якщо відкладати, як {pace} (у середньому {money(avg_saved)} на місяць), "
                f"подушка буде в {MONTHS_LOC[reach.month]} {reach.year}")
        text += f" — на {plural(early, 'місяць', 'місяці', 'місяців')} раніше за {deadline}" if early else f", якраз до {deadline}"
        text += f". Щоб устигнути вчасно, досить відкладати {money(needed)} на місяць."
    else:
        text = (f"За нинішнього темпу ({money(avg_saved)} на місяць) до {deadline} не встигнути: "
                f"щоб устигнути, треба відкладати {money(needed)} на місяць")
        text += f" — на {money(needed - avg_saved)} більше." if avg_saved > 0 else "."
    latest = balances[latest_month]
    latest_op = max(info["last_operation"] for _, info in months)
    return {
        "now": float(now),
        "now_at": f"на кінець {MONTHS_GEN[last_full.month]}",
        "target": float(target),
        "avg_expenses": float(avg_expenses),
        "avg_saved": float(avg_saved),
        "months": CUSHION_MONTHS,
        "have_months": float(now / avg_expenses),
        "needed": float(needed),
        "on_track": on_track,
        "reach": f"{MONTHS[reach.month].capitalize()} {reach.year}" if reach else None,
        "deadline": deadline,
        "deadline_key": add_months(CUSHION_DEADLINE, -1).strftime("%Y-%m"),
        "latest": float(latest) if latest_month != last_full else None,
        "latest_at": f"{latest_op.day:02d}.{latest_op.month:02d}",
        "points": points,
        "conclusion": text,
    }


# ---------- Дані для сторінки ----------

def page_data(months, results):
    totals = {}
    for _, info in months:
        for name, value in info["categories"].items():
            totals[name] = totals.get(name, Decimal(0)) + value
    # Порядок категорій сталий для всіх місяців: за сумою, «Без Категорії» — останньою.
    order = sorted(totals, key=lambda n: (n == "Без Категорії", -totals[n]))
    items = []
    for i, (m, info) in enumerate(months):
        prev_month, prev = months[i - 1] if i else (None, None)
        d = info["last_operation"]
        items.append({
            "key": m.strftime("%Y-%m"),
            "name": MONTHS[m.month].capitalize(),
            "loc": MONTHS_LOC[m.month],
            "gen": MONTHS_GEN[m.month],
            "year": m.year,
            "incomplete": info["incomplete"],
            "until": f"{d.day:02d}.{d.month:02d}" if info["incomplete"] else None,
            "income": float(info["income"]),
            "expenses": float(info["expenses"]),
            "saved": float(info["saved"]),
            # Місяць без доходу (зарплата ще не прийшла) — норму заощаджень порахувати не можна.
            "rate": float(info["rate"]) if info["rate"] is not None else None,
            "fixed": float(info["fixed"]),
            "variable": float(info["variable"]),
            "categories": {n: float(info["categories"].get(n, 0)) for n in order},
            "conclusions": [
                categories_conclusion(info, prev, prev_month),
                savings_conclusion(info, prev),
                fixed_conclusion(info),
            ],
        })
    first = months[0][0]
    last = months[-1][1]["last_operation"]
    return {
        "categories": order,
        "months": items,
        "cushion": cushion(results, months),
        "period": f"01.{first.month:02d}–{last.day:02d}.{last.month:02d}.{last.year}",
    }


# ---------- Сторінка ----------

CSS = """
:root{color-scheme:light;
--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink-2:#52514e;--muted:#898781;
--grid:#e1e0d9;--axis:#c3c2b7;--border:rgba(11,11,11,.10);
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--track:#e9e8e3;
--good:#006300;--bad:#d03b3b;--accent:#2a78d6;--on-accent:#fff}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){color-scheme:dark;
--page:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;--ink-2:#c3c2b7;--muted:#898781;
--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--track:#2a2a28;
--good:#0ca30c;--bad:#e66767;--accent:#3987e5;--on-accent:#fff}}
:root[data-theme="dark"]{color-scheme:dark;
--page:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;--ink-2:#c3c2b7;--muted:#898781;
--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);
--s1:#3987e5;--s2:#d95926;--s3:#199e70;--track:#2a2a28;
--good:#0ca30c;--bad:#e66767;--accent:#3987e5;--on-accent:#fff}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);
font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:780px;margin:0 auto;padding:24px 16px 56px}
header h1{font-size:28px;line-height:1.2;margin:0 0 4px;font-weight:650}
header p{margin:0;color:var(--ink-2)}
.switch{position:sticky;top:0;z-index:5;background:var(--page);padding:14px 0 12px;margin:0 0 6px;
display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.switch button,.who button{font:inherit;font-size:15px;border:1px solid var(--border);background:var(--surface);
color:var(--ink);padding:7px 14px;border-radius:999px;cursor:pointer}
.switch button:hover,.who button:hover{border-color:var(--axis)}
.switch button[aria-pressed=true],.who button[aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
.switch button:focus-visible,.who button:focus-visible,.hit:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.who{display:flex;flex-wrap:wrap;gap:6px;margin:14px 0 0}
.debt-total{font-size:24px;font-weight:650;margin:4px 0 2px}
.lrow{margin:0 0 12px}
.lrow .lab{display:flex;justify-content:space-between;gap:12px;font-size:15px;margin-bottom:4px}
.lrow .lab span:last-child{font-variant-numeric:tabular-nums;color:var(--ink-2)}
.ltrack{height:10px;border-radius:999px;background:var(--track);overflow:hidden}
.ltrack i{display:block;height:100%;border-radius:999px}
.lrow small{font-size:13px}
#debt .tile .v{font-size:20px;white-space:normal}
.switch .note{color:var(--muted);font-size:14px;margin-left:4px}
.tiles{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:0 0 16px}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:16px}
.tile .k{color:var(--ink-2);font-size:14px;margin:0}
.tile .v{font-size:30px;font-weight:650;line-height:1.2;margin:4px 0 2px;white-space:nowrap}
.tile .d{font-size:14px;margin:0;color:var(--ink-2)}
.d .up,.d .down{font-weight:600}
.d .bad{color:var(--bad)} .d .good{color:var(--good)}
.card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:20px;margin:0 0 16px}
.card h2{font-size:18px;line-height:1.35;margin:0 0 4px;font-weight:650}
.card .sub{color:var(--ink-2);font-size:14px;margin:0 0 16px}
.card h3{font-size:15px;font-weight:600;margin:22px 0 10px}
.concl{margin:16px 0 0;padding-top:14px;border-top:1px solid var(--grid)}
.bars{display:grid;gap:8px}
.brow{display:grid;grid-template-columns:minmax(96px,28%) minmax(0,1fr) auto;align-items:center;gap:10px;font-size:14px}
.brow .bar{height:14px;display:block;width:100%}
.brow .val{font-variant-numeric:tabular-nums;white-space:nowrap;text-align:right}
.brow .val small{display:block;color:var(--muted);font-size:12px}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;list-style:none;padding:0;margin:0 0 12px;font-size:14px;color:var(--ink-2)}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:0}
.stack{display:grid;gap:12px}
.srow{cursor:pointer;border-radius:8px;padding:4px 6px;margin:0 -6px}
.srow.sel{background:var(--page)}
.srow .lab{display:flex;flex-wrap:wrap;gap:0 12px;justify-content:space-between;font-size:14px;margin-bottom:4px;color:var(--ink-2)}
.srow.sel .lab{color:var(--ink);font-weight:600}
.segs{display:flex;gap:2px;height:24px}
.seg{height:100%;display:flex;align-items:center;justify-content:center;font-size:12px;color:#fff;
white-space:nowrap;overflow:visible;min-width:2px}
.seg:first-child{border-radius:4px 0 0 4px}.seg:last-child{border-radius:0 4px 4px 0}
.chart{width:100%;display:block;overflow:visible}
.chart text{font-size:12px;fill:var(--muted);font-variant-numeric:tabular-nums}
.chart .lbl{fill:var(--ink);font-size:13px;font-weight:600}
.heat{width:100%;border-collapse:separate;border-spacing:2px;font-size:14px}
.heat th{font-weight:500;color:var(--ink-2);text-align:right;padding:4px 8px}
.heat th:first-child{text-align:left}
.heat th.sel{color:var(--ink);font-weight:650}
.heat td{text-align:right;padding:6px 8px;border-radius:4px;font-variant-numeric:tabular-nums;white-space:nowrap}
.heat td:first-child{text-align:left;color:var(--ink);padding-left:0;white-space:normal}
.heat td.sel{box-shadow:inset 0 0 0 2px var(--ink)}
.scroll{overflow-x:auto}
.scale{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--muted);margin-top:8px}
.scale span{display:inline-block;width:18px;height:10px;border-radius:2px}
#tip{position:fixed;pointer-events:none;z-index:10;background:var(--ink);color:var(--surface);
font-size:13px;line-height:1.4;padding:6px 9px;border-radius:6px;max-width:240px;opacity:0;transition:opacity .1s}
#tip.on{opacity:1}
footer{color:var(--muted);font-size:13px;margin-top:24px}
.card .tiles{margin:16px 0 4px}
.card .tile{background:var(--page)}
.card .tile .v{font-size:24px}
.tile .v.good{color:var(--good)} .tile .v.bad{color:var(--bad)}
.progress{display:flex;justify-content:space-between;gap:12px;font-size:14px;color:var(--ink-2);margin:0 0 6px}
.progress b{color:var(--ink);font-weight:600}
.pbar{height:14px;border-radius:999px;background:var(--track);overflow:hidden}
.pbar i{display:block;height:100%;background:var(--s3);border-radius:999px}
.note2{font-size:14px;color:var(--ink-2);margin:12px 0 0}
@media (max-width:640px){
.tiles{grid-template-columns:1fr}
.tile .v{font-size:26px}
.brow{grid-template-columns:1fr auto}
.brow .bar{grid-column:1/-1;order:3}
.heat{font-size:13px}.heat td,.heat th{padding:5px 4px}
}
"""

JS = r"""
// Дані для кожного звіту: Оля, Марко, разом. D і M — поточний звіт, міняються перемикачем.
const ALL = JSON.parse(document.getElementById('data').textContent);
let D = ALL.views[0], M = D.months;
let sel = M.length - 1;
const $ = s => document.querySelector(s);
const nf = new Intl.NumberFormat('uk-UA', {minimumFractionDigits: 2, maximumFractionDigits: 2});
const money = v => nf.format(v) + ' зл';
const pct = v => v == null ? '—' : v.toFixed(1).replace('.', ',') + ' %';
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const mName = m => m.name + (m.incomplete ? ' (до ' + m.until + ')' : '');
const mShort = m => m.name + (m.incomplete ? '*' : '');
const nf0 = new Intl.NumberFormat('uk-UA', {maximumFractionDigits: 0});
// Послідовна шкала одного відтінку (синій), від світлого до темного.
// Лише світлі кроки, щоб таблиця не була важкою, текст скрізь темний.
const RAMP = ['#e6f0fc', '#cde2fb', '#b7d3f6', '#9ec5f4', '#86b6ef'];

// Зміна відносно минулого місяця. upIsBad: для витрат зростання — погано.
function delta(cur, prev, unit, upIsBad) {
  const m = M[sel], p = M[sel - 1];
  if (!p) return 'перший місяць у даних';
  if (m.incomplete) return 'дані до ' + m.until + ', порівнювати зарано';
  if (cur == null || prev == null) return 'доходу не було, порівнювати нема з чим';
  const d = cur - prev;
  if (Math.abs(d) < 0.005) return 'як у ' + p.loc;
  const up = d > 0, good = up !== upIsBad;
  const val = unit === 'pp' ? Math.abs(d).toFixed(1).replace('.', ',') + ' п. п.' : money(Math.abs(d));
  return `<span class="${up ? 'up' : 'down'} ${good ? 'good' : 'bad'}">${up ? '▲' : '▼'} ${val}</span> ${up ? 'більше' : 'менше'}, ніж у ${p.loc}`;
}

function tiles() {
  const m = M[sel], p = M[sel - 1] || {};
  $('#tiles').innerHTML = [
    ['Витрати за місяць', money(m.expenses), delta(m.expenses, p.expenses, 'money', true)],
    ['Норма заощаджень', pct(m.rate), delta(m.rate, p.rate, 'pp', false)],
    ['Змінні витрати', money(m.variable), delta(m.variable, p.variable, 'money', true)],
  ].map(([k, v, d]) => `<div class="tile"><p class="k">${k}</p><p class="v">${v}</p><p class="d">${d}</p></div>`).join('');
}

// Питання 1: смуги за категоріями для обраного місяця + зміна до минулого.
function categoryBars() {
  const m = M[sel], p = M[sel - 1];
  const rows = D.categories.map(n => [n, m.categories[n]]).filter(r => r[1] > 0).sort((a, b) => b[1] - a[1]);
  const top = rows[0][1];
  $('#bars').innerHTML = rows.map(([n, v]) => {
    const w = Math.max(0.6, v / top * 100);
    let note = Math.round(v / m.expenses * 100) + ' % витрат';
    if (p && !m.incomplete) {
      const d = v - (p.categories[n] || 0);
      if (Math.abs(d) >= 0.005) note += ` · ${d > 0 ? '▲ +' : '▼ −'}${money(Math.abs(d))}`;
    }
    return `<div class="brow hit" tabindex="0" data-tip="${esc(n)}: ${esc(money(v))}<br>${esc(note)}">
      <span>${esc(n)}</span>
      <svg class="bar" viewBox="0 0 100 14" preserveAspectRatio="none" aria-hidden="true">
        <rect width="100" height="14" rx="0" fill="var(--track)" opacity=".6"/>
        <rect width="${w.toFixed(2)}" height="14" fill="var(--s1)"/></svg>
      <span class="val">${money(v)}<small>${esc(note)}</small></span></div>`;
  }).join('');
}

// Питання 1, тенденція: таблиця «категорія × місяць», колір — відносно максимуму рядка.
function heat() {
  const head = '<tr><th>Категорія</th>' + M.map((m, i) => `<th class="${i === sel ? 'sel' : ''}" title="${esc(mName(m))}">${esc(mShort(m))}</th>`).join('') + '</tr>';
  const body = D.categories.map(n => {
    const vals = M.map(m => m.categories[n]);
    const max = Math.max(...vals) || 1;
    return `<tr><td>${esc(n)}</td>` + vals.map((v, i) => {
      const step = v > 0 ? Math.min(RAMP.length - 1, Math.floor(v / max * (RAMP.length - 0.01))) : -1;
      const bg = step < 0 ? 'transparent' : RAMP[step];
      const fg = step < 0 ? 'var(--muted)' : '#0b0b0b';
      return `<td class="hit ${i === sel ? 'sel' : ''}" style="background:${bg};color:${fg}" data-tip="${esc(n)}, ${esc(mName(M[i]))}: ${esc(money(v))}">${v > 0 ? nf0.format(v) : '—'}</td>`;
    }).join('') + '</tr>';
  }).join('');
  $('#heat').innerHTML = `<thead>${head}</thead><tbody>${body}</tbody>`;
}

// Питання 2: лінія норми заощаджень по місяцях.
function rateLine() {
  const box = $('#rate'), W = box.clientWidth, H = 200;
  const L = 36, R = 36, T = 26, B = 30;
  const max = Math.max(60, Math.ceil(Math.max(...M.filter(m => m.rate != null).map(m => m.rate)) / 20) * 20);
  const x = i => M.length === 1 ? (L + W - R) / 2 : L + i * (W - L - R) / (M.length - 1);
  const y = v => T + (1 - v / max) * (H - T - B);
  let s = `<svg class="chart" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="Норма заощаджень по місяцях">`;
  for (let g = 0; g <= max; g += 20) {
    s += `<line x1="${L}" x2="${W - R}" y1="${y(g)}" y2="${y(g)}" stroke="${g ? 'var(--grid)' : 'var(--axis)'}" stroke-width="1"/>`;
    s += `<text x="${L - 8}" y="${y(g) + 4}" text-anchor="end">${g}%</text>`;
  }
  const pts = M.map((m, i) => [x(i), m.rate == null ? null : y(m.rate)]);
  const has = pts.map((p, i) => i).filter(i => pts[i][1] != null);
  if (has.length) s += `<path d="M${pts[has[0]][0]},${y(0)} ${has.map(i => 'L' + pts[i].join(',')).join(' ')} L${pts[has.at(-1)][0]},${y(0)}Z" fill="var(--s1)" opacity=".1"/>`;
  // Відрізок до неповного місяця — пунктир: значення ще зміниться.
  has.forEach((i, k) => {
    if (!k) return;
    const j = has[k - 1], dash = M[i].incomplete ? ' stroke-dasharray="4 4"' : '';
    s += `<line x1="${pts[j][0]}" y1="${pts[j][1]}" x2="${pts[i][0]}" y2="${pts[i][1]}" stroke="var(--s1)" stroke-width="2" stroke-linecap="round"${dash}/>`;
  });
  M.forEach((m, i) => {
    const px = pts[i][0], py = pts[i][1] ?? y(0), r = i === sel ? 6 : 4.5;
    if (pts[i][1] == null) {
      s += `<text x="${px}" y="${y(0) - 8}" text-anchor="middle">без доходу</text>`;
      const name0 = (W - L - R) / M.length < 130 ? mShort(m) : mName(m);
      s += `<text x="${px}" y="${H - 8}" text-anchor="middle"${i === sel ? ' style="fill:var(--ink);font-weight:600"' : ''}>${esc(name0)}</text>`;
      s += `<rect class="hit" data-i="${i}" data-tip="${esc(mName(m))}: доходу не було" x="${px - 30}" y="${T - 10}" width="60" height="${H - T - B + 10}" fill="transparent" style="cursor:pointer"/>`;
      return;
    }
    const fill = m.incomplete ? 'var(--surface)' : 'var(--s1)';
    s += `<circle cx="${px}" cy="${py}" r="${r + 2}" fill="var(--surface)"/>`;
    s += `<circle cx="${px}" cy="${py}" r="${r}" fill="${fill}" stroke="var(--s1)" stroke-width="2"/>`;
    s += `<text class="lbl" x="${px}" y="${py - 12}" text-anchor="middle">${pct(m.rate)}</text>`;
    // На вузькому екрані «(до 15.10)» не влазить — лишаємо назву місяця із зірочкою.
    const name = (W - L - R) / M.length < 130 ? mShort(m) : mName(m);
    s += `<text x="${px}" y="${H - 8}" text-anchor="middle"${i === sel ? ' style="fill:var(--ink);font-weight:600"' : ''}>${esc(name)}</text>`;
    s += `<rect class="hit" data-i="${i}" data-tip="${esc(mName(m))}: норма ${esc(pct(m.rate))}<br>відкладено ${esc(money(m.saved))}" x="${px - 30}" y="${T - 10}" width="60" height="${H - T - B + 10}" fill="transparent" style="cursor:pointer"/>`;
  });
  box.innerHTML = s + '</svg>';
}

// Питання 3: куди йде дохід — фіксовані / змінні / відкладено, по місяцях.
function stack() {
  // Колір тексту всередині сегмента — за яскравістю заливки: на світлому бірюзовому темний.
  const parts = [['Фіксовані', 'fixed', 'var(--s1)', '#fff'], ['Змінні', 'variable', 'var(--s2)', '#fff'], ['Відкладено', 'saved', 'var(--s3)', '#0b0b0b']];
  $('#stack-legend').innerHTML = parts.map(([n, , c]) => `<li><i style="background:${c}"></i>${n}</li>`).join('');
  $('#stack').innerHTML = M.map((m, i) => {
    const segs = !m.income ? '<div class="seg" style="flex:1 0 0;background:var(--track);color:var(--ink-2)">доходу ще не було</div>' : parts.map(([n, k, c, fg]) => {
      const v = m[k], w = v / m.income * 100;
      return `<div class="seg hit" style="flex:${w} 0 0;background:${c};color:${fg}" data-label="${pct(w)}" data-tip="${esc(mName(m))} · ${n}: ${esc(money(v))} (${esc(pct(w))} доходу)"></div>`;
    }).join('');
    return `<div class="srow ${i === sel ? 'sel' : ''}" data-i="${i}"><div class="lab"><span>${esc(mName(m))}</span><span>дохід ${money(m.income)}</span></div><div class="segs">${segs}</div></div>`;
  }).join('');
  // Підпис усередині сегмента — лише якщо вміщається.
  document.querySelectorAll('#stack .seg').forEach(el => {
    if (el.clientWidth > 46) el.textContent = el.dataset.label;
  });
}

function texts() {
  const m = M[sel];
  m.conclusions.forEach((t, i) => { $('#c' + (i + 1)).textContent = t; });
  $('#period').textContent = m.name + ' ' + m.year + (m.incomplete ? ' · дані до ' + m.until : '');
  document.querySelectorAll('.switch button').forEach((b, i) => b.setAttribute('aria-pressed', i === sel));
}

// Подушка безпеки: не залежить від обраного місяця — завжди на останні дані.
function goal() {
  const c = D.cushion, share = Math.min(100, c.now / c.target * 100);
  const have = c.have_months.toFixed(1).replace('.', ',');
  $('#goal-sub').textContent = `Ціль — ${c.months} × середні витрати повного місяця (${money(c.avg_expenses)}) = ${money(c.target)}. Подушка — це гроші на всіх рахунках ${c.now_at}.`;
  $('#goal-progress').innerHTML = `<div class="progress"><span><b>${money(c.now)}</b> — ${have} з ${c.months} місяців витрат</span><span>${Math.round(share)} % цілі</span></div>
    <div class="pbar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(share)}" aria-label="Прогрес до цілі"><i style="width:${share.toFixed(1)}%"></i></div>`;
  const status = c.on_track ? `встигаєте до ${c.deadline}` : `не встигаєте до ${c.deadline}`;
  $('#goal-tiles').innerHTML = [
    ['Є зараз', money(c.now), c.now_at, ''],
    ['Ціль', money(c.target), `${c.months} місяців витрат`, ''],
    ['Буде готова', c.reach || '—', status, c.on_track ? 'good' : 'bad'],
  ].map(([k, v, d, cls]) => `<div class="tile"><p class="k">${k}</p><p class="v ${cls}">${esc(v)}</p><p class="d">${esc(d)}</p></div>`).join('');
  $('#goal-note').textContent = c.latest === null ? '' :
    `На ${c.latest_at} на рахунках уже ${money(c.latest)}, але частину цих грошей ще буде витрачено до кінця місяця, тому до подушки рахуємо залишок ${c.now_at}. Пунктир — прогноз, якщо відкладати по ${money(c.avg_saved)} на місяць.`;
  $('#c0').textContent = c.conclusion;
  goalLine();
}

// Графік подушки: факт (суцільна лінія) і прогноз (пунктир), лінія цілі й позначка дедлайну.
function goalLine() {
  const c = D.cushion, P = c.points, box = $('#goal'), W = box.clientWidth, H = 230;
  const L = 52, R = 40, T = 28, B = 30;
  const step = 10000, max = Math.ceil(Math.max(c.target, ...P.map(p => p.value)) * 1.05 / step) * step;
  const x = i => L + i * (W - L - R) / (P.length - 1);
  const y = v => T + (1 - v / max) * (H - T - B);
  const k = v => v ? nf0.format(v / 1000) + ' тис.' : '0';
  let s = `<svg class="chart" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="Подушка безпеки: факт і прогноз">`;
  for (let g = 0; g <= max; g += step) {
    s += `<line x1="${L}" x2="${W - R}" y1="${y(g)}" y2="${y(g)}" stroke="${g ? 'var(--grid)' : 'var(--axis)'}" stroke-width="1"/>`;
    s += `<text x="${L - 8}" y="${y(g) + 4}" text-anchor="end">${k(g)}</text>`;
  }
  // Дедлайн — вертикальна позначка на кінці місяця перед ним.
  const di = P.findIndex(p => p.key === c.deadline_key);
  if (di >= 0) {
    s += `<line x1="${x(di)}" x2="${x(di)}" y1="${T - 14}" y2="${H - B}" stroke="var(--axis)" stroke-width="1"/>`;
    s += `<text x="${x(di) - 6}" y="${T - 16}" text-anchor="end">${esc(c.deadline)}</text>`;
  }
  // Ціль — горизонтальна пунктирна лінія.
  s += `<line x1="${L}" x2="${W - R}" y1="${y(c.target)}" y2="${y(c.target)}" stroke="var(--s2)" stroke-width="1.5" stroke-dasharray="6 4"/>`;
  s += `<text x="${L + 6}" y="${y(c.target) - 6}" style="fill:var(--s2);font-weight:600">ціль ${nf0.format(c.target)} зл</text>`;
  const pts = P.map((p, i) => [x(i), y(p.value)]);
  pts.forEach((pt, i) => {
    if (!i) return;
    const dash = P[i].projected ? ' stroke-dasharray="4 4"' : '';
    s += `<line x1="${pts[i - 1][0]}" y1="${pts[i - 1][1]}" x2="${pt[0]}" y2="${pt[1]}" stroke="var(--s3)" stroke-width="2" stroke-linecap="round"${dash}/>`;
  });
  // Підписи осі — лише ключові місяці, щоб не налазили: перший, останній факт, досягнення цілі, останній.
  const lastFact = P.findLastIndex(p => !p.projected);
  const ri = P.findIndex(p => p.value >= c.target);
  const marks = [...new Set([0, lastFact, ri, P.length - 1])].filter(i => i >= 0).sort((a, b) => a - b);
  const shown = []; marks.forEach(i => { if (!shown.length || x(i) - x(shown.at(-1)) >= 84) shown.push(i); });
  const first = P[0].year;
  P.forEach((p, i) => {
    const [px, py] = pts[i];
    const fill = p.projected ? 'var(--surface)' : 'var(--s3)';
    s += `<circle cx="${px}" cy="${py}" r="6" fill="var(--surface)"/>`;
    s += `<circle cx="${px}" cy="${py}" r="4" fill="${fill}" stroke="var(--s3)" stroke-width="2"/>`;
    const label = p.name + (p.year !== first ? ' ' + p.year : '');
    if (shown.includes(i)) {
      const anchor = i === 0 ? 'start' : i === P.length - 1 ? 'end' : 'middle';
      s += `<text x="${i === 0 ? px - 4 : i === P.length - 1 ? px + 4 : px}" y="${H - 8}" text-anchor="${anchor}">${esc(label)}</text>`;
    }
    if (i === lastFact) s += `<text class="lbl" x="${px}" y="${py - 12}" text-anchor="middle">${nf0.format(p.value)}</text>`;
    const half = (W - L - R) / (P.length - 1) / 2;
    s += `<rect class="hit" data-tip="${esc(label)}, кінець місяця: ${esc(money(p.value))}${p.projected ? '<br>прогноз' : ''}" x="${px - half}" y="${T}" width="${half * 2}" height="${H - T - B}" fill="transparent"/>`;
  });
  box.innerHTML = s + '</svg>';
}

// Хто кому винен: спільні витрати (#shared) навпіл мінус перекази між партнерами.
// Борг > 0 — Марко винен Олі, < 0 — Оля винна Марку. Не залежить від перемикача «чий звіт».
function debt() {
  const SH = ALL.shared || {}, m = M[sel], keys = Object.keys(SH).sort();
  const box = $('#debt');
  if (!keys.length) { box.hidden = true; return; }
  box.hidden = false;
  const zero = {olya: 0, marko: 0, settled: 0};
  const owe = v => (v.olya - v.marko) / 2 - v.settled;
  const cur = SH[m.key] || zero, monthDebt = owe(cur);
  const total = keys.filter(k => k <= m.key).reduce((a, k) => a + owe(SH[k]), 0);
  const who = d => Math.abs(d) < 0.005 ? 'Ви в розрахунку' : d > 0 ? `Марко винен Олі ${money(d)}` : `Оля винна Марку ${money(-d)}`;
  const sum = cur.olya + cur.marko;
  $('#debt-total').textContent = who(total);
  $('#debt-since').textContent = `загалом з 1 ${M[0].gen} до ${m.incomplete ? m.until : 'кінця ' + m.gen}`;
  $('#debt-month').innerHTML = `<div class="tile"><p class="k">Оля заплатила</p><p class="v">${money(cur.olya)}</p></div>` +
    `<div class="tile"><p class="k">Марко заплатив</p><p class="v">${money(cur.marko)}</p></div>` +
    `<div class="tile"><p class="k">За ${esc(mName(m).toLowerCase())}</p><p class="v">${esc(who(monthDebt).replace('Ви в розрахунку', '0 зл'))}</p></div>`;
  $('#debt-note').textContent = `Спільні за ${m.name.toLowerCase()}: ${money(sum)}, тобто по ${money(sum / 2)} на кожного` +
    (Math.abs(cur.settled) >= 0.005 ? `; переказано між вами ${money(Math.abs(cur.settled))}.` : '. Переказів між вами цього місяця не було.');
}

// Ліміти обраного звіту: Оля / Марко — свої, «Разом» — сума обох.
function limitsCard() {
  const L = ALL.limits || {}, box = $('#limits'), m = M[sel];
  let lim = {};
  if (D.key === 'all') { for (const o of Object.values(L)) for (const [c, v] of Object.entries(o)) lim[c] = (lim[c] || 0) + v; }
  else lim = L[D.key] || {};
  const cats = Object.keys(lim).sort((a, b) => (m.categories[b] || 0) / lim[b] - (m.categories[a] || 0) / lim[a]);
  box.hidden = !cats.length;
  if (!cats.length) return;
  const [y, mo] = m.key.split('-').map(Number), days = new Date(y, mo, 0).getDate();
  const daysLeft = m.incomplete ? Math.max(1, days - parseInt(m.until)) : 0;
  $('#limits-sub').textContent = m.incomplete ? `${mName(m)}: лишилося ${daysLeft} дн. до кінця місяця` : mName(m);
  $('#limits-rows').innerHTML = cats.map(c => {
    const spent = m.categories[c] || 0, cap = lim[c], pct = spent / cap;
    const color = pct >= 1 ? 'var(--bad)' : pct >= 0.8 ? 'var(--s2)' : 'var(--s3)';
    const left = cap - spent;
    const note = left < 0 ? `перевищено на ${money(-left)}` :
      m.incomplete ? `лишилося ${money(left)}, ≈ ${money(left / daysLeft)} на день` : `лишилося ${money(left)}`;
    return `<div class="lrow"><div class="lab"><span>${esc(c)}</span><span>${money(spent)} з ${money(cap)}</span></div>
      <div class="ltrack"><i style="width:${Math.min(100, pct * 100).toFixed(1)}%;background:${color}"></i></div>
      <small style="color:${pct >= 1 ? 'var(--bad)' : 'var(--ink-2)'}">${note}</small></div>`;
  }).join('');
}

function render() { texts(); tiles(); limitsCard(); debt(); categoryBars(); heat(); rateLine(); stack(); }

function select(i) { sel = i; render(); }

function monthSwitch() {
  $('#switch').innerHTML = M.map((m, i) =>
    `<button type="button" data-i="${i}">${esc(m.name)}${m.incomplete ? ' *' : ''}</button>`).join('') +
    (M.some(m => m.incomplete) ? `<span class="note">* неповний місяць</span>` : '');
}

// Перемикач «чий звіт». Обраний місяць зберігаємо, якщо він є і в іншому звіті.
$('#who').innerHTML = ALL.views.map(v => `<button type="button" data-view="${v.key}">${esc(v.label)}</button>`).join('');
function setView(key) {
  const v = ALL.views.find(x => x.key === key) || ALL.views[0];
  const month = M[sel] && M[sel].key;
  D = v; M = v.months;
  const i = M.findIndex(m => m.key === month);
  sel = i >= 0 ? i : M.length - 1;
  document.querySelectorAll('#who button').forEach(b => b.setAttribute('aria-pressed', b.dataset.view === v.key));
  $('#whose').textContent = v.key === 'all' ? 'Оля й Марко разом' : v.label;
  try { localStorage.setItem('view', v.key); } catch (e) {}
  monthSwitch(); goal(); render();
}

document.addEventListener('click', e => {
  const v = e.target.closest('[data-view]');
  if (v) { setView(v.dataset.view); return; }
  const t = e.target.closest('[data-i]');
  if (t) select(+t.dataset.i);
});

// Підказка при наведенні або фокусі.
const tip = $('#tip');
function showTip(el, x, y) {
  tip.innerHTML = el.dataset.tip; tip.classList.add('on');
  const r = tip.getBoundingClientRect();
  tip.style.left = Math.min(window.innerWidth - r.width - 8, x + 12) + 'px';
  tip.style.top = Math.max(8, y - r.height - 12) + 'px';
}
document.addEventListener('pointermove', e => {
  const el = e.target.closest('[data-tip]');
  el ? showTip(el, e.clientX, e.clientY) : tip.classList.remove('on');
});
document.addEventListener('focusin', e => {
  const el = e.target.closest('[data-tip]');
  if (el) { const r = el.getBoundingClientRect(); showTip(el, r.left, r.top); }
});
document.addEventListener('focusout', () => tip.classList.remove('on'));

let rt; window.addEventListener('resize', () => { clearTimeout(rt); rt = setTimeout(() => { goalLine(); rateLine(); stack(); }, 120); });
let saved = null;
try { saved = localStorage.getItem('view'); } catch (e) {}
setView((location.hash || '').slice(1) || window.DEFAULT_VIEW || saved || ALL.views[0].key);
"""

PAGE = """<!doctype html>
<html lang="uk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Фінанси Олі й Марка</title>
<style>__CSS__</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Фінанси: <span id="whose">Оля</span></h1>
    <p>Щомісячний звіт за __PERIOD__ · <span id="period"></span></p>
    <nav class="who" id="who" aria-label="Чий звіт"></nav>
  </header>

  <article class="card" style="margin-top:20px">
    <h2>Чи встигаю зібрати подушку безпеки до літа?</h2>
    <p class="sub" id="goal-sub"></p>
    <div id="goal-progress"></div>
    <section class="tiles" id="goal-tiles" aria-label="Подушка безпеки"></section>
    <div id="goal"></div>
    <p class="note2" id="goal-note"></p>
    <p class="concl" id="c0"></p>
  </article>

  <nav class="switch" id="switch" aria-label="Місяць"></nav>

  <article class="card" id="debt" hidden>
    <h2>Хто кому винен за спільне</h2>
    <p class="sub">Оренда, продукти, світло й домашній інтернет — навпіл. Перекази між вами закривають борг.</p>
    <p class="debt-total" id="debt-total"></p>
    <p class="sub" id="debt-since"></p>
    <section class="tiles" id="debt-month" aria-label="Спільні витрати за місяць"></section>
    <p class="concl" id="debt-note"></p>
  </article>

  <section class="tiles" id="tiles" aria-label="Головні цифри"></section>

  <article class="card" id="limits" hidden>
    <h2>Ліміти на місяць</h2>
    <p class="sub" id="limits-sub"></p>
    <div id="limits-rows"></div>
  </article>

  <article class="card">
    <h2>1. Скільки грошей іде на кожну категорію?</h2>
    <p class="sub">Витрати обраного місяця за категоріями; під сумою — частка і зміна до минулого місяця.</p>
    <div class="bars" id="bars"></div>
    <h3>Тенденція за категоріями, зл</h3>
    <div class="scroll"><table class="heat" id="heat"></table></div>
    <div class="scale" aria-hidden="true">менше <span style="background:#e6f0fc"></span><span style="background:#b7d3f6"></span><span style="background:#9ec5f4"></span><span style="background:#86b6ef"></span> більше (у межах рядка)</div>
    <p class="concl" id="c1"></p>
  </article>

  <article class="card">
    <h2>2. Яку частку доходу я відкладаю?</h2>
    <p class="sub">Норма заощаджень = (дохід − витрати) / дохід. Порожня точка і пунктир — неповний місяць.</p>
    <div id="rate"></div>
    <p class="concl" id="c2"></p>
  </article>

  <article class="card">
    <h2>3. Скільки витрат фіксовані, а скільки — під моїм контролем?</h2>
    <p class="sub">Куди йде кожен злотий доходу: фіксовані витрати, змінні (ними можна керувати) і відкладене.</p>
    <ul class="legend" id="stack-legend"></ul>
    <div class="stack" id="stack"></div>
    <p class="concl" id="c3"></p>
  </article>

  <footer>Згенеровано report.py з main.beancount __GENERATED__. Цифри — збережені запити 1–3 і 5; натисніть місяць на графіку, щоб перейти до нього.</footer>
</div>
<div id="tip" role="tooltip"></div>
<script type="application/json" id="data">__DATA__</script>
<script>__JS__</script>
</body>
</html>
"""


def shared_summary(conn, saved):
    """Хто скільки заплатив за спільне (тег #shared) і скільки переказали одне одному, по місяцях.

    Кожен переказ між партнерами видно в обох виписках (у платника +, в отримувача −),
    тому сума «від Марка до Олі» = (Марко − Оля) / 2.
    """
    def by_month(name, owner, column):
        cursor = conn.execute(for_owner(saved[name], owner))
        cols = [c.name for c in cursor.description]
        return {f"{r['month']:%Y-%m}": float(r[column]) for r in (dict(zip(cols, row)) for row in cursor.fetchall())}
    for name in (Q_SHARED, Q_SETTLE):
        if name not in saved:
            raise SystemExit(f"У main.beancount немає збереженого запиту «{name}».")
    paid = {o: by_month(Q_SHARED, o, "shared") for o in ("olya", "marko")}
    sent = {o: by_month(Q_SETTLE, o, "settled") for o in ("olya", "marko")}
    keys = sorted(set().union(*paid.values(), *sent.values()))
    return {k: {"olya": paid["olya"].get(k, 0.0), "marko": paid["marko"].get(k, 0.0),
                "settled": (sent["marko"].get(k, 0.0) - sent["olya"].get(k, 0.0)) / 2} for k in keys}


LIMITS_FILE = ROOT / "limits.csv"


def read_limits():
    """{власник: {назва категорії у звіті: ліміт}} з limits.csv (його правлять форми app.py)."""
    import csv
    out = {}
    if LIMITS_FILE.exists():
        with LIMITS_FILE.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                if r.get("ліміт"):
                    out.setdefault(r["хто"], {})[label(r["категорія"])] = float(r["ліміт"])
    return out


def render(views, shared=None):
    """views — [{key, label, …page_data}] для Олі, Марка і разом; перший — звіт за замовчуванням."""
    payload = json.dumps({"views": views, "shared": shared or {}, "limits": read_limits()},
                         ensure_ascii=False).replace("</", "<\\/")
    return (PAGE.replace("__CSS__", CSS)
                .replace("__JS__", JS)
                .replace("__DATA__", payload)
                .replace("__PERIOD__", views[0]["period"])
                .replace("__GENERATED__", dt.date.today().strftime("%d.%m.%Y")))


def main():
    conn, saved = open_ledger()
    views = []
    for owner, label in VIEWS:
        results = run_saved_queries(conn, saved, owner)
        months = build_months(results)
        views.append({"key": owner or "all", "label": label, **page_data(months, results)})
        print(f"— {label}")
        for m, info in months:
            mark = " (неповний)" if info["incomplete"] else ""
            print(f"  {m:%Y-%m}{mark}: витрати {info['expenses']}, норма {info['rate']} %, "
                  f"фіксовані {info['fixed']}, змінні {info['variable']}")
        c = cushion(results, months)
        print(f"  Подушка {c['now_at']}: {c['now']:.2f} з {c['target']:.2f}, "
              f"буде: {c['reach'] or '—'}, треба на місяць: {c['needed']:.2f}")
    shared = shared_summary(conn, saved)
    for k, v in shared.items():
        print(f"  Спільне {k}: Оля {v['olya']:.2f}, Марко {v['marko']:.2f}, переказ Марко→Оля {v['settled']:.2f}")
    OUTPUT.write_text(render(views, shared), encoding="utf-8")
    print(f"Записано {OUTPUT.name}.")


if __name__ == "__main__":
    main()
