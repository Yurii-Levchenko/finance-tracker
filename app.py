"""Трекер Олі й Марка: звіт плюс форми для ручних операцій, з входом за паролем.

WSGI-застосунок на стандартному Python (без Flask), тож працює і локально, і на PythonAnywhere.

  Локально:  uv run python app.py            → http://127.0.0.1:5055
  Хеш пароля: uv run python app.py hash       → рядок для TRACKER_USERS
  Налаштування: python app.py setup          → генерує паролі й ключ у ~/.tracker.env
  Сервер:    WSGI-файл імпортує `application` з цього модуля (див. README).

Паролі й ключі — лише в змінних середовища (або у файлі ~/.tracker.env поза репозиторієм):
  TRACKER_USERS  = olya=<хеш>,marko=<хеш>   (хеш дає `app.py hash`, самого пароля ніде немає)
  TRACKER_SECRET = випадковий рядок для підпису сесійних cookie

Без входу сторінка не показує жодних даних. Форми правлять accounts.csv і manual.csv; після
кожного збереження запускаються import.py, перевірка обліку (як bean-check) і report.py,
а якщо облік зламався, зміна скасовується.
"""

import datetime as dt
import getpass
import hashlib
import hmac
import importlib.util
import json
import os
import re
import secrets
import sys
import threading
import struct
import time
import zlib
from http import cookies
from pathlib import Path
from urllib.parse import parse_qs
from wsgiref.simple_server import WSGIRequestHandler, make_server

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


REPORT_STALE = True  # звіт треба перерахувати перед наступним показом


def rebuild(report_now=True):
    """import.py → перевірка обліку (як bean-check) → report.py. Повертає текст помилки або None.

    report_now=False: звіт не перераховуємо одразу (це ~3 с), а позначаємо застарілим —
    його перерахує report_page, коли звіт справді відкриють. Збереження витрати лишається миттєвим.
    """
    global REPORT_STALE
    bank_import.main()
    _, errors, _ = loader.load_file(str(LEDGER))
    if errors:
        return errors[0].message
    if report_now:
        report.main()
        REPORT_STALE = False
    else:
        REPORT_STALE = True
    return None


# Тег власника → як його показувати і його рахунок готівки в main.beancount.
NAMES = {"olya": "Оля", "marko": "Марко"}
CASH = {"olya": "Assets:Готівка:Оля", "marko": "Assets:Готівка:Марко"}


TX_RE = re.compile(r'^\d{4}-\d{2}-\d{2} \* "(?:[^"\\]|\\.)*"([^\n]*)\n  \S+\s+-?[\d.]+ PLN\n  (Expenses:\S+)', re.M)
_usage_cache = {}


def category_usage(user):
    """Скільки витрат у кожній категорії має цей власник (з transactions.beancount, кешується за часом зміни)."""
    path = ROOT / "transactions.beancount"
    if not path.exists():
        return {}
    key = (user, path.stat().st_mtime)
    if key not in _usage_cache:
        counts = {}
        for tags, category in TX_RE.findall(path.read_text(encoding="utf-8")):
            if f"#{user}" in tags.split():
                counts[category] = counts.get(category, 0) + 1
        _usage_cache.clear()
        _usage_cache[key] = counts
    return _usage_cache[key]


SPEND_RE = re.compile(r'^(\d{4}-\d{2})-\d{2} \* "(?:[^"\\]|\\.)*"([^\n]*)\n  \S+\s+(-?[\d.]+) PLN\n  (Expenses:\S+)', re.M)
_spend_cache = {}
LIMITS_FILE = ROOT / "limits.csv"
LIMIT_FIELDS = ["хто", "категорія", "ліміт"]


def spend_by_month(user):
    """{місяць: {категорія: витрачено}} для власника — з transactions.beancount (банк + готівка)."""
    path = ROOT / "transactions.beancount"
    if not path.exists():
        return {}
    key = (user, path.stat().st_mtime)
    if key not in _spend_cache:
        out = {}
        for month, tags, amount, category in SPEND_RE.findall(path.read_text(encoding="utf-8")):
            if f"#{user}" in tags.split():
                cats = out.setdefault(month, {})
                cats[category] = round(cats.get(category, 0) - float(amount), 2)
        _spend_cache.clear()
        _spend_cache[key] = out
    return _spend_cache[key]


def read_limits():
    return manual._read(LIMITS_FILE, LIMIT_FIELDS)


def user_limits(user):
    return {r["категорія"]: float(r["ліміт"]) for r in read_limits() if r["хто"] == user and r["ліміт"]}


def save_limits(body, user):
    """Ліміти користувача: {категорія: сума або порожньо}. Порожнє — ліміту немає."""
    global REPORT_STALE
    expense = set(state(user)["expense"])
    mine = {}
    for category, value in (body.get("limits") or {}).items():
        value = str(value).strip()
        if category not in expense:
            return f"Невідома категорія {manual.readable(category)}."
        if value:
            try:
                mine[category] = f"{manual.parse_amount(value):.2f}"
            except ValueError as e:
                return f"{manual.readable(category)}: {e}"
    rows = [r for r in read_limits() if r["хто"] != user]
    rows += [{"хто": user, "категорія": c, "ліміт": v} for c, v in sorted(mine.items())]
    manual._write(LIMITS_FILE, LIMIT_FIELDS, rows)
    REPORT_STALE = True  # ліміти показує звіт
    return None


