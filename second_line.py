#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Пайплайн «2-я линия»: целевые контакты (созданные с 01.01.2026) + их сделки.
Замена связки n8n: 8fx4n8y0vRFWBkJv (родитель) → 8M7GRpi6twP7A0KN (контакты) → 4kLwTMlkWBOWvsAt (сделки).

Логика 1-в-1 с n8n (текущее поведение, без «доизобретения» флагов брошенности):
  1. Тянем контакты amoCRM, созданные с CONTACTS_SINCE, с привязанными сделками.
  2. Оставляем «целевые»: Оборот>50, кол-во сотрудников>5, есть сделка в воронке «Первая линия» (8733326).
  3. Чистим лист «контакты второй линии» и пишем их (47 колонок).
  4. По их сделкам (воронки 8733326/9701010) тянем сами сделки + пользователей + воронки + заметки,
     считаем последнее касание/звонок/заметку, чистим лист «сделки второй линии» и пишем.

Запуск по расписанию (см. .github/workflows/second-line.yml). DRY_RUN=1 — без записи в таблицу.
"""

import os
import re
import sys
import json
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ============================================================
#  НАСТРОЙКИ
# ============================================================

AMO_BASE_URL = "https://pavelgitelman.amocrm.ru"

CONTACTS_SINCE = "2026-01-01"     # целевые контакты, созданные с этой даты (МСК)
FIRST_LINE = 8733326              # воронка «Первая линия» (обязательна у контакта)
WANT_PIPELINES = {8733326, 9701010}   # какие сделки попадают в колонки «Сделка 1..10»
OBOROT_GT = 50                   # порог по обороту
STAFF_GT = 5                     # порог по кол-ву сотрудников

# Таблица 2-й линии (отдельная, НЕ месячная). Дайте сервис-аккаунту доступ Редактора!
SECOND_LINE_SHEET_ID = "1_cI3G94UuGEDoHK58xSCX8Gb5UQSRB78SKVo9f321KY"
SHEET_CONTACTS = "контакты второй линии"
SHEET_DEALS = "сделки второй линии"

TIMEZONE = "Europe/Moscow"
MSK = ZoneInfo(TIMEZONE)

# ---- Секреты / режим ----
AMO_TOKEN = os.environ.get("AMO_TOKEN", "").strip()
if AMO_TOKEN[:7].lower() == "bearer ":
    AMO_TOKEN = AMO_TOKEN[7:].strip()
GOOGLE_SA_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")

CONTACTS_FROM_TS = int(datetime.strptime(CONTACTS_SINCE, "%Y-%m-%d").replace(tzinfo=MSK).timestamp())

# Апостроф в названии поля WhatsApp
_APO = chr(39)
WA_FIELD = "Твой номер телефона на What" + _APO + "s app"
WA_KEY = "Твой номер телефона на What" + _APO + "s app (контакт)"

CONTACT_COLUMNS = [
    'Ссылка на контакт', 'ID', 'Тип', 'Наименование', 'Имя', 'Фамилия', 'Компания', 'Ниша',
    'Оборот', 'кол-во сотрудников', 'Сайт', 'Дата создания', 'Создатель', 'Дата изменения',
    'Автор изменения', 'Теги', 'Ближайшая задача', 'Ответственный',
    'Сделка 1', 'Сделка 2', 'Сделка 3', 'Сделка 4', 'Сделка 5',
    'Сделка 6', 'Сделка 7', 'Сделка 8', 'Сделка 9', 'Сделка 10',
    'Должность (контакт)', 'Рабочий email', 'Личный email', 'Другой email',
    'Рабочий телефон', 'Мобильный телефон', 'Другой телефон',
    'WhatsappLid_WZ (контакт)', 'REGION TIME - Область или город (контакт)',
    'REGION TIME - Страна (контакт)', 'REGION TIME - Часовой пояс (контакт)',
    'TelegramUsername_WZ (контакт)', 'Username (контакт)', 'Instagram_WZ (контакт)',
    'Instagram (контакт)', WA_KEY, 'tg_platform_id (контакт)', 'tg_username (контакт)', 'ЦА',
]

DEAL_FIXED_COLUMNS = [
    'Контакт', 'Контакт Оборот', 'Контакт кол-во', 'Ссылка на контакт', 'Дата создания (контакт)',
    'Ссылка на сделку', 'ID сделки', 'Название', 'Воронка', 'Этап', 'Ответственный (сделка)',
    'Бюджет', 'Дата создания (сделка)', 'Дата изменения (сделка)', 'Дата закрытия',
    'Последнее касание (примеч.)', 'Дней без касания', 'Последний звонок', 'Тип посл. звонка',
    'Последняя заметка', 'Текст посл. заметки',
]


# ============================================================
#  Хелперы
# ============================================================

def num_of(v):
    m = re.search(r'-?\d+([.,]\d+)?', str('' if v is None else v))
    return float(m.group(0).replace(',', '.')) if m else None


def empty_or_gt(v, thr):
    s = str('' if v is None else v).strip()
    if s == '':
        return False
    n = num_of(s)
    return n is not None and n > thr


def is_true(v):
    return v in (True, 'true', 1, '1')


def fmt_dt(ts, with_seconds=False):
    if ts in (None, '', 0, '0'):
        return ''
    try:
        d = datetime.fromtimestamp(int(ts), tz=MSK)
    except (TypeError, ValueError):
        return ''
    return d.strftime('%d.%m.%Y %H:%M:%S' if with_seconds else '%d.%m.%Y %H:%M')


def cf_by_name(entity, name):
    for f in (entity.get('custom_fields_values') or []):
        if f.get('field_name') == name:
            vals = [v.get('value') for v in (f.get('values') or []) if v.get('value') not in (None, '')]
            return ', '.join(str(x) for x in vals)
    return ''


def cf_by_code(entity, code):
    for f in (entity.get('custom_fields_values') or []):
        if f.get('field_code') == code:
            return ', '.join(str(v.get('value')) for v in (f.get('values') or []))
    return ''


def cf_by_code_enum(entity, code, en):
    for f in (entity.get('custom_fields_values') or []):
        if f.get('field_code') == code:
            return ', '.join(str(v.get('value')) for v in (f.get('values') or [])
                             if str(v.get('enum_code') or '').upper() == en)
    return ''


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram-отбивка пропущена (нет TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True},
            timeout=30,
        )
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
    headers = {
        'Authorization': f'Bearer {AMO_TOKEN}',
        'Accept': 'application/json',
        'Content-Type': 'application/json',
    }
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


def fetch_contacts():
    out, page = [], 1
    while True:
        data = amo_get('/api/v4/contacts', {
            'limit': 250, 'page': page, 'with': 'leads',
            'filter[created_at][from]': CONTACTS_FROM_TS,
        })
        cs = (data.get('_embedded') or {}).get('contacts') or []
        if not cs:
            break
        out.extend(cs)
        page += 1
        time.sleep(0.3)
    return out


def fetch_leads_by_ids(ids):
    leads = []
    for chunk in chunked(list(ids), 100):
        data = amo_get('/api/v4/leads', {'limit': 250, 'filter[id][]': chunk})
        leads.extend((data.get('_embedded') or {}).get('leads') or [])
        time.sleep(0.2)
    return leads


def fetch_users():
    out, page = [], 1
    while page <= 10:
        data = amo_get('/api/v4/users', {'limit': 250, 'page': page})
        us = (data.get('_embedded') or {}).get('users') or []
        if not us:
            break
        out.extend(us)
        page += 1
        time.sleep(0.3)
    return out


def fetch_notes(deal_ids):
    by = {}
    for chunk in chunked(list(deal_ids), 15):
        data = amo_get('/api/v4/leads/notes', {
            'limit': 250, 'order[id]': 'desc', 'filter[entity_id][]': chunk,
        })
        for n in ((data.get('_embedded') or {}).get('notes') or []):
            eid = n.get('entity_id')
            ts = int(n.get('created_at') or 0)
            t = n.get('note_type')
            d = by.setdefault(eid, {'touch': 0, 'call': 0, 'callType': '', 'note': 0, 'noteText': ''})
            if ts > d['touch']:
                d['touch'] = ts
            if t in ('call_in', 'call_out') and ts > d['call']:
                d['call'] = ts
                d['callType'] = 'входящий' if t == 'call_in' else 'исходящий'
            if t == 'common' and ts > d['note']:
                d['note'] = ts
                d['noteText'] = (n.get('params') or {}).get('text') or ''
        time.sleep(0.2)
    return by


# ============================================================
#  Построение строк
# ============================================================

def build_contact_rows(contacts):
    deal_pipe = {}
    all_lead_ids = set()
    for c in contacts:
        for l in ((c.get('_embedded') or {}).get('leads') or []):
            if l.get('id') is not None:
                all_lead_ids.add(l['id'])
    for l in fetch_leads_by_ids(all_lead_ids):
        deal_pipe[l['id']] = int(l.get('pipeline_id') or 0)

    rows, seen = [], set()
    for c in contacts:
        if c['id'] in seen:
            continue
        seen.add(c['id'])
        if int(c.get('created_at') or 0) < CONTACTS_FROM_TS:
            continue
        oborot = cf_by_name(c, 'Оборот')
        kolvo = cf_by_name(c, 'кол-во сотрудников')
        if not empty_or_gt(oborot, OBOROT_GT):
            continue
        if not empty_or_gt(kolvo, STAFF_GT):
            continue
        lead_ids = [l['id'] for l in ((c.get('_embedded') or {}).get('leads') or [])]
        if not any(deal_pipe.get(i) == FIRST_LINE for i in lead_ids):
            continue

        emb = c.get('_embedded') or {}
        tags = ', '.join(t.get('name') for t in (emb.get('tags') or []) if t.get('name'))
        companies = emb.get('companies') or []
        company = companies[0].get('name') if companies and companies[0].get('name') else ''
        kept = sorted({i for i in lead_ids if deal_pipe.get(i) in WANT_PIPELINES}, reverse=True)

        row = {
            'Ссылка на контакт': f"{AMO_BASE_URL}/contacts/detail/{c['id']}",
            'ID': c['id'], 'Тип': 'контакт', 'Наименование': c.get('name') or '',
            'Имя': c.get('first_name') or '', 'Фамилия': c.get('last_name') or '',
            'Компания': company, 'Ниша': cf_by_name(c, 'Ниша'),
            'Оборот': oborot, 'кол-во сотрудников': kolvo,
            'Сайт': cf_by_code(c, 'WEB') or cf_by_name(c, 'Сайт'),
            'Дата создания': fmt_dt(c.get('created_at'), with_seconds=True),
            'Создатель': '', 'Дата изменения': fmt_dt(c.get('updated_at'), with_seconds=True),
            'Автор изменения': '', 'Теги': tags, 'Ближайшая задача': '', 'Ответственный': '',
            'Должность (контакт)': cf_by_code(c, 'POSITION') or cf_by_name(c, 'Должность'),
            'Рабочий email': cf_by_code_enum(c, 'EMAIL', 'WORK'),
            'Личный email': cf_by_code_enum(c, 'EMAIL', 'PRIV'),
            'Другой email': cf_by_code_enum(c, 'EMAIL', 'OTHER'),
            'Рабочий телефон': cf_by_code_enum(c, 'PHONE', 'WORK'),
            'Мобильный телефон': cf_by_code_enum(c, 'PHONE', 'MOB'),
            'Другой телефон': cf_by_code_enum(c, 'PHONE', 'OTHER'),
            'WhatsappLid_WZ (контакт)': cf_by_name(c, 'WhatsappLid_WZ'),
            'REGION TIME - Область или город (контакт)': cf_by_name(c, 'REGION TIME - Область или город'),
            'REGION TIME - Страна (контакт)': cf_by_name(c, 'REGION TIME - Страна'),
            'REGION TIME - Часовой пояс (контакт)': cf_by_name(c, 'REGION TIME - Часовой пояс'),
            'TelegramUsername_WZ (контакт)': cf_by_name(c, 'TelegramUsername_WZ'),
            'Username (контакт)': cf_by_name(c, 'Username'),
            'Instagram_WZ (контакт)': cf_by_name(c, 'Instagram_WZ'),
            'Instagram (контакт)': cf_by_name(c, 'Instagram'),
            WA_KEY: cf_by_name(c, WA_FIELD),
            'tg_platform_id (контакт)': cf_by_name(c, 'tg_platform_id'),
            'tg_username (контакт)': cf_by_name(c, 'tg_username'),
            'ЦА': cf_by_name(c, 'ЦА'),
        }
        for k in range(10):
            row['Сделка ' + str(k + 1)] = kept[k] if k < len(kept) else ''
        rows.append(row)
    return rows


def build_deal_rows(contact_rows, users, pipelines):
    user_map = {str(u['id']): (u.get('name') or '') for u in users}
    pipe_name, status_name = {}, {}
    for p in pipelines:
        pipe_name[str(p['id'])] = p.get('name') or ''
        for s in ((p.get('_embedded') or {}).get('statuses') or []):
            status_name[str(s['id'])] = s.get('name') or ''

    contact_by_deal = {}
    deal_ids = set()
    for r in contact_rows:
        if not (empty_or_gt(r.get('Оборот'), OBOROT_GT) and empty_or_gt(r.get('кол-во сотрудников'), STAFF_GT)):
            continue
        for k in range(1, 11):
            d = r.get('Сделка ' + str(k))
            if d not in (None, '', 0):
                did = str(d).strip()
                deal_ids.add(did)
                contact_by_deal.setdefault(did, {
                    'cname': r.get('Наименование'), 'ob': r.get('Оборот'), 'ko': r.get('кол-во сотрудников'),
                    'clink': r.get('Ссылка на контакт'), 'ccreated': r.get('Дата создания'),
                })
    if not deal_ids:
        return [], []

    deals = fetch_leads_by_ids(deal_ids)
    notes = fetch_notes([l['id'] for l in deals])

    checkbox_fields = set()
    custom_cols, custom_seen = [], set()
    for l in deals:
        for f in (l.get('custom_fields_values') or []):
            name = f.get('field_name') or ('field_' + str(f.get('field_id')))
            if f.get('field_type') == 'checkbox':
                checkbox_fields.add(name)
            if name not in custom_seen:
                custom_seen.add(name)
                custom_cols.append(name)

    rows, seen = [], set()
    for l in deals:
        if l['id'] in seen:
            continue
        seen.add(l['id'])
        ci = contact_by_deal.get(str(l['id']), {})
        nd = notes.get(l['id'], {})
        row = {
            'Контакт': ci.get('cname') or '', 'Контакт Оборот': ci.get('ob') or '',
            'Контакт кол-во': ci.get('ko') or '', 'Ссылка на контакт': ci.get('clink') or '',
            'Дата создания (контакт)': ci.get('ccreated') or '',
            'Ссылка на сделку': f"{AMO_BASE_URL}/leads/detail/{l['id']}",
            'ID сделки': l['id'], 'Название': l.get('name') or '',
            'Воронка': pipe_name.get(str(l.get('pipeline_id'))) or str(l.get('pipeline_id')),
            'Этап': status_name.get(str(l.get('status_id'))) or str(l.get('status_id')),
            'Ответственный (сделка)': user_map.get(str(l.get('responsible_user_id'))) or str(l.get('responsible_user_id') or ''),
            'Бюджет': l.get('price') or 0,
            'Дата создания (сделка)': fmt_dt(l.get('created_at')),
            'Дата изменения (сделка)': fmt_dt(l.get('updated_at')),
            'Дата закрытия': fmt_dt(l.get('closed_at')),
            'Последнее касание (примеч.)': fmt_dt(nd.get('touch')),
            'Дней без касания': (int((datetime.now(MSK).timestamp() - nd['touch']) // 86400) if nd.get('touch') else ''),
            'Последний звонок': fmt_dt(nd.get('call')),
            'Тип посл. звонка': nd.get('callType') or '',
            'Последняя заметка': fmt_dt(nd.get('note')),
            'Текст посл. заметки': nd.get('noteText') or '',
        }
        present = set()
        for f in (l.get('custom_fields_values') or []):
            name = f.get('field_name') or ('field_' + str(f.get('field_id')))
            present.add(name)
            ft = f.get('field_type')
            vals = f.get('values') or []
            if ft == 'checkbox':
                row[name] = 'да' if (vals and is_true(vals[0].get('value'))) else 'нет'
            elif ft in ('date', 'date_time'):
                row[name] = ', '.join(fmt_dt(v.get('value')) for v in vals)
            elif name == '1-линия ответственный':
                row[name] = ', '.join(str(user_map.get(str(v.get('value')), v.get('value'))) for v in vals)
            else:
                row[name] = ', '.join(str(v.get('value')) for v in vals)
        for cb in checkbox_fields:
            if cb not in present:
                row[cb] = 'нет'
        rows.append(row)

    return rows, DEAL_FIXED_COLUMNS + custom_cols


# ============================================================
#  Google Sheets
# ============================================================

def sheets_values():
    info = json.loads(GOOGLE_SA_JSON)
    creds = Credentials.from_service_account_info(
        info, scopes=['https://www.googleapis.com/auth/spreadsheets'])
    return build('sheets', 'v4', credentials=creds, cache_discovery=False).spreadsheets().values()


def write_sheet(values, sheet_name, columns, rows):
    values.clear(spreadsheetId=SECOND_LINE_SHEET_ID, range=f"'{sheet_name}'").execute()
    matrix = [columns] + [[r.get(c, '') for c in columns] for r in rows]
    values.update(
        spreadsheetId=SECOND_LINE_SHEET_ID,
        range=f"'{sheet_name}'!A1",
        valueInputOption='USER_ENTERED',
        body={'values': matrix},
    ).execute()


def main():
    missing = [n for n, v in [('AMO_TOKEN', AMO_TOKEN),
                              ('GOOGLE_SERVICE_ACCOUNT_JSON', GOOGLE_SA_JSON)] if not v]
    if missing:
        print("ОШИБКА: нет переменных окружения: " + ", ".join(missing))
        sys.exit(1)

    print(f"Целевые контакты с {CONTACTS_SINCE} (created_at >= {CONTACTS_FROM_TS})"
          + (" [DRY_RUN]" if DRY_RUN else ""))

    contacts = fetch_contacts()
    print(f"Контактов получено: {len(contacts)}")
    contact_rows = build_contact_rows(contacts)
    print(f"Целевых контактов: {len(contact_rows)}")

    users = fetch_users()
    pipelines = (amo_get('/api/v4/leads/pipelines').get('_embedded') or {}).get('pipelines') or []
    deal_rows, deal_columns = build_deal_rows(contact_rows, users, pipelines)
    print(f"Сделок по ним: {len(deal_rows)}")

    if DRY_RUN:
        print("DRY_RUN — в таблицу ничего не писали.")
        return {'contacts': len(contact_rows), 'deals': len(deal_rows)}

    values = sheets_values()
    write_sheet(values, SHEET_CONTACTS, CONTACT_COLUMNS, contact_rows)
    write_sheet(values, SHEET_DEALS, deal_columns or DEAL_FIXED_COLUMNS, deal_rows)

    print(f"ГОТОВО. Контактов: {len(contact_rows)}, сделок: {len(deal_rows)}.")
    return {'contacts': len(contact_rows), 'deals': len(deal_rows)}


if __name__ == '__main__':
    try:
        s = main()
        if not DRY_RUN:
            send_telegram(
                "✅ amoCRM 2-я линия: контакты и сделки выгружены\n"
                f"Целевых контактов: {s['contacts']}\n"
                f"Сделок: {s['deals']}\n"
                f"Таблица: https://docs.google.com/spreadsheets/d/{SECOND_LINE_SHEET_ID}"
                + run_url_line()
            )
    except Exception as e:
        send_telegram(
            "❌ amoCRM 2-я линия: ПАЙПЛАЙН УПАЛ\n"
            f"Ошибка: {type(e).__name__}: {str(e)[:300]}"
            + run_url_line()
        )
        raise
