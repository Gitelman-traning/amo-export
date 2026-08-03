#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Наполнение листа import_clients в месячной таблице БЕЗ IMPORTRANGE.

Проблема, которую решает: месячные таблицы создаются копированием шаблона, а в нём
import_clients!A1 = IMPORTRANGE(«Клиенты 2025»; «Сводная!B:ao»). При копировании связь
IMPORTRANGE требует ручного «разрешить доступ», иначе весь блок = #REF! → отчёт по
оплатам показывает нули. Чтобы это не повторялось, здесь сервис-аккаунт САМ читает
данные из источника и пишет их значениями на лист import_clients (перетирая формулу).

Запуск: обычно вторым шагом ночного прогона (после amo_export.py, до отчёта).
  python sync_import_clients.py
  DRY_RUN=1 python sync_import_clients.py    # прочитать источник, в таблицу не писать

Переменные окружения: GOOGLE_SERVICE_ACCOUNT_JSON, SPREADSHEET_ID (таблица-приёмник),
                      TELEGRAM_* (необязательно для отбивки об ошибке).
"""

import os
import sys
import json

import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ============================================================
#  НАСТРОЙКИ
# ============================================================

# Источник: таблица «Клиенты 2025 (группы по месяцам)», лист «Сводная», диапазон B:AO.
# (то, что раньше тянул IMPORTRANGE в import_clients!A1)
SOURCE_SHEET_ID = "1-6O6sXrgneHBW311cvlSAzPxNyRY1caV9cjtuEwhQSk"
SOURCE_RANGE = "Сводная!B:AO"

# Приёмник: месячная таблица (та же, что у остальных выгрузок).
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "").strip()
TARGET_SHEET = "import_clients"
TARGET_ANCHOR = "A1"          # IMPORTRANGE разливался с A1 — пишем туда же 1-в-1
CLEAR_RANGE = "import_clients!A1:AN200"   # чистим прежний блок перед записью

# ---- Секреты / режим ----
GOOGLE_SA_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                            "disable_web_page_preview": True}, timeout=30)
    except Exception as ex:
        print(f"Telegram ошибка: {ex}")


def run_url_line():
    s, r, i = (os.environ.get("GITHUB_SERVER_URL", ""), os.environ.get("GITHUB_REPOSITORY", ""),
               os.environ.get("GITHUB_RUN_ID", ""))
    return f"\nЛог: {s}/{r}/actions/runs/{i}" if (s and r and i) else ""


def main():
    missing = [n for n, v in [('GOOGLE_SERVICE_ACCOUNT_JSON', GOOGLE_SA_JSON),
                              ('SPREADSHEET_ID', SPREADSHEET_ID)] if not v]
    if missing:
        print("ОШИБКА: нет переменных окружения: " + ", ".join(missing))
        sys.exit(1)

    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_SA_JSON), scopes=['https://www.googleapis.com/auth/spreadsheets'])
    values = build('sheets', 'v4', credentials=creds, cache_discovery=False).spreadsheets().values()

    # 1. Читаем источник значениями (не формулами), числа — числами.
    try:
        src = values.get(spreadsheetId=SOURCE_SHEET_ID, range=SOURCE_RANGE,
                         valueRenderOption='UNFORMATTED_VALUE').execute().get('values', [])
    except Exception as ex:
        msg = str(ex)
        if 'PERMISSION_DENIED' in msg or '403' in msg:
            raise RuntimeError(
                "Нет доступа к таблице-источнику. Дайте сервис-аккаунту "
                "clode-60@amo-export-500512.iam.gserviceaccount.com доступ «Просмотр» к "
                f"https://docs.google.com/spreadsheets/d/{SOURCE_SHEET_ID}") from ex
        raise
    rows = len(src)
    cols = max((len(r) for r in src), default=0)
    print(f"Источник «{SOURCE_RANGE}»: {rows} строк, до {cols} колонок")
    if rows == 0:
        raise RuntimeError("Источник вернул 0 строк — проверьте лист/диапазон.")

    # быстрый контроль: строка с «Август» и её оплаты (как в отчёте)
    for r in src:
        if r and str(r[0]).strip() == 'Август':
            print(f"  контроль строки «Август»: {r[:14]}")
            break

    if DRY_RUN:
        print("DRY_RUN — в таблицу не пишу.")
        return {'rows': rows, 'cols': cols}

    # 2. Чистим прежний блок (в т.ч. формулу IMPORTRANGE) и пишем значения.
    values.clear(spreadsheetId=SPREADSHEET_ID, range=CLEAR_RANGE).execute()
    values.update(spreadsheetId=SPREADSHEET_ID, range=f"'{TARGET_SHEET}'!{TARGET_ANCHOR}",
                  valueInputOption='RAW', body={'values': src}).execute()
    print(f"ГОТОВО. Записано в {TARGET_SHEET}: {rows} строк.")
    return {'rows': rows, 'cols': cols}


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        send_telegram(f"❌ import_clients: синхронизация упала\n"
                      f"Ошибка: {type(e).__name__}: {str(e)[:300]}" + run_url_line())
        raise