def quick_hints(user, operations, expense):
    """Найчастіші категорії користувача (для кнопок) і памʼять «опис → категорія» з ручних операцій."""
    counts = dict(category_usage(user))
    for o in operations:
        if o.get("хто") == user and o["категорія"] in expense:
            counts[o["категорія"]] = counts.get(o["категорія"], 0) + 3  # ручні важать більше: це саме готівка
    top = sorted((c for c in expense if counts.get(c)), key=lambda c: -counts[c])[:6]
    memory = {}
    for o in sorted(operations, key=lambda o: (o.get("хто") == user, o["дата"], int(o["id"]))):
        memory[o["опис"].strip().lower()] = {"категорія": o["категорія"], "тип": o["тип"]}
    return top, memory


def shared_default_categories():
    """Категорії, де всі правила в rules.csv позначені як спільні (Житло, Продукти, Комунальні)."""
    by_cat = {}
    for _, account, _, shared in bank_import.load_rules():
        by_cat.setdefault(account, []).append(shared)
    return [c for c, flags in by_cat.items() if c.startswith("Expenses:") and all(flags)]


def state(user=None):
    known = main_accounts()
    accounts = manual.read_accounts()
    mine = [a["рахунок"] for a in accounts]
    operations = manual.read_operations()
    cash = [a for a in known if a.startswith("Assets:") and a not in manual.BANK_ACCOUNTS]
    return {
        "me": user,
        "my_cash": CASH.get(user),
        "accounts": accounts,
        "operations": operations,
        "builtin": [{"рахунок": a, "відкрито": str(d)} for a, d in known.items()
                    if a.split(":")[0] in ("Assets", "Income")],
        "expense": (expense := [a for a in known if a.startswith("Expenses:") and a != "Expenses:Без-Категорії"]),
        "income": [a for a in known if a.startswith("Income:")] + [a for a in mine if a.startswith("Income:")],
        "assets": cash + [a for a in mine if a.startswith("Assets:")],
        "today": str(dt.date.today()),
        "shared_default": shared_default_categories(),
        "partner": {"olya": "Марком", "marko": "Олею"}.get(user, "партнером"),
        "limits": user_limits(user),
        "spent": (spent := spend_by_month(user)),
        # Середнє за повні місяці — підказка, який ліміт реалістичний.
        "avg": {c: round(sum(m.get(c, 0) for m in full) / len(full), 2)
                for full in [[spent[k] for k in sorted(spent)[:-1]]] if full
                for c in {c for m in full for c in m}},
        **dict(zip(("top_categories", "memory"), quick_hints(user, operations, expense))),
    }


def apply(accounts, operations):
    """Перевіряє, записує й перераховує; якщо облік зламався — повертає старі файли."""
    error = manual.validate(accounts, operations, main_accounts())
    if error:
        return error
    old_accounts, old_ops = manual.read_accounts(), manual.read_operations()
    manual.write_accounts(accounts)
    manual.write_operations(operations)
    error = rebuild(report_now=False)
    if error:
        manual.write_accounts(old_accounts)
        manual.write_operations(old_ops)
        rebuild(report_now=False)
        return f"Облік не пройшов перевірку, зміну скасовано: {error}"
    return None


# ---------- Дії форм ----------

def save_operation(body, user):
    ops = manual.read_operations()
    op = {k: str(body.get(k, "")).strip() for k in manual.MANUAL_FIELDS}
    op["хто"] = user
    op["спільна"] = "так" if op["тип"] == "витрата" and body.get("спільна") in (True, "так") else "ні"
    op["фіксована"] = "так" if op["тип"] == "витрата" and body.get("фіксована") in (True, "так") else "ні"
    if op["сума"]:
        try:
            op["сума"] = f"{manual.parse_amount(op['сума']):.2f}"
        except ValueError as e:
            return str(e)
    if op["id"]:
        old = next((o for o in ops if o["id"] == op["id"]), None)
        if old is None:
            return "Операцію не знайдено — оновіть сторінку."
        op["хто"] = old.get("хто") or user  # редагування не змінює, чия це операція
        ops = [op if o["id"] == op["id"] else o for o in ops]
    else:
        op["id"] = str(max((int(o["id"]) for o in ops), default=0) + 1)
        ops.append(op)
    return apply(manual.read_accounts(), ops)


def delete_operation(body, user):
    ops = manual.read_operations()
    return apply(manual.read_accounts(), [o for o in ops if o["id"] != str(body.get("id"))])


def save_account(body, user):
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
           "відкрито": str(body.get("відкрито", "")).strip(), "стартовий залишок": start, "власник": user}
    old = str(body.get("old", "")).strip()
    if old:
        if not any(a["рахунок"] == old for a in accounts):
            return "Рахунок не знайдено — оновіть сторінку."
        if name != old and any(o["рахунок"] == old or o["категорія"] == old for o in ops) \
                and name.split(":")[0] != old.split(":")[0]:
            return "Тип рахунку з операціями змінити не можна."
        row["власник"] = next(a for a in accounts if a["рахунок"] == old).get("власник") or user
        accounts = [row if a["рахунок"] == old else a for a in accounts]
        # Перейменування — оновлюємо посилання в ручних операціях.
        for o in ops:
            for k in ("рахунок", "категорія"):
                if o[k] == old:
                    o[k] = name
    else:
        accounts.append(row)
    return apply(accounts, ops)


