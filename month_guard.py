#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Защита от выгрузки в таблицу ПРОШЛОГО месяца (когда 1-го числа забыли
переключить переменную SPREADSHEET_ID на новую месячную таблицу).

Как работает: месячные таблицы называются «MM.YY - ОТЧЁТ по выгрузке АМО»
(напр. «09.26 - ОТЧЁТ…»). Сверяем месяц из названия с месяцем выгружаемого
периода. Не совпало → процесс НЕ пишет данные, а шлёт предупреждение в Telegram.

Почему это не мешает штатной работе 1-го числа: период считается «по вчера»,
поэтому в ночь 1-го числа ожидаемый месяц = ПРОШЛЫЙ, и старая таблица корректна.
А со 2-го числа ожидаемый месяц — новый, и старая таблица будет отклонена.

Обойти (сознательно писать в старую таблицу) — переменная FORCE_MONTH=1.
"""

import os
import re
import json
import sys

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

FORCE = os.environ.get("FORCE_MONTH", "").strip().lower() in ("1", "true", "yes")


def _title(spreadsheet_id, sa_json):
    creds = Credentials.from_service_account_info(
        json.loads(sa_json), scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'])
    svc = build('sheets', 'v4', credentials=creds, cache_discovery=False).spreadsheets()
    meta = svc.get(spreadsheetId=spreadsheet_id, fields='properties.title').execute()
    return (meta.get('properties') or {}).get('title') or ''


def check(spreadsheet_id, expected_dt, sa_json):
    """→ (ok, title, expected_mm_yy, msg). ok=False только если месяц явно НЕ тот."""
    expected = f"{expected_dt.month:02d}.{expected_dt.year % 100:02d}"
    try:
        title = _title(spreadsheet_id, sa_json)
    except Exception as ex:
        return True, '', expected, f"не смог прочитать название таблицы ({ex}) — не блокирую"
    m = re.match(r'\s*(\d{2})\.(\d{2})', title)
    if not m:
        return True, title, expected, f"в названии «{title}» нет метки месяца — не блокирую"
    found = f"{m.group(1)}.{m.group(2)}"
    if found == expected:
        return True, title, expected, f"месяц совпадает ({found})"
    return False, title, expected, (f"таблица «{title}» — это {found}, а данные за {expected}")


def guard_or_exit(process_name, spreadsheet_id, expected_dt, sa_json, telegram_fn=None):
    """Останавливает процесс, если таблица от другого месяца (кроме FORCE_MONTH=1)."""
    ok, title, expected, msg = check(spreadsheet_id, expected_dt, sa_json)
    print(f"[month_guard] {msg}")
    if ok:
        return
    if FORCE:
        print("[month_guard] FORCE_MONTH=1 — продолжаю несмотря на несовпадение.")
        return
    text = (f"⚠️ {process_name}: выгрузка ОСТАНОВЛЕНА\n"
            f"Похоже, не переключили таблицу на новый месяц.\n"
            f"Сейчас в переменной SPREADSHEET_ID: «{title}»\n"
            f"А данные идут за {expected}.\n\n"
            f"Что сделать: создать таблицу нового месяца и вписать её ID в "
            f"GitHub → Settings → Secrets and variables → Actions → Variables → SPREADSHEET_ID, "
            f"затем запустить процессы заново.\n"
            f"Если нужно писать именно в эту таблицу — запустить с FORCE_MONTH=1.")
    print(text)
    if telegram_fn:
        try:
            telegram_fn(text)
        except Exception as ex:
            print(f"[month_guard] Telegram не отправлен: {ex}")
    sys.exit(1)
