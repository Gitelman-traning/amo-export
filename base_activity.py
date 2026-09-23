#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Активность менеджеров в воронках работы с базой: «Реанимация 1 линия»,
«Реанимация» и «Повторных диагностик».

Что делает:
  1. Тянет события смены этапа (lead_status_changed) за период — с 1-го числа
     месяца по ВЧЕРА (Москва), окнами по 3 дня (обход 408 на глубокой пагинации).
  2. Оставляет только перемещения ВНУТРИ наших воронок (воронка до = воронка после)
     и только те, что сделали нужные менеджеры (для «Повторных диагностик» — свой список).
  3. Схлопывает в одну строку на сделку: маршрут перемещений с датами, счётчик,
     кто двигал, текущий этап, должность / кол-во сотрудников / оборот.
  4. Пишет на лист месячной таблицы (SPREADSHEET_ID, лист по SHEET_GID).

Запуск:
  python base_activity.py               # текущий месяц по вчера
  DRY_RUN=1 python base_activity.py     # посчитать, в таблицу не писать
  DATE_FROM=01.09.2026 DATE_TO=15.09.2026 python base_activity.py

Переменные окружения: AMO_TOKEN, GOOGLE_SERVICE_ACCOUNT_JSON, SPREADSHEET_ID,
                      TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (необязательно).
