#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Точечная правка: верхняя граница недели AH15 на листе «Проверка» → =AE14 (по вчера).
Показывает старую формулу, пишет новую, читает обратно. Ничего больше не трогает."""
import os, json
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SID = os.environ.get("SPREADSHEET_ID", "").strip()
SA = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
CELL = os.environ.get("CELL", "").strip() or "AH15"
NEW_FORMULA = os.environ.get("NEW_FORMULA", "").strip() or "=AE14"
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")


def main():
    creds = Credentials.from_service_account_info(
        json.loads(SA), scopes=['https://www.googleapis.com/auth/spreadsheets'])
    v = build('sheets', 'v4', credentials=creds, cache_discovery=False).spreadsheets().values()
    rng = f"'Проверка'!{CELL}"

    old_f = v.get(spreadsheetId=SID, range=rng, valueRenderOption='FORMULA').execute().get('values', [['']])
    old_v = v.get(spreadsheetId=SID, range=rng).execute().get('values', [['']])
    print(f"Таблица {SID}")
    print(f"{CELL} сейчас: значение=[{old_v[0][0] if old_v and old_v[0] else ''}] "
          f"формула=[{old_f[0][0] if old_f and old_f[0] else ''}]")
    print(f"Новая формула: {NEW_FORMULA}")

    if DRY_RUN:
        print("DRY_RUN — не пишу.")
        return

    v.update(spreadsheetId=SID, range=rng, valueInputOption='USER_ENTERED',
             body={'values': [[NEW_FORMULA]]}).execute()
    new_f = v.get(spreadsheetId=SID, range=rng, valueRenderOption='FORMULA').execute().get('values', [['']])
    new_v = v.get(spreadsheetId=SID, range=rng).execute().get('values', [['']])
    print(f"ГОТОВО. {CELL} теперь: значение=[{new_v[0][0] if new_v and new_v[0] else ''}] "
          f"формула=[{new_f[0][0] if new_f and new_f[0] else ''}]")


if __name__ == '__main__':
    main()