def delete_account(body, user):
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
    "/api/limits": save_limits,
}


# ---------- Вхід: паролі й сесії ----------

def load_env_file():
    """Підтягує змінні з ~/.tracker.env (або TRACKER_ENV_FILE), не перезаписуючи вже задані."""
    path = Path(os.environ.get("TRACKER_ENV_FILE", Path.home() / ".tracker.env"))
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


load_env_file()

COOKIE = "tracker_session"
SESSION_SECONDS = 30 * 24 * 3600
MAX_FAILS, FAIL_WINDOW = 5, 15 * 60          # 5 невдалих спроб за 15 хвилин з однієї адреси
FAILS = {}                                    # IP → час невдалих спроб
SECRET = os.environ.get("TRACKER_SECRET", "").encode()
if not SECRET:
    # Без секрету сесії живуть до перезапуску — для локальної перевірки досить, на сервері задайте TRACKER_SECRET.
    SECRET = secrets.token_bytes(32)
    print("Увага: TRACKER_SECRET не задано, сесії скинуться після перезапуску.", file=sys.stderr)


def hash_password(password, salt=None, iterations=200_000):
    """Пароль → «pbkdf2$ітерації$сіль$хеш». Сам пароль ніде не зберігається."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), iterations).hex()
    return f"pbkdf2${iterations}${salt}${digest}"


def check_password(password, stored):
    try:
        _, iterations, salt, _ = stored.split("$")
        return hmac.compare_digest(hash_password(password, salt, int(iterations)), stored)
    except ValueError:
        return False


def users():
    """TRACKER_USERS = «olya=<хеш>,marko=<хеш>» → {логін: хеш}."""
    pairs = (item.split("=", 1) for item in os.environ.get("TRACKER_USERS", "").split(",") if "=" in item)
    return {login.strip(): stored.strip() for login, stored in pairs if login.strip() in NAMES}


def sign(user, expires):
    message = f"{user}|{expires}"
    return f"{message}|{hmac.new(SECRET, message.encode(), hashlib.sha256).hexdigest()}"


def session_user(environ):
    """Хто увійшов: перевіряє підпис і строк дії cookie. None — не увійшов."""
    jar = cookies.SimpleCookie(environ.get("HTTP_COOKIE", ""))
    if COOKIE not in jar:
        return None
    try:
        user, expires, _ = jar[COOKIE].value.split("|")
    except ValueError:
        return None
    if not hmac.compare_digest(sign(user, expires), jar[COOKIE].value) or int(expires) < time.time():
        return None
    return user if user in users() else None


def client_ip(environ):
    return environ.get("HTTP_X_REAL_IP") or environ.get("REMOTE_ADDR", "?")


def too_many_fails(ip):
    now = time.time()
    FAILS[ip] = [t for t in FAILS.get(ip, []) if now - t < FAIL_WINDOW]
    return len(FAILS[ip]) >= MAX_FAILS


def is_https(environ):
    return (environ.get("HTTP_X_FORWARDED_PROTO") or environ.get("wsgi.url_scheme")) == "https"


def is_local(environ):
    host = environ.get("HTTP_HOST", "").split(":")[0]
    return host in ("127.0.0.1", "localhost")


# ---------- Сторінки ----------

NAV_CSS = (".appnav{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 16px;font-size:15px}"
           ".appnav a{color:var(--ink);text-decoration:none;border:1px solid var(--border);"
           "background:var(--surface);padding:6px 12px;border-radius:999px}"
           ".appnav a[aria-current]{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}"
           ".appnav .out{margin-left:auto;color:var(--ink-2)}")


def nav(user, current):
    links = [("/", "Звіт"), ("/manage", "Додати витрату")]
    html = "".join(f'<a href="{href}"{" aria-current=page" if href == current else ""}>{text}</a>'
                   for href, text in links)
    return f'<nav class="appnav">{html}<a class="out" href="/logout">Вийти ({NAMES[user]})</a></nav>'


FAB = ('<a class="fab" href="/manage" aria-label="Додати витрату">+ Витрата</a>')
FAB_CSS = (".fab{position:fixed;right:16px;bottom:calc(16px + env(safe-area-inset-bottom,0px));z-index:30;"
           "background:var(--accent);color:var(--on-accent);text-decoration:none;font-weight:600;font-size:17px;"
           "padding:14px 20px;border-radius:999px;box-shadow:0 4px 16px rgba(0,0,0,.25)}"
           ".wrap{padding-bottom:88px}")


def report_page(user):
    with LOCK:
        if REPORT_STALE or not report.OUTPUT.exists():
            rebuild()
    html = report.OUTPUT.read_text(encoding="utf-8")
    # Звіт за замовчуванням — того, хто увійшов (перемикач Оля / Марко / Разом лишається).
    default = f"<script>window.DEFAULT_VIEW={json.dumps(user)}</script>"
    return with_pwa(html.replace("</style>", NAV_CSS + FAB_CSS + "</style>", 1)
                .replace('<div id="tip"', FAB + '<div id="tip"', 1)
                .replace('<div class="wrap">', '<div class="wrap">' + nav(user, "/"), 1)
                .replace('<script type="application/json" id="data">', default + '<script type="application/json" id="data">', 1))


LOGIN = """<!doctype html>
<html lang="uk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Вхід · Фінанси Олі й Марка</title>
<style>__CSS__
.login{max-width:360px;margin:12vh auto 0;padding:0 16px}
.login form{display:grid;gap:12px;background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:20px}
.login label{display:grid;gap:4px;font-size:14px;color:var(--ink-2)}
.login input{font:inherit;font-size:16px;color:var(--ink);background:var(--page);border:1px solid var(--axis);border-radius:8px;padding:10px}
.login button{font:inherit;font-size:16px;border:0;border-radius:999px;padding:10px;background:var(--accent);color:var(--on-accent);cursor:pointer}
.login .err{color:var(--bad);margin:0}
</style>
</head>
<body>
<main class="login">
  <h1>Фінанси Олі й Марка</h1>
  <form method="post" action="/login">
    __ERROR__
    <label>Логін<input name="login" autocomplete="username" autocapitalize="none" required></label>
    <label>Пароль<input name="password" type="password" autocomplete="current-password" required></label>
    <button type="submit">Увійти</button>
  </form>
