#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Читает лист import_clients в июльской и августовской таблицах, находит все
IMPORTRANGE (источник данных) и показывает структуру. Read-only."""

import os, json, re
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

GOOGLE_SA_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
TABLES = {
    'ИЮЛЬ (рабочая, с данными)': "1UVN3nLBQ2YEg05mC0B-0tCYAgMXspVj2y2ED66hoXgg",
    'АВГУСТ (была пустая)': "1dXfewTDa4SDvvvUnK7l3Dwtq8LdeXFna7mgjrO6oOLY",
}


def main():
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_SA_JSON), scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'])
    ss = build('sheets', 'v4', credentials=creds, cache_discovery=False).spreadsheets()

    for label, sid in TABLES.items():
        print(f"\n########## import_clients — {label}\n{sid}")
        # все формулы листа
        frm = ss.values().get(spreadsheetId=sid, range="'import_clients'!A1:AN60",
                              valueRenderOption='FORMULA').execute().get('values', [])
        # уникальные IMPORTRANGE-адреса
        seen = set()
        for i, row in enumerate(frm, 1):
            for j, c in enumerate(row):
                s = str(c)
                if 'IMPORTRANGE' in s.upper():
                    col = chr(ord('A') + j) if j < 26 else 'A' + chr(ord('A') + j - 26)
                    if s not in seen:
                        seen.add(s)
                        print(f"  {col}{i}: {s[:300]}")
        if not seen:
            print("  IMPORTRANGE не найден в A1:AN60")

        # что реально видно в блоке месяцев (значения)
        val = ss.values().get(spreadsheetId=sid, range="'import_clients'!C25:AK40").execute().get('values', [])
        print("  --- C25:AK40 значения (непустые строки) ---")
        for i, row in enumerate(val, 25):
            if row and any(str(x).strip() for x in row):
                # печатаем компактно: номер строки + первые непустые
                cells = [f"{chr(ord('C')+k) if k<24 else 'A'+chr(ord('C')+k-24)}={v}"
                         for k, v in enumerate(row) if str(v).strip()]
                print(f"    r{i}: {', '.join(cells[:12])}")


if __name__ == '__main__':
    main()
