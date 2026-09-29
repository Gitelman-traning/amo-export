#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Заявки на оплату сервисов отдела продаж.

Что делает:
  1. Читает таблицу «Ежемесячные расходы ОП»: колонку нужного месяца на главном
     листе и блоки «по отделам» на листах «Телефония расход», «Wazzup расход»,
     «Zoom расход».
  2. Режет каждую сумму на 1-линию / 2-линию / прочее по правилам из RULES
     (телефония 80/20 плюс комиссия банка в прочее, АТС и Wazzup по блокам
     отделов, подписки целиком в прочее).
  3. Переводит валюту заявки (AED для Wazzup, USD → ₽ для Claude) по курсу
     на день запуска.
  4. Пишет лист «Заявки» в той же таблице (история по месяцам) и шлёт в
     Telegram свод на согласование плюс ссылку на КАЖДУЮ заявку, где все поля
     гугл-формы уже заполнены. Файл согласования форма ссылкой не принимает —
     его добавляешь руками и жмёшь «Отправить».

Режимы:
  python service_payments.py                 — месяц оплаты = следующий за текущим
  PAY_MONTH=2026-10 python service_payments.py
  DRY_RUN=1                                  — не пишет в таблицу и не шлёт в Telegram
  SHEETS_LOCAL_DIR=<папка с csv>             — читать листы из CSV (для проверки без доступа)
  RUN_MODE=schedule                          — плановый запуск: работает только в день
                                               «5 рабочих дней до 5-го числа», иначе тихо выходит