</main>
</body>
</html>
"""


def login_page(error=""):
    err = f'<p class="err" role="alert">{error}</p>' if error else ""
    return with_pwa(LOGIN.replace("__CSS__", report.CSS).replace("__ERROR__", err))


def manage_page(user):
    return with_pwa(MANAGE.replace("__CSS__", report.CSS + NAV_CSS + MANAGE_CSS).replace("__NAV__", nav(user, "/manage")))


# ---------- PWA: іконка на головному екрані ----------
# Manifest, іконки й service worker публічні й не містять жодних даних.

THEME = "#2a78d6"


def icon_png(size):
    """PNG-іконка без бібліотек: синій квадрат із трьома білими стовпчиками (графік витрат)."""
    bg, fg = (0x2A, 0x78, 0xD6), (0xFF, 0xFF, 0xFF)
    bars = [(0.24, 0.55), (0.43, 0.35), (0.62, 0.45)]  # (лівий край, верх) у частках розміру
    width, bottom = 0.14, 0.76
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            fx, fy = x / size, y / size
            inside = any(left <= fx < left + width and top <= fy < bottom for left, top in bars)
            row += bytes(fg if inside else bg)
        rows.append(bytes(row))
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


ICONS = {f"/icon-{n}.png": icon_png(n) for n in (192, 512)}

MANIFEST = json.dumps({
    "name": "Фінанси Олі й Марка",
    "short_name": "Фінанси",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#f9f9f7",
    "theme_color": THEME,
    "lang": "uk",
    "icons": [{"src": path, "sizes": f"{n}x{n}", "type": "image/png", "purpose": "any maskable"}
              for path, n in (("/icon-192.png", 192), ("/icon-512.png", 512))],
    "shortcuts": [{"name": "Додати витрату", "short_name": "Витрата", "url": "/manage",
                   "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"}]}],
}, ensure_ascii=False)

# Без кешування: фінансові дані не лишаються в памʼяті телефона, усе йде в мережу.
SERVICE_WORKER = """self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
"""

PWA_HEAD = (f'<link rel="manifest" href="/manifest.webmanifest"><meta name="theme-color" content="{THEME}">'
            '<link rel="apple-touch-icon" href="/icon-192.png"><meta name="apple-mobile-web-app-capable" content="yes">'
            "<script>if('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js');</script>")


def with_pwa(html):
    return html.replace("</head>", PWA_HEAD + "</head>", 1)


PUBLIC = {
    "/manifest.webmanifest": (MANIFEST, "application/manifest+json"),
    "/sw.js": (SERVICE_WORKER, "text/javascript"),
}


# ---------- WSGI ----------

def respond(start_response, status, body=b"", ctype="text/html", headers=(), https=False):
    if isinstance(body, str):
        body = body.encode("utf-8")
    charset = "" if ctype.startswith("image/") else "; charset=utf-8"
    base = [("Content-Type", f"{ctype}{charset}"), ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "DENY"), ("Referrer-Policy", "same-origin")]
    if https:
        base.append(("Strict-Transport-Security", "max-age=31536000"))
    start_response(status, base + list(headers))
    return [body]


def cookie_header(value, max_age, https):
    flags = f"Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax" + ("; Secure" if https else "")
    return ("Set-Cookie", f"{COOKIE}={value}; {flags}")


def read_body(environ):
    try:
        size = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        size = 0
    return environ["wsgi.input"].read(min(size, 100_000)) if size > 0 else b""


def application(environ, start_response):
    path, method = environ.get("PATH_INFO", "/"), environ.get("REQUEST_METHOD", "GET")
    https = is_https(environ)

    # Лише HTTPS: звичайний HTTP (крім локального запуску) перенаправляємо.
    if not https and not is_local(environ):
        location = f"https://{environ.get('HTTP_HOST', '')}{path}"
        return respond(start_response, "301 Moved Permanently", "", headers=[("Location", location)])

    # Публічні файли PWA: без даних, тож доступні й без входу.
    if path in PUBLIC:
        body, ctype = PUBLIC[path]
        return respond(start_response, "200 OK", body, ctype, https=https)
    if path in ICONS:
        return respond(start_response, "200 OK", ICONS[path], "image/png", https=https)

    if path == "/login":
        if method == "POST":
            ip = client_ip(environ)
            if too_many_fails(ip):
                return respond(start_response, "429 Too Many Requests",
                               login_page("Забагато невдалих спроб. Спробуйте через 15 хвилин."), https=https)
            form = parse_qs(read_body(environ).decode("utf-8", "replace"))
            login = form.get("login", [""])[0].strip().lower()
            password = form.get("password", [""])[0]
            stored = users().get(login)
            if stored and check_password(password, stored):
                FAILS.pop(ip, None)
                value = sign(login, int(time.time()) + SESSION_SECONDS)
                return respond(start_response, "303 See Other", "", https=https,
                               headers=[("Location", "/"), cookie_header(value, SESSION_SECONDS, https)])
            FAILS.setdefault(ip, []).append(time.time())
            return respond(start_response, "401 Unauthorized", login_page("Невірний логін або пароль."), https=https)
        return respond(start_response, "200 OK", login_page(), https=https)

    if path == "/logout":
        return respond(start_response, "303 See Other", "", https=https,
                       headers=[("Location", "/login"), cookie_header("", 0, https)])

    # Далі — лише для тих, хто увійшов. Без входу жодних даних.
    user = session_user(environ)
    if not user:
        if path.startswith("/api/"):
            return respond(start_response, "401 Unauthorized", json.dumps({"error": "Потрібен вхід."}),
                           "application/json", https=https)
        return respond(start_response, "303 See Other", "", headers=[("Location", "/login")], https=https)

    if method == "GET" and path == "/":
        return respond(start_response, "200 OK", report_page(user), https=https)
    if method == "GET" and path == "/manage":
        return respond(start_response, "200 OK", manage_page(user), https=https)
    if method == "GET" and path == "/api/state":
        with LOCK:
            return respond(start_response, "200 OK", json.dumps(state(user), ensure_ascii=False),
                           "application/json", https=https)
    action = ACTIONS.get(path)
    if method == "POST" and action:
        # Лише JSON зі своєї ж сторінки: чужий сайт не зможе нічого записати від імені користувача.
        origin = environ.get("HTTP_ORIGIN")
        same_origin = not origin or origin.split("://", 1)[-1] == environ.get("HTTP_HOST")
        if not environ.get("CONTENT_TYPE", "").startswith("application/json") or not same_origin:
            return respond(start_response, "400 Bad Request", json.dumps({"error": "Невідомий запит."}),
                           "application/json", https=https)
        try:
            body = json.loads(read_body(environ) or b"{}")
        except json.JSONDecodeError:
            return respond(start_response, "400 Bad Request", json.dumps({"error": "Некоректні дані."}),
                           "application/json", https=https)
        with LOCK:
            error = action(body, user)
            payload = json.dumps({"error": error, "state": state(user)}, ensure_ascii=False)
        return respond(start_response, "400 Bad Request" if error else "200 OK", payload, "application/json", https=https)
    return respond(start_response, "404 Not Found", "Не знайдено", "text/plain", https=https)


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
button.big{font-size:17px;padding:12px 28px;min-height:48px;width:100%}
.limits{display:grid;grid-template-columns:1fr 130px;gap:8px 12px;align-items:center;margin-top:8px}
.limits span{font-size:15px;color:var(--ink)}
.limits small{display:block;color:var(--muted);font-size:12px}
.amount input{font-size:30px;font-weight:650;padding:10px 12px;font-variant-numeric:tabular-nums}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chips button{font-size:15px;padding:8px 14px;min-height:40px}
.chips button.on{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
details.extra{border:1px solid var(--border);border-radius:10px;padding:8px 12px}
details.extra summary{cursor:pointer;font-size:14px;color:var(--ink-2)}
details.extra summary b{color:var(--ink);font-weight:600}
details.extra[open] summary{margin-bottom:10px}
details.extra .inner{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px 14px}
#msg button.undo{margin-left:10px;padding:4px 12px;font-size:14px;background:var(--surface);color:var(--ink);border:0}
details.more summary{cursor:pointer;list-style:none}
details.more summary::-webkit-details-marker{display:none}
details.more summary h2::before{content:"▸ ";color:var(--muted)}
details.more[open] summary h2::before{content:"▾ "}
details.more summary .hint{display:block;margin-bottom:4px}
table.list{width:100%;border-collapse:collapse;font-size:14px}
.list th{text-align:left;font-weight:500;color:var(--ink-2);padding:6px 8px;border-bottom:1px solid var(--grid)}
.list td{padding:8px;border-bottom:1px solid var(--grid);vertical-align:top}
.list td.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.list .neg{color:var(--ink)} .list .pos{color:var(--good)}
.list tr.editing td{background:var(--page)}
.tag{display:inline-block;font-size:12px;color:var(--ink-2);border:1px solid var(--border);border-radius:999px;padding:0 7px;margin-left:4px}
.empty{color:var(--muted);font-size:14px;margin:0}
@media (max-width:640px){form.grid{grid-template-columns:1fr}details.extra .inner{grid-template-columns:1fr}
.list thead{display:none}.list tr{display:grid;grid-template-columns:1fr auto;padding:6px 0;border-bottom:1px solid var(--grid)}
.list td{border:none;padding:2px 0}.list td.acts{grid-column:1/-1}}
"""

