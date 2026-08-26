#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Активность Первой линии за текущий месяц: какие контакты обрабатывают менеджеры.

Строка = контакт со сделкой в воронке 8733326, которого КАСАЛИСЬ в текущем месяце.
Касание = событие живого менеджера (без ботов) по сделке первой линии.

Колонки: ID контакта · Телефон · Менеджер · Дата последнего касания · Дата создания контакта.
  • Менеджер = ответственный той сделки, по которой было последнее касание.
  • Период — [1-е число месяца(вчера) … вчера] (по вчера, как в events_export).

Цель: видеть, сколько контактов из базы обрабатывает каждый менеджер за месяц,
и по дате создания отличать новых клиентов от старой базы.

Запуск: шагом ночной цепочки tg-report. Вручную — workflow, DRY_RUN=1 не пишет в таблицу.
Переменные: AMO_TOKEN, GOOGLE_SERVICE_ACCOUNT_JSON, SPREADSHEET_ID, TELEGRAM_* (необяз.).
"""

import os
import sys
import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

import amo_export as ax  # переиспользуем amo_get / amo_fetch_all / helpers

# ============================================================
#  НАСТРОЙКИ
# ============================================================

PIPELINE_ID = 8733326                 # Первая линия
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "").strip()
SHEET_NAME = os.environ.get("SHEET_NAME", "").strip() or "Активность 1-линии"

DATE_FROM = os.environ.get("DATE_FROM", "").strip()   # ДД.ММ.ГГГГ (необяз.)
DATE_TO = os.environ.get("DATE_TO", "").strip()

TIMEZONE = "Europe/Moscow"
MSK = ZoneInfo(TIMEZONE)
EVENTS_PAGE = 100
REQUEST_INTERVAL = 0.15

COLUMNS = ['ID контакта', 'Телефон', 'Менеджер', 'Дата последнего касания', 'Дата создания']

AMO_TOKEN = ax.AMO_TOKEN
GOOGLE_SA_JSON = ax.GOOGLE_SA_JSON
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")


# ============================================================
#  Хелперы
# ============================================================

def period_bounds():
    now = datetime.now(MSK)
    ref = now - timedelta(days=1)   # по вчера
    if DATE_FROM:
        d = datetime.strptime(DATE_FROM, '%d.%m.%Y')
        start = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=MSK)
    else:
        start = datetime(ref.year, ref.month, 1, 0, 0, 0, tzinfo=MSK)
    if DATE_TO:
        d = datetime.strptime(DATE_TO, '%d.%m.%Y')
        end = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=MSK)
    else:
        end = datetime(ref.year, ref.month, ref.day, 23, 59, 59, tzinfo=MSK)
    return int(start.timestamp()), int(end.timestamp()), start, end


def fmt_dt(ts, with_time=True):
    if ts in (None, '', 0, '0'):
        return ''
    try:
        d = datetime.fromtimestamp(int(ts), tz=MSK)
    except (TypeError, ValueError):
        return ''
    return d.strftime('%d.%m.%Y %H:%M' if with_time else '%d.%m.%Y')


def safe_cell(v):
    if v is None:
        return ''
    if isinstance(v, str) and v[:1] in ('=', '+', '-', '@'):
        return "'" + v
    return v


def col_letter(n):
    s = ''
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    import requests
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                            "disable_web_page_preview": True}, timeout=30)
    except Exception as ex:
        print(f"Telegram ошибка: {ex}")


def run_url_line():
    s, r, i = (os.environ.get("GITHUB_SERVER_URL", ""), os.environ.get("GITHUB_REPOSITORY", ""),
               os.environ.get("GITHUB_RUN_ID", ""))
    return f"\nЛог: {s}/{r}/actions/runs/{i}" if (s and r and i) else ""


# ============================================================
#  amoCRM
# ============================================================

def fetch_manager_lead_events(ts_from, ts_to, user_ids=None):
    """События по сделкам за период, сделанные живыми менеджерами (created_by != 0).
    Возвращает список (lead_id, ts). Ботов/Систему (created_by == 0) отсеиваем у себя —
    серверный фильтр filter[created_by] в events ограничен 10 значениями, а менеджеров ~100."""
    out = []
    params = {
        'limit': EVENTS_PAGE,
        'filter[created_at][from]': ts_from,
        'filter[created_at][to]': ts_to,
        'filter[entity][]': 'lead',
    }
    url, first, page, seen = '/api/v4/events', True, 0, 0
    while url and page < 5000:
        data = ax.amo_get(url, params if first else None)
        first = False
        evs = (data.get('_embedded') or {}).get('events') or []
        if not evs:
            break
        for e in evs:
            seen += 1
            if str(e.get('created_by') or '0') == '0':
                continue   # бот / Система — не «обработка менеджером»
            eid = e.get('entity_id')
            if eid:
                out.append((eid, int(e.get('created_at') or 0)))
        page += 1
        if page % 25 == 0:
            print(f"  ...events страница {page}, просмотрено {seen}, менеджерских {len(out)}")
        url = ((data.get('_links') or {}).get('next') or {}).get('href')
        time.sleep(REQUEST_INTERVAL)
    print(f"  событий просмотрено: {seen}, менеджерских по сделкам: {len(out)}")
    return out


def fetch_leads(lead_ids):
    """Сделки по id (with=contacts) → id: {pipeline_id, responsible_user_id, contact_id}."""
    info = {}
    ids = list(lead_ids)
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        params = {'limit': 250, 'with': 'contacts'}
        for j, cid in enumerate(chunk):
            params[f'filter[id][{j}]'] = cid
        data = ax.amo_get('/api/v4/leads', params)
        for l in ((data.get('_embedded') or {}).get('leads') or []):
            info[l['id']] = {
                'pipeline_id': int(l.get('pipeline_id') or 0),
                'responsible': str(l.get('responsible_user_id') or ''),
                'contact_id': ax.get_main_contact_id(l),
            }
        time.sleep(REQUEST_INTERVAL)
    return info


def fetch_contacts(contact_ids):
    """Контакты по id → id: {name, phone, created_at}."""
    info = {}
    ids = [c for c in contact_ids if c]
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        params = {'limit': 250}
        for j, cid in enumerate(chunk):
            params[f'filter[id][{j}]'] = cid
        data = ax.amo_get('/api/v4/contacts', params)
        for c in ((data.get('_embedded') or {}).get('contacts') or []):
            info[str(c['id'])] = {
                'name': c.get('name') or '',
                'phone': ax.get_phone(c),
                'created_at': c.get('created_at'),
            }
        time.sleep(REQUEST_INTERVAL)
    return info


# ============================================================
#  Google Sheets
# ============================================================

def sheets_values():
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_SA_JSON), scopes=['https://www.googleapis.com/auth/spreadsheets'])
    return build('sheets', 'v4', credentials=creds, cache_discovery=False).spreadsheets()


def ensure_sheet(svc, title, need_rows, need_cols):
    meta = svc.get(spreadsheetId=SPREADSHEET_ID,
                   fields='sheets.properties(title,sheetId,gridProperties)').execute()
    props = {s['properties']['title']: s['properties'] for s in meta.get('sheets', [])}
    if title not in props:
        svc.batchUpdate(spreadsheetId=SPREADSHEET_ID, body={'requests': [
            {'addSheet': {'properties': {'title': title, 'gridProperties': {
                'rowCount': need_rows, 'columnCount': need_cols}}}}]}).execute()
        print(f"Создал лист «{title}»")
        return
    p = props[title]
    grid = p.get('gridProperties') or {}
    if (grid.get('rowCount') or 0) < need_rows or (grid.get('columnCount') or 0) < need_cols:
        svc.batchUpdate(spreadsheetId=SPREADSHEET_ID, body={'requests': [
            {'updateSheetProperties': {
                'properties': {'sheetId': p['sheetId'], 'gridProperties': {
                    'rowCount': max(grid.get('rowCount') or 0, need_rows),
                    'columnCount': max(grid.get('columnCount') or 0, need_cols)}},
                'fields': 'gridProperties.rowCount,gridProperties.columnCount'}}]}).execute()


def write_sheet(svc, rows):
    ensure_sheet(svc, SHEET_NAME, len(rows) + 10, len(COLUMNS))
    values = svc.values()
    # чистим только колонки выгрузки (правее не трогаем)
    values.clear(spreadsheetId=SPREADSHEET_ID,
                 range=f"'{SHEET_NAME}'!A:{col_letter(len(COLUMNS))}").execute()
    matrix = [COLUMNS] + [[safe_cell(r.get(c, '')) for c in COLUMNS] for r in rows]
    values.update(spreadsheetId=SPREADSHEET_ID, range=f"'{SHEET_NAME}'!A1",
                  valueInputOption='USER_ENTERED', body={'values': matrix}).execute()


# ============================================================
#  main
# ============================================================

def main():
    missing = [n for n, v in [('AMO_TOKEN', AMO_TOKEN),
                              ('GOOGLE_SERVICE_ACCOUNT_JSON', GOOGLE_SA_JSON)] if not v]
    if not DRY_RUN and not SPREADSHEET_ID:
        missing.append('SPREADSHEET_ID')
    if missing:
        print("ОШИБКА: нет переменных окружения: " + ", ".join(missing))
        sys.exit(1)

    ts_from, ts_to, d_from, d_to = period_bounds()
    print(f"Период: {d_from:%d.%m.%Y} — {d_to:%d.%m.%Y} (МСК)"
          + (" [DRY_RUN]" if DRY_RUN else ""))

    # менеджеры (для фильтра created_by и расшифровки ответственного)
    users = ax.amo_fetch_all('/api/v4/users', {'limit': 250}, 'users')
    user_map = {str(u['id']): (u.get('name') or '') for u in users}
    user_ids = [u['id'] for u in users]
    print(f"Менеджеров в amo: {len(user_ids)}")

    events = fetch_manager_lead_events(ts_from, ts_to)
    print(f"Событий менеджеров по сделкам за период: {len(events)}")

    lead_ids = {eid for eid, _ in events}
    leads = fetch_leads(lead_ids)
    first_line = {lid: v for lid, v in leads.items() if v['pipeline_id'] == PIPELINE_ID}
    print(f"Сделок затронуто: {len(leads)}; из них Первой линии: {len(first_line)}")

    # группируем по контакту: последнее касание + ответственный сделки этого касания
    by_contact = {}
    for lead_id, ts in events:
        v = first_line.get(lead_id)
        if not v:
            continue
        cid = str(v['contact_id'] or '')
        if not cid:
            continue
        cur = by_contact.get(cid)
        if cur is None or ts > cur['ts']:
            by_contact[cid] = {'ts': ts, 'responsible': v['responsible']}
    print(f"Контактов с касанием за период: {len(by_contact)}")

    contacts = fetch_contacts(by_contact.keys())

    rows = []
    for cid, d in by_contact.items():
        c = contacts.get(cid, {})
        rows.append({
            'ID контакта': cid,
            'Телефон': c.get('phone', ''),
            'Менеджер': user_map.get(d['responsible'], d['responsible']),
            'Дата последнего касания': fmt_dt(d['ts'], with_time=True),
            'Дата создания': fmt_dt(c.get('created_at'), with_time=False),
        })
    rows.sort(key=lambda r: r['Дата последнего касания'], reverse=True)

    per_mgr = {}
    for r in rows:
        m = r['Менеджер'] or '—'
        per_mgr[m] = per_mgr.get(m, 0) + 1
    per_mgr = dict(sorted(per_mgr.items(), key=lambda x: -x[1]))
    print(f"Строк к записи: {len(rows)}; по менеджерам:")
    for m, c in per_mgr.items():
        print(f"  {m} - {c}")

    if DRY_RUN:
        print("DRY_RUN — в таблицу не пишу.")
        return {'rows': len(rows), 'per_mgr': per_mgr, 'from': d_from, 'to': d_to}

    write_sheet(sheets_values(), rows)
    print(f"ГОТОВО. Контактов: {len(rows)}.")
    return {'rows': len(rows), 'per_mgr': per_mgr, 'from': d_from, 'to': d_to}


if __name__ == '__main__':
    try:
        s = main()
        if not DRY_RUN:
            lines = "\n".join(f"{m} - {c}" for m, c in s['per_mgr'].items()) or "—"
            send_telegram(
                f"✅ Активность Первой линии\n"
                f"Период: {s['from']:%d.%m.%Y} — {s['to']:%d.%m.%Y}\n"
                f"Контактов обработано: {s['rows']}\n\n"
                f"{lines}\n\n"
                f"Таблица: https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}"
                + run_url_line())
    except Exception as e:
        send_telegram(f"❌ Активность Первой линии: упало\n"
                      f"Ошибка: {type(e).__name__}: {str(e)[:300]}" + run_url_line())
        raise
