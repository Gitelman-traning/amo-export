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

    cells = {
        'V14 (маркетинг: опорная дата)': 'V14',
        'V15 (маркетинг: дата до?)': 'V15',
        'V16 (маркетинг: дата от?)': 'V16',
        'V17 (Маркетинг ВСЕГО ЛИДОВ)': 'V17',
        'BG32 (Продажи факт по лидам, неделя)': 'BG32',
        'BG37 (Продажи факт по лидам, месяц)': 'BG37',
        'AE14 (продажи опорная дата)': 'AE14',
        'AH14 (неделя с)': 'AH14',
        'AH15 (неделя по)': 'AH15',
        'AH16 (месяц с)': 'AH16',
        'AH17 (месяц по)': 'AH17',
    }
    for label, addr in cells.items():
        fval = ss.values().get(spreadsheetId=SID, range=f"'Проверка'!{addr}").execute().get('values', [['']])
        frm = ss.values().get(spreadsheetId=SID, range=f"'Проверка'!{addr}",
                              valueRenderOption='FORMULA').execute().get('values', [['']])
        val = fval[0][0] if fval and fval[0] else ''
        fm = frm[0][0] if frm and frm[0] else ''
        print(f"\n{label}: значение=[{val}]\n  формула: {fm}")


if __name__ == '__main__':
    main()
