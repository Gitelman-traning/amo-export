#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ежемесячное напоминание в Telegram: завести новую таблицу выгрузки и вписать её ID.
Запускается 1-го числа (см. .github/workflows/monthly-reminder.yml).
"""

import os
import requests

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
CURRENT_ID = os.environ.get("SPREADSHEET_ID", "").strip() or "(значение по умолчанию в скрипте)"

# Робот Google, которому нужно давать доступ к новой таблице
SERVICE_ACCOUNT_EMAIL = "clode-60@amo-export-500512.iam.gserviceaccount.com"
# Страница, где меняется переменная SPREADSHEET_ID
VARS_URL = "https://github.com/Gitelman-traning/amo-export/settings/variables/actions"

MESSAGE = (
    "📅 Новый месяц — пора завести новую таблицу для выгрузки amoCRM.\n\n"
    "Что сделать:\n"
    "1) Создайте Google-таблицу на этот месяц. Лист (вкладку) назовите так же — "
    "«общая выгрузка от Никиты».\n"
    f"2) Дайте доступ Редактора роботу:\n{SERVICE_ACCOUNT_EMAIL}\n"
    f"3) Впишите ID новой таблицы в переменную SPREADSHEET_ID здесь:\n{VARS_URL}\n\n"
    "ID — это часть ссылки между /d/ и /edit.\n\n"
    f"Сейчас выгрузка пишет в таблицу с ID:\n{CURRENT_ID}"
)


def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Нет TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID — напоминание не отправлено.")
        return
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": MESSAGE, "disable_web_page_preview": True},
        timeout=30,
    )
    if r.status_code == 200:
        print("Напоминание отправлено.")
    else:
        print(f"Не отправлено ({r.status_code}): {r.text[:200]}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
