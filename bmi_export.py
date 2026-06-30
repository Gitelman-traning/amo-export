#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Выгрузка бюджета и расходов оператора связи BMI.io → Google Sheets.

Каждый запуск пробегает по МЕСЯЦАМ заданного диапазона и на каждый месяц:
  • GET /billing/spend     → расходы по категориям (breakdown[]) + абонплата/звонки/итого
  • GET /reconciliation    → бюджет за месяц: начальное сальдо, обороты (приход/расход), конечное сальдо

Кладёт результат в две вкладки Google Sheets (обе перезаписываются целиком, чтобы запуск был идемпотентным):
  • «РАСХОДЫ»  — Месяц | Категория | Сумма | Валюта
  • «БЮДЖЕТ»   — Месяц | Нач. сальдо | Поступления | Расходы | Кон. сальдо | Валюта

Диапазон задаётся переменными окружения:
  • BMI_DATE_FROM / BMI_DATE_TO  — границы (YYYY-MM-DD). Если не заданы —
    берутся последние BMI_MONTHS_BACK месяцев (по умолчанию 12) по текущий месяц.

Запускается по расписанию через GitHub Actions (см. .github/workflows/bmi-export.yml).
Документация API: https://rest.bmi.io/v1/docs/
"""

import os
import sys
import json
import time as _time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ============================================================
#  НАСТРОЙКИ
# ============================================================

BMI_API = "https://rest.bmi.io/v1"
TIMEZONE = "Europe/Moscow"
MSK = ZoneInfo(TIMEZONE)

SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "").strip()
SHEET_EXPENSES = os.environ.get("BMI_SHEET_EXPENSES", "").strip() or "BMI расходы"
SHEET_BUDGET = os.environ.get("BMI_SHEET_BUDGET", "").strip() or "BMI бюджет"

MONTHS_BACK = int(os.environ.get("BMI_MONTHS_BACK", "12") or "12")

COLUMNS_EXPENSES = ["Месяц", "Категория", "Сумма", "Валюта"]
COLUMNS_BUDGET = ["Месяц", "Нач. сальдо", "Поступления", "Расходы", "Кон. сальдо", "Валюта"]

# ---- Секреты из окружения ----
BMI_API_KEY = os.environ.get("BMI_API_KEY", "").strip()
GOOGLE_SA_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# Гайд BMI: заголовок «X-API-Key: <id>:<key>» (НЕ Bearer). Ключ передаётся целиком,
# вместе с двоеточием. Bearer оставлен запасным вариантом на случай смены схемы.
_AUTH_SCHEME = os.environ.get("BMI_AUTH_SCHEME", "").strip().lower()  # "apikey" | "bearer" | ""


# ============================================================
#  УТИЛИТЫ
# ============================================================

def safe_cell(v):
    """Экранируем формульную инъекцию Google Sheets: текст, начинающийся с =,+,-,@."""
    if v is None:
        return ""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@"):
        return "'" + v
    return v


def num(v):
    """Аккуратно приводим к числу; пустое/мусор → 0."""
    if v is None or v == "":
        return 0
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return 0


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram-отбивка пропущена (нет TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True},
            timeout=30,
        )
        if r.status_code != 200:
            print(f"Telegram не отправлен ({r.status_code}): {r.text[:200]}")
    except Exception as ex:
        print(f"Telegram ошибка: {ex}")


def run_url_line():
    server = os.environ.get("GITHUB_SERVER_URL", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if server and repo and run_id:
        return f"\nЛог: {server}/{repo}/actions/runs/{run_id}"
    return ""


# ============================================================
#  BMI.io API
# ============================================================

def _auth_headers(scheme):
    if scheme == "apikey":
        return {"X-API-Key": BMI_API_KEY, "Accept": "application/json"}
    return {"Authorization": f"Bearer {BMI_API_KEY}", "Accept": "application/json"}


def bmi_get(path, params=None):
    """GET к BMI с автоопределением схемы авторизации (Bearer / X-API-Key)."""
    global _AUTH_SCHEME
    schemes = [_AUTH_SCHEME] if _AUTH_SCHEME in ("bearer", "apikey") else ["apikey", "bearer"]
    last = None
    for scheme in schemes:
        r = requests.get(
            f"{BMI_API}{path}",
            headers=_auth_headers(scheme),
            params=params or {},
            timeout=120,
        )
        # 401 — заголовок не принят (не та схема), пробуем следующую.
        # 403 — ключ невалиден/заблокирован/истёк (по гайду BMI) — менять схему бессмысленно.
        if r.status_code == 401 and len(schemes) > 1:
            last = r
            continue
        if r.status_code == 403:
            raise RuntimeError(f"BMI {path}: ключ отклонён (403 — невалиден/заблокирован/истёк).")
        r.raise_for_status()
        _AUTH_SCHEME = scheme  # запомнили рабочую схему
        return r.json()
    raise RuntimeError(f"BMI {path}: авторизация не прошла (401 — заголовок не принят).")


def month_ranges(d_from, d_to):
    """Список (year, month, first_day, last_day, label) от d_from до d_to включительно."""
    out = []
    y, m = d_from.year, d_from.month
    while (y, m) <= (d_to.year, d_to.month):
        first = date(y, m, 1)
        if m == 12:
            ny, nm = y + 1, 1
        else:
            ny, nm = y, m + 1
        last = date(ny, nm, 1).toordinal() - 1
        last = date.fromordinal(last)
        out.append((y, m, first, last, f"{y}-{m:02d}"))
        y, m = ny, nm
    return out


def fetch_account_currency():
    """Берём валюту счёта из /accounts (в /billing/spend её нет). Возвращает код, напр. 'RUB'."""
    try:
        j = bmi_get("/accounts", {"page_size": 1})
        data = j.get("data") or []
        if data:
            return _currency_code(data[0].get("currency"))
    except Exception as ex:
        print(f"  валюту счёта определить не удалось ({type(ex).__name__}); ставлю пусто")
    return ""


def fetch_spend(first, last):
    """GET /billing/spend → список строк расходов по категориям за месяц + валюта."""
    j = bmi_get("/billing/spend", {"date_from": first.isoformat(), "date_to": last.isoformat()})
    currency = _currency_code(j.get("currency"))  # в /billing/spend обычно отсутствует
    rows = []
    breakdown = j.get("breakdown") or []
    if breakdown:
        for b in breakdown:
            rows.append({
                "category": b.get("label") or b.get("category") or "—",
                "amount": num(b.get("amount")),
            })
    else:
        # запасной вариант: из плоских полей, если breakdown пуст
        if j.get("monthly_fee") not in (None, ""):
            rows.append({"category": "Абонентская плата", "amount": num(j.get("monthly_fee"))})
        if j.get("calls_cost") not in (None, ""):
            rows.append({"category": "Платные звонки", "amount": num(j.get("calls_cost"))})
    total = num(j.get("total"))
    return rows, total, currency


def fetch_reconciliation(first, last):
    """GET /reconciliation → бюджет за месяц (сальдо и обороты)."""
    j = bmi_get("/reconciliation", {"date_from": first.isoformat(), "date_to": last.isoformat()})
    currency = _currency_code(j.get("currency"))
    # Сальдо в пользу клиента: кредит − дебет (плюс = деньги на счёте / предоплата).
    return {
        "opening": num(j.get("opening_credit")) - num(j.get("opening_debit")),
        "income": num(j.get("turnover_credit")),   # поступления/оплаты = кредит
        "expense": num(j.get("turnover_debit")),   # начисления за услуги = дебет
        "closing": num(j.get("closing_credit")) - num(j.get("closing_debit")),
        "currency": currency,
    }


def _currency_code(cur):
    if isinstance(cur, dict):
        return cur.get("code") or cur.get("symbol") or cur.get("name") or ""
    return cur or ""


# ============================================================
#  Google Sheets
# ============================================================

def sheets_service():
    info = json.loads(GOOGLE_SA_JSON)
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()


def ensure_sheet(svc, title):
    """Создаёт вкладку, если её ещё нет."""
    meta = svc.get(spreadsheetId=SPREADSHEET_ID, fields="sheets.properties.title").execute()
    titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    if title not in titles:
        svc.batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
        ).execute()


def write_sheet(svc, title, columns, matrix_rows):
    """Полная перезапись вкладки: чистим всё, пишем заголовки + данные с A1."""
    ensure_sheet(svc, title)
    values = svc.values()
    values.clear(spreadsheetId=SPREADSHEET_ID, range=f"'{title}'").execute()
    matrix = [columns] + [[safe_cell(c) for c in row] for row in matrix_rows]
    values.update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{title}'!A1",
        valueInputOption="USER_ENTERED",
        body={"values": matrix},
    ).execute()


# ============================================================
#  MAIN
# ============================================================

def resolve_range():
    df = os.environ.get("BMI_DATE_FROM", "").strip()
    dt = os.environ.get("BMI_DATE_TO", "").strip()
    if df and dt:
        return date.fromisoformat(df), date.fromisoformat(dt)
    today = datetime.now(MSK).date()
    end = date(today.year, today.month, 1)
    # отматываем MONTHS_BACK-1 месяцев назад от текущего
    y, m = end.year, end.month
    back = MONTHS_BACK - 1
    m -= back
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, 1), today


def main():
    missing = [n for n, v in [
        ("BMI_API_KEY", BMI_API_KEY),
        ("GOOGLE_SERVICE_ACCOUNT_JSON", GOOGLE_SA_JSON),
        ("SPREADSHEET_ID", SPREADSHEET_ID),
    ] if not v]
    if missing:
        print("ОШИБКА: нет переменных окружения: " + ", ".join(missing))
        sys.exit(1)

    d_from, d_to = resolve_range()
    months = month_ranges(d_from, d_to)
    print(f"Диапазон: {d_from} — {d_to} ({len(months)} мес.). Схема авторизации: "
          f"{_AUTH_SCHEME or 'автоопределение'}")

    expense_rows = []   # Месяц | Категория | Сумма | Валюта
    budget_rows = []    # Месяц | Нач. | Приход | Расход | Кон. | Валюта
    acc_currency = fetch_account_currency()  # валюта счёта — дефолт для всех строк
    currency_seen = acc_currency

    for (y, m, first, last, label) in months:
        # --- расходы по категориям ---
        rows, total, cur = fetch_spend(first, last)
        cur = cur or acc_currency
        currency_seen = currency_seen or cur
        for r in rows:
            expense_rows.append([label, r["category"], r["amount"], cur])
        if rows:
            expense_rows.append([label, "ИТОГО", total, cur])

        # --- бюджет (акт сверки) ---
        try:
            b = fetch_reconciliation(first, last)
            budget_rows.append([label, b["opening"], b["income"], b["expense"],
                                b["closing"], b["currency"] or cur])
        except Exception as ex:
            print(f"  {label}: /reconciliation недоступен ({type(ex).__name__}: {str(ex)[:120]})")

        print(f"  {label}: категорий {len(rows)}, итого {total} {cur}")
        _time.sleep(0.3)

    svc = sheets_service()
    write_sheet(svc, SHEET_EXPENSES, COLUMNS_EXPENSES, expense_rows)
    print(f"Записано в «{SHEET_EXPENSES}»: {len(expense_rows)} строк.")
    if budget_rows:
        write_sheet(svc, SHEET_BUDGET, COLUMNS_BUDGET, budget_rows)
        print(f"Записано в «{SHEET_BUDGET}»: {len(budget_rows)} строк.")

    return {
        "period": f"{d_from} — {d_to}",
        "months": len(months),
        "expense_rows": len(expense_rows),
        "budget_rows": len(budget_rows),
        "currency": currency_seen,
    }


if __name__ == "__main__":
    try:
        s = main()
        send_telegram(
            "✅ BMI.io → Google Sheets: бюджет и расходы выгружены\n"
            f"Период: {s['period']} ({s['months']} мес.)\n"
            f"Строк расходов: {s['expense_rows']}, строк бюджета: {s['budget_rows']}\n"
            f"Таблица: https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}"
            + run_url_line()
        )
    except Exception as e:
        send_telegram(
            "❌ BMI.io → Google Sheets: ВЫГРУЗКА УПАЛА\n"
            f"Ошибка: {type(e).__name__}: {str(e)[:300]}"
            + run_url_line()
        )
        raise
