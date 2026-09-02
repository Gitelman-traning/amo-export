#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only: сравнить счётчики лидов Маркетинг vs Продажи на листе «Проверка»."""
import os, json
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SID = os.environ.get("SPREADSHEET_ID", "").strip()
SA = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()


def main():
    creds = Credentials.from_service_account_info(
        json.loads(SA), scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'])
    ss = build('sheets', 'v4', credentials=creds, cache_discovery=False).spreadsheets()

    def dump(rng, label):
        for render in ('FORMATTED_VALUE', 'FORMULA'):
            v = ss.values().get(spreadsheetId=SID, range=rng, valueRenderOption=render).execute().get('values', [])
            print(f"\n=== {label} [{render}] {rng} ===")
            for i, row in enumerate(v, 1):
                for j, c in enumerate(row):
                    if str(c).strip():
                        col = chr(ord('A') + j)
                        print(f"  {col}{i}: {str(c)[:260]}")

    dump("'Проверка'!V14:W44", "Маркетинг V(кол-во)/W(источник)")
    dump("'Проверка'!BG30:BG40", "Продажи BG (лиды/диагностики)")
    dump("'Проверка'!AH12:AH17", "Границы периодов AH")
    dump("'Проверка'!AE14:AF14", "Опорная дата продаж AE14")


if __name__ == '__main__':
    main()