MANAGE = """<!doctype html>
<html lang="uk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Додати витрату · Фінанси</title>
<style>__CSS__</style>
</head>
<body>
<div class="wrap">
__NAV__
<header><h1>Додати витрату</h1>
<p>Готівка та все, чого немає у виписці з банку. Звіт оновлюється одразу після збереження.</p></header>
<p id="msg" role="status" aria-live="polite"></p>

<article class="card" style="margin-top:8px">
  <h2 id="op-title">Нова операція</h2>
  <form class="grid" id="op-form">
    <div class="full seg2" role="radiogroup" aria-label="Тип операції">
      <label><input type="radio" name="тип" value="витрата" checked> Витрата</label>
      <label><input type="radio" name="тип" value="дохід"> Дохід</label>
    </div>
    <label class="full amount">Сума, зл<input name="сума" inputmode="decimal" placeholder="0,00" required autocomplete="off" enterkeyhint="next"></label>
    <label class="full">Що купили<input name="опис" placeholder="Кава, ринок…" required maxlength="120" autocomplete="off" list="desc-list" enterkeyhint="done"></label>
    <datalist id="desc-list"></datalist>
    <div class="full"><span class="hint" id="chips-label">Категорія</span><div class="chips" id="cat-chips"></div></div>
    <label class="full"><span id="cat-label">Або інша категорія</span><select name="категорія" required></select></label>
    <label class="check full" id="shared-box"><input type="checkbox" name="спільна"> <span id="shared-label">Спільна, навпіл</span></label>
    <details class="full extra" id="extra"><summary><span id="extra-sum"></span> · змінити</summary>
      <div class="inner">
        <label><span id="acc-label">З якого рахунку</span><select name="рахунок" required></select></label>
        <label>Дата<input type="date" name="дата" required></label>
        <label class="check" id="fixed-box"><input type="checkbox" name="фіксована"> Фіксована (щомісяця однакова)</label>
      </div>
    </details>
    <div class="full btns">
      <button class="primary big" type="submit" id="op-save">Зберегти</button>
      <button type="button" id="op-cancel" hidden>Скасувати</button>
    </div>
    <p class="hint full" id="op-hint"></p>
  </form>
</article>

<article class="card">
  <h2>Додані вручну</h2>
  <div class="scroll" id="ops"></div>
</article>

<details class="card more" id="limits-card">
  <summary><h2>Ліміти на місяць</h2><span class="hint">скільки готові витрачати на категорію; звіт покаже, скільки лишилося</span></summary>
  <form id="limits-form"><div class="limits" id="limits-rows"></div>
    <div class="btns" style="margin-top:12px"><button class="primary" type="submit">Зберегти ліміти</button></div>
    <p class="hint" style="margin-top:8px">Підказка в полі — середнє за минулі місяці. Порожнє поле — без ліміту.</p>
  </form>
</details>

<details class="card more">
  <summary><h2 id="acc-title">Рахунки і джерела доходу</h2><span class="hint">рідко потрібно: нова картка, нове джерело доходу</span></summary>
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
</details>
</div>

<script>
const $ = s => document.querySelector(s);
const nf = new Intl.NumberFormat('uk-UA', {minimumFractionDigits: 2, maximumFractionDigits: 2});
const money = v => nf.format(v) + ' зл';
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const short = a => a.split(':').slice(1).join(' · ').replaceAll('-', ' ');
let S = null, editOp = null, editAcc = null;

let sayT;
function say(text, ok, undo) {
  const m = $('#msg'); m.textContent = text; m.className = 'on ' + (ok ? 'ok' : 'err');
  if (undo) {
    const b = document.createElement('button'); b.className = 'undo'; b.type = 'button'; b.textContent = 'Скасувати';
    b.onclick = () => { m.className = ''; undo(); }; m.append(b);
  }
  clearTimeout(sayT); sayT = setTimeout(() => m.className = '', undo ? 6000 : ok ? 3500 : 8000);
}

async function post(url, body, okText, undo) {
  const r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const j = await r.json();
  if (j.state) { S = j.state; }
  if (j.error) { say(j.error, false); return false; }
  say(typeof okText === 'function' ? okText() : okText, true, undo); return true;
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
  // Без вибору за замовчуванням: інакше кава легко зберігається як «Житло».
  if (!(keep && keep['категорія'])) opForm.elements['категорія'].insertAdjacentHTML('afterbegin', '<option value="" selected disabled>Оберіть категорію…</option>');
  options(opForm.elements['рахунок'], S.assets, (keep && keep['рахунок']) || S.my_cash);
  $('#cat-label').textContent = k === 'витрата' ? 'Або інша категорія' : 'Або інше джерело';
  $('#acc-label').textContent = k === 'витрата' ? 'З якого рахунку' : 'На який рахунок';
  $('#fixed-box').hidden = k !== 'витрата';
  $('#shared-box').hidden = k !== 'витрата';
  $('#shared-label').textContent = `Спільна з ${S.partner}, навпіл`;
  // Кнопки найчастіших категорій: вибір в один дотик.
  const top = k === 'витрата' ? S.top_categories : S.income.slice(0, 4);
  $('#chips-label').textContent = k === 'витрата' ? 'Категорія' : 'Джерело доходу';
  $('#cat-chips').innerHTML = top.map(c => `<button type="button" data-cat="${esc(c)}">${esc(short(c))}</button>`).join('');
  $('#desc-list').innerHTML = Object.keys(S.memory).map(d => `<option value="${esc(d)}">`).join('');
  markChip(); extraSummary();
  const none = !S.assets.length;
  $('#op-hint').textContent = none ? 'Спершу додайте рахунок (наприклад, «Готівка») у блоці «Рахунки і джерела доходу» нижче.' : '';
  $('#op-save').disabled = none;
}

let sharedTouched = false;  // якщо людина сама клацнула «спільна», не перебиваємо її вибір
function markChip() {
  const v = opForm.elements['категорія'].value;
  document.querySelectorAll('#cat-chips button').forEach(b => b.classList.toggle('on', b.dataset.cat === v));
  if (!sharedTouched && opKind() === 'витрата') opForm.elements['спільна'].checked = S.shared_default.includes(v);
}
function extraSummary() {
  const acc = opForm.elements['рахунок'].value, d = opForm.elements['дата'].value;
  const when = d === S.today ? 'сьогодні' : d.split('-').reverse().join('.');
  $('#extra-sum').innerHTML = `<b>${esc(acc ? short(acc) : '—')}</b>, ${esc(when)}`;
}
function rememberCategory() {
  // «Кава» → категорія, обрана для цього опису минулого разу.
  const m = S.memory[opForm.elements['опис'].value.trim().toLowerCase()];
  if (m && m['тип'] === opKind() && [...opForm.elements['категорія'].options].some(o => o.value === m['категорія'])) {
    opForm.elements['категорія'].value = m['категорія']; markChip();
  }
}

function renderLimits() {
  $('#limits-rows').innerHTML = S.expense.map(c => {
    const v = S.limits[c], avg = S.avg[c];
    return `<label for="lim-${esc(c)}"><span>${esc(short(c))}</span><small>${avg ? 'середнє ' + money(avg) : 'ще не витрачали'}</small></label>
      <input id="lim-${esc(c)}" data-limit="${esc(c)}" inputmode="decimal" placeholder="${avg ? Math.round(avg) : '—'}" value="${v ? String(v).replace('.', ',') : ''}">`;
  }).join('');
}
$('#limits-form').addEventListener('submit', async e => {
  e.preventDefault();
  const limits = {}; document.querySelectorAll('[data-limit]').forEach(i => limits[i.dataset.limit] = i.value);
  if (await post('/api/limits', {limits}, 'Ліміти збережено. Їх видно у звіті.')) renderLimits();
});

function renderOps() {
  const ops = [...S.operations].sort((a, b) => b['дата'].localeCompare(a['дата']) || b.id - a.id);
  $('#ops').innerHTML = ops.length ? `<table class="list"><thead><tr><th>Дата</th><th>Опис</th><th>Категорія</th><th>Рахунок</th><th style="text-align:right">Сума</th><th></th></tr></thead><tbody>` +
    ops.map(o => {
      const neg = o['тип'] === 'витрата';
      return `<tr class="${editOp === o.id ? 'editing' : ''}"><td>${esc(o['дата'])}</td><td>${esc(o['опис'])}${o['фіксована'] === 'так' ? '<span class="tag">фіксована</span>' : ''}${o['спільна'] === 'так' ? '<span class="tag">спільна</span>' : ''}</td>
        <td>${esc(short(o['категорія']))}</td><td>${esc(short(o['рахунок']))}</td>
        <td class="num ${neg ? 'neg' : 'pos'}">${neg ? '−' : '+'}${money(+o['сума'])}</td>
        <td class="acts"><button class="link" data-edit-op="${o.id}">Змінити</button><button class="link danger" data-del-op="${o.id}">Видалити</button></td></tr>`;
    }).join('') + '</tbody></table>' : '<p class="empty">Поки немає жодної ручної операції.</p>';
}

function renderAccs() {
  const kind = a => a.startsWith('Assets:') ? 'рахунок' : 'джерело доходу';
  const rows = S.builtin.map(b => `<tr><td>${esc(short(b['рахунок']))}</td><td>${kind(b['рахунок'])}</td><td>${esc(b['відкрито'])}</td><td class="num">—</td>
      <td class="acts"><span class="hint">${b['рахунок'].startsWith('Assets:Готівка') ? 'готівка, вручну' : b['рахунок'].startsWith('Assets:') ? 'з виписки' : 'з виписки, правила в rules.csv'}</span></td></tr>`)
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

function resetOp(focus) {
  editOp = null; sharedTouched = false; opForm.reset(); opForm.elements['дата'].value = S.today; $('#extra').open = false;
  $('#op-title').textContent = 'Нова операція'; $('#op-save').textContent = 'Зберегти'; $('#op-cancel').hidden = true;
  syncOpForm(); renderOps();
  if (focus) opForm.elements['сума'].focus();
}
function resetAcc() {
  editAcc = null; accForm.reset(); accForm.elements['відкрито'].value = S.today;
  $('#acc-form-title').textContent = 'Додати'; $('#acc-save').textContent = 'Додати'; $('#acc-cancel').hidden = true;
  syncAccForm(); renderAccs();
}
function renderAll() { syncOpForm(editOp && S.operations.find(o => o.id === editOp)); renderOps(); renderAccs(); }

opForm.addEventListener('change', e => {
  if (e.target.name === 'тип') syncOpForm();
  if (e.target.name === 'категорія') markChip();
  if (e.target.name === 'рахунок' || e.target.name === 'дата') extraSummary();
  if (e.target.name === 'опис') rememberCategory();
  if (e.target.name === 'спільна') sharedTouched = true;
});
opForm.elements['опис'].addEventListener('input', rememberCategory);
// Enter у сумі веде до опису, а не відправляє форму.
opForm.elements['сума'].addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); opForm.elements['опис'].focus(); } });
accForm.addEventListener('input', syncAccForm);
$('#op-cancel').onclick = resetOp;
$('#acc-cancel').onclick = resetAcc;

opForm.addEventListener('submit', async e => {
  e.preventDefault();
  const body = {id: editOp || ''};
  ['тип', 'дата', 'сума', 'опис', 'категорія', 'рахунок', 'фіксована', 'спільна'].forEach(n => body[n] = n === 'тип' ? opKind() : val(opForm, n));
  const before = new Set(S.operations.map(o => o.id)), editing = editOp;
  const btn = $('#op-save'); btn.disabled = true; btn.textContent = 'Зберігаю…';
  const what = `${body['опис']}, ${money(+String(body['сума']).replace(',', '.'))}`;
  const undo = editing ? null : async () => {
    const added = S.operations.find(o => !before.has(o.id));
    if (added && await post('/api/operation/delete', {id: added.id}, 'Скасовано.')) renderOps();
  };
  // Після збереження — нагадування про ліміт цієї категорії: «Кафе: 412 з 500 зл».
  const limitNote = () => {
    const c = body['категорія'], lim = S.limits[c];
    if (!lim || body['тип'] !== 'витрата') return '';
    const spent = (S.spent[body['дата'].slice(0, 7)] || {})[c] || 0, pct = Math.round(spent / lim * 100);
    return ` ${short(c)}: ${money(spent)} з ${money(lim)}` + (pct >= 100 ? ' — ліміт перевищено!' : pct >= 80 ? ` (${pct} %)` : '');
  };
  const ok = await post('/api/operation', body, () => (editing ? 'Зміни збережено.' : `Збережено: ${what}.`) + limitNote(), undo);
  btn.disabled = false; btn.textContent = editOp ? 'Зберегти зміни' : 'Зберегти';
  if (ok) resetOp(!editing); else renderAll();
});
accForm.addEventListener('submit', async e => {
  e.preventDefault();
  const body = {old: editAcc || '', 'тип': accForm.querySelector('[name="тип"]:checked').value,
    'назва': val(accForm, 'назва'), 'відкрито': val(accForm, 'відкрито'), 'стартовий залишок': val(accForm, 'стартовий залишок')};
  if (await post('/api/account', body, editAcc ? 'Зміни збережено.' : 'Додано.')) { resetAcc(); syncOpForm(); } else renderAll();
});

document.addEventListener('click', async e => {
  const t = e.target;
  if (t.dataset.cat) {
    opForm.elements['категорія'].value = t.dataset.cat; markChip();
    if (!opForm.elements['опис'].value) opForm.elements['опис'].focus();
  } else if (t.dataset.editOp) {
    const o = S.operations.find(x => x.id === t.dataset.editOp); editOp = o.id;
    opForm.querySelector(`[name="тип"][value="${o['тип']}"]`).checked = true;
    syncOpForm(o);
    ['дата', 'опис'].forEach(n => opForm.elements[n].value = o[n]);
    opForm.elements['сума'].value = String(o['сума']).replace('.', ',');
    opForm.elements['фіксована'].checked = o['фіксована'] === 'так';
    opForm.elements['спільна'].checked = o['спільна'] === 'так'; sharedTouched = true;
    $('#op-title').textContent = 'Змінити операцію'; $('#op-save').textContent = 'Зберегти зміни'; $('#op-cancel').hidden = false;
    $('#extra').open = true; extraSummary(); markChip();
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

fetch('/api/state').then(r => r.json()).then(j => { S = j; resetOp(true); resetAcc(); renderLimits(); });
</script>
</body>
</html>
"""


