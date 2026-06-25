#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Выгрузка сделок (+контактов) из amoCRM в Google Sheets.

Замена n8n-воркфлоу "Амо выгрузка авто" (xxwtEZjp3rUrzjr1).
Запускается по расписанию через GitHub Actions (см. .github/workflows/amo-export.yml),
либо вручную:  python amo_export.py

Логика 1-в-1 повторяет n8n-версию:
  1. Берём период: сделки, созданные за последние 3 месяца до вчера (по Москве).
  2. Тянем справочники (пользователи, воронки/этапы) из amoCRM.
  3. Тянем сделки трёх воронок постранично (with=contacts,loss_reason).
  4. Разворачиваем каждую сделку в строку с ~70 колонками (кастомные поля по именам).
  5. Догружаем основные контакты сделок и обогащаем строки (телефон, tg, должность...).
  6. Чистим старые данные в таблице и пишем новые.
"""

import os
import re
import sys
import json
import time
import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from dateutil.relativedelta import relativedelta
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ============================================================
#  НАСТРОЙКИ — здесь всё, что можно безопасно менять руками.
#  Секреты (токены) сюда НЕ пишем — они приходят из окружения (см. ниже).
# ============================================================

AMO_BASE_URL = "https://pavelgitelman.amocrm.ru"

# Воронки, которые выгружаем (id из amoCRM)
PIPELINE_IDS = [8733326, 9701010, 7295078]

# Глубина периода: сделки, созданные за последние N месяцев до вчера
MONTHS_BACK = 3

# Google-таблица назначения.
# ID берём из переменной SPREADSHEET_ID (GitHub: Settings → Variables), чтобы менять
# таблицу каждый месяц без правки кода. Если переменная пустая — значение по умолчанию.
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "").strip() or "1GyCp56dqcAMykbNUjU64gd40ZYZ4aNXgVsjCfcBTFzk"
SHEET_NAME = "общая выгрузка от Никиты"   # вкладка (название одинаковое во всех месячных файлах)
HEADER_ROW = 2          # строка с заголовками колонок в таблице
DATA_START_ROW = 15     # с какой строки писать данные (строки 3-14 — шапка/формулы)
LAST_COLUMN = "DZ"      # правая граница диапазона для очистки (с запасом)
ROWS_TO_CLEAR = 10000   # сколько строк данных чистить перед записью

# Куда писать дату выгрузки. Пусто ("") = не писать. Пример: "C1"
EXPORT_DATE_CELL = ""

TIMEZONE = "Europe/Moscow"
AMO_PAGE_LIMIT = 250         # сколько сделок за одну страницу amo
CONTACTS_PER_REQUEST = 200   # сколько контактов за один запрос
REQUEST_INTERVAL = 0.5       # пауза между запросами к amo, сек

# ---- Секреты: берём из переменных окружения (GitHub Secrets / .env локально) ----
AMO_TOKEN = os.environ.get("AMO_TOKEN", "").strip()
# Если токен записали с префиксом "Bearer " — срезаем, его добавит сам скрипт.
if AMO_TOKEN[:7].lower() == "bearer ":
    AMO_TOKEN = AMO_TOKEN[7:].strip()
GOOGLE_SA_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()

# Телеграм-уведомления (необязательно). Если не заданы — отбивка просто пропускается.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()


# ============================================================
#  Справочники соответствия кастомных полей amoCRM → колонки таблицы
#  (перенесены 1-в-1 из code-нод n8n)
# ============================================================

DATE_FIELDS = {
    'Дата вступил в чат', 'Дата взят в работу', 'Дата взят в работу ответил',
    'Дата Квалифицирован не назначили', 'Дата Квалифицирован прогрев',
    'дата ответил на 3 вопроса', 'Ответил не интересно', 'Дата Встреча назначена',
    'Дата Встреча подтверждена', 'Дата встреча перенесена', 'Дата и время диагностики',
    'На какую дату назначили диагностику', 'Дата Встреча проведена', 'Дата ЗИН 1-линия',
    'Дата Новый лид', 'Дата Диагностика проведена', 'Дата подали заявку на комитет',
    'Дата Повторная диагностика проведена', 'Дата Отбор проведен',
    'Дата Предложение согласовано', 'Дата клиент отреагировал', 'Дата Клиент подт. дату $',
    'Дата Договор / счет отправлен', 'Дата Предоплата / оплата получена',
    'Дата 100% оплаты получено', 'Дата ЗИН 2-линия', 'Дата Клиент прислал чек',
    'Дата изменения', 'Дата по договоренности доплаты',
}

FIELD_ALIASES = {
    'Источник': ['Источник', 'Источник сделки', 'Источник лида', 'Источник заявки', 'source', 'SOURCE'],
    'utm_medium': ['utm_medium', 'UTM_MEDIUM', 'utm medium'],
    'utm_campaign': ['utm_campaign', 'UTM_CAMPAIGN', 'utm campaign'],
    'utm_source': ['utm_source', 'UTM_SOURCE', 'utm source'],
    'Ссылка zoom запись': ['Ссылка zoom запись', 'Ссылка Zoom запись', 'Ссылка на zoom запись',
                           'Zoom запись', 'Ссылка записи Zoom', 'Ссылка на запись Zoom'],
    'Код. зума': ['Код. зума', 'Код зума', 'Код Zoom', 'Zoom code'],
    'Месяц участия': ['Месяц участия'],
    'Оборот компани (млн рублей в год)': ['Оборот компани (млн рублей в год)',
                                          'Оборот компании (млн рублей в год)', 'Оборот компании',
                                          'Оборот компании в год'],
    'Кол-во сотрудников': ['Кол-во сотрудников', 'Количество сотрудников'],
    'Должность': ['Должность'],
    'Квалификация': ['Квалификация'],
    'Ниша': ['Ниша'],
    'Дата вступил в чат': ['Дата вступил в чат', 'Дата вступления в чат', 'Дата вступления'],
    'Дата взят в работу': ['Дата взят в работу'],
    'Дата взят в работу ответил': ['Дата взят в работу ответил'],
    'Дата Квалифицирован не назначили': ['Дата Квалифицирован не назначили'],
    'Дата Квалифицирован прогрев': ['Дата Квалифицирован прогрев'],
    'дата ответил на 3 вопроса': ['дата ответил на 3 вопроса', 'Дата ответил на 3 вопроса'],
    'Ответил не интересно': ['Ответил не интересно'],
    'Дата Встреча назначена': ['Дата Встреча назначена'],
    'Дата Встреча подтверждена': ['Дата Встреча подтверждена'],
    'Дата встреча перенесена': ['Дата встреча перенесена'],
    'Дата и время диагностики': ['Дата и время диагностики'],
    'На какую дату назначили диагностику': ['На какую дату назначили диагностику'],
    'Дата Встреча проведена': ['Дата Встреча проведена'],
    'Дата Диагностика проведена': ['Дата Диагностика проведена', 'Дата диагностика проведена',
                                   'Дата диагностики проведена'],
    'Дата Повторная диагностика проведена': ['Дата Повторная диагностика проведена',
                                             'Дата повторная диагностика проведена',
                                             'Дата повторной диагностики проведена',
                                             'Дата повторной диагностики'],
    'Дата ЗИН 1-линия': ['Дата ЗИН 1-линия', 'Дата ЗИН 1 линия', 'Дата ЗИН 1-линии'],
    'галочка по фильтру (1-л)': ['галочка по фильтру (1-л)', 'ЗИН 1-линия', 'ЗИН 1 линия'],
    'Дата ЗИН 2-линия': ['Дата ЗИН 2-линия', 'Дата ЗИН 2 линия', 'Дата ЗИН 2-линии'],
    'галочка по фильтру (2-л)': ['галочка по фильтру (2-л)', 'ЗИН 2-линия', 'ЗИН 2 линия'],
    '1-линия ответственный': ['1-линия ответственный', '1 линия ответственный',
                              'Ответственный 1-линия', 'Ответственный 1 линия'],
    'Дата Новый лид': ['Дата Новый лид', 'Дата нового лида'],
    'Дата подали заявку на комитет': ['Дата подали заявку на комитет', 'Дата подачи заявки на комитет',
                                      'Дата заявка на комитет', 'Дата заявки на комитет', 'Дата комитет',
                                      'Дата отправки на комитет', 'Дата подача на комитет'],
    'Дата Отбор проведен': ['Дата Отбор проведен', 'Дата отбор проведен', 'Дата отбора'],
    'Дата Предложение согласовано': ['Дата Предложение согласовано', 'Дата предложение согласовано'],
    'Дата клиент отреагировал': ['Дата клиент отреагировал', 'Дата клиент отреагирова',
                                 'Дата клиент отреагировал на предложение'],
    'Дата Клиент подт. дату $': ['Дата Клиент подт. дату $', 'Дата клиент подт. дату $',
                                 'Дата Клиент подтвердил дату $', 'Дата клиент подтвердил дату $',
                                 'Дата подтверждения даты оплаты', 'Дата подтвердил дату оплаты',
                                 'Дата подтверждения оплаты', 'Клиент подт. дату $',
                                 'Клиент подтвердил дату $'],
    'Дата Договор / счет отправлен': ['Дата Договор / счет отправлен', 'Дата Договор / счёт отправлен',
                                      'Дата договор / счет отправлен', 'Дата договор / счёт отправлен',
                                      'Дата счет отправлен', 'Дата счёт отправлен', 'Дата счет выставлен',
                                      'Дата счёт выставлен', 'Дата счета выставлен', 'Дата счёта выставлен',
                                      'Дата выставлен счет', 'Дата выставлен счёт'],
    'Дата Предоплата / оплата получена': ['Дата Предоплата / оплата получена',
                                          'Предоплата / оплата получена', 'Дата предоплата получена',
                                          'Дата оплата получена'],
    'Дата 100% оплаты получено': ['Дата 100% оплаты получено', 'Дата 100% оплаты получена',
                                  '100% оплаты получено', '100% оплаты получена'],
    'Дата Клиент прислал чек': ['Дата Клиент прислал чек', 'Дата клиент прислал чек',
                                'Дата прислал чек', 'Дата чек прислал', 'Дата отправил чек',
                                'Дата клиент отправил чек', 'Дата получения чека'],
    'Договор подписан': ['Договор подписан'],
    'Не лид': ['Не лид'],
    'Название компании': ['Название компании'],
    'ИНН': ['ИНН'],
    'Сумма договора': ['Сумма договора'],
    'Дата по договоренности доплаты': ['Дата по договоренности доплаты',
                                       'Дата договоренности доплаты', 'Дата доплаты'],
    'Сумма доплаты': ['Сумма доплаты'],
    'Способ оплаты': ['Способ оплаты'],
}

# Поля, которые добираем из основного контакта сделки
CONTACT_DATE_FIELDS = {'Дата вступил в чат'}
CONTACT_FIELD_ALIASES = {
    'Дата вступил в чат': ['Дата вступил в чат', 'Дата вступления в чат', 'Дата вступления'],
    'Оборот компани (млн рублей в год)': ['Оборот компани (млн рублей в год)',
                                          'Оборот компании (млн рублей в год)', 'Оборот компании',
                                          'Оборот компании в год'],
    'Рабочий телефон (контакт)': ['Рабочий телефон', 'Телефон', 'PHONE'],
    'tg_username (контакт)': ['tg_username', 'tg username', 'Telegram', 'Telegram username', 'username'],
    'Должность': ['Должность', 'POSITION'],
    'Кол-во сотрудников': ['Кол-во сотрудников', 'Количество сотрудников'],
    'Ниша': ['Ниша'],
}


# ============================================================
#  Вспомогательные функции
# ============================================================

def normalize_name(value):
    return re.sub(r'\s+', ' ', str(value or '').strip().lower())


def to_sheet_serial_date(ts):
    """Unix-секунды → серийная дата Google Sheets (целое число дней), +3ч (МСК)."""
    if ts in (None, '', 0, '0'):
        return ''
    try:
        num = float(ts)
    except (TypeError, ValueError):
        return ''
    if math.isnan(num) or num < 1000000000:
        return ''
    serial = (num + 3 * 3600) / 86400 + 25569  # 25569 = дней между 1899-12-30 и 1970-01-01
    return int(math.floor(serial))


def fmt_msk_datetime(ts):
    """Unix-секунды → текст 'dd.MM.yyyy HH:mm:ss' по Москве."""
    try:
        num = float(ts)
    except (TypeError, ValueError):
        return ''
    if not num or num < 1000000000:
        return ''
    d = datetime.fromtimestamp(num, tz=timezone.utc) + timedelta(hours=3)
    return d.strftime('%d.%m.%Y %H:%M:%S')


def is_empty(v):
    return v is None or v == ''


def get_lead_field(lead, column):
    """Достаёт значение кастомного поля сделки по списку возможных имён (с учётом дат)."""
    names = FIELD_ALIASES.get(column, [column])
    is_date = column in DATE_FIELDS
    return _custom_value_ordered(lead, names, is_date)


def _custom_value_ordered(entity, field_names, is_date=False):
    """Идёт по алиасам по порядку, возвращает первое непустое значение (логика Normalize)."""
    names = field_names if isinstance(field_names, list) else [field_names]
    norm = [normalize_name(n) for n in names]
    fields = (entity or {}).get('custom_fields_values') or []
    for alias in norm:
        matched = [f for f in fields
                   if normalize_name(f.get('field_name')) == alias
                   or normalize_name(f.get('field_code')) == alias]
        for field in matched:
            out = _extract_values(field, is_date)
            if out:
                return out
    return ''


def _custom_value_any(entity, field_names, is_date=False):
    """Берёт первое поле, чьё имя/код совпало с любым алиасом (логика Enrich для контактов)."""
    if not entity:
        return ''
    names = field_names if isinstance(field_names, list) else [field_names]
    norm = set(normalize_name(n) for n in names)
    fields = entity.get('custom_fields_values') or []
    for f in fields:
        if normalize_name(f.get('field_name')) in norm or normalize_name(f.get('field_code')) in norm:
            return _extract_values(f, is_date)
    return ''


def _extract_values(field, is_date):
    vals = field.get('values') or []
    out = []
    for v in vals:
        value = v.get('value', '')
        if value is None:
            value = ''
        if is_date and value != '':
            value = to_sheet_serial_date(value)
        if value != '':
            out.append(value)
    if not out:
        return ''
    if len(out) == 1:
        return out[0]
    return ', '.join(str(x) for x in out)


def get_contact_field(contact, column):
    names = CONTACT_FIELD_ALIASES.get(column, [column])
    is_date = column in CONTACT_DATE_FIELDS
    return _custom_value_any(contact, names, is_date)


def get_phone(contact):
    if not contact:
        return ''
    for f in (contact.get('custom_fields_values') or []):
        if normalize_name(f.get('field_code')) == 'phone' or normalize_name(f.get('field_name')) == 'телефон':
            vals = [v.get('value', '') for v in (f.get('values') or []) if v.get('value')]
            return ', '.join(str(x) for x in vals)
    return ''


def get_tags(lead):
    tags = (lead.get('_embedded') or {}).get('tags') or []
    return ', '.join(t.get('name') for t in tags if t.get('name'))


def get_main_contact_id(lead):
    contacts = (lead.get('_embedded') or {}).get('contacts') or []
    main = next((c for c in contacts if c.get('is_main')), None)
    if main:
        return main.get('id') or ''
    return contacts[0].get('id') if contacts else ''


def get_loss_reason_name(lead):
    e = (lead.get('_embedded') or {}).get('loss_reason')
    if isinstance(e, list) and e:
        return e[0].get('name') or ''
    if isinstance(e, dict):
        return e.get('name') or ''
    return ''


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# ============================================================
#  Клиент amoCRM
# ============================================================

def amo_get(url, params=None):
    """GET к amoCRM с авторизацией и повторами при 429/5xx."""
    if not url.startswith('http'):
        url = AMO_BASE_URL + url
    headers = {
        'Authorization': f'Bearer {AMO_TOKEN}',
        'Accept': 'application/json',
        'Content-Type': 'application/json',
    }
    for attempt in range(5):
        r = requests.get(url, headers=headers, params=params, timeout=60)
        if r.status_code == 204:        # amo: нет данных
            return {}
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 502, 503, 504):
            wait = 2 ** attempt
            print(f"  amo вернул {r.status_code}, повтор через {wait}с...")
            time.sleep(wait)
            continue
        raise RuntimeError(f"amoCRM {r.status_code}: {r.text[:300]}  ({url})")
    raise RuntimeError(f"amoCRM: не удалось получить {url} после 5 попыток")


def amo_fetch_all(path, params, embed_key):
    """Постраничная выгрузка по _links.next.href."""
    out = []
    url = AMO_BASE_URL + path
    first = True
    while url:
        data = amo_get(url, params if first else None)
        first = False
        if not data:
            break
        out.extend((data.get('_embedded') or {}).get(embed_key) or [])
        url = ((data.get('_links') or {}).get('next') or {}).get('href')
        if url:
            time.sleep(REQUEST_INTERVAL)
    return out


# ============================================================
#  Google Sheets
# ============================================================

def send_telegram(text):
    """Шлёт сообщение в Telegram. Если секреты не заданы — тихо пропускает."""
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
    """Ссылка на лог прогона в GitHub Actions (если запущено там)."""
    server = os.environ.get("GITHUB_SERVER_URL", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if server and repo and run_id:
        return f"\nЛог: {server}/{repo}/actions/runs/{run_id}"
    return ""


def sheets_values():
    info = json.loads(GOOGLE_SA_JSON)
    creds = Credentials.from_service_account_info(
        info, scopes=['https://www.googleapis.com/auth/spreadsheets'])
    svc = build('sheets', 'v4', credentials=creds, cache_discovery=False)
    return svc.spreadsheets().values()


# ============================================================
#  Основная логика
# ============================================================

def build_row(lead, user_map, pipeline_map, status_map, pipeline_status_map):
    main_contact_id = get_main_contact_id(lead)
    responsible_id = str(lead.get('responsible_user_id') or '')
    created_by = str(lead.get('created_by') or '')
    updated_by = str(lead.get('updated_by') or '')
    pid = str(lead.get('pipeline_id') or '')
    sid = str(lead.get('status_id') or '')
    first_line = get_lead_field(lead, '1-линия ответственный')

    row = {
        'ID': lead.get('id') or '',
        'Название сделки': lead.get('name') or '',
        'Компания': '',
        'Основной контакт': main_contact_id,
        'Компания контакта': '',
        'Ответственный': user_map.get(responsible_id) or lead.get('responsible_user_id') or '',
        'Этап сделки': pipeline_status_map.get(f"{pid}:{sid}") or status_map.get(sid) or lead.get('status_id') or '',
        'Воронка': pipeline_map.get(pid) or lead.get('pipeline_id') or '',
        'Бюджет': lead.get('price', '') if lead.get('price') is not None else '',
        'Дата создания': to_sheet_serial_date(lead.get('created_at')),
        'Кем создана': user_map.get(created_by) or lead.get('created_by') or '',
        'Дата изменения': fmt_msk_datetime(lead.get('updated_at')),   # текст (= n8n "Fix fields")
        'Кем изменена': user_map.get(updated_by) or lead.get('updated_by') or '',
        'Теги сделки': get_tags(lead),
        'Ближайшая задача': '',
        'Дата закрытия': to_sheet_serial_date(lead.get('closed_at')) if lead.get('closed_at') else 'не закрыта',

        'Источник': get_lead_field(lead, 'Источник'),
        'utm_medium': get_lead_field(lead, 'utm_medium'),
        'utm_campaign': get_lead_field(lead, 'utm_campaign'),
        'utm_source': get_lead_field(lead, 'utm_source'),

        'Ссылка zoom запись': get_lead_field(lead, 'Ссылка zoom запись'),
        'Код. зума': get_lead_field(lead, 'Код. зума'),
        'Месяц участия': get_lead_field(lead, 'Месяц участия'),
        'Оборот компани (млн рублей в год)': get_lead_field(lead, 'Оборот компани (млн рублей в год)'),
        'Кол-во сотрудников': get_lead_field(lead, 'Кол-во сотрудников'),
        'Должность': get_lead_field(lead, 'Должность'),
        'Ниша': get_lead_field(lead, 'Ниша'),

        'Дата вступил в чат': get_lead_field(lead, 'Дата вступил в чат'),
        'Дата взят в работу': get_lead_field(lead, 'Дата взят в работу'),
        'Дата взят в работу ответил': get_lead_field(lead, 'Дата взят в работу ответил'),
        'Квалификация': get_lead_field(lead, 'Квалификация'),
        'Дата Квалифицирован не назначили': get_lead_field(lead, 'Дата Квалифицирован не назначили'),
        'Дата Квалифицирован прогрев': get_lead_field(lead, 'Дата Квалифицирован прогрев'),
        'дата ответил на 3 вопроса': get_lead_field(lead, 'дата ответил на 3 вопроса'),
        'Ответил не интересно': get_lead_field(lead, 'Ответил не интересно'),

        'Дата Встреча назначена': get_lead_field(lead, 'Дата Встреча назначена'),
        'Дата Встреча подтверждена': get_lead_field(lead, 'Дата Встреча подтверждена'),
        'Дата встреча перенесена': get_lead_field(lead, 'Дата встреча перенесена'),
        'Дата и время диагностики': get_lead_field(lead, 'Дата и время диагностики'),
        'На какую дату назначили диагностику': get_lead_field(lead, 'На какую дату назначили диагностику'),
        'Дата Встреча проведена': get_lead_field(lead, 'Дата Встреча проведена'),

        'Дата ЗИН 1-линия': get_lead_field(lead, 'Дата ЗИН 1-линия'),
        '1-линия ответственный': (user_map.get(str(first_line), first_line) if not is_empty(first_line) else ''),
        'галочка по фильтру (1-л)': get_lead_field(lead, 'галочка по фильтру (1-л)'),
        'галочка по фильтру (2-л)': get_lead_field(lead, 'галочка по фильтру (2-л)'),
        'Дата ЗИН 2-линия': get_lead_field(lead, 'Дата ЗИН 2-линия'),

        'Дата Новый лид': get_lead_field(lead, 'Дата Новый лид'),
        'Дата Диагностика проведена': get_lead_field(lead, 'Дата Диагностика проведена'),
        'Дата подали заявку на комитет': get_lead_field(lead, 'Дата подали заявку на комитет'),
        'Дата Повторная диагностика проведена': get_lead_field(lead, 'Дата Повторная диагностика проведена'),
        'Дата Отбор проведен': get_lead_field(lead, 'Дата Отбор проведен'),
        'Дата Предложение согласовано': get_lead_field(lead, 'Дата Предложение согласовано'),
        'Дата клиент отреагировал': get_lead_field(lead, 'Дата клиент отреагировал'),
        'Дата Клиент подт. дату $': get_lead_field(lead, 'Дата Клиент подт. дату $'),
        'Дата Договор / счет отправлен': get_lead_field(lead, 'Дата Договор / счет отправлен'),
        'Дата Предоплата / оплата получена': get_lead_field(lead, 'Дата Предоплата / оплата получена'),
        'Дата 100% оплаты получено': get_lead_field(lead, 'Дата 100% оплаты получено'),
        'Дата Клиент прислал чек': get_lead_field(lead, 'Дата Клиент прислал чек'),

        'Договор подписан': get_lead_field(lead, 'Договор подписан'),
        'Не лид': get_lead_field(lead, 'Не лид'),
        'Название компании': get_lead_field(lead, 'Название компании'),
        'ИНН': get_lead_field(lead, 'ИНН'),
        'Сумма договора': get_lead_field(lead, 'Сумма договора'),
        'Дата по договоренности доплаты': get_lead_field(lead, 'Дата по договоренности доплаты'),
        'Сумма доплаты': get_lead_field(lead, 'Сумма доплаты'),
        'Способ оплаты': get_lead_field(lead, 'Способ оплаты'),

        'Причина отказа': get_loss_reason_name(lead),   # имя (= n8n "Fix fields")

        'Рабочий телефон (контакт)': '',
        'tg_username (контакт)': '',

        # технические — в таблицу не пишутся (нет таких заголовков), нужны для обогащения
        '_main_contact_id': main_contact_id,
    }
    return row


def enrich_row(row, contact_map):
    contact = contact_map.get(str(row.get('_main_contact_id') or ''))
    contact_name = (contact.get('name') if contact else '') or row.get('Основной контакт') or ''

    row['Основной контакт'] = contact_name
    if is_empty(row['Дата вступил в чат']):
        row['Дата вступил в чат'] = get_contact_field(contact, 'Дата вступил в чат')
    if is_empty(row['Оборот компани (млн рублей в год)']):
        row['Оборот компани (млн рублей в год)'] = get_contact_field(contact, 'Оборот компани (млн рублей в год)')
    row['Рабочий телефон (контакт)'] = get_phone(contact) or get_contact_field(contact, 'Рабочий телефон (контакт)')
    row['tg_username (контакт)'] = get_contact_field(contact, 'tg_username (контакт)')
    if is_empty(row['Должность']):
        row['Должность'] = get_contact_field(contact, 'Должность')
    if is_empty(row['Кол-во сотрудников']):
        row['Кол-во сотрудников'] = get_contact_field(contact, 'Кол-во сотрудников')
    if is_empty(row['Ниша']):
        row['Ниша'] = get_contact_field(contact, 'Ниша')
    return row


def main():
    # --- проверка секретов ---
    missing = []
    if not AMO_TOKEN:
        missing.append('AMO_TOKEN')
    if not GOOGLE_SA_JSON:
        missing.append('GOOGLE_SERVICE_ACCOUNT_JSON')
    if missing:
        print("ОШИБКА: не заданы переменные окружения: " + ', '.join(missing))
        print("Локально — создайте .env по образцу .env.example; в GitHub — задайте Secrets.")
        sys.exit(1)

    msk = ZoneInfo(TIMEZONE)
    yesterday = datetime.now(msk) - timedelta(days=1)
    date_to_dt = yesterday.replace(hour=23, minute=59, second=59, microsecond=0)
    date_from_dt = (yesterday - relativedelta(months=MONTHS_BACK)).replace(hour=0, minute=0, second=0, microsecond=0)
    date_to_ts = int(date_to_dt.timestamp())
    date_from_ts = int(date_from_dt.timestamp())
    date_to_text = yesterday.strftime('%d.%m.%Y')

    print(f"Период выгрузки: {date_from_dt:%d.%m.%Y} — {date_to_dt:%d.%m.%Y} (created_at, МСК)")

    # --- справочники ---
    print("Тяну пользователей и воронки...")
    users = amo_fetch_all('/api/v4/users', {'limit': 250}, 'users')
    pipelines_data = amo_get('/api/v4/leads/pipelines')
    pipelines = (pipelines_data.get('_embedded') or {}).get('pipelines') or []

    user_map = {str(u['id']): (u.get('name') or '') for u in users}
    pipeline_map, status_map, pipeline_status_map = {}, {}, {}
    for p in pipelines:
        pid = str(p['id'])
        pipeline_map[pid] = p.get('name') or ''
        for s in ((p.get('_embedded') or {}).get('statuses') or []):
            sid = str(s['id'])
            status_map[sid] = s.get('name') or ''
            pipeline_status_map[f"{pid}:{sid}"] = s.get('name') or ''
    print(f"  пользователей: {len(users)}, воронок: {len(pipelines)}, этапов: {len(status_map)}")

    # --- сделки ---
    print("Тяну сделки...")
    params = {
        'limit': AMO_PAGE_LIMIT,
        'filter[created_at][from]': date_from_ts,
        'filter[created_at][to]': date_to_ts,
        'order[created_at]': 'asc',
        'with': 'contacts,loss_reason',
    }
    for i, pid in enumerate(PIPELINE_IDS):
        params[f'filter[pipeline_id][{i}]'] = pid
    leads = amo_fetch_all('/api/v4/leads', params, 'leads')
    print(f"  получено сделок: {len(leads)}")

    allowed = set(PIPELINE_IDS)
    rows = []
    for lead in leads:
        created = int(lead.get('created_at') or 0)
        if created < date_from_ts or created > date_to_ts:
            continue
        if int(lead.get('pipeline_id') or 0) not in allowed:
            continue
        rows.append(build_row(lead, user_map, pipeline_map, status_map, pipeline_status_map))
    print(f"  строк после фильтра: {len(rows)}")

    # --- контакты ---
    contact_ids = list({str(r['_main_contact_id']) for r in rows
                        if r.get('_main_contact_id') not in (None, '', 0)})
    print(f"Тяну контакты: {len(contact_ids)} шт...")
    contact_map = {}
    for chunk in chunked(contact_ids, CONTACTS_PER_REQUEST):
        cp = {'limit': 250}
        for i, cid in enumerate(chunk):
            cp[f'filter[id][{i}]'] = cid
        data = amo_get('/api/v4/contacts', cp)
        for c in ((data.get('_embedded') or {}).get('contacts') or []):
            contact_map[str(c['id'])] = c
        time.sleep(REQUEST_INTERVAL)
    print(f"  получено контактов: {len(contact_map)}")

    for r in rows:
        enrich_row(r, contact_map)

    # --- запись в Google Sheets ---
    print("Пишу в Google Sheets...")
    values = sheets_values()

    hdr_resp = values.get(spreadsheetId=SPREADSHEET_ID,
                          range=f"'{SHEET_NAME}'!{HEADER_ROW}:{HEADER_ROW}").execute()
    headers = (hdr_resp.get('values') or [[]])
    headers = headers[0] if headers else []
    if not headers:
        raise RuntimeError(f"Не нашёл заголовки в строке {HEADER_ROW} вкладки «{SHEET_NAME}». "
                           f"Проверь HEADER_ROW и SHEET_NAME.")

    matrix = []
    for r in rows:
        matrix.append([_cell(r.get(h, '')) for h in headers])

    clear_range = f"'{SHEET_NAME}'!A{DATA_START_ROW}:{LAST_COLUMN}{DATA_START_ROW + ROWS_TO_CLEAR}"
    values.clear(spreadsheetId=SPREADSHEET_ID, range=clear_range).execute()

    if matrix:
        values.update(spreadsheetId=SPREADSHEET_ID,
                      range=f"'{SHEET_NAME}'!A{DATA_START_ROW}",
                      valueInputOption='USER_ENTERED',
                      body={'values': matrix}).execute()

    if EXPORT_DATE_CELL:
        values.update(spreadsheetId=SPREADSHEET_ID,
                      range=f"'{SHEET_NAME}'!{EXPORT_DATE_CELL}",
                      valueInputOption='USER_ENTERED',
                      body={'values': [[date_to_text]]}).execute()

    print(f"ГОТОВО. Записано строк: {len(matrix)} (с {DATA_START_ROW}-й строки).")
    return {
        'rows': len(matrix),
        'leads': len(leads),
        'contacts': len(contact_map),
        'period': f"{date_from_dt:%d.%m.%Y} — {date_to_dt:%d.%m.%Y}",
    }


def _cell(v):
    if v is None:
        return ''
    return v


if __name__ == '__main__':
    try:
        s = main()
        send_telegram(
            "✅ amoCRM → Google Sheets: выгрузка выполнена\n"
            f"Период: {s['period']}\n"
            f"Сделок: {s['leads']}, контактов: {s['contacts']}\n"
            f"Записано строк: {s['rows']}\n"
            f"Таблица: https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}"
            + run_url_line()
        )
    except Exception as e:
        send_telegram(
            "❌ amoCRM → Google Sheets: ВЫГРУЗКА УПАЛА\n"
            f"Ошибка: {type(e).__name__}: {str(e)[:300]}"
            + run_url_line()
        )
        raise
