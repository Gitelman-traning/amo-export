#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Выгрузка истории звонков OnlinePBX → Google Sheets.
Замена n8n-воркфлоу «автовыгрузка пбх» (Mtol887fTXwQGaRE).

Каждый запуск берёт звонки за ТЕКУЩИЙ МЕСЯЦ (с 1-го числа по вчера, МСК),
полностью переписывает вкладку и кладёт свежий список. Запускается по расписанию
через GitHub Actions (см. .github/workflows/pbx-export.yml).
"""

import os
import sys
import time as _time
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ============================================================
#  НАСТРОЙКИ
# ============================================================

ONLINEPBX_DOMAIN = "pbx20965.onpbx.ru"
ONLINEPBX_API = "https://api2.onlinepbx.ru"

# Таблица — та же, что у выгрузки amoCRM (меняется помесячно через переменную SPREADSHEET_ID)
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "").strip() or "1GyCp56dqcAMykbNUjU64gd40ZYZ4aNXgVsjCfcBTFzk"
SHEET_NAME = "общая выгрузка от Никиты ЗВОНКИ"   # вкладка для звонков

CHUNK_DAYS = 7              # размер окна запроса к OnlinePBX
TIMEZONE = "Europe/Moscow"

# Колонки в порядке записи (как в n8n)
COLUMNS = [
    'Тип звонка', 'Кто', 'Кому', 'Внешний номер', 'Дата',
    'Продолжительность', 'Время разговора', 'Примечание', 'Оценка качества',
]

# ---- Секреты из окружения ----
ONLINEPBX_AUTH_KEY = os.environ.get("ONLINEPBX_AUTH_KEY", "").strip()
GOOGLE_SA_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

MSK = ZoneInfo(TIMEZONE)


def map_type(accountcode):
    return {
        'outbound': 'Исходящий',
        'inbound': 'Входящий',
        'local': 'Внутренний',
        'internal': 'Внутренний',
    }.get(accountcode, accountcode or '')


def format_ts(ts):
    if not ts:
        return ''
    d = datetime.fromtimestamp(int(ts), tz=MSK)
    return d.strftime('[%H:%M:%S] %Y-%m-%d')


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


def pbx_auth():
    """Логинимся в OnlinePBX, получаем сессионный ключ key_id:key."""
    r = requests.post(
        f"{ONLINEPBX_API}/{ONLINEPBX_DOMAIN}/auth.json",
        data={"auth_key": ONLINEPBX_AUTH_KEY},
        timeout=60,
    )
    j = r.json()
    data = j.get("data") or {}
    if not data.get("key_id") or not data.get("key"):
        raise RuntimeError(f"OnlinePBX auth не удалась: {str(j)[:300]}")
    return f"{data['key_id']}:{data['key']}"


def build_chunks():
    """Окна [1-е число месяца 00:00:01 .. вчера 23:59:59] по МСК, кусками по CHUNK_DAYS."""
    yesterday = (datetime.now(MSK) - timedelta(days=1)).date()
    start_date = yesterday.replace(day=1)
    chunks = []
    cur = start_date
    guard = 0
    while cur <= yesterday and guard < 10:
        guard += 1
        c_end = min(cur + timedelta(days=CHUNK_DAYS - 1), yesterday)
        c_from = int(datetime.combine(cur, time(0, 0, 1), MSK).timestamp())
        c_to = int(datetime.combine(c_end, time(23, 59, 59), MSK).timestamp())
        chunks.append((c_from, c_to))
        cur = c_end + timedelta(days=1)
    return chunks, start_date, yesterday


def fetch_calls(api_key, c_from, c_to):
    headers = {
        "x-pbx-authentication": api_key,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    r = requests.post(
        f"{ONLINEPBX_API}/{ONLINEPBX_DOMAIN}/mongo_history/search.json",
        headers=headers,
        json={"start_stamp_from": c_from, "start_stamp_to": c_to},
        timeout=120,
    )
    j = r.json()
    return j.get("data") or []


def normalize(call):
    return {
        'Тип звонка': map_type(call.get('accountcode')),
        'Кто': call.get('caller_id_name') or call.get('caller_id_number') or '',
        'Кому': call.get('destination_number') or '',
        'Внешний номер': call.get('gateway') or '',
        'Дата': format_ts(call.get('start_stamp')),
        'Продолжительность': int(call.get('duration') or 0),
        'Время разговора': int(call.get('user_talk_time') or 0),
        'Примечание': '',
        'Оценка качества': int(call.get('quality_score') or 0),
    }


def sheets_values():
    info = __import__('json').loads(GOOGLE_SA_JSON)
    creds = Credentials.from_service_account_info(
        info, scopes=['https://www.googleapis.com/auth/spreadsheets'])
    return build('sheets', 'v4', credentials=creds, cache_discovery=False).spreadsheets().values()


def main():
    missing = [n for n, v in [('ONLINEPBX_AUTH_KEY', ONLINEPBX_AUTH_KEY),
                              ('GOOGLE_SERVICE_ACCOUNT_JSON', GOOGLE_SA_JSON)] if not v]
    if missing:
        print("ОШИБКА: нет переменных окружения: " + ", ".join(missing))
        sys.exit(1)

    api_key = pbx_auth()
    chunks, d_from, d_to = build_chunks()
    print(f"Период звонков: {d_from:%d.%m.%Y} — {d_to:%d.%m.%Y} ({len(chunks)} окон)")

    rows = []
    for c_from, c_to in chunks:
        calls = fetch_calls(api_key, c_from, c_to)
        rows.extend(normalize(c) for c in calls)
        _time.sleep(0.3)
    print(f"Всего звонков: {len(rows)}")

    if not rows:
        print("Звонков нет — таблицу не трогаем (как в оригинале).")
        return {'rows': 0, 'period': f"{d_from:%d.%m.%Y} — {d_to:%d.%m.%Y}"}

    values = sheets_values()
    # Полная перезапись вкладки: чистим всё и пишем заголовки + данные с A1.
    values.clear(spreadsheetId=SPREADSHEET_ID, range=f"'{SHEET_NAME}'").execute()
    matrix = [COLUMNS] + [[r[c] for c in COLUMNS] for r in rows]
    values.update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{SHEET_NAME}'!A1",
        valueInputOption='USER_ENTERED',
        body={'values': matrix},
    ).execute()

    print(f"ГОТОВО. Записано звонков: {len(rows)}.")
    return {'rows': len(rows), 'period': f"{d_from:%d.%m.%Y} — {d_to:%d.%m.%Y}"}


if __name__ == '__main__':
    try:
        s = main()
        send_telegram(
            "✅ OnlinePBX → Google Sheets: звонки выгружены\n"
            f"Период: {s['period']}\n"
            f"Записано звонков: {s['rows']}\n"
            f"Таблица: https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}"
            + run_url_line()
        )
    except Exception as e:
        send_telegram(
            "❌ OnlinePBX → Google Sheets: ВЫГРУЗКА ЗВОНКОВ УПАЛА\n"
            f"Ошибка: {type(e).__name__}: {str(e)[:300]}"
            + run_url_line()
        )
        raise