def setup():
    """Генерує паролі для olya і marko та ключ сесій, пише ~/.tracker.env (лише для власника)."""
    path = Path(os.environ.get("TRACKER_ENV_FILE", Path.home() / ".tracker.env"))
    if path.exists() and sys.argv[2:] != ["--force"]:
        raise SystemExit(f"{path} уже існує. Щоб перегенерувати паролі: python app.py setup --force")
    passwords = {user: secrets.token_urlsafe(9) for user in NAMES}
    lines = ["TRACKER_USERS=" + ",".join(f"{u}={hash_password(p)}" for u, p in passwords.items()),
             f"TRACKER_SECRET={secrets.token_hex(32)}"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)
    print(f"Записано {path} (доступ лише власнику). Паролі показуються один раз — перепишіть їх:")
    for user, password in passwords.items():
        print(f"  {user}: {password}")


def main():
    if sys.argv[1:2] == ["setup"]:
        setup()
        return
    if sys.argv[1:] == ["hash"]:
        password = getpass.getpass("Пароль: ")
        if password != getpass.getpass("Ще раз: "):
            raise SystemExit("Паролі не збігаються.")
        print(hash_password(password))
        return
    if not users():
        raise SystemExit("Не задано TRACKER_USERS (olya=<хеш>,marko=<хеш>). Хеш дає: uv run python app.py hash")
    error = rebuild()
    if error:
        raise SystemExit(f"Облік має помилку, спершу виправте її: {error}")

    class QuietHandler(WSGIRequestHandler):
        def log_message(self, *args):
            pass

    server = make_server("127.0.0.1", 5055, application, handler_class=QuietHandler)
    print("Відкрийте http://127.0.0.1:5055 — звіт і форми після входу. Зупинити: Ctrl+C.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
