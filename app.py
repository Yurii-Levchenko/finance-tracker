"""Локальна сторінка Олі: звіт плюс форми для ручних операцій, рахунків і джерел доходу.

Запуск: uv run python app.py → http://127.0.0.1:5055

Форми правлять accounts.csv і manual.csv. Після кожного збереження сторінка заново
запускає import.py, перевіряє облік (як bean-check) і перегенеровує report.html.
Якщо облік після зміни має помилку, зміна скасовується і сторінка показує чому.
Працює лише на цьому компʼютері (127.0.0.1) і лише на стандартному Python.
"""

import datetime as dt
import importlib.util
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from beancount import loader

import manual
import report

sys.stdout.reconfigure(encoding="utf-8")

HOST, PORT = "127.0.0.1", 5055
ROOT = manual.ROOT
LEDGER = ROOT / "main.beancount"
LOCK = threading.Lock()

# import — зарезервоване слово Python, тому import.py підвантажуємо за шляхом.
_spec = importlib.util.spec_from_file_location("bank_import", ROOT / "import.py")
bank_import = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bank_import)


# ---------- Облік ----------

def main_accounts():
    """Рахунки, відкриті в main.beancount: {назва: дата відкриття}."""
    text = LEDGER.read_text(encoding="utf-8")
    return {m[2]: dt.date.fromisoformat(m[1])
            for m in re.finditer(r"^(\d{4}-\d{2}-\d{2}) open (\S+)", text, re.M)}


def rebuild():
    """import.py → перевірка обліку → report.py. Повертає текст помилки або None."""
    bank_import.main()
    _, errors, _ = loader.load_file(str(LEDGER))
    if errors:
        return errors[0].message
    report.main()
    return None


def state():
    known = main_accounts()
    accounts = manual.read_accounts()
    mine = [a["рахунок"] for a in accounts]
    return {
        "accounts": accounts,
        "operations": manual.read_operations(),
        "builtin": [{"рахунок": a, "відкрито": str(d)} for a, d in known.items()
                    if a.split(":")[0] in ("Assets", "Income")],
        "expense": [a for a in known if a.startswith("Expenses:") and a != "Expenses:Без-Категорії"],
        "income": [a for a in known if a.startswith("Income:")] + [a for a in mine if a.startswith("Income:")],
        "assets": [a for a in mine if a.startswith("Assets:")],
        "today": str(dt.date.today()),
    }


def apply(accounts, operations):
    """Перевіряє, записує й перераховує; якщо облік зламався — повертає старі файли."""
    error = manual.validate(accounts, operations, main_accounts())
    if error:
        return error
    old_accounts, old_ops = manual.read_accounts(), manual.read_operations()
    manual.write_accounts(accounts)
    manual.write_operations(operations)
    error = rebuild()
    if error:
        manual.write_accounts(old_accounts)
        manual.write_operations(old_ops)
        rebuild()
        return f"Облік не пройшов перевірку, зміну скасовано: {error}"
    return None


# ---------- Дії форм ----------

def save_operation(body):
    ops = manual.read_operations()
    op = {k: str(body.get(k, "")).strip() for k in manual.MANUAL_FIELDS}
    op["фіксована"] = "так" if op["тип"] == "витрата" and body.get("фіксована") in (True, "так") else "ні"
    if op["сума"]:
        try:
            op["сума"] = f"{manual.parse_amount(op['сума']):.2f}"
        except ValueError as e:
            return str(e)
    if op["id"]:
        if not any(o["id"] == op["id"] for o in ops):
            return "Операцію не знайдено — оновіть сторінку."
        ops = [op if o["id"] == op["id"] else o for o in ops]
    else:
        op["id"] = str(max((int(o["id"]) for o in ops), default=0) + 1)
        ops.append(op)
    return apply(manual.read_accounts(), ops)


def delete_operation(body):
    ops = manual.read_operations()
    return apply(manual.read_accounts(), [o for o in ops if o["id"] != str(body.get("id"))])