"""

import csv
import json
import os
import re
import sys
import datetime as dt
from dataclasses import dataclass, field
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests

# ============================================================
#  НАСТРОЙКИ
# ============================================================

MSK = ZoneInfo("Europe/Moscow")

# Таблица «Ежемесячные расходы ОП»
SPREADSHEET_ID = os.environ.get("SERVICES_SPREADSHEET_ID", "").strip() or "1nbeuUG1IPAMcj-uBmI7grCzHTSdlKTy4BD5sCuxIGyw"
MAIN_SHEET = "Ежемесячые расходы ОП"      # именно так, с опечаткой, как в таблице
TEL_SHEET = "Телефония расход"
WAZ_SHEET = "Wazzup расход"
ZOOM_SHEET = "Zoom расход"
OUT_SHEET = "Заявки"                       # сюда пишем результат

# Гугл-форма «Добавить новую заявку» и идентификаторы её полей
FORM_URL = "https://docs.google.com/forms/d/e/1FAIpQLSeCo80p1Ax2Ubp9PfJe-x0WtsgyKz53DOEhoT3ZSCtdvEJkdA/viewform"
ENTRY = {
    "department": "entry.139364840",   # ДЕПАРТАМЕНТ (список)
    "person": "entry.1071536968",      # ФИО ответственного (список)
    "date": "entry.1547769729",        # Крайний срок оплаты (дата: _year/_month/_day)
    "amount": "entry.1309195459",      # Сумма платежа
    "currency": "entry.2127271534",    # Валюта (флажки)
    "description": "entry.695982355",  # Краткое описание
    "period": "entry.850347275",       # За какой период (флажки «ОКТЯБРЬ 2026»)
    "project": "entry.684391703",      # Проект (радио)
    "article": "entry.1270503907",     # Статья расхода (список)
    "in_budget": "entry.742588165",    # Расход в бюджете? (радио)
}
FORM_FIXED = {
    "department": "Sales department",
    "person": "Никита Каганович",
    "project": "Тренинг Команда",
    "in_budget": "Да",
}

# Отдел → статья расхода в форме
DEPTS = ("1-линия", "2-линия", "прочее")
ARTICLE = {
    "1-линия": "2.5.1. Телефония и софт 1 линия",
    "2-линия": "2.5.2. Телефония и софт 2 линия",
    "прочее": "2.5.3. Телефония и софт прочее",
}
DEPT_LABEL = {"1-линия": "1 линия", "2-линия": "2 линия", "прочее": "прочее"}

# ---- Правила по сервисам ---------------------------------------------------
# key   — начало названия в колонке A главного листа (без учёта регистра/пробелов)
# label — как называть в описании заявки
# split — как делить между отделами:
#     "телефония"  → доли из «Телефония расход» K22:K24 (80/20/0), комиссия L31 (5%) → в прочее
#     "блок:атс"   → блок «Телефония расход» I4:K9
#     "блок:wazzup"→ блок «Wazzup расход» L4:N25
#     "блок:zoom"  → блок «Zoom расход» J17:K19
#     "1-линия" / "2-линия" / "прочее" → вся сумма на один отдел
#     "пропустить" → не подаём (AMO платится раз в год)
# pay   — валюта заявки: RUB или AED (сумма в таблице в ₽ переводится по курсу)
RULES = [
    {"key": "баланс телефонии kz", "label": "Телефония KZ", "split": "прочее", "pay": "RUB"},
    {"key": "баланс телефонии", "label": "Телефония", "split": "телефония", "pay": "RUB"},
    {"key": "атс online pbx", "label": "АТС OnlinePBX", "split": "блок:атс", "pay": "RUB"},
    {"key": "wazzupp waba баланс", "label": "Wazzup WABA баланс", "split": "1-линия", "pay": "RUB"},
    {"key": "wazzupp waba", "label": "Wazzup WABA", "split": "1-линия", "pay": "RUB"},
    {"key": "wazzupp whatsupp", "label": "Wazzup", "split": "блок:wazzup", "pay": "AED"},
    {"key": "телеграмм премиум", "label": "Telegram Premium", "split": "прочее", "pay": "RUB"},
    {"key": "amocrm", "label": "amoCRM", "split": "пропустить", "pay": "RUB"},
    {"key": "виджет триггеры", "label": "Виджет Триггеры (amoCRM)", "split": "прочее", "pay": "RUB"},
    {"key": "s3 подключение waba", "label": "S3 подключение WABA", "split": "1-линия", "pay": "RUB"},
    {"key": "s3 waba", "label": "S3 WABA", "split": "1-линия", "pay": "RUB"},
    {"key": "zoom", "label": "Zoom", "split": "блок:zoom", "pay": "RUB"},
    {"key": "marquiz", "label": "Marquiz", "split": "прочее", "pay": "RUB"},
    {"key": "vpn", "label": "VPN", "split": "прочее", "pay": "RUB"},
    {"key": "cloude", "label": "Claude", "split": "прочее", "pay": "RUB"},
    {"key": "сервер", "label": "Сервер", "split": "прочее", "pay": "RUB"},
]
# Строки главного листа, после которых сервисы заканчиваются
STOP_ROWS = ("итого", "без амо")

# Сколько символов (вместе с URL) кладём в одно сообщение со ссылками
LINKS_CHUNK_LIMIT = 3000

# Расписание: подаём за N рабочих дней до 5-го числа месяца оплаты
DAYS_BEFORE = 5
PAY_DAY = 5

# ---- Секреты / режим ----
GOOGLE_SA_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = (os.environ.get("SERVICE_PAY_CHAT_ID", "").strip()
                    or os.environ.get("TELEGRAM_CHAT_ID", "").strip())
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")
LOCAL_DIR = os.environ.get("SHEETS_LOCAL_DIR", "").strip()
RUN_MODE = os.environ.get("RUN_MODE", "manual").strip().lower()
ONLY_LINKS = os.environ.get("ONLY_LINKS", "").strip().lower() in ("1", "true", "yes")   # повтор: без свода и таблицы

MONTHS_NOM = ["январь", "февраль", "март", "апрель", "май", "июнь",
              "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря"]


# ============================================================
#  ЧТЕНИЕ ТАБЛИЦЫ (API или локальные CSV)
# ============================================================

def col_letter(idx):
    """0 → A, 25 → Z, 26 → AA."""
    s, i = "", idx + 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def col_index(letters):
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch.upper()) - 64)
    return n - 1


def parse_a1(rng):
    """'I4:K9' → (row0, col0, row1, col1) нулевые индексы."""
    m = re.match(r"^([A-Z]+)(\d+):([A-Z]+)(\d+)$", rng)
    if not m:
        raise ValueError(f"диапазон {rng!r}")
    return int(m.group(2)) - 1, col_index(m.group(1)), int(m.group(4)) - 1, col_index(m.group(3))


class SheetSource:
    """Читает диапазоны листа. Либо через Sheets API, либо из CSV в SHEETS_LOCAL_DIR."""

    LOCAL_FILES = {MAIN_SHEET: "main.csv", TEL_SHEET: "tel.csv", WAZ_SHEET: "waz.csv", ZOOM_SHEET: "zoom.csv"}

    def __init__(self):
        self._svc = None
        self._cache = {}
        if not LOCAL_DIR:
            from google.oauth2.service_account import Credentials
            from googleapiclient.discovery import build
            if not GOOGLE_SA_JSON:
                raise SystemExit("Нет GOOGLE_SERVICE_ACCOUNT_JSON и не задан SHEETS_LOCAL_DIR.")
            creds = Credentials.from_service_account_info(
                json.loads(GOOGLE_SA_JSON), scopes=["https://www.googleapis.com/auth/spreadsheets"])
            self._svc = build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()

    def _whole(self, sheet):
        if sheet in self._cache:
            return self._cache[sheet]
        if self._svc:
            resp = self._svc.values().get(spreadsheetId=SPREADSHEET_ID, range=f"'{sheet}'").execute()
            rows = resp.get("values", [])
        else:
            path = os.path.join(LOCAL_DIR, self.LOCAL_FILES[sheet])
            with open(path, encoding="utf-8") as f:
                rows = list(csv.reader(f))
        self._cache[sheet] = rows
        return rows

    def get(self, sheet, rng):
        """Возвращает прямоугольник строк (списки строк), пустые ячейки — ''."""
        r0, c0, r1, c1 = parse_a1(rng)
        rows = self._whole(sheet)
        out = []
        for r in range(r0, r1 + 1):
            row = rows[r] if r < len(rows) else []
            out.append([(row[c] if c < len(row) else "") for c in range(c0, c1 + 1)])
        return out

    # --- запись ---
    def sheets(self):
        meta = self._svc.get(spreadsheetId=SPREADSHEET_ID, fields="sheets.properties.title").execute()
        return [s["properties"]["title"] for s in meta.get("sheets", [])]

    def ensure_sheet(self, title):
        if title not in self.sheets():
            self._svc.batchUpdate(spreadsheetId=SPREADSHEET_ID,
                                  body={"requests": [{"addSheet": {"properties": {"title": title}}}]}).execute()

    def write(self, sheet, rng, values):
        self._svc.values().update(spreadsheetId=SPREADSHEET_ID, range=f"'{sheet}'!{rng}",
                                  valueInputOption="USER_ENTERED", body={"values": values}).execute()

    def clear(self, sheet, rng):
        self._svc.values().clear(spreadsheetId=SPREADSHEET_ID, range=f"'{sheet}'!{rng}").execute()


# ============================================================
#  РАЗБОР ЗНАЧЕНИЙ
# ============================================================

def norm(s):
    return re.sub(r"\s+", " ", str(s or "").replace(" ", " ")).strip().lower()


def parse_money(raw):
    """'р. 44 000,00' → (44000.0, 'RUB'); '$100' → (100.0, 'USD'); 'dh1 319' → (1319.0, 'AED').
    Пусто / '-' → (None, None)."""
    s = str(raw or "").replace(" ", " ").strip()
    if not s or s in ("-", "р. -", "р.-"):
        return None, None
    cur = "RUB"
    low = s.lower()
    if "$" in s or "usd" in low:
        cur = "USD"
    elif low.startswith("dh") or "aed" in low:
        cur = "AED"
    # отрезаем валютные пометки по краям («р.», «dh», «$», «₽», «%»), оставляем само число
    num = re.sub(r"^[^\d\-]+", "", s)
    num = re.sub(r"[^\d]+$", "", num)
    num = num.replace(" ", "")
    # В таблице русская локаль: тысячи — пробелом, дробная часть — запятой (4 320,00; 84,7996).
    if num.count(",") == 1 and "." not in num:
        num = num.replace(",", ".")
    elif num.count(".") == 1 and "," not in num:
        pass
    else:                                        # 1,234,567.00 и прочая экзотика
        num = num.replace(",", "")
    try:
        return float(num), cur
    except ValueError:
        return None, None


def parse_percent(raw, default):
    v, _ = parse_money(raw)
    if v is None:
        return default
    return v / 100.0 if "%" in str(raw) else v


def norm_dept(raw):
    s = norm(raw).replace("—", "-").replace("–", "-")
    if s.startswith("1"):
        return "1-линия"
    if s.startswith("2"):
        return "2-линия"
    if s.startswith("проч"):
        return "прочее"
    return None


def month_key(header):
    """'Октябь ' → 'окт' (первые три буквы без мусора)."""
    letters = re.sub(r"[^а-яё]", "", norm(header))
    return letters[:3]


def find_month_col(header_row, month):
    """Индекс колонки нужного месяца: САМАЯ ПРАВАЯ с таким названием, и она должна
    стоять правее самой правой колонки предыдущего месяца (иначе колонка ещё не создана)."""
    want = month_key(MONTHS_NOM[month - 1])
    prev = month_key(MONTHS_NOM[(month - 2) % 12])
    idx_want = [i for i, h in enumerate(header_row) if month_key(h) == want]
    idx_prev = [i for i, h in enumerate(header_row) if month_key(h) == prev]
    if not idx_want:
        return None
    col = max(idx_want)
    if idx_prev and col < max(idx_prev):
        return None
    return col


# ============================================================
#  КУРСЫ
# ============================================================

def fetch_rates_api():
    """→ {'USD': ₽ за 1 USD, 'AED': ₽ за 1 AED}. Источник — open.er-api.com (без ключа)."""
    r = requests.get("https://open.er-api.com/v6/latest/USD", timeout=30)
    r.raise_for_status()
    rates = r.json().get("rates") or {}
    rub, aed = rates.get("RUB"), rates.get("AED")
    if not rub or not aed:
        raise RuntimeError("в ответе курсов нет RUB/AED")
    return {"USD": float(rub), "AED": float(rub) / float(aed)}


# Редактируемые ячейки с курсом в таблице. Заполнена — берём её, пустая — курс дня из API.
# Нужно, когда счёт выставлен по одному курсу, а заявка подаётся в другой день.
RATE_CELLS = {
    "AED": (WAZ_SHEET, "R16"),     # «курс aed» на листе Wazzup расход
    "USD": (MAIN_SHEET, "B29"),    # «курс доллара гугл финанс» на главном листе
}


def resolve_rates(src):
    """→ (rates, sources). Приоритет: переменная AED_RATE/USD_RATE (ручной запуск) →
    ячейка из RATE_CELLS → API → для AED запасной пересчёт через привязку 3.6725 AED за $."""
    api, api_err = None, None
    rates, sources = {}, {}
    for cur, (sheet, cell) in RATE_CELLS.items():
        env = os.environ.get(f"{cur}_RATE", "").strip()
        if env:
            v, _ = parse_money(env)
            if v:
                rates[cur], sources[cur] = v, "задан при запуске"
                continue
        v, _ = parse_money(src.get(sheet, f"{cell}:{cell}")[0][0])
        if v:
            rates[cur], sources[cur] = v, f"из таблицы, {sheet}!{cell}"
            continue
        if api is None and api_err is None:
            try:
                api = fetch_rates_api()
            except Exception as e:  # noqa: BLE001
                api_err = e
        if api:
            rates[cur], sources[cur] = api[cur], "курс дня, open.er-api.com"
    if "USD" not in rates:
        raise SystemExit(f"Нет курса USD: ни ячейка, ни API ({api_err}).")
    if "AED" not in rates:
        rates["AED"], sources["AED"] = rates["USD"] / 3.6725, "через доллар (AED привязан 3.6725 за $)"
    return rates, sources


# ============================================================
#  РАСЧЁТ ЗАЯВОК
# ============================================================

@dataclass
class Request:
    service: str
    dept: str
    amount_rub: float
    pay_currency: str = "RUB"
    pay_amount: float = 0.0
    rate: float = 1.0
    note: str = ""

    @property
    def article(self):
        return ARTICLE[self.dept]


@dataclass
class Result:
    month: int
    year: int
    requests: list = field(default_factory=list)
    summary: list = field(default_factory=list)   # (label, сумма ₽, валюта источника, значение источника)
    warnings: list = field(default_factory=list)


def rule_for(name):
    n = norm(name)
    for rule in RULES:
        if n.startswith(rule["key"]):
            return rule
    return None


def split_telephony(src, total):
    """80/20 из K22:K24, комиссия банка из L31 (5%) — комиссия уходит в прочее."""
    shares = src.get(TEL_SHEET, "K22:K24")
    p1 = parse_percent(shares[0][0], 0.80)
    p2 = parse_percent(shares[1][0], 0.20)
    fee = parse_percent(src.get(TEL_SHEET, "L31:L31")[0][0], 0.05)
    base = total / (1 + fee)
    d1 = round(base * p1)
    d2 = round(base * p2)
    d3 = round(total) - d1 - d2
    note = f"{p1:.0%}/{p2:.0%}, комиссия {fee:.0%} → прочее"
    return {"1-линия": d1, "2-линия": d2, "прочее": d3}, note


def split_block(src, sheet, rng, extra_to_other=()):
    """Блок вида [отдел | кол-во | сумма]: суммируем по отделам.
    Строки из extra_to_other (например «Запись звонков») уходят в прочее."""
    out = {d: 0.0 for d in DEPTS}
    for row in src.get(sheet, rng):
        name = norm(row[0])
        val, _ = parse_money(row[-1])
        if val is None:
            continue
        dept = norm_dept(name)
        if dept is None and any(name.startswith(x) for x in extra_to_other):
            dept = "прочее"
        if dept:
            out[dept] += val
    return out


def build(src, month, year, rates):
    res = Result(month=month, year=year)
    main = src.get(MAIN_SHEET, "A1:AZ40")
    header = main[0]
    col = find_month_col(header, month)
    if col is None:
        res.warnings.append(f"На главном листе нет колонки за {MONTHS_NOM[month - 1]} {year} — добавь её и заполни.")
        return res
    # колонка прошлого месяца — чтобы подсветить, что было заполнено, а теперь пусто
    prev_col = find_month_col(header, (month - 2) % 12 + 1)

    for row in main[1:]:
        name = row[0].strip()
        if not name:
            continue
        if norm(name).startswith(STOP_ROWS):
            break
        raw = row[col] if col < len(row) else ""
        prev_raw = row[prev_col] if (prev_col is not None and prev_col < len(row)) else ""
        value, cur = parse_money(raw)
        rule = rule_for(name)
        if rule is None:
            if value:
                res.warnings.append(f"«{name}»: {raw.strip()} — правила нет, заявка не собрана. Добавь в RULES.")
            continue
        if rule["split"] == "пропустить":
            if value:
                res.warnings.append(f"«{name}»: {raw.strip()} стоит в таблице, но правило «пропустить» — подай руками, если надо.")
            continue
        if value is None or value == 0:
            pv, _ = parse_money(prev_raw)
            if pv:
                res.warnings.append(f"«{name}»: в прошлом месяце было {fmt_rub(pv)}, сейчас пусто — проверь.")
            continue

        # сумма в рублях
        if cur == "RUB":
            total_rub = value
        else:
            total_rub = value * rates[cur]
        res.summary.append((rule["label"], total_rub, cur, value))

        # делим по отделам
        note = ""
        split = rule["split"]
        if split == "телефония":
            parts, note = split_telephony(src, total_rub)
        elif split == "блок:атс":
            parts = split_block(src, TEL_SHEET, "I4:K9", extra_to_other=("запись",))
        elif split == "блок:wazzup":
            parts = split_block(src, WAZ_SHEET, "L4:N25")
        elif split == "блок:zoom":
            parts = split_block(src, ZOOM_SHEET, "J17:K19")
        elif split in DEPTS:
            parts = {d: (total_rub if d == split else 0.0) for d in DEPTS}
        else:
            res.warnings.append(f"«{name}»: неизвестное правило {split!r}.")
            continue

        # сверка блока с суммой в главном листе
        if split.startswith("блок"):
            block_total = sum(parts.values())
            if abs(block_total - total_rub) > 1:
                res.warnings.append(
                    f"«{name}»: в главном листе {fmt_rub(total_rub)}, а блок по отделам даёт {fmt_rub(block_total)} — "
                    f"обнови блок или сумму. Заявки собраны по блоку.")
            if block_total == 0:
                res.warnings.append(f"«{name}»: блок по отделам пустой — вся сумма ушла в прочее.")
                parts = {"1-линия": 0.0, "2-линия": 0.0, "прочее": total_rub}

        # валюта заявки
        pay_cur = rule["pay"]
        rate = 1.0 if pay_cur == "RUB" else rates[pay_cur]
        for dept in DEPTS:
            amt = parts.get(dept, 0.0)
            if amt <= 0:
                continue
            pay_amt = round(amt) if pay_cur == "RUB" else round(amt / rate, 2)
            res.requests.append(Request(service=rule["label"], dept=dept, amount_rub=round(amt),
                                        pay_currency=pay_cur, pay_amount=pay_amt, rate=rate, note=note))
    return res


# ============================================================
#  ССЫЛКИ НА ФОРМУ, ТАБЛИЦА, TELEGRAM
# ============================================================

def period_label(month, year):
    return f"{MONTHS_NOM[month - 1].upper()} {year}"


def description(req, month, year):
    return f"{req.service}, {DEPT_LABEL[req.dept]}, {MONTHS_NOM[month - 1]} {year}"


def prefill_url(req, month, year, date):
    amount = f"{req.pay_amount:.0f}" if float(req.pay_amount).is_integer() else f"{req.pay_amount:.2f}"
    params = [
        ("usp", "pp_url"),
        (ENTRY["department"], FORM_FIXED["department"]),
        (ENTRY["person"], FORM_FIXED["person"]),
        (ENTRY["date"] + "_year", f"{date.year}"),
        (ENTRY["date"] + "_month", f"{date.month:02d}"),
        (ENTRY["date"] + "_day", f"{date.day:02d}"),
        (ENTRY["amount"], amount),
        (ENTRY["currency"], req.pay_currency),
        (ENTRY["description"], description(req, month, year)),
        (ENTRY["period"], period_label(month, year)),
        (ENTRY["project"], FORM_FIXED["project"]),
        (ENTRY["article"], req.article),
        (ENTRY["in_budget"], FORM_FIXED["in_budget"]),
    ]
    return FORM_URL + "?" + urlencode(params)


def fmt_rub(v):
    return f"{v:,.0f} ₽".replace(",", " ")


def fmt_pay(req):
    if req.pay_currency == "RUB":
        return fmt_rub(req.pay_amount)
    return f"{req.pay_amount:,.2f} {req.pay_currency}".replace(",", " ")


def write_out_sheet(src, res, date, links):
    src.ensure_sheet(OUT_SHEET)
    header = ["Сформировано", "Период", "Сервис", "Отдел", "Статья расхода", "Сумма ₽",
              "Сумма заявки", "Валюта", "Курс", "Описание в форме", "Ссылка на заявку", "Примечание"]
    period = period_label(res.month, res.year)
    existing = src.get(OUT_SHEET, "A1:L2000")
    keep = [r for r in existing[1:] if any(r) and r[1] != period]   # старые периоды оставляем
    rows = []
    for req, link in zip(res.requests, links):
        rows.append([date.strftime("%d.%m.%Y"), period, req.service, req.dept, req.article,
                     req.amount_rub, req.pay_amount, req.pay_currency,
                     (round(req.rate, 4) if req.pay_currency != "RUB" else ""),
                     description(req, res.month, res.year), link, req.note])
    src.clear(OUT_SHEET, "A1:L2000")
    src.write(OUT_SHEET, "A1", [header] + keep + rows)


def html_escape(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_messages(res, date, links, rates, rate_sources):
    period = period_label(res.month, res.year)
    total_rub = sum(rub for _, rub, _, _ in res.summary)

    # 1) свод на согласование
    lines = [f"<b>Оплата сервисов за {html_escape(period)}</b> — свод на согласование"]
    for label, rub, cur, val in res.summary:
        extra = "" if cur == "RUB" else f" ({val:,.0f} {cur})".replace(",", " ")
        lines.append(f"• {html_escape(label)}: {fmt_rub(rub)}{extra}")
    lines.append(f"<b>Итого: {fmt_rub(total_rub)}</b>")
    by_dept = {d: sum(r.amount_rub for r in res.requests if r.dept == d) for d in DEPTS}
    lines.append("По отделам: " + ", ".join(f"{DEPT_LABEL[d]} {fmt_rub(v)}" for d, v in by_dept.items()))
    lines.append(f"Курс USD {rates['USD']:.2f} ₽ ({rate_sources['USD']}), AED {rates['AED']:.2f} ₽ ({rate_sources['AED']})")
    if "курс дня" in rate_sources["AED"] or "через доллар" in rate_sources["AED"]:
        lines.append(f"Чтобы задать свой курс AED, впиши его в «{WAZ_SHEET}» {RATE_CELLS['AED'][1]} и перезапусти.")
    if res.warnings:
        lines.append("")
        lines.append("<b>Проверь:</b>")
        lines += [f"⚠️ {html_escape(w)}" for w in res.warnings]
    msg1 = "\n".join(lines)

    # 2) ссылки на заявки — Telegram не принимает много длинных URL в одном сообщении
    #    (ENTITIES_TOO_LONG), поэтому режем на части с запасом
    head = [f"<b>Заявки в форму — {len(links)} шт.</b>",
            "Открой ссылку, добавь скриншот согласования, нажми «Отправить». "
            "Если форма спросит про черновик — жми «Продолжить»."]
    items = []
    for i, (req, link) in enumerate(zip(res.requests, links), 1):
        items.append(f'{i}. <a href="{link}">{html_escape(req.service)} — {DEPT_LABEL[req.dept]}</a>: '
                     f"{fmt_pay(req)}" + ("" if req.pay_currency == "RUB" else f" (= {fmt_rub(req.amount_rub)})"))
    tail = f"История: https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID} → лист «{OUT_SHEET}»"
    parts, cur = [], list(head)
    for item in items:
        if sum(len(x) for x in cur) + len(item) > LINKS_CHUNK_LIMIT and len(cur) > len(head):
            parts.append("\n".join(cur))
            cur = []
        cur.append(item)
    cur.append("\n" + tail)
    parts.append("\n".join(cur))
    msg2 = parts
    return msg1, msg2


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram пропущен (нет TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    body = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    r = requests.post(url, json=body, timeout=30)
    if r.status_code == 400 and "<a href" in text:
        # HTML-ссылки не прошли (ENTITIES_TOO_LONG и т.п.) — шлём тот же текст голыми адресами
        plain = re.sub(r'<a href="([^"]+)">([^<]*)</a>', lambda m: m.group(2) + chr(10) + m.group(1), text)
        plain = re.sub(r"<[^>]+>", "", plain).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        print(f"Telegram не принял HTML ({r.text[:120]}), повторяю без разметки.")
        r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": plain,
                                     "disable_web_page_preview": True}, timeout=30)
    if r.status_code != 200:
        print(f"Telegram не принял ({r.status_code}): {r.text[:300]}")
        raise SystemExit(1)


# ============================================================
#  РАСПИСАНИЕ
# ============================================================

def minus_working_days(d, n):
    while n > 0:
        d -= dt.timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def pay_month_for(today):
    """Месяц оплаты: PAY_MONTH=YYYY-MM, иначе следующий за текущим."""
    env = os.environ.get("PAY_MONTH", "").strip()
    if env:
        y, m = env.split("-")
        return int(m), int(y)
    m = today.month % 12 + 1
    y = today.year + (1 if m == 1 else 0)
    return m, y


def main():
    today = dt.datetime.now(MSK).date()
    month, year = pay_month_for(today)
    target = minus_working_days(dt.date(year, month, PAY_DAY), DAYS_BEFORE)
    if RUN_MODE == "schedule" and today != target:
        print(f"Плановый запуск: сегодня {today}, а день подачи за {MONTHS_NOM[month - 1]} — {target}. Выход.")
        return

    src = SheetSource()
    rates, rate_sources = resolve_rates(src)

    res = build(src, month, year, rates)
    links = [prefill_url(r, month, year, today) for r in res.requests]

    # консоль
    print(f"Месяц оплаты: {period_label(month, year)}, дата в форме: {today}")
    for cur in ("USD", "AED"):
        print(f"  курс {cur}: {rates[cur]:.4f} ₽ ({rate_sources[cur]})")
    for label, rub, cur, val in res.summary:
        print(f"  {label:28s} {rub:>12,.0f} ₽".replace(",", " ") + ("" if cur == "RUB" else f"  ({val} {cur})"))
    print(f"Заявок: {len(res.requests)}")
    for req, link in zip(res.requests, links):
        print(f"  {req.service:22s} {req.dept:8s} {fmt_pay(req):>16s}  {req.article}")
        print(f"     {link}")
    for w in res.warnings:
        print(f"  ⚠ {w}")

    if not res.requests:
        send_telegram(f"⚠️ Заявки за {period_label(month, year)} не собраны.\n" + "\n".join(res.warnings))
        return

    msg1, msg2 = build_messages(res, today, links, rates, rate_sources)
    if DRY_RUN:
        print("\n--- DRY_RUN: в таблицу не пишу, в Telegram не шлю ---\n")
        print(re.sub(r"<[^>]+>", "", msg1))
        print()
        for part in msg2:
            print(re.sub(r"<[^>]+>", "", part)[:600], "…", end="\n\n")
        print(f"Сообщений со ссылками: {len(msg2)}, длины: {[len(p) for p in msg2]}")
        return

    if not ONLY_LINKS:
        if not LOCAL_DIR:
            write_out_sheet(src, res, today, links)
            print(f"Лист «{OUT_SHEET}» обновлён.")
        send_telegram(msg1)
    for part in msg2:
        send_telegram(part)
    print(f"Отправлено в Telegram: {'' if ONLY_LINKS else 'свод + '}{len(msg2)} сообщ. со ссылками.")


if __name__ == "__main__":
    main()
