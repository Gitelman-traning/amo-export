#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Диагностика отчёта: читает формулы/значения из месячной таблицы (read-only).
Ничего не меняет. Запуск через Actions (нужен GOOGLE_SERVICE_ACCOUNT_JSON)."""

import os, json
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "").strip()
GOOGLE_SA_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()


def main():
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_SA_JSON), scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'])
    ss = build('sheets', 'v4', credentials=creds, cache_discovery=False).spreadsheets()

    meta = ss.get(spreadsheetId=SPREADSHEET_ID,
                  fields='properties.title,sheets.properties(title,sheetId,gridProperties)').execute()
    print(f"ТАБЛИЦА: {meta['properties']['title']}  ({SPREADSHEET_ID})")
    print("=== ЛИСТЫ ===")
    for s in meta['sheets']:
        p = s['properties']; g = p.get('gridProperties', {})
        print(f"  «{p['title']}»  {g.get('rowCount')}x{g.get('columnCount')}")

    # tg_reports — и значения, и формулы
    for render in ('FORMATTED_VALUE', 'FORMULA'):
        try:
            r = ss.values().get(spreadsheetId=SPREADSHEET_ID, range="'tg_reports'!A1:D40",
                                valueRenderOption=render).execute().get('values', [])
        except Exception as ex:
            print(f"\ntg_reports ({render}) — ошибка: {ex}"); continue
        print(f"\n=== tg_reports [{render}] ===")
        for i, row in enumerate(r, 1):
            for j, cell in enumerate(row):
                c = str(cell)
                if c.strip():
                    print(f"  R{i}C{j+1}: {c[:400]}")

    # export_date на листе выгрузки
    print("\n=== export_date (KO/KP на 'общая выгрузка от Никиты') ===")
    try:
        r = ss.values().get(spreadsheetId=SPREADSHEET_ID,
                            range="'общая выгрузка от Никиты'!KO1:KP20").execute().get('values', [])
        for i, row in enumerate(r, 1):
            if row and any(str(x).strip() for x in row):
                print(f"  строка {i}: {row}")
    except Exception as ex:
        print(f"  ошибка: {ex}")


if __name__ == '__main__':
    main()