def save_account(body):
    accounts, ops = manual.read_accounts(), manual.read_operations()
    kind = str(body.get("тип", "")).strip()
    title = str(body.get("назва", "")).strip()
    if kind not in manual.ROOTS:
        return "Оберіть тип: рахунок або джерело доходу."
    try:
        name = manual.account_name(kind, title)
    except ValueError as e:
        return str(e)
    start = str(body.get("стартовий залишок", "")).strip() if kind == "рахунок" else ""
    if start:
        try:
            start = f"{manual.parse_amount(start, allow_zero=True):.2f}"
        except ValueError as e:
            return str(e)
    row = {"рахунок": name, "тип": kind, "назва": title,
           "відкрито": str(body.get("відкрито", "")).strip(), "стартовий залишок": start}
    old = str(body.get("old", "")).strip()
    if old:
        if not any(a["рахунок"] == old for a in accounts):
            return "Рахунок не знайдено — оновіть сторінку."
        if name != old and any(o["рахунок"] == old or o["категорія"] == old for o in ops) \
                and name.split(":")[0] != old.split(":")[0]:
            return "Тип рахунку з операціями змінити не можна."
        accounts = [row if a["рахунок"] == old else a for a in accounts]
        # Перейменування — оновлюємо посилання в ручних операціях.
        for o in ops:
            for k in ("рахунок", "категорія"):
                if o[k] == old:
                    o[k] = name
    else:
        accounts.append(row)
    return apply(accounts, ops)


def delete_account(body):
    name = str(body.get("рахунок", ""))
    ops = manual.read_operations()
    used = sum(1 for o in ops if name in (o["рахунок"], o["категорія"]))
    if used:
        return f"На {name} є ручні операції ({used}) — спершу видаліть або перенесіть їх."
    return apply([a for a in manual.read_accounts() if a["рахунок"] != name], ops)


ACTIONS = {
    "/api/operation": save_operation,
    "/api/operation/delete": delete_operation,
    "/api/account": save_account,
    "/api/account/delete": delete_account,
}


# ---------- Сервер ----------

NAV = ('<nav class="appnav"><a href="/">Звіт</a><a href="/manage">Ручні операції і рахунки</a></nav>')
NAV_CSS = (".appnav{display:flex;gap:6px;margin:0 0 16px;font-size:15px}"
           ".appnav a{color:var(--ink);text-decoration:none;border:1px solid var(--border);"
           "background:var(--surface);padding:6px 12px;border-radius:999px}"
           ".appnav a[aria-current]{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}")


def report_page():
    html = report.OUTPUT.read_text(encoding="utf-8")
    nav = NAV.replace('href="/"', 'href="/" aria-current="page"')
    return (html.replace("</style>", NAV_CSS + "</style>", 1)
                .replace('<div class="wrap">', '<div class="wrap">' + nav, 1))


class Handler(BaseHTTPRequestHandler):
    def send(self, code, body, ctype):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, obj, code=200):
        self.send(code, json.dumps(obj, ensure_ascii=False), "application/json")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self.send(200, report_page(), "text/html")
        elif path == "/manage":
            nav = NAV.replace('href="/manage"', 'href="/manage" aria-current="page"')
            page = (MANAGE.replace("__CSS__", report.CSS + NAV_CSS + MANAGE_CSS)
                          .replace("__NAV__", nav))
            self.send(200, page, "text/html")
        elif path == "/api/state":
            with LOCK:
                self.send_json(state())
        else:
            self.send(404, "Не знайдено", "text/plain")

    def do_POST(self):
        action = ACTIONS.get(self.path)
        # Лише JSON-запити зі своєї ж сторінки: чужий сайт не зможе нічого записати.
        if not action or not self.headers.get("Content-Type", "").startswith("application/json"):
            return self.send_json({"error": "Невідомий запит."}, 400)
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except json.JSONDecodeError:
            return self.send_json({"error": "Некоректні дані."}, 400)
        with LOCK:
            error = action(body)
            self.send_json({"error": error, "state": state()}, 400 if error else 200)

    def log_message(self, fmt, *args):
        pass


