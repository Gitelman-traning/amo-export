#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Разовая выгрузка конкретных сделок amoCRM (по списку ID) в ОТДЕЛЬНУЮ таблицу.

Состав колонок 1-в-1 как в основной выгрузке (amo_export.py) — переиспользуем
её функции build_row/enrich_row, плюс первой колонкой добавляем ссылку на сделку.

Запуск:
  python deals_by_id.py                 # запись в таблицу DEALS_SHEET_ID
  DRY_RUN=1 python deals_by_id.py       # показать в логе, в таблицу не писать

Переменные окружения: AMO_TOKEN, GOOGLE_SERVICE_ACCOUNT_JSON,
                      DEALS_SHEET_ID (ID новой таблицы), DEAL_IDS (необяз., через запятую),
                      SHEET_NAME (необяз., по умолч. «Сделки»), TELEGRAM_* (необяз.).
"""

import os
import sys
import json
import time

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# Переиспользуем всю машинерию основной выгрузки: клиент amo, справочники полей,
# разворот сделки в строку и обогащение контактом — чтобы колонки совпадали 1-в-1.
import amo_export as ax

# ============================================================
#  НАСТРОЙКИ
# ============================================================

AMO_BASE_URL = ax.AMO_BASE_URL

# Сделки для выгрузки. Можно переопределить через env DEAL_IDS="id,id,id".
DEFAULT_DEAL_IDS = [31638617, 31638705, 31626657, 31631515]
_ids_env = os.environ.get("DEAL_IDS", "").strip()
DEAL_IDS = ([int(x) for x in _ids_env.replace(';', ',').split(',') if x.strip().isdigit()]
            or DEFAULT_DEAL_IDS)

# Новая таблица (создаёт Никита, даёт доступ сервис-аккаунту clode-60@...).
DEALS_SHEET_ID = os.environ.get("DEALS_SHEET_ID", "").strip()
SHEET_NAME = os.environ.get("SHEET_NAME", "").strip() or "Сделки"

# ---- Секреты / режим ----
AMO_TOKEN = ax.AMO_TOKEN
GOOGLE_SA_JSON = ax.GOOGLE_SA_JSON
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")

# Колонки-даты, которые amo_export отдаёт серийным числом Google Sheets —
# им выставим формат ДАТА, чтобы отображались как даты, а не числа.
SERIAL_DATE_COLUMNS = set(ax.DATE_FIELDS) | {'Дата создания', 'Дата закрытия'}


# ============================================================
#  Google Sheets
# ============================================================

def sheets_service():
    info = json.loads(GOOGLE_SA_JSON)
    creds = Credentials.from_service_account_info(
        info, scopes=['https://www.googleapis.com/auth/spreadsheets'])
    return build('sheets', 'v4', credentials=creds, cache_discovery=False).spreadsheets()


def ensure_sheet(svc, title, need_rows, need_cols):
    """Создаёт лист если нет и расширяет сетку под объём."""
    meta = svc.get(spreadsheetId=DEALS_SHEET_ID,
                   fields='sheets.properties(title,sheetId,gridProperties)').execute()
    props = {s['properties']['title']: s['properties'] for s in meta.get('sheets', [])}
    if title not in props:
        r = svc.batchUpdate(spreadsheetId=DEALS_SHEET_ID, body={'requests': [
            {'addSheet': {'properties': {'title': title, 'gridProperties': {
                'rowCount': need_rows, 'columnCount': need_cols}}}}]}).execute()
        return r['replies'][0]['addSheet']['properties']['sheetId']
    p = props[title]
    grid = p.get('gridProperties') or {}
    if (grid.get('rowCount') or 0) < need_rows or (grid.get('columnCount') or 0) < need_cols:
        svc.batchUpdate(spreadsheetId=DEALS_SHEET_ID, body={'requests': [
            {'updateSheetProperties': {
                'properties': {'sheetId': p['sheetId'], 'gridProperties': {
                    'rowCount': max(grid.get('rowCount') or 0, need_rows),
                    'columnCount': max(grid.get('columnCount') or 0, need_cols)}},
                'fields': 'gridProperties.rowCount,gridProperties.columnCount'}}]}).execute()
    return p['sheetId']


def apply_date_format(svc, sheet_id, headers):
    """Колонкам-датам (серийные числа) выставляем формат ДАТА dd.mm.yyyy."""
    requests = []
    for idx, h in enumerate(headers):
        if h in SERIAL_DATE_COLUMNS:
            requests.append({'repeatCell': {
                'range': {'sheetId': sheet_id, 'startColumnIndex': idx, 'endColumnIndex': idx + 1,
                          'startRowIndex': 1},
                'cell': {'userEnteredFormat': {'numberFormat': {'type': 'DATE', 'pattern': 'dd.mm.yyyy'}}},
                'fields': 'userEnteredFormat.numberFormat'}})
    if requests:
        svc.batchUpdate(spreadsheetId=DEALS_SHEET_ID, body={'requests': requests}).execute()


# ============================================================
#  main
# ============================================================

def main():
    missing = [n for n, v in [('AMO_TOKEN', AMO_TOKEN),
                              ('GOOGLE_SERVICE_ACCOUNT_JSON', GOOGLE_SA_JSON)] if not v]
    if not DRY_RUN and not DEALS_SHEET_ID:
        missing.append('DEALS_SHEET_ID')
    if missing:
        print("ОШИБКА: нет переменных окружения: " + ", ".join(missing))
        sys.exit(1)

    print(f"Сделки: {', '.join(str(i) for i in DEAL_IDS)}")
    print(f"Таблица: {DEALS_SHEET_ID or '(dry-run)'}, лист «{SHEET_NAME}»"
          + (" [DRY_RUN]" if DRY_RUN else ""))

    # --- справочники (как в основной выгрузке) ---
    users = ax.amo_fetch_all('/api/v4/users', {'limit': 250}, 'users')
    pipelines = (ax.amo_get('/api/v4/leads/pipelines').get('_embedded') or {}).get('pipelines') or []
    user_map = {str(u['id']): (u.get('name') or '') for u in users}
    pipeline_map, status_map, pipeline_status_map = {}, {}, {}
    for p in pipelines:
        pid = str(p['id'])
        pipeline_map[pid] = p.get('name') or ''
        for s in ((p.get('_embedded') or {}).get('statuses') or []):
            status_map[str(s['id'])] = s.get('name') or ''
            pipeline_status_map[f"{pid}:{s['id']}"] = s.get('name') or ''

    # --- сами сделки по ID (with=contacts) ---
    params = {'limit': 250, 'with': 'contacts'}
    for i, did in enumerate(DEAL_IDS):
        params[f'filter[id][{i}]'] = did
    data = ax.amo_get('/api/v4/leads', params)
    leads = (data.get('_embedded') or {}).get('leads') or []
    found_ids = {int(l['id']) for l in leads}
    missing_ids = [i for i in DEAL_IDS if i not in found_ids]
    print(f"Найдено сделок: {len(leads)} из {len(DEAL_IDS)}"
          + (f"; НЕ найдены: {missing_ids}" if missing_ids else ""))

    # Порядок как в списке DEAL_IDS.
    order = {did: n for n, did in enumerate(DEAL_IDS)}
    leads.sort(key=lambda l: order.get(int(l['id']), 1e9))

    rows = [ax.build_row(l, user_map, pipeline_map, status_map, pipeline_status_map) for l in leads]

    # --- контакты для обогащения ---
    contact_ids = list({str(r['_main_contact_id']) for r in rows
                        if r.get('_main_contact_id') not in (None, '', 0)})
    contact_map = {}
    for chunk in ax.chunked(contact_ids, 200):
        cp = {'limit': 250}
        for i, cid in enumerate(chunk):
            cp[f'filter[id][{i}]'] = cid
        cd = ax.amo_get('/api/v4/contacts', cp)
        for c in ((cd.get('_embedded') or {}).get('contacts') or []):
            contact_map[str(c['id'])] = c
        time.sleep(0.3)
    for r in rows:
        ax.enrich_row(r, contact_map)

    # --- колонки: как в основной выгрузке (порядок ключей build_row, без служебных)
    #     + первой добавляем ссылку на сделку ---
    base_cols = [k for k in rows[0].keys() if not k.startswith('_')] if rows else []
    headers = ['Ссылка на сделку'] + base_cols
    for r, l in zip(rows, leads):
        r['Ссылка на сделку'] = f"{AMO_BASE_URL}/leads/detail/{l['id']}"

    print(f"Колонок: {len(headers)}, строк: {len(rows)}")

    if DRY_RUN:
        print("DRY_RUN — в таблицу не пишу. Первые значения по строкам:")
        for r in rows:
            print(f"  #{r.get('ID')} | {r.get('Название сделки')} | этап: {r.get('Этап сделки')} | "
                  f"воронка: {r.get('Воронка')} | бюджет: {r.get('Бюджет')} | "
                  f"контакт: {r.get('Основной контакт')} | тел: {r.get('Рабочий телефон (контакт)')}")
        return {'rows': len(rows), 'found': len(leads), 'missing': missing_ids}

    # --- запись ---
    svc = sheets_service()
    sheet_id = ensure_sheet(svc, SHEET_NAME, len(rows) + 10, len(headers))
    values = svc.values()
    values.clear(spreadsheetId=DEALS_SHEET_ID, range=f"'{SHEET_NAME}'").execute()
    matrix = [headers] + [[ax._cell(r.get(h, '')) for h in headers] for r in rows]
    values.update(spreadsheetId=DEALS_SHEET_ID, range=f"'{SHEET_NAME}'!A1",
                  valueInputOption='USER_ENTERED', body={'values': matrix}).execute()
    apply_date_format(svc, sheet_id, headers)

    print(f"ГОТОВО. Записано строк: {len(rows)}.")
    return {'rows': len(rows), 'found': len(leads), 'missing': missing_ids}


if __name__ == '__main__':
    try:
        s = main()
        if not DRY_RUN:
            tail = f"\nНе найдены: {s['missing']}" if s.get('missing') else ""
            ax.send_telegram(
                f"✅ amoCRM: разовая выгрузка сделок\n"
                f"Записано: {s['rows']} из {len(DEAL_IDS)}{tail}\n"
                f"Таблица: https://docs.google.com/spreadsheets/d/{DEALS_SHEET_ID}"
                + ax.run_url_line())
    except Exception as e:
        ax.send_telegram(f"❌ amoCRM: выгрузка сделок упала\n"
                         f"Ошибка: {type(e).__name__}: {str(e)[:300]}" + ax.run_url_line())
        raise
