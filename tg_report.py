#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Публикация отчётов в Telegram.
Замена n8n-воркфлоу «публикация отчет в ТГ» (AnNwBhY4WYl1n1NO).

Читает вкладку tg_reports (колонки: enabled / sort / chat_id / message_text / report_key),
берёт включённые строки, режет длинный текст на части по 3800 символов и шлёт каждую
часть в указанный в строке чат. САМ текст отчёта не строит — он уже лежит в таблице
(обычно собирается формулами). Запуск по расписанию (см. .github/workflows/tg-report.yml).

DRY_RUN=1 — ничего не отправляет, только печатает, что отправил бы (для проверки).
"""

import os
import sys

import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ============================================================
#  НАСТРОЙКИ
# ============================================================

SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "").strip() or "1GyCp56dqcAMykbNUjU64gd40ZYZ4aNXgVsjCfcBTFzk"
REPORTS_SHEET = "tg_reports"          # вкладка с готовыми сообщениями
TELEGRAM_MAX_LENGTH = 3800            # макс. длина одной части сообщения

# ---- Секреты / режим ----
GOOGLE_SA_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")


def is_enabled(value):
    return str(value or "").strip().lower() == "true"


def split_message(text, max_len=TELEGRAM_MAX_LENGTH):
    """Режет текст по строкам так, чтобы каждая часть была <= max_len."""
    lines = str(text or "").split("\n")
    chunks, current = [], ""
    for line in lines:
        nxt = f"{current}\n{line}" if current else line
        if len(nxt) > max_len:
            if current:
                chunks.append(current)
            current = line
        else:
            current = nxt
    if current:
        chunks.append(current)
    return chunks


def read_reports():
    info = __import__('json').loads(GOOGLE_SA_JSON)
    creds = Credentials.from_service_account_info(
        info, scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'])
    values = build('sheets', 'v4', credentials=creds, cache_discovery=False).spreadsheets().values()
    resp = values.get(spreadsheetId=SPREADSHEET_ID, range=f"'{REPORTS_SHEET}'").execute()
    data = resp.get("values", [])
    if not data:
        return []
    headers = data[0]
    rows = []
    for raw in data[1:]:
        row = {headers[i]: (raw[i] if i < len(raw) else "") for i in range(len(headers))}
        rows.append(row)
    return rows


def send_telegram(chat_id, text):
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        timeout=30,
    )
    if r.status_code != 200:
        print(f"  Telegram не отправлен в {chat_id} ({r.status_code}): {r.text[:200]}")
        return False
    return True


def main():
    if not GOOGLE_SA_JSON:
        print("ОШИБКА: нет GOOGLE_SERVICE_ACCOUNT_JSON")
        sys.exit(1)
    if not DRY_RUN and not TELEGRAM_BOT_TOKEN:
        print("ОШИБКА: нет TELEGRAM_BOT_TOKEN (или включите DRY_RUN=1)")
        sys.exit(1)

    rows = read_reports()
    rows = [r for r in rows if is_enabled(r.get("enabled"))]
    rows.sort(key=lambda r: float(r.get("sort") or 0))
    print(f"Включённых отчётов: {len(rows)}" + (" [DRY_RUN — без отправки]" if DRY_RUN else ""))

    sent = 0
    for row in rows:
        chat_id = str(row.get("chat_id") or "").strip()
        text = str(row.get("message_text") or "").strip()
        key = row.get("report_key") or ""
        if not chat_id or not text:
            continue
        chunks = split_message(text)
        for i, chunk in enumerate(chunks):
            body = f"{chunk}\n\n{i + 1}/{len(chunks)}" if len(chunks) > 1 else chunk
            if DRY_RUN:
                print(f"  [{key}] → чат {chat_id}, часть {i + 1}/{len(chunks)}, {len(body)} симв.")
            else:
                if send_telegram(chat_id, body):
                    sent += 1
    print(f"ГОТОВО. {'Проверено (dry-run)' if DRY_RUN else 'Отправлено сообщений: ' + str(sent)}.")


if __name__ == "__main__":
    main()