# ---------- Сторінка «Ручні операції і рахунки» ----------

MANAGE_CSS = """
form.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px 14px}
form .full{grid-column:1/-1}
label{display:flex;flex-direction:column;gap:4px;font-size:14px;color:var(--ink-2)}
input,select{font:inherit;font-size:16px;color:var(--ink);background:var(--page);border:1px solid var(--axis);
border-radius:8px;padding:8px 10px;min-width:0}
input:focus-visible,select:focus-visible,button:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.check{flex-direction:row;align-items:center;gap:8px;color:var(--ink)}
.check input{width:18px;height:18px}
.seg2{display:flex;gap:6px}
.seg2 label{flex-direction:row;align-items:center;gap:6px;border:1px solid var(--border);border-radius:999px;
padding:6px 12px;color:var(--ink);cursor:pointer}
.seg2 label:has(input:checked){border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}
.btns{display:flex;gap:8px;flex-wrap:wrap}
button{font:inherit;font-size:15px;border-radius:999px;padding:8px 16px;cursor:pointer;
border:1px solid var(--border);background:var(--surface);color:var(--ink)}
button.primary{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
button.link{border:none;background:none;padding:4px 6px;color:var(--accent)}
button.link.danger{color:var(--bad)}
.hint{font-size:13px;color:var(--muted);margin:0}
#msg{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);z-index:20;max-width:min(560px,calc(100% - 32px));
margin:0;padding:10px 16px;border-radius:10px;font-size:15px;background:var(--ink);color:var(--surface);
box-shadow:0 4px 16px rgba(0,0,0,.18);opacity:0;pointer-events:none;transition:opacity .15s}
#msg.on{opacity:1;pointer-events:auto}
#msg.err{background:var(--bad);color:#fff}
button:disabled{opacity:.45;cursor:not-allowed}
table.list{width:100%;border-collapse:collapse;font-size:14px}
.list th{text-align:left;font-weight:500;color:var(--ink-2);padding:6px 8px;border-bottom:1px solid var(--grid)}
.list td{padding:8px;border-bottom:1px solid var(--grid);vertical-align:top}
.list td.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.list .neg{color:var(--ink)} .list .pos{color:var(--good)}
.list tr.editing td{background:var(--page)}
.tag{display:inline-block;font-size:12px;color:var(--ink-2);border:1px solid var(--border);border-radius:999px;padding:0 7px;margin-left:4px}
.empty{color:var(--muted);font-size:14px;margin:0}
@media (max-width:640px){form.grid{grid-template-columns:1fr}
.list thead{display:none}.list tr{display:grid;grid-template-columns:1fr auto;padding:6px 0;border-bottom:1px solid var(--grid)}
.list td{border:none;padding:2px 0}.list td.acts{grid-column:1/-1}}
"""

