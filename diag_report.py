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

    # tg_reports: headers + message_text (значение и формула) по каждой строке
    hdr = ss.values().get(spreadsheetId=SPREADSHEET_ID, range="'tg_reports'!1:1").execute().get('values', [[]])[0]
    print(f"\n=== tg_reports заголовки: {hdr}")
    col_msg = None
    for i, h in enumerate(hdr):
        if str(h).strip().lower() == 'message_text':
            col_msg = i
            break
    if col_msg is None:
        print("  колонка message_text не найдена!")
    else:
        letter = chr(ord('A') + col_msg)
        for render in ('FORMATTED_VALUE', 'FORMULA'):
            r = ss.values().get(spreadsheetId=SPREADSHEET_ID,
                                range=f"'tg_reports'!{letter}2:{letter}6",
                                valueRenderOption=render).execute().get('values', [])
            print(f"\n=== message_text [{render}] ===")
            for i, row in enumerate(r, 2):
                if row and str(row[0]).strip():
                    print(f"  строка {i}:\n{row[0]}\n---")

    # Лист «Проверка»: ячейки-сборщики отчётов W73 (маркетинг) и AC73 (продажи)
    for addr in ('W73', 'AC73'):
        for render in ('FORMULA', 'FORMATTED_VALUE'):
            v = ss.values().get(spreadsheetId=SPREADSHEET_ID, range=f"'Проверка'!{addr}",
                                valueRenderOption=render).execute().get('values', [['']])
            cell = v[0][0] if v and v[0] else ''
            print(f"\n=== Проверка!{addr} [{render}] ===\n{cell}")

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
