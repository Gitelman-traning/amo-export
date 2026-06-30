#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Выгрузка по оператору связи BMI.io → Google Sheets. Две вкладки:

  • «BMI абонплата»        — состав текущего тарифа («за что» абонентка: DID-номера,
                             пакет, городской и т.п.) + фактический итог абонплаты по
                             месяцам из биллинга.
  • «BMI звонки по странам» — помесячно: исходящие звонки в разрезе стран назначения
                             (количество, минуты, стоимость). Источник — детальный
                             экспорт /stats/calls/export (XLSX), где есть колонки
                             «Страна (куда)» и «Цена».

Диапазон: BMI_DATE_FROM/BMI_DATE_TO (YYYY-MM-DD) или последние BMI_MONTHS_BACK месяцев (12).
Запуск — GitHub Actions (см. workflow bmi-export.yml). Документация: https://rest.bmi.io/v1/docs/

Примечание: построчной ПОМЕСЯЧНОЙ детализации абонплаты в API нет (акты отдают только
итог, /tariffs/current — текущий снимок). Поэтому состав показываем по актуальному тарифу,
а помесячно — фактический итог абонплаты из /billing/spend.
"""

import io
import os
import sys
import csv
import json
import time as _time
from datetime import date, datetime
from collections import defaultdict
from zoneinfo import ZoneInfo

import requests
import openpyxl
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ============================================================
#  НАСТРОЙКИ
# ============================================================

BMI_API = "https://rest.bmi.io/v1"
TIMEZONE = "Europe/Moscow"
MSK = ZoneInfo(TIMEZONE)

SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "").strip()
SHEET_SUBSCRIPTION = os.environ.get("BMI_SHEET_SUBSCRIPTION", "").strip() or "BMI абонплата"
SHEET_CALLS = os.environ.get("BMI_SHEET_CALLS", "").strip() or "BMI звонки по странам"
# Старые вкладки прошлой версии — удаляем, чтобы не путались.
SHEETS_TO_REMOVE = ["BMI расходы", "BMI бюджет"]

MONTHS_BACK = int(os.environ.get("BMI_MONTHS_BACK", "12") or "12")

# Колонки экспорта /stats/calls/export (ищем по названию заголовка, не по индексу)
COL_DIRECTION = "Направление"
COL_COUNTRY_DST = "Страна (куда)"
COL_SECONDS = "Тарифиц. время, сек"
COL_PRICE = "Цена"
COL_CURRENCY = "Валюта"
DIRECTION_OUT = "Исходящий"

# ---- Секреты из окружения ----
BMI_API_KEY = os.environ.get("BMI_API_KEY", "").strip()
GOOGLE_SA_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# Заголовок авторизации: «X-API-Key: <id>:<key>» (Bearer запасной). Override через BMI_AUTH_SCHEME.
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


def _currency_code(cur):
    # Валюта счёта приходит объектом {name:"RUB", code:"810", symbol:"₽"} — берём имя (RUB).
    if isinstance(cur, dict):
        return cur.get("name") or cur.get("symbol") or cur.get("code") or ""
    return cur or ""


# ============================================================
#  BMI.io API
# ============================================================

def _auth_headers(scheme):
    if scheme == "bearer":
        return {"Authorization": f"Bearer {BMI_API_KEY}", "Accept": "application/json"}
    return {"X-API-Key": BMI_API_KEY, "Accept": "application/json"}


def bmi_get(path, params=None):
    """GET к BMI с автоопределением схемы авторизации (X-API-Key / Bearer)."""
    global _AUTH_SCHEME
    schemes = [_AUTH_SCHEME] if _AUTH_SCHEME in ("bearer", "apikey") else ["apikey", "bearer"]
    last = None
    for scheme in schemes:
        r = requests.get(f"{BMI_API}{path}", headers=_auth_headers(scheme),
                         params=params or {}, timeout=120)
        if r.status_code == 401 and len(schemes) > 1:
            last = r
            continue
        if r.status_code == 403:
            raise RuntimeError(f"BMI {path}: ключ отклонён (403 — невалиден/заблокирован/истёк).")
        r.raise_for_status()
        _AUTH_SCHEME = scheme
        return r.json()
    raise RuntimeError(f"BMI {path}: авторизация не прошла (401 — заголовок не принят).")


def month_ranges(d_from, d_to):
    """Список (year, month, first_day, last_day, label) от d_from до d_to включительно."""
    out = []
    y, m = d_from.year, d_from.month
    while (y, m) <= (d_to.year, d_to.month):
        first = date(y, m, 1)
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        last = date.fromordinal(date(ny, nm, 1).toordinal() - 1)
        out.append((y, m, first, last, f"{y}-{m:02d}"))
        y, m = ny, nm
    return out


def fetch_account_currency():
    try:
        j = bmi_get("/accounts", {"page_size": 1})
        data = j.get("data") or []
        if data:
            return _currency_code(data[0].get("currency"))
    except Exception as ex:
        print(f"  валюту счёта определить не удалось ({type(ex).__name__})")
    return ""


def fetch_monthly_fee(first, last):
    """GET /billing/spend → фактическая абонплата за месяц (поле monthly_fee)."""
    j = bmi_get("/billing/spend", {"date_from": first.isoformat(), "date_to": last.isoformat()})
    return num(j.get("monthly_fee"))


def fetch_tariff_composition():
    """GET /tariffs/current → состав абонплаты: строки {tariff, item, count, price, line}."""
    j = bmi_get("/tariffs/current")
    currency = _currency_code(j.get("currency"))
    rows, total = [], 0.0
    for t in j.get("tariffs", []):
        tname = t.get("name") or ""
        for f in t.get("fees", []):
            count = f.get("count") or 0
            price = num(f.get("price"))
            line = round(count * price, 2)
            total += line
            rows.append({"tariff": tname, "item": f.get("name") or "",
                         "count": count, "price": price, "line": line})
    return rows, round(total, 2), currency


def fetch_calls_by_country(first, last):
    """Детальный экспорт звонков за месяц → агрегат по стране назначения (исходящие).

    Возвращает (rows, currency), где rows = [{country, calls, minutes, cost}] (сорт по cost desc).
    """
    j = bmi_get("/stats/calls/export",
                {"date_from": first.isoformat(), "date_to": last.isoformat()})
    url = j.get("file_url")
    count = j.get("count") or 0
    fmt = (j.get("format") or "xlsx").lower()
    if not url or not count:
        return [], ""

    resp = requests.get(url, timeout=300)
    resp.raise_for_status()
    data = resp.content

    agg = defaultdict(lambda: {"calls": 0, "seconds": 0, "cost": 0.0})
    currency = ""

    if fmt == "csv":
        text = data.decode("utf-8-sig", errors="replace")
        reader = csv.reader(io.StringIO(text), delimiter=";")
        header = next(reader, [])
        idx = {h: i for i, h in enumerate(header)}
        for row in reader:
            currency = _accumulate(agg, row, idx, currency)
    else:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.active
        it = ws.iter_rows(values_only=True)
        header = list(next(it, []))
        idx = {h: i for i, h in enumerate(header)}
        for row in it:
            currency = _accumulate(agg, row, idx, currency)
        wb.close()

    rows = [{"country": c, "calls": v["calls"],
             "minutes": round(v["seconds"] / 60),
             "cost": round(v["cost"], 2)}
            for c, v in agg.items()]
    rows.sort(key=lambda r: r["cost"], reverse=True)
    return rows, currency


def _accumulate(agg, row, idx, currency):
    """Учитываем одну строку экспорта: только исходящие, группировка по стране назначения."""
    def cell(name):
        i = idx.get(name)
        return row[i] if i is not None and i < len(row) else None

    if (cell(COL_DIRECTION) or "") != DIRECTION_OUT:
        return currency
    country = cell(COL_COUNTRY_DST) or "—"
    a = agg[country]
    a["calls"] += 1
    a["seconds"] += int(cell(COL_SECONDS) or 0)
    a["cost"] += num(cell(COL_PRICE))
    return currency or (cell(COL_CURRENCY) or "")


# ============================================================
#  Google Sheets
# ============================================================

def sheets_service():
    info = json.loads(GOOGLE_SA_JSON)
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()


def _sheet_map(svc):
    meta = svc.get(spreadsheetId=SPREADSHEET_ID,
                   fields="sheets.properties(sheetId,title)").execute()
    return {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta.get("sheets", [])}


def ensure_sheet(svc, title, existing):
    if title not in existing:
        svc.batchUpdate(spreadsheetId=SPREADSHEET_ID,
                        body={"requests": [{"addSheet": {"properties": {"title": title}}}]}).execute()


def delete_sheets(svc, titles, existing):
    reqs = [{"deleteSheet": {"sheetId": existing[t]}} for t in titles if t in existing]
    if reqs:
        svc.batchUpdate(spreadsheetId=SPREADSHEET_ID, body={"requests": reqs}).execute()
        print(f"Удалены старые вкладки: {', '.join(t for t in titles if t in existing)}")


def write_matrix(svc, title, matrix, existing):
    """Полная перезапись вкладки сырой матрицей строк."""
    ensure_sheet(svc, title, existing)
    values = svc.values()
    values.clear(spreadsheetId=SPREADSHEET_ID, range=f"'{title}'").execute()
    safe = [[safe_cell(c) for c in row] for row in matrix]
    values.update(spreadsheetId=SPREADSHEET_ID, range=f"'{title}'!A1",
                  valueInputOption="USER_ENTERED", body={"values": safe}).execute()


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
    y, m = end.year, end.month
    m -= (MONTHS_BACK - 1)
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, 1), today


def build_subscription_matrix(months, currency):
    """Матрица вкладки «BMI абонплата»: состав тарифа + помесячный итог."""
    comp, comp_total, comp_cur = fetch_tariff_composition()
    cur = comp_cur or currency
    matrix = []
    matrix.append([f"Состав абонентской платы (текущий тариф), {cur}"])
    matrix.append(["Тариф", "Статья", "Кол-во", "Цена за ед.", "Сумма в месяц"])
    for r in comp:
        matrix.append([r["tariff"], r["item"], r["count"], r["price"], r["line"]])
    matrix.append(["", "ИТОГО по тарифу", "", "", comp_total])
    matrix.append([])
    matrix.append(["Фактически начислено по месяцам (биллинг)"])
    matrix.append(["Месяц", f"Абонплата, {cur}"])
    for (y, m, first, last, label) in months:
        matrix.append([label, fetch_monthly_fee(first, last)])
        _time.sleep(0.2)
    return matrix, len(comp)


def build_calls_matrix(months, currency):
    """Матрица вкладки «BMI звонки по странам»: помесячно × страна назначения."""
    matrix = [["Месяц", "Страна (куда)", "Звонков", "Минуты", "Стоимость", "Валюта"]]
    total_rows = 0
    for (y, m, first, last, label) in months:
        rows, cur = fetch_calls_by_country(first, last)
        cur = cur or currency
        for r in rows:
            matrix.append([label, r["country"], r["calls"], r["minutes"], r["cost"], cur])
        total_rows += len(rows)
        print(f"  {label}: стран {len(rows)}, "
              f"сумма {round(sum(r['cost'] for r in rows), 2)} {cur}")
        _time.sleep(0.3)
    return matrix, total_rows


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
    print(f"Диапазон: {d_from} — {d_to} ({len(months)} мес.)")

    currency = fetch_account_currency()

    print("Абонплата (состав тарифа + помесячный итог)...")
    sub_matrix, comp_items = build_subscription_matrix(months, currency)

    print("Звонки по странам (детальный экспорт по месяцам)...")
    calls_matrix, calls_rows = build_calls_matrix(months, currency)

    svc = sheets_service()
    existing = _sheet_map(svc)
    delete_sheets(svc, SHEETS_TO_REMOVE, existing)
    existing = _sheet_map(svc)  # обновили после удаления

    write_matrix(svc, SHEET_SUBSCRIPTION, sub_matrix, existing)
    print(f"Записано в «{SHEET_SUBSCRIPTION}»: статей тарифа {comp_items}, месяцев {len(months)}.")
    existing = _sheet_map(svc)
    write_matrix(svc, SHEET_CALLS, calls_matrix, existing)
    print(f"Записано в «{SHEET_CALLS}»: строк (мес.×страна) {calls_rows}.")

    return {"period": f"{d_from} — {d_to}", "months": len(months),
            "comp_items": comp_items, "calls_rows": calls_rows, "currency": currency}


if __name__ == "__main__":
    try:
        s = main()
        send_telegram(
            "✅ BMI.io → Google Sheets: выгрузка обновлена\n"
            f"Период: {s['period']} ({s['months']} мес.)\n"
            f"Абонплата: статей тарифа {s['comp_items']} + итоги по месяцам\n"
            f"Звонки по странам: строк {s['calls_rows']}\n"
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