MANAGE = """<!doctype html>
<html lang="uk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ручні операції</title>
<style>__CSS__</style>
</head>
<body>
<div class="wrap">
__NAV__
<header><h1>Ручні операції і рахунки</h1>
<p>Те, чого немає у банківській виписці: готівка, інша картка, інші джерела доходу. Після збереження звіт перераховується сам.</p></header>
<p id="msg" role="status" aria-live="polite"></p>

<article class="card" style="margin-top:8px">
  <h2 id="op-title">Додати операцію</h2>
  <p class="sub" id="op-sub">Операції банківського рахунку беруться з виписки — тут лише інші рахунки.</p>
  <form class="grid" id="op-form">
    <div class="full seg2" role="radiogroup" aria-label="Тип операції">
      <label><input type="radio" name="тип" value="витрата" checked> Витрата</label>
      <label><input type="radio" name="тип" value="дохід"> Дохід</label>
    </div>
    <label>Дата<input type="date" name="дата" required></label>
    <label>Сума, зл<input name="сума" inputmode="decimal" placeholder="45,00" required></label>
    <label class="full">Опис<input name="опис" placeholder="Наприклад: ринок, фріланс-проєкт" required maxlength="120"></label>
    <label><span id="cat-label">Категорія</span><select name="категорія" required></select></label>
    <label><span id="acc-label">З якого рахунку</span><select name="рахунок" required></select></label>
    <label class="check full" id="fixed-box"><input type="checkbox" name="фіксована"> Фіксована витрата (щомісяця однакова)</label>
    <div class="full btns">
      <button class="primary" type="submit" id="op-save">Додати</button>
      <button type="button" id="op-cancel" hidden>Скасувати</button>
    </div>
    <p class="hint full" id="op-hint"></p>
  </form>
</article>

<article class="card">
  <h2>Ручні операції</h2>
  <div class="scroll" id="ops"></div>
</article>

<article class="card">
  <h2 id="acc-title">Рахунки і джерела доходу</h2>
  <p class="sub">Рахунок — де лежать гроші (готівка, інша картка); вони рахуються в подушку безпеки. Джерело доходу — звідки гроші приходять.</p>
  <div class="scroll" id="accs"></div>
  <h3 id="acc-form-title">Додати</h3>
  <form class="grid" id="acc-form">
    <div class="full seg2" role="radiogroup" aria-label="Що додаємо">
      <label><input type="radio" name="тип" value="рахунок" checked> Рахунок</label>
      <label><input type="radio" name="тип" value="дохід"> Джерело доходу</label>
    </div>
    <label>Назва<input name="назва" placeholder="Готівка" required maxlength="40"></label>
    <label>Відкрито з<input type="date" name="відкрито" required></label>
    <label id="start-box">Скільки було на рахунку на цю дату, зл<input name="стартовий залишок" inputmode="decimal" placeholder="0,00"></label>
    <p class="hint full" id="acc-preview"></p>
    <div class="full btns">
      <button class="primary" type="submit" id="acc-save">Додати</button>
      <button type="button" id="acc-cancel" hidden>Скасувати</button>
    </div>
  </form>
</article>
</div>

<script>
const $ = s => document.querySelector(s);
const nf = new Intl.NumberFormat('uk-UA', {minimumFractionDigits: 2, maximumFractionDigits: 2});
const money = v => nf.format(v) + ' зл';
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const short = a => a.split(':').slice(1).join(' · ').replaceAll('-', ' ');
let S = null, editOp = null, editAcc = null;

let sayT;
function say(text, ok) {
  const m = $('#msg'); m.textContent = text; m.className = 'on ' + (ok ? 'ok' : 'err');
  clearTimeout(sayT); sayT = setTimeout(() => m.className = '', ok ? 3500 : 8000);
}

async function post(url, body, okText) {
  const r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const j = await r.json();
  if (j.state) { S = j.state; }
  if (j.error) { say(j.error, false); return false; }
  say(okText + ' Звіт перераховано.', true); return true;
}

const opForm = $('#op-form'), accForm = $('#acc-form');
const val = (f, n) => f.elements[n].type === 'checkbox' ? f.elements[n].checked : f.elements[n].value;
const opKind = () => opForm.querySelector('[name="тип"]:checked').value;

function options(sel, list, current) {
  sel.innerHTML = list.length ? list.map(a => `<option value="${esc(a)}"${a === current ? ' selected' : ''}>${esc(short(a))}</option>`).join('')
                              : '<option value="">— немає —</option>';
}

function syncOpForm(keep) {
  const k = opKind();
  options(opForm.elements['категорія'], k === 'витрата' ? S.expense : S.income, keep && keep['категорія']);
  options(opForm.elements['рахунок'], S.assets, keep && keep['рахунок']);
  $('#cat-label').textContent = k === 'витрата' ? 'Категорія' : 'Джерело доходу';
  $('#acc-label').textContent = k === 'витрата' ? 'З якого рахунку' : 'На який рахунок';
  $('#fixed-box').hidden = k !== 'витрата';
  const none = !S.assets.length;
  $('#op-hint').textContent = none ? 'Спершу додайте рахунок (наприклад, «Готівка») у блоці «Рахунки і джерела доходу» нижче.' : '';
  $('#op-save').disabled = none;
}

function renderOps() {
  const ops = [...S.operations].sort((a, b) => b['дата'].localeCompare(a['дата']) || b.id - a.id);
  $('#ops').innerHTML = ops.length ? `<table class="list"><thead><tr><th>Дата</th><th>Опис</th><th>Категорія</th><th>Рахунок</th><th style="text-align:right">Сума</th><th></th></tr></thead><tbody>` +
    ops.map(o => {
      const neg = o['тип'] === 'витрата';
      return `<tr class="${editOp === o.id ? 'editing' : ''}"><td>${esc(o['дата'])}</td><td>${esc(o['опис'])}${o['фіксована'] === 'так' ? '<span class="tag">фіксована</span>' : ''}</td>
        <td>${esc(short(o['категорія']))}</td><td>${esc(short(o['рахунок']))}</td>
        <td class="num ${neg ? 'neg' : 'pos'}">${neg ? '−' : '+'}${money(+o['сума'])}</td>
        <td class="acts"><button class="link" data-edit-op="${o.id}">Змінити</button><button class="link danger" data-del-op="${o.id}">Видалити</button></td></tr>`;
    }).join('') + '</tbody></table>' : '<p class="empty">Поки немає жодної ручної операції.</p>';
}

function renderAccs() {
  const kind = a => a.startsWith('Assets:') ? 'рахунок' : 'джерело доходу';
  const rows = S.builtin.map(b => `<tr><td>${esc(short(b['рахунок']))}</td><td>${kind(b['рахунок'])}</td><td>${esc(b['відкрито'])}</td><td class="num">—</td>
      <td class="acts"><span class="hint">${b['рахунок'].startsWith('Assets:') ? 'з виписки' : 'з виписки, правила в rules.csv'}</span></td></tr>`)
    .concat(S.accounts.map(a => `<tr class="${editAcc === a['рахунок'] ? 'editing' : ''}"><td>${esc(a['назва'])}</td><td>${kind(a['рахунок'])}</td><td>${esc(a['відкрито'])}</td>
      <td class="num">${a['тип'] === 'рахунок' ? money(+(a['стартовий залишок'] || 0)) : '—'}</td>
      <td class="acts"><button class="link" data-edit-acc="${esc(a['рахунок'])}">Змінити</button><button class="link danger" data-del-acc="${esc(a['рахунок'])}">Видалити</button></td></tr>`));
  $('#accs').innerHTML = `<table class="list"><thead><tr><th>Назва</th><th>Тип</th><th>Відкрито</th><th style="text-align:right">Стартовий залишок</th><th></th></tr></thead><tbody>${rows.join('')}</tbody></table>`;
}

function syncAccForm() {
  const k = accForm.querySelector('[name="тип"]:checked').value;
  $('#start-box').hidden = k !== 'рахунок';
  const name = accForm.elements['назва'].value.trim();
  $('#acc-preview').textContent = name ? 'У Beancount: ' + (k === 'рахунок' ? 'Assets:' : 'Income:') +
    name.replace(/[ʼ'’`"]/g, '').split(/\\s+/).filter(Boolean).map(w => w[0].toUpperCase() + w.slice(1)).join('-') : '';
}

function resetOp() {
  editOp = null; opForm.reset(); opForm.elements['дата'].value = S.today;
  $('#op-title').textContent = 'Додати операцію'; $('#op-save').textContent = 'Додати'; $('#op-cancel').hidden = true;
  syncOpForm(); renderOps();
}
function resetAcc() {
  editAcc = null; accForm.reset(); accForm.elements['відкрито'].value = S.today;
  $('#acc-form-title').textContent = 'Додати'; $('#acc-save').textContent = 'Додати'; $('#acc-cancel').hidden = true;
  syncAccForm(); renderAccs();
}
function renderAll() { syncOpForm(editOp && S.operations.find(o => o.id === editOp)); renderOps(); renderAccs(); }

opForm.addEventListener('change', e => { if (e.target.name === 'тип') syncOpForm(); });
accForm.addEventListener('input', syncAccForm);
$('#op-cancel').onclick = resetOp;
$('#acc-cancel').onclick = resetAcc;

opForm.addEventListener('submit', async e => {
  e.preventDefault();
  const body = {id: editOp || ''};
  ['тип', 'дата', 'сума', 'опис', 'категорія', 'рахунок', 'фіксована'].forEach(n => body[n] = n === 'тип' ? opKind() : val(opForm, n));
  if (await post('/api/operation', body, editOp ? 'Операцію змінено.' : 'Операцію додано.')) resetOp(); else renderAll();
});
accForm.addEventListener('submit', async e => {
  e.preventDefault();
  const body = {old: editAcc || '', 'тип': accForm.querySelector('[name="тип"]:checked').value,
    'назва': val(accForm, 'назва'), 'відкрито': val(accForm, 'відкрито'), 'стартовий залишок': val(accForm, 'стартовий залишок')};
  if (await post('/api/account', body, editAcc ? 'Зміни збережено.' : 'Додано.')) { resetAcc(); syncOpForm(); } else renderAll();
});

document.addEventListener('click', async e => {
  const t = e.target;
  if (t.dataset.editOp) {
    const o = S.operations.find(x => x.id === t.dataset.editOp); editOp = o.id;
    opForm.querySelector(`[name="тип"][value="${o['тип']}"]`).checked = true;
    syncOpForm(o);
    ['дата', 'опис'].forEach(n => opForm.elements[n].value = o[n]);
    opForm.elements['сума'].value = String(o['сума']).replace('.', ',');
    opForm.elements['фіксована'].checked = o['фіксована'] === 'так';
    $('#op-title').textContent = 'Змінити операцію'; $('#op-save').textContent = 'Зберегти зміни'; $('#op-cancel').hidden = false;
    renderOps(); opForm.scrollIntoView({behavior: 'smooth', block: 'start'}); opForm.elements['сума'].focus({preventScroll: true});
  } else if (t.dataset.delOp) {
    const o = S.operations.find(x => x.id === t.dataset.delOp);
    if (confirm(`Видалити «${o['опис']}» від ${o['дата']}?`) && await post('/api/operation/delete', {id: o.id}, 'Операцію видалено.')) {
      if (editOp === o.id) resetOp(); else renderOps();
    }
  } else if (t.dataset.editAcc) {
    const a = S.accounts.find(x => x['рахунок'] === t.dataset.editAcc); editAcc = a['рахунок'];
    accForm.querySelector(`[name="тип"][value="${a['тип']}"]`).checked = true;
    accForm.elements['назва'].value = a['назва']; accForm.elements['відкрито'].value = a['відкрито'];
    accForm.elements['стартовий залишок'].value = (a['стартовий залишок'] || '').replace('.', ',');
    $('#acc-form-title').textContent = 'Змінити: ' + a['назва']; $('#acc-save').textContent = 'Зберегти зміни'; $('#acc-cancel').hidden = false;
    syncAccForm(); renderAccs(); accForm.scrollIntoView({behavior: 'smooth', block: 'start'});
  } else if (t.dataset.delAcc) {
    const a = S.accounts.find(x => x['рахунок'] === t.dataset.delAcc);
    if (confirm(`Видалити «${a['назва']}»?`) && await post('/api/account/delete', {'рахунок': a['рахунок']}, 'Видалено.')) {
      if (editAcc === a['рахунок']) resetAcc(); else renderAccs();
      syncOpForm();
    }
  }
});

fetch('/api/state').then(r => r.json()).then(j => { S = j; resetOp(); resetAcc(); });
</script>
</body>
</html>
"""


def main():
    error = rebuild()
    if error:
        raise SystemExit(f"Облік має помилку, спершу виправте її: {error}")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Відкрийте http://{HOST}:{PORT} — звіт і форми. Зупинити: Ctrl+C.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
