"""Звіт Олі: виконує збережені запити 1–3 з main.beancount і генерує report.html.

Сторінка українською, без зовнішніх файлів і бібліотек: дані вбудовані в сторінку
як JSON, графіки малює вбудований скрипт у SVG під ширину екрана.

Запуск: uv run python report.py
"""

import calendar
import datetime as dt
import json
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
# «ніж у серпні»
MONTHS_LOC = ["", "січні", "лютому", "березні", "квітні", "травні", "червні",
              "липні", "серпні", "вересні", "жовтні", "листопаді", "грудні"]


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
    text = f"Ви відклали {money(info['saved'])} з {money(info['income'])} доходу"
    if info["incomplete"]:
        text += ", але місяць ще не закінчився, тож норма поки завищена"
    elif prev:
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


# ---------- Дані для сторінки ----------

def page_data(months):
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
            "year": m.year,
            "incomplete": info["incomplete"],
            "until": f"{d.day:02d}.{d.month:02d}" if info["incomplete"] else None,
            "income": float(info["income"]),
            "expenses": float(info["expenses"]),
            "saved": float(info["saved"]),
            "rate": float(info["rate"]),
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
.switch button{font:inherit;font-size:15px;border:1px solid var(--border);background:var(--surface);
color:var(--ink);padding:7px 14px;border-radius:999px;cursor:pointer}
.switch button:hover{border-color:var(--axis)}
.switch button[aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
.switch button:focus-visible,.hit:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
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
@media (max-width:640px){
.tiles{grid-template-columns:1fr}
.tile .v{font-size:26px}
.brow{grid-template-columns:1fr auto}
.brow .bar{grid-column:1/-1;order:3}
.heat{font-size:13px}.heat td,.heat th{padding:5px 4px}
}
"""

JS = r"""
const D = JSON.parse(document.getElementById('data').textContent);
const M = D.months;
let sel = M.length - 1;
const $ = s => document.querySelector(s);
const nf = new Intl.NumberFormat('uk-UA', {minimumFractionDigits: 2, maximumFractionDigits: 2});
const money = v => nf.format(v) + ' зл';
const pct = v => v.toFixed(1).replace('.', ',') + ' %';
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
  const max = Math.max(60, Math.ceil(Math.max(...M.map(m => m.rate)) / 20) * 20);
  const x = i => M.length === 1 ? (L + W - R) / 2 : L + i * (W - L - R) / (M.length - 1);
  const y = v => T + (1 - v / max) * (H - T - B);
  let s = `<svg class="chart" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="Норма заощаджень по місяцях">`;
  for (let g = 0; g <= max; g += 20) {
    s += `<line x1="${L}" x2="${W - R}" y1="${y(g)}" y2="${y(g)}" stroke="${g ? 'var(--grid)' : 'var(--axis)'}" stroke-width="1"/>`;
    s += `<text x="${L - 8}" y="${y(g) + 4}" text-anchor="end">${g}%</text>`;
  }
  const pts = M.map((m, i) => [x(i), y(m.rate)]);
  s += `<path d="M${pts[0][0]},${y(0)} ${pts.map(p => 'L' + p.join(',')).join(' ')} L${pts.at(-1)[0]},${y(0)}Z" fill="var(--s1)" opacity=".1"/>`;
  // Відрізок до неповного місяця — пунктир: значення ще зміниться.
  pts.forEach((p, i) => {
    if (!i) return;
    const dash = M[i].incomplete ? ' stroke-dasharray="4 4"' : '';
    s += `<line x1="${pts[i - 1][0]}" y1="${pts[i - 1][1]}" x2="${p[0]}" y2="${p[1]}" stroke="var(--s1)" stroke-width="2" stroke-linecap="round"${dash}/>`;
  });
  M.forEach((m, i) => {
    const [px, py] = pts[i], r = i === sel ? 6 : 4.5;
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
    const segs = parts.map(([n, k, c, fg]) => {
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

function render() { texts(); tiles(); categoryBars(); heat(); rateLine(); stack(); }

function select(i) { sel = i; render(); }

$('#switch').innerHTML = M.map((m, i) =>
  `<button type="button" data-i="${i}">${esc(m.name)}${m.incomplete ? ' *' : ''}</button>`).join('') +
  (M.some(m => m.incomplete) ? `<span class="note">* неповний місяць</span>` : '');
document.addEventListener('click', e => {
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

let rt; window.addEventListener('resize', () => { clearTimeout(rt); rt = setTimeout(() => { rateLine(); stack(); }, 120); });
render();
"""

PAGE = """<!doctype html>
<html lang="uk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Фінанси Олі</title>
<style>__CSS__</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Фінанси Олі</h1>
    <p>Щомісячний звіт за __PERIOD__ · <span id="period"></span></p>
  </header>

  <nav class="switch" id="switch" aria-label="Місяць"></nav>

  <section class="tiles" id="tiles" aria-label="Головні цифри"></section>

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

  <footer>Згенеровано report.py з main.beancount __GENERATED__. Цифри — збережені запити 1–3; натисніть місяць на графіку, щоб перейти до нього.</footer>
</div>
<div id="tip" role="tooltip"></div>
<script type="application/json" id="data">__DATA__</script>
<script>__JS__</script>
</body>
</html>
"""


def render(months):
    info = page_data(months)
    payload = json.dumps(info, ensure_ascii=False).replace("</", "<\\/")
    return (PAGE.replace("__CSS__", CSS)
                .replace("__JS__", JS)
                .replace("__DATA__", payload)
                .replace("__PERIOD__", info["period"])
                .replace("__GENERATED__", dt.date.today().strftime("%d.%m.%Y")))


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
