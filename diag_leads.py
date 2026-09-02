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
        'V14 (маркетинг: дата от)': 'V14',
        'V15 (маркетинг: дата до)': 'V15',
        'V17 (Маркетинг ВСЕГО ЛИДОВ)': 'V17',
        'AC6 (метка неделя)': 'AC6',
        'AC7 (метка факт по лидам неделя → ссылка)': 'AC7',
        'AE20 (стар. факт по лидам неделя)': 'AE20',
        'BG32 (нов. факт по лидам неделя)': 'BG32',
        'AE14 (продажи опорная дата)': 'AE14',
        'AH14 (неделя с)': 'AH14',
        'AH15 (неделя по)': 'AH15',
    }
    for label, addr in cells.items():
        fval = ss.values().get(spreadsheetId=SID, range=f"'Проверка'!{addr}").execute().get('values', [['']])
        frm = ss.values().get(spreadsheetId=SID, range=f"'Проверка'!{addr}",
                              valueRenderOption='FORMULA').execute().get('values', [['']])
        val = fval[0][0] if fval and fval[0] else ''
        fm = frm[0][0] if frm and frm[0] else ''
        print(f"\n{label}: значение=[{val}]\n  формула: {fm}")

    # ---- Разложение 35→40: считаем в '⬇️ОБЩАЯ ВЫГРУЗКА' сами ----
    def num(addr):
        v = ss.values().get(spreadsheetId=SID, range=f"'Проверка'!{addr}",
                            valueRenderOption='UNFORMATTED_VALUE').execute().get('values', [['']])
        try:
            return float(v[0][0])
        except (ValueError, IndexError, TypeError):
            return None
    day = num('V14')          # опорный день (маркетинг)
    aw, bw = num('AH14'), num('AH15')   # неделя
    print(f"\nГраницы (serial): день={day}, неделя=[{aw}..{bw}]")

    M = ss.values().get(spreadsheetId=SID, range="'⬇️ОБЩАЯ ВЫГРУЗКА'!M5:M20000",
                        valueRenderOption='UNFORMATTED_VALUE').execute().get('values', [])
    D = ss.values().get(spreadsheetId=SID, range="'⬇️ОБЩАЯ ВЫГРУЗКА'!D5:D20000").execute().get('values', [])
    import math
    a = b = c = d = 0
    voronki = {}
    for i in range(max(len(M), len(D))):
        try:
            m = float(M[i][0])
        except (ValueError, IndexError, TypeError):
            continue
        dv = D[i][0] if i < len(D) and D[i] else ''
        exact = (str(dv) == "Первая линия Продажи тренинга")
        star = str(dv).startswith("Первая линия ")
        if star:
            voronki[str(dv)] = voronki.get(str(dv), 0) + 1
        is_day = day is not None and math.floor(m) == math.floor(day)
        is_week = aw is not None and bw is not None and aw <= m <= bw
        if is_day and exact: a += 1
        if is_day and star:  b += 1
        if is_week and exact: c += 1
        if is_week and star:  d += 1
    print(f"\n=== РАЗЛОЖЕНИЕ (лиды в ⬇️ОБЩАЯ ВЫГРУЗКА) ===")
    print(f"  A день+точное имя  = {a}   (маркетинг V17 = 35)")
    print(f"  B день+звёздочка    = {b}")
    print(f"  C неделя+точное имя = {c}")
    print(f"  D неделя+звёздочка  = {d}   (продажи 40)")
    print(f"  → вклад периода (C-A): {c-a}; вклад звёздочки за неделю (D-C): {d-c}")
    print(f"\nВоронки, начинающиеся с «Первая линия », среди лидов:")
    for k, v in sorted(voronki.items(), key=lambda x: -x[1]):
        print(f"    «{k}»: {v}")

    # Распределение НЕДЕЛЬНЫХ лидов (точное имя) по датам — где те 5 сверх 01.09
    from datetime import datetime as _dt, timedelta as _td
    def ser2date(s):
        return (_dt(1899, 12, 30) + _td(days=int(s))).strftime('%d.%m.%Y')
    bydate = {}
    for i in range(max(len(M), len(D))):
        try:
            m = float(M[i][0])
        except (ValueError, IndexError, TypeError):
            continue
        dv = D[i][0] if i < len(D) and D[i] else ''
        if str(dv) == "Первая линия Продажи тренинга" and aw <= m <= bw:
            k = math.floor(m)
            bydate[k] = bydate.get(k, 0) + 1
    print(f"\n=== Недельные лиды (01–07.09) по дате M ===")
    for k in sorted(bydate):
        print(f"    {ser2date(k)}: {bydate[k]}")


if __name__ == '__main__':
    main()