"""

import os
import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ============================================================
#  НАСТРОЙКИ
# ============================================================

AMO_BASE_URL = "https://pavelgitelman.amocrm.ru"

SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "").strip()
SHEET_GID = os.environ.get("SHEET_GID", "").strip() or "1018816900"
SHEET_NAME = os.environ.get("SHEET_NAME", "").strip() or "выгрузка работа с базой клиентов"

DATE_FROM = os.environ.get("DATE_FROM", "").strip()
DATE_TO = os.environ.get("DATE_TO", "").strip()

MSK = ZoneInfo("Europe/Moscow")
AMO_PAGE_LIMIT = 100
REQUEST_INTERVAL = 0.15
WINDOW_DAYS = 3          # окно выкачки событий: глубокая пагинация падает с 408
MAX_PAGES = 5000
SHEETS_CHUNK = 5000

AMO_TOKEN = os.environ.get("AMO_TOKEN", "").strip()
if AMO_TOKEN[:7].lower() == "bearer ":
    AMO_TOKEN = AMO_TOKEN[7:].strip()
GOOGLE_SA_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")

# Воронки и кто в них считается. Имена — как в amo, id резолвим по справочнику
# пользователей: при пересоздании сотрудника имя переживает смену id.
REANIMATION_MANAGERS = [
    'Дарья Орещенко', 'Полина Плашенкова', 'Юлия Карпухина',
    'Алина Пруница', 'Маргарита Меленцова',
]
PIPELINES = {
    10637862: {'name': 'Реанимация 1 линия', 'short': 'Р1', 'managers': REANIMATION_MANAGERS},
    6609134:  {'name': 'Реанимация', 'short': 'Р', 'managers': REANIMATION_MANAGERS},
    10242466: {'name': 'Повторных диагностик', 'short': 'ПД', 'managers': ['Полина Ахметшина']},
}

# Поля сделки для отчёта: id → заголовок колонки
LEAD_FIELDS = [
    (1444185, 'Должность'),
    (1426297, 'Кол-во сотрудников'),
    (1442933, 'Оборот (млн ₽)'),
]

BASE_COLUMNS = [
    'Воронка', 'ID сделки', 'Сделка', 'Менеджер', 'Перемещений',
    'Первое перемещение', 'Последнее перемещение', 'Маршрут',
    'Текущий этап', 'Ответственный',
] + [title for _, title in LEAD_FIELDS] + ['Ссылка']

# Дальше идут колонки этапов — по одной на каждый этап каждой воронки, с датой
# попадания сделки на этот этап. Состав берётся из amo при запуске (stage_columns).
STAGE_SEP = ': '


# ============================================================
#  Утилиты
# ============================================================

def period_bounds():
    """С 1-го числа месяца ВЧЕРАШНЕГО дня по вчера включительно (Москва).
    По вчера — чтобы 1-го числа дописать прошлый месяц в старую таблицу,
    а не затереть её пустым новым месяцем."""
    ref = datetime.now(MSK) - timedelta(days=1)
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


def fmt_dt(ts, with_year=False):
    if not ts:
        return ''
    fmt = '%d.%m.%Y %H:%M' if with_year else '%d.%m %H:%M'
    return datetime.fromtimestamp(int(ts), tz=MSK).strftime(fmt)


def safe_cell(v):
    """Экранируем формульную инъекцию Google Sheets."""
    if v is None:
        return ''
    if isinstance(v, str) and v[:1] in ('=', '+', '-', '@'):
        return "'" + v
    return v


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram-отбивка пропущена (нет TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).")
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                          json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                                "disable_web_page_preview": True}, timeout=30)
        if r.status_code != 200:
            print(f"Telegram не отправлен ({r.status_code}): {r.text[:200]}")
    except Exception as ex:
        print(f"Telegram ошибка: {ex}")


def run_url_line():
    server = os.environ.get("GITHUB_SERVER_URL", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    return f"\nЛог: {server}/{repo}/actions/runs/{run_id}" if (server and repo and run_id) else ""


# ============================================================
#  amoCRM
# ============================================================

def amo_get(path, params=None):
    url = path if path.startswith('http') else AMO_BASE_URL + path
    headers = {'Authorization': f'Bearer {AMO_TOKEN}', 'Accept': 'application/json',
               'Content-Type': 'application/json'}
    for attempt in range(5):
        r = requests.get(url, headers=headers, params=params, timeout=90)
        if r.status_code == 204:
            return {}
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 502, 503, 504):
            wait = 2 ** attempt
            print(f"  amo {r.status_code}, повтор через {wait}с...")
            time.sleep(wait)
            continue
        raise RuntimeError(f"amoCRM {r.status_code}: {r.text[:300]} ({url})")
    raise RuntimeError(f"amoCRM: не удалось получить {url}")


def fetch_users():
    out, page = [], 1
    while page <= 10:
        data = amo_get('/api/v4/users', {'limit': 250, 'page': page})
        us = (data.get('_embedded') or {}).get('users') or []
        if not us:
            break
        out.extend(us)
        page += 1
        time.sleep(REQUEST_INTERVAL)
    return out


def fetch_pipelines():
    """pipeline_id → {status_id: название этапа} и порядок этапов наших воронок."""
    data = amo_get('/api/v4/leads/pipelines')
    names, order = {}, {}
    for p in ((data.get('_embedded') or {}).get('pipelines') or []):
        sts = (p.get('_embedded') or {}).get('statuses') or []
        names[p['id']] = {s['id']: s.get('name') or str(s['id']) for s in sts}
        if p['id'] in PIPELINES:
            order[p['id']] = [s['id'] for s in sorted(sts, key=lambda x: int(x.get('sort') or 0))]
    return names, order


def stage_columns(order, names):
    """Заголовки колонок этапов: «Р1: НДЗ 1». Ключ — (pipeline_id, status_id)."""
    cols, key_by = [], {}
    for pid, statuses in order.items():
        short = PIPELINES[pid]['short']
        for sid in statuses:
            title = f"{short}{STAGE_SEP}{names[pid].get(sid, sid)}"
            cols.append(title)
            key_by[(pid, sid)] = title
    return cols, key_by


def col_letter(n):
    """1 → A, 27 → AA (для диапазона очистки)."""
    s = ''
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def fetch_status_events(ts_from, ts_to, creator_ids):
    """События смены этапа за период, окнами по WINDOW_DAYS дней."""
    out = []
    win = WINDOW_DAYS * 86400
    start = ts_from
    while start <= ts_to:
        stop = min(start + win - 1, ts_to)
        params = {
            'limit': AMO_PAGE_LIMIT,
            'filter[type][]': 'lead_status_changed',
            'filter[entity]': 'lead',
            'filter[created_at][from]': start,
            'filter[created_at][to]': stop,
        }
        if creator_ids:
            params['filter[created_by][]'] = list(creator_ids)
        url, page = '/api/v4/events', 0
        while url and page < MAX_PAGES:
            data = amo_get(url, params if page == 0 else None)
            evs = (data.get('_embedded') or {}).get('events') or []
            if not evs:
                break
            out.extend(evs)
            page += 1
            url = ((data.get('_links') or {}).get('next') or {}).get('href')
            time.sleep(REQUEST_INTERVAL)
        print(f"  окно {fmt_dt(start)} — {fmt_dt(stop)}: событий всего {len(out)}")
        start = stop + 1
    return out


def fetch_leads_by_ids(ids):
    """Сделки батчами по id (нужны название, текущий этап, ответственный, поля)."""
    out = {}
    ids = list(ids)
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        data = amo_get('/api/v4/leads', {'limit': 250, 'filter[id][]': chunk})
        for l in ((data.get('_embedded') or {}).get('leads') or []):
            out[l['id']] = l
        time.sleep(REQUEST_INTERVAL)
        if (i // 50) % 10 == 0 and i:
            print(f"  ...сделок получено: {len(out)}/{len(ids)}")
    return out


def lead_field(lead, field_id):
    for f in (lead.get('custom_fields_values') or []):
        if f.get('field_id') == field_id:
            vals = [v.get('value') for v in (f.get('values') or []) if v.get('value') not in (None, '')]
            return vals[0] if vals else ''
    return ''


# ============================================================
#  Сборка строк
# ============================================================

def status_pair(ev):
    """(pipeline_id_до, status_id_до, pipeline_id_после, status_id_после)."""
    def one(key):
        items = ev.get(key) or []
        st = (items[0].get('lead_status') or {}) if items else {}
        return st.get('pipeline_id'), st.get('id')
    b_pipe, b_st = one('value_before')
    a_pipe, a_st = one('value_after')
    return b_pipe, b_st, a_pipe, a_st


def build_rows(events, user_map, pipes):
    """Одна строка на сделку: маршрут перемещений внутри воронки."""
    by_lead = {}
    for ev in sorted(events, key=lambda e: e.get('created_at') or 0):
        b_pipe, b_st, a_pipe, a_st = status_pair(ev)
        # только перемещения ВНУТРИ наших воронок
        if not a_pipe or a_pipe != b_pipe or a_pipe not in PIPELINES:
            continue
        author = user_map.get(ev.get('created_by'), '')
        if author not in PIPELINES[a_pipe]['managers']:
            continue
        lead_id = ev.get('entity_id')
        rec = by_lead.setdefault(lead_id, {'pipeline_id': a_pipe, 'steps': []})
        names = pipes.get(a_pipe) or {}
        rec['steps'].append({
            'ts': ev.get('created_at'),
            'author': author,
            'from': names.get(b_st, str(b_st)),
            'to': names.get(a_st, str(a_st)),
            'to_id': a_st,
        })
    return by_lead


def format_route(steps, single_author):
    parts = []
    for s in steps:
        tail = '' if single_author else f" ({s['author'].split()[0]})"
        parts.append(f"{fmt_dt(s['ts'])} {s['from']} → {s['to']}{tail}")
    return '; '.join(parts)


def make_matrix(by_lead, leads, user_map, pipes, stage_key):
    rows = []
    for lead_id, rec in by_lead.items():
        steps = rec['steps']
        authors = []
        for s in steps:
            if s['author'] not in authors:
                authors.append(s['author'])
        lead = leads.get(lead_id) or {}
        cur_pipe = lead.get('pipeline_id')
        cur_status = (pipes.get(cur_pipe) or {}).get(lead.get('status_id'), '')
        row = {
            'Воронка': PIPELINES[rec['pipeline_id']]['name'],
            'ID сделки': lead_id,
            'Сделка': lead.get('name') or '',
            'Менеджер': ', '.join(authors),
            'Перемещений': len(steps),
            'Первое перемещение': fmt_dt(steps[0]['ts'], with_year=True),
            'Последнее перемещение': fmt_dt(steps[-1]['ts'], with_year=True),
            'Маршрут': format_route(steps, len(authors) == 1),
            'Текущий этап': cur_status,
            'Ответственный': user_map.get(lead.get('responsible_user_id'), ''),
            'Ссылка': f"{AMO_BASE_URL}/leads/detail/{lead_id}",
        }
        for fid, title in LEAD_FIELDS:
            row[title] = lead_field(lead, fid)
        # дата попадания на каждый этап; если этап проходили несколько раз —
        # показываем последнюю дату и сколько всего заходов
        hits = {}
        for s in steps:
            hits.setdefault((rec['pipeline_id'], s['to_id']), []).append(s['ts'])
        for key, times in hits.items():
            col = stage_key.get(key)
            if not col:
                continue
            row[col] = (fmt_dt(times[-1], with_year=True)
                        + (f" (×{len(times)})" if len(times) > 1 else ''))
        rows.append(row)
    rows.sort(key=lambda r: (r['Воронка'], r['Менеджер'], -r['Перемещений']))
    return rows


# ============================================================
#  Google Sheets
# ============================================================

def sheets_service():
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_SA_JSON), scopes=['https://www.googleapis.com/auth/spreadsheets'])
    return build('sheets', 'v4', credentials=creds, cache_discovery=False).spreadsheets()


def resolve_sheet(svc):
    """Имя листа по SHEET_GID; если такого gid нет — по имени SHEET_NAME."""
    meta = svc.get(spreadsheetId=SPREADSHEET_ID,
                   fields='properties.title,sheets.properties(title,sheetId,gridProperties)').execute()
    sheets = [s['properties'] for s in meta.get('sheets', [])]
    by_gid = {str(p['sheetId']): p for p in sheets}
    if SHEET_GID in by_gid:
        return by_gid[SHEET_GID], meta
    by_name = {p['title']: p for p in sheets}
    if SHEET_NAME in by_name:
        print(f"[лист] gid {SHEET_GID} не найден — беру по имени «{SHEET_NAME}»")
        return by_name[SHEET_NAME], meta
    raise RuntimeError(f"В таблице нет листа с gid={SHEET_GID} и имени «{SHEET_NAME}». "
                       f"Есть: {', '.join(p['title'] for p in sheets)}")


def write_sheet(svc, rows, columns):
    props, meta = resolve_sheet(svc)
    title = props['title']
    matrix = [columns] + [[safe_cell(r.get(c, '')) for c in columns] for r in rows]
    need_rows, need_cols = len(matrix) + 100, len(columns)
    grid = props.get('gridProperties') or {}
    if (grid.get('rowCount') or 0) < need_rows or (grid.get('columnCount') or 0) < need_cols:
        svc.batchUpdate(spreadsheetId=SPREADSHEET_ID, body={'requests': [{'updateSheetProperties': {
            'properties': {'sheetId': props['sheetId'], 'gridProperties': {
                'rowCount': max(grid.get('rowCount') or 0, need_rows),
                'columnCount': max(grid.get('columnCount') or 0, need_cols)}},
            'fields': 'gridProperties.rowCount,gridProperties.columnCount'}}]}).execute()

    last_col = col_letter(need_cols)
    values = svc.values()
    values.clear(spreadsheetId=SPREADSHEET_ID, range=f"'{title}'!A:{last_col}").execute()
    for i in range(0, len(matrix), SHEETS_CHUNK):
        chunk = matrix[i:i + SHEETS_CHUNK]
        values.update(spreadsheetId=SPREADSHEET_ID, range=f"'{title}'!A{i + 1}",
                      valueInputOption='USER_ENTERED', body={'values': chunk}).execute()
        print(f"  записано строк: {min(i + SHEETS_CHUNK, len(matrix))}/{len(matrix)}")
    print(f"Готово: лист «{title}» таблицы «{(meta.get('properties') or {}).get('title')}»")


# ============================================================

def main():
    if not AMO_TOKEN:
        raise SystemExit("Нет AMO_TOKEN")
    if not SPREADSHEET_ID:
        raise SystemExit("Нет SPREADSHEET_ID")

    ts_from, ts_to, d_from, d_to = period_bounds()
    print(f"Период: {d_from:%d.%m.%Y} — {d_to:%d.%m.%Y} (Москва)")

    if GOOGLE_SA_JSON and not DRY_RUN:
        import month_guard
        month_guard.guard_or_exit("Активность по базе", SPREADSHEET_ID, d_to,
                                  GOOGLE_SA_JSON, send_telegram)

    users = fetch_users()
    user_map = {u['id']: u.get('name') or '' for u in users}
    wanted_names = {n for p in PIPELINES.values() for n in p['managers']}
    creator_ids = [uid for uid, name in user_map.items() if name in wanted_names]
    missing = wanted_names - {user_map[uid] for uid in creator_ids}
    if missing:
        print(f"ВНИМАНИЕ: не нашёл в amo пользователей: {', '.join(sorted(missing))}")
    print(f"Менеджеров в фильтре: {len(creator_ids)}")

    pipes, order = fetch_pipelines()
    stage_cols, stage_key = stage_columns(order, pipes)
    columns = BASE_COLUMNS + stage_cols
    print(f"Колонок: {len(columns)} (из них этапов — {len(stage_cols)})")
    print("Качаю события смены этапа...")
    events = fetch_status_events(ts_from, ts_to, creator_ids)
    print(f"Событий смены этапа получено: {len(events)}")

    by_lead = build_rows(events, user_map, pipes)
    moves = sum(len(r['steps']) for r in by_lead.values())
    print(f"Подходящих сделок: {len(by_lead)}, перемещений: {moves}")

    leads = fetch_leads_by_ids(by_lead.keys()) if by_lead else {}
    rows = make_matrix(by_lead, leads, user_map, pipes, stage_key)

    # сводка по менеджерам — в лог и в Telegram
    per_manager = {}
    for rec in by_lead.values():
        for s in rec['steps']:
            per_manager[s['author']] = per_manager.get(s['author'], 0) + 1
    summary = '\n'.join(f"{n} - {c}" for n, c in
                        sorted(per_manager.items(), key=lambda x: -x[1]))
    print("\nПеремещений по менеджерам:\n" + (summary or '  нет'))

    if DRY_RUN:
        print("\nDRY_RUN — в таблицу не пишу.")
        for r in rows[:10]:
            print(f"  {r['Воронка']} | {r['ID сделки']} | {r['Менеджер']} | "
                  f"{r['Перемещений']} | {r['Маршрут'][:90]}")
        return

    write_sheet(sheets_service(), rows, columns)
    send_telegram(f"Активность по базе за {d_from:%d.%m} — {d_to:%d.%m}: "
                  f"{len(rows)} сделок, {moves} перемещений.\n\n"
                  f"{summary or 'перемещений нет'}{run_url_line()}")


if __name__ == '__main__':
    main()
