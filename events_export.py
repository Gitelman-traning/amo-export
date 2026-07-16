#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Выгрузка ленты событий amoCRM (Аналитика → Список событий) в Google Sheets.

Что делает:
  1. Тянет события из /api/v4/events за период (по умолчанию — текущий месяц по Москве).
  2. Тянет справочники: пользователи, воронки/этапы, кастомные поля (сделки/контакты/компании).
  3. Расшифровывает «Значение до» / «Значение после» в человекочитаемый вид.
  4. Пишет всё на отдельный лист месячной таблицы (SPREADSHEET_ID).

Запуск:
  python events_export.py                # текущий месяц
  PROBE=1 python events_export.py        # разведка: листы таблицы + сырой JSON по каждому типу события
  DRY_RUN=1 python events_export.py      # посчитать, но в таблицу не писать

Переменные окружения: AMO_TOKEN, GOOGLE_SERVICE_ACCOUNT_JSON, SPREADSHEET_ID,
                      TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (необязательно).
"""

import os
import re
import sys
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ============================================================
#  НАСТРОЙКИ
# ============================================================

AMO_BASE_URL = "https://pavelgitelman.amocrm.ru"

# Таблица назначения — та же месячная, что и у остальных выгрузок (GitHub → Variables).
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "").strip() or "1UVN3nLBQ2YEg05mC0B-0tCYAgMXspVj2y2ED66hoXgg"
SHEET_NAME = os.environ.get("SHEET_NAME", "").strip() or "События"

# Период. По умолчанию — с 1-го числа текущего месяца по «сейчас» (Москва).
# Можно переопределить руками: DATE_FROM/DATE_TO в формате ДД.ММ.ГГГГ.
DATE_FROM = os.environ.get("DATE_FROM", "").strip()
DATE_TO = os.environ.get("DATE_TO", "").strip()

TIMEZONE = "Europe/Moscow"
MSK = ZoneInfo(TIMEZONE)

AMO_PAGE_LIMIT = 100      # у /api/v4/events максимум 100 на страницу
REQUEST_INTERVAL = 0.15   # пауза между запросами к amo (лимит ~7 rps)
MAX_PAGES = 5000          # предохранитель от бесконечного цикла
SHEETS_CHUNK = 5000       # по столько строк пишем в таблицу за один запрос

# ---- Секреты / режимы ----
AMO_TOKEN = os.environ.get("AMO_TOKEN", "").strip()
if AMO_TOKEN[:7].lower() == "bearer ":
    AMO_TOKEN = AMO_TOKEN[7:].strip()
GOOGLE_SA_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")
PROBE = os.environ.get("PROBE", "").strip().lower() in ("1", "true", "yes")

# Только действия живых людей (как фильтр «Менеджеры» в интерфейсе amo).
# Отсекает события ботов/интеграций (created_by = 0): salebot, utm-метки и прочий шум.
MANAGERS_ONLY = (os.environ.get("MANAGERS_ONLY", "").strip().lower() or "true") in ("1", "true", "yes")

# Какие типы событий оставлять:
#   key  — только смысловые: этапы, ответственные, поля, примечания, звонки, задачи, теги, бюджет, имя
#   all  — вообще все типы (включая привязки, чат-сообщения, беседы — сильно больше строк)
EVENTS_SCOPE = (os.environ.get("EVENTS_SCOPE", "").strip().lower() or "key")

# Чьи события выгружаем. Список имён как в amo, через запятую (env AUTHORS).
# Пустой AUTHORS в окружении = этот список по умолчанию. Значение "all" = все авторы.
# Фильтр уходит в amo (filter[created_by][]) — качаем только нужное, это в разы быстрее.
DEFAULT_AUTHORS = [
    'Евгений Кротов', 'Илья Огнев', 'Камилла Пацкевич', 'Мурад Мурзаев',
    'Русакова Любовь', 'Ткачева Татьяна', 'Узянов Дмитрий',
]
_authors_env = os.environ.get("AUTHORS", "").strip()
AUTHORS = ([] if _authors_env.lower() == 'all'
           else [a.strip() for a in _authors_env.split(',') if a.strip()] or DEFAULT_AUTHORS)

# Группировка: серия действий одного автора по одной сущности в пределах одного
# календарного часа схлопывается в одну строку (последнее действие + счётчик).
GROUP_HOURLY = (os.environ.get("GROUP_HOURLY", "").strip().lower() or "true") in ("1", "true", "yes")

# Смысловые типы (кастомные поля добавляются отдельно регуляркой CF_TYPE_RE).
KEY_TYPES = {
    'lead_status_changed', 'entity_responsible_changed',
    'lead_added', 'contact_added', 'company_added',
    'common_note_added', 'incoming_call', 'outgoing_call',
    'task_added', 'task_completed', 'task_result_added', 'task_deadline_changed',
    'name_field_changed', 'sale_field_changed',
    'entity_tag_added', 'entity_tag_deleted',
}

COLUMNS = [
    'Дата', 'Автор', 'Объект', 'Название', 'Событие',
    'Значение до', 'Значение после', 'Действий за час', 'Ссылка', 'ID объекта', 'Тип события',
]

# Тип сущности → как называется в интерфейсе
ENTITY_RU = {
    'lead': 'Сделка',
    'contact': 'Контакт',
    'company': 'Компания',
    'customer': 'Покупатель',
    'catalog_element': 'Элемент списка',
    'task': 'Задача',
    'talk': 'Беседа',
}

ENTITY_PLURAL = {'lead': 'leads', 'contact': 'contacts', 'company': 'companies'}

# Раздел ссылки на карточку сущности
ENTITY_URL = {
    'lead': 'leads/detail',
    'contact': 'contacts/detail',
    'company': 'companies/detail',
    'customer': 'customers/detail',
}

# Код типа события → название как в интерфейсе amo.
# Если тип не найден — в таблицу попадёт сам код (данные не теряем).
EVENT_RU = {
    'lead_added': 'Новая сделка',
    'lead_deleted': 'Сделка удалена',
    'lead_restored': 'Сделка восстановлена',
    'lead_status_changed': 'Изменение этапа продажи',
    'lead_linked': 'Сделка привязана',
    'lead_unlinked': 'Сделка отвязана',
    'contact_added': 'Новый контакт',
    'contact_deleted': 'Контакт удалён',
    'contact_restored': 'Контакт восстановлен',
    'contact_linked': 'Контакт привязан',
    'contact_unlinked': 'Контакт отвязан',
    'company_added': 'Новая компания',
    'company_deleted': 'Компания удалена',
    'company_restored': 'Компания восстановлена',
    'company_linked': 'Компания привязана',
    'company_unlinked': 'Компания отвязана',
    'customer_added': 'Новый покупатель',
    'customer_deleted': 'Покупатель удалён',
    'customer_status_changed': 'Изменение этапа покупателя',
    'entity_responsible_changed': 'Смена ответственного',
    'entity_tag_added': 'Добавлен тег',
    'entity_tag_deleted': 'Удалён тег',
    'entity_linked': 'Привязка',
    'entity_unlinked': 'Отвязка',
    'entity_merged': 'Объединение',
    'custom_field_value_changed': 'Изменение поля',
    'sale_field_changed': 'Изменение бюджета',
    'name_field_changed': 'Изменение названия',
    'ltv_field_changed': 'Изменение LTV',
    'common_note_added': 'Новое примечание',
    'common_note_deleted': 'Примечание удалено',
    'attachment_note_added': 'Добавлен файл',
    'service_note_added': 'Системное примечание',
    'site_visit_note_added': 'Визит на сайт',
    'geo_note_added': 'Геометка',
    'targeting_in_note_added': 'Таргетинг',
    'message_to_cashier_note_added': 'Сообщение кассиру',
    'task_added': 'Новая задача',
    'task_deleted': 'Задача удалена',
    'task_completed': 'Задача выполнена',
    'task_deadline_changed': 'Изменён срок задачи',
    'task_type_changed': 'Изменён тип задачи',
    'task_text_changed': 'Изменён текст задачи',
    'task_result_added': 'Результат по задаче',
    'incoming_call': 'Входящий звонок',
    'outgoing_call': 'Исходящий звонок',
    'incoming_chat_message': 'Входящее сообщение',
    'outgoing_chat_message': 'Исходящее сообщение',
    'incoming_sms': 'Входящее SMS',
    'outgoing_sms': 'Исходящее SMS',
    'robot_replied': 'Ответ робота',
    'nps_rate_added': 'Оценка NPS',
    'link_followed': 'Переход по ссылке',
    'transaction_added': 'Добавлена покупка',
    'intent_identified': 'Определено намерение',
    'talk_created': 'Начата беседа',
    'talk_closed': 'Беседа закрыта',
    'talk_missed_event': 'Пропущенная беседа',
}

# Изменение кастомного поля приходит типом вида custom_field_1439087_value_changed —
# ID поля зашит в сам тип события, поэтому вытаскиваем его регуляркой.
CF_TYPE_RE = re.compile(r'^custom_field_(\d+)_value_changed$')


# ============================================================
#  Хелперы
# ============================================================

def period_bounds():
    """Границы периода в unix-времени (Москва). По умолчанию — текущий месяц."""
    now = datetime.now(MSK)
    if DATE_FROM:
        d = datetime.strptime(DATE_FROM, '%d.%m.%Y')
        start = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=MSK)
    else:
        start = datetime(now.year, now.month, 1, 0, 0, 0, tzinfo=MSK)
    if DATE_TO:
        d = datetime.strptime(DATE_TO, '%d.%m.%Y')
        end = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=MSK)
    else:
        end = now
    return int(start.timestamp()), int(end.timestamp()), start, end


def fmt_dt(ts):
    if ts in (None, '', 0, '0'):
        return ''
    try:
        return datetime.fromtimestamp(int(ts), tz=MSK).strftime('%d.%m.%Y %H:%M')
    except (TypeError, ValueError):
        return ''


def safe_cell(v):
    """Экранируем формульную инъекцию Google Sheets: текст, начинающийся с =,+,-,@."""
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


def fetch_events(ts_from, ts_to, creator_ids=None):
    """События за период. Пагинация — по _links.next (у events она надёжнее page).
    creator_ids — фильтр по авторам на стороне amo: качаем только их события."""
    out = []
    params = {
        'limit': AMO_PAGE_LIMIT,
        'filter[created_at][from]': ts_from,
        'filter[created_at][to]': ts_to,
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
        if page % 20 == 0:
            print(f"  ...страница {page}, событий получено: {len(out)}")
        url = ((data.get('_links') or {}).get('next') or {}).get('href')
        time.sleep(REQUEST_INTERVAL)
    return out


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


def fetch_custom_fields(entity):
    """Кастомные поля сущности: id → {name, enums{enum_id: value}}."""
    out, page = {}, 1
    while page <= 20:
        try:
            data = amo_get(f'/api/v4/{entity}/custom_fields', {'limit': 250, 'page': page})
        except RuntimeError as ex:
            print(f"  справочник полей {entity}: пропускаю ({str(ex)[:80]})")
            break
        fs = (data.get('_embedded') or {}).get('custom_fields') or []
        if not fs:
            break
        for f in fs:
            enums = {}
            for e in (f.get('enums') or []):
                enums[str(e.get('id'))] = e.get('value')
            out[str(f.get('id'))] = {'name': f.get('name') or '', 'enums': enums}
        page += 1
        time.sleep(REQUEST_INTERVAL)
    return out


def build_context():
    """Справочники для расшифровки значений."""
    users = fetch_users()
    user_map = {str(u['id']): (u.get('name') or '') for u in users}
    user_map['0'] = 'Система'

    pipelines = (amo_get('/api/v4/leads/pipelines').get('_embedded') or {}).get('pipelines') or []
    pipe_name, status_name = {}, {}
    for p in pipelines:
        pipe_name[str(p['id'])] = p.get('name') or ''
        for s in ((p.get('_embedded') or {}).get('statuses') or []):
            status_name[str(s['id'])] = s.get('name') or ''

    fields = {}
    for ent in ('leads', 'contacts', 'companies'):
        fields.update(fetch_custom_fields(ent))

    print(f"Справочники: пользователей {len(user_map)}, воронок {len(pipe_name)}, "
          f"этапов {len(status_name)}, кастомных полей {len(fields)}")
    # notes/tasks наполняются позже — после того, как узнаем, какие события пришли.
    return {'users': user_map, 'pipes': pipe_name, 'statuses': status_name,
            'fields': fields, 'notes': {}, 'tasks': {}}


# ============================================================
#  Расшифровка значений события
# ============================================================

def _plain(v):
    """Достаём осмысленный текст из неизвестной структуры, ничего не теряя."""
    if v is None:
        return ''
    if isinstance(v, (str, int, float)):
        return str(v)
    if isinstance(v, list):
        return ', '.join(x for x in (_plain(i) for i in v) if x)
    if isinstance(v, dict):
        for key in ('name', 'text', 'value', 'title'):
            if v.get(key) not in (None, ''):
                return str(v[key])
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def decode_wrapper(key, val, ctx):
    """Одна обёртка внутри value_before/value_after → строка для таблицы."""
    if key == 'lead_status':
        sid = str((val or {}).get('id') or '')
        pid = str((val or {}).get('pipeline_id') or '')
        status = ctx['statuses'].get(sid) or sid
        pipe = ctx['pipes'].get(pid) or ''
        return f"{pipe} / {status}" if pipe else status

    if key in ('responsible_user', 'created_user'):
        uid = str((val or {}).get('id') or '')
        return ctx['users'].get(uid) or uid

    if key == 'custom_field_value':
        # Само значение amo кладёт в "text" (для списков там уже подставлено имя пункта).
        v = val or {}
        for k in ('text', 'text_value', 'value'):
            if v.get(k) not in (None, ''):
                return _plain(v[k])
        # На случай, если текста нет — расшифровываем enum_id по справочнику.
        if v.get('enum_id') not in (None, ''):
            meta = ctx['fields'].get(str(v.get('field_id') or '')) or {}
            return (meta.get('enums') or {}).get(str(v['enum_id'])) or str(v['enum_id'])
        return ''

    if key == 'link':
        # Привязка/отвязка: {"link": {"entity": {"type": "contact", "id": .., "name": ""}}}
        ent = (val or {}).get('entity') or {}
        label = ENTITY_RU.get(ent.get('type'), ent.get('type') or '')
        name = ent.get('name') or ''
        eid = ent.get('id') or ''
        tail = name or (f"#{eid}" if eid else '')
        return f"{label}: {tail}".strip(': ') if (label or tail) else ''

    if key == 'message':
        # {"message": {"id": "..", "origin": "pro.salebot", "talk_id": ..}} — текста нет,
        # показываем источник канала.
        return (val or {}).get('origin') or ''

    if key == 'note':
        # В событии только id заметки — текст догружен заранее.
        nid = (val or {}).get('id')
        return ctx['notes'].get(nid) or (f"примечание #{nid}" if nid else '')

    if key in ('tags', 'tag'):
        return _plain(val)

    if key in ('sale', 'price', 'ltv'):
        if isinstance(val, dict):
            for k in ('sale', 'price', 'ltv', 'value'):
                if val.get(k) not in (None, ''):
                    return str(val[k])
        return _plain(val)

    # note / task / message / name / прочее — универсально
    return _plain(val)


def decode_value(items, ctx):
    if not items:
        return ''
    parts = []
    for it in items:
        if isinstance(it, dict):
            for key, val in it.items():
                s = decode_wrapper(key, val, ctx)
                if s:
                    parts.append(s)
        else:
            s = _plain(it)
            if s:
                parts.append(s)
    return '; '.join(parts)


def resolve_author_ids(user_map, wanted):
    """Имена менеджеров → их id в amo (без учёта регистра и крайних пробелов)."""
    by_name = {}
    for uid, name in user_map.items():
        by_name.setdefault(str(name).strip().casefold(), uid)
    ids, missing = [], []
    for w in wanted:
        uid = by_name.get(w.strip().casefold())
        (ids if uid else missing).append(uid or w)
    return ids, missing


def group_hourly(events):
    """Серия действий одного автора по одной сущности в пределах календарного часа →
    одно событие (последнее по времени) с числом действий в '_grp'."""
    groups = {}
    for e in events:
        ts = int(e.get('created_at') or 0)
        key = (e.get('entity_type'), e.get('entity_id'), e.get('created_by'), ts // 3600)
        g = groups.get(key)
        if g is None:
            groups[key] = {'event': e, 'count': 1}
        else:
            g['count'] += 1
            if ts > int(g['event'].get('created_at') or 0):
                g['event'] = e
    out = []
    for g in groups.values():
        e = dict(g['event'])
        e['_grp'] = g['count']
        out.append(e)
    return out


def event_label(etype, ctx):
    """Название события как в интерфейсе. Для кастомных полей — с именем поля."""
    m = CF_TYPE_RE.match(etype or '')
    if m:
        meta = ctx['fields'].get(m.group(1)) or {}
        name = meta.get('name')
        return f'Изменение поля "{name}"' if name else f'Изменение поля {m.group(1)}'
    return EVENT_RU.get(etype, etype)


def fetch_by_ids(path, key, ids):
    """Универсальная догрузка сущностей пачками по filter[id][]."""
    out, ids = [], list(ids)
    for i in range(0, len(ids), 100):
        try:
            data = amo_get(path, {'limit': 250, 'filter[id][]': ids[i:i + 100]})
        except RuntimeError as ex:
            print(f"    пропускаю пачку {path} ({str(ex)[:80]})")
            continue
        out.extend((data.get('_embedded') or {}).get(key) or [])
        time.sleep(REQUEST_INTERVAL)
    return out


def fetch_tasks(events):
    """Задачи: в событии только id задачи, а текст («итоги») лежит в самой задаче."""
    ids = {e['entity_id'] for e in events
           if e.get('entity_type') == 'task' and e.get('entity_id')}
    if not ids:
        return {}
    print(f"  задачи: {len(ids)} шт.")
    tasks = {}
    for t in fetch_by_ids('/api/v4/tasks', 'tasks', ids):
        tasks[t['id']] = {
            'text': t.get('text') or '',
            'entity_id': t.get('entity_id'),
            'entity_type': t.get('entity_type'),
            'complete_till': t.get('complete_till'),
            'result': ((t.get('result') or {}) or {}).get('text') or '',
        }
    return tasks


def note_text(n):
    """Человекочитаемый текст заметки: примечание, звонок, системное сообщение."""
    p = n.get('params') or {}
    t = n.get('note_type') or ''
    if t in ('call_in', 'call_out'):
        bits = ['входящий звонок' if t == 'call_in' else 'исходящий звонок']
        if p.get('phone'):
            bits.append(str(p['phone']))
        if p.get('duration') not in (None, ''):
            bits.append(f"{p['duration']} сек")
        if p.get('text'):
            bits.append(str(p['text']))
        return ', '.join(bits)
    for k in ('text', 'comment', 'message'):
        if p.get(k):
            return str(p[k])
    return t


def fetch_notes(events):
    """Тексты примечаний/звонков: в событии приходит только id заметки."""
    need = {}
    for e in events:
        ent = e.get('entity_type')
        if ent not in ENTITY_PLURAL:
            continue
        for item in (e.get('value_after') or []) + (e.get('value_before') or []):
            if isinstance(item, dict) and isinstance(item.get('note'), dict):
                nid = item['note'].get('id')
                if nid:
                    need.setdefault(ent, set()).add(nid)
    notes = {}
    for ent, ids in need.items():
        print(f"  примечания: {ent} — {len(ids)} шт.")
        for n in fetch_by_ids(f'/api/v4/{ENTITY_PLURAL[ent]}/notes', 'notes', ids):
            notes[n['id']] = note_text(n)
    return notes


def fetch_entity_names(events, tasks):
    """Названия сущностей: API событий их не отдаёт, догружаем пачками по id."""
    need = {}
    for e in events:
        ent, eid = e.get('entity_type'), e.get('entity_id')
        if ent in ENTITY_PLURAL and eid:
            need.setdefault(ent, set()).add(eid)
    # Для событий по задачам показываем название сделки/контакта, к которым задача привязана.
    for t in tasks.values():
        if t.get('entity_type') in ENTITY_PLURAL and t.get('entity_id'):
            need.setdefault(t['entity_type'], set()).add(t['entity_id'])

    names = {}
    for ent, ids in need.items():
        print(f"  названия: {ent} — {len(ids)} шт.")
        for x in fetch_by_ids(f'/api/v4/{ENTITY_PLURAL[ent]}', ENTITY_PLURAL[ent], ids):
            names[f"{ent}:{x['id']}"] = x.get('name') or ''
    return names


def build_rows(events, ctx, names):
    rows = []
    # Новые события сверху — как в интерфейсе amo.
    events = sorted(events, key=lambda e: int(e.get('created_at') or 0), reverse=True)
    for e in events:
        etype = e.get('type') or ''
        ent = e.get('entity_type') or ''
        eid = e.get('entity_id')
        after = decode_value(e.get('value_after'), ctx)
        name = names.get(f"{ent}:{eid}", '')
        link = f"{AMO_BASE_URL}/{ENTITY_URL[ent]}/{eid}" if ent in ENTITY_URL and eid else ''

        # Событие по задаче: сама задача значения не несёт — берём её текст,
        # а название и ссылку — у сделки/контакта, к которым задача привязана.
        if ent == 'task':
            t = ctx['tasks'].get(eid) or {}
            after = after or t.get('result') or t.get('text') or ''
            tent, tid = t.get('entity_type'), t.get('entity_id')
            if tent in ENTITY_PLURAL and tid:
                name = names.get(f"{tent}:{tid}", '')
                link = f"{AMO_BASE_URL}/{ENTITY_URL[tent]}/{tid}" if tent in ENTITY_URL else ''

        rows.append({
            'Дата': fmt_dt(e.get('created_at')),
            'Автор': ctx['users'].get(str(e.get('created_by'))) or str(e.get('created_by') or ''),
            'Объект': ENTITY_RU.get(ent, ent),
            'Название': name,
            'Событие': event_label(etype, ctx),
            'Значение до': decode_value(e.get('value_before'), ctx),
            'Значение после': after,
            'Действий за час': e.get('_grp', 1),
            'Ссылка': link,
            'ID объекта': eid or '',
            'Тип события': etype,
        })
    return rows


# ============================================================
#  Google Sheets
# ============================================================

def sheets_service():
    info = json.loads(GOOGLE_SA_JSON)
    creds = Credentials.from_service_account_info(
        info, scopes=['https://www.googleapis.com/auth/spreadsheets'])
    return build('sheets', 'v4', credentials=creds, cache_discovery=False).spreadsheets()


def list_sheets(svc):
    meta = svc.get(
        spreadsheetId=SPREADSHEET_ID,
        fields='properties.title,sheets.properties(title,sheetId,gridProperties)').execute()
    return meta


def ensure_sheet(svc, title, need_rows):
    """Создаёт лист, если нет, и расширяет сетку до нужного числа строк/колонок.
    По умолчанию у листа лимит 1000 строк — при большем объёме запись падает с 400."""
    meta = list_sheets(svc)
    props = {s['properties']['title']: s['properties'] for s in meta.get('sheets', [])}
    need_cols = len(COLUMNS)

    if title not in props:
        svc.batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={'requests': [{'addSheet': {'properties': {
                'title': title,
                'gridProperties': {'rowCount': need_rows, 'columnCount': need_cols},
            }}}]},
        ).execute()
        print(f"Создал лист «{title}» ({need_rows}×{need_cols})")
        return

    p = props[title]
    grid = p.get('gridProperties') or {}
    cur_rows = grid.get('rowCount') or 0
    cur_cols = grid.get('columnCount') or 0
    if cur_rows < need_rows or cur_cols < need_cols:
        svc.batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={'requests': [{'updateSheetProperties': {
                'properties': {'sheetId': p['sheetId'], 'gridProperties': {
                    'rowCount': max(cur_rows, need_rows),
                    'columnCount': max(cur_cols, need_cols),
                }},
                'fields': 'gridProperties.rowCount,gridProperties.columnCount',
            }}]},
        ).execute()
        print(f"Расширил лист «{title}» до {max(cur_rows, need_rows)}×{max(cur_cols, need_cols)}")


def write_sheet(svc, rows):
    matrix = [COLUMNS] + [[safe_cell(r.get(c, '')) for c in COLUMNS] for r in rows]
    ensure_sheet(svc, SHEET_NAME, len(matrix) + 100)
    values = svc.values()
    values.clear(spreadsheetId=SPREADSHEET_ID, range=f"'{SHEET_NAME}'").execute()
    for i in range(0, len(matrix), SHEETS_CHUNK):
        chunk = matrix[i:i + SHEETS_CHUNK]
        values.update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"'{SHEET_NAME}'!A{i + 1}",
            valueInputOption='USER_ENTERED',
            body={'values': chunk},
        ).execute()
        print(f"  записано строк: {min(i + SHEETS_CHUNK, len(matrix))}/{len(matrix)}")


# ============================================================
#  Разведка (PROBE)
# ============================================================

def probe(ts_from, ts_to):
    """Печатает листы таблицы и сырой JSON по одному событию каждого типа."""
    if GOOGLE_SA_JSON:
        try:
            meta = list_sheets(sheets_service())
            print(f"\n=== ЛИСТЫ ТАБЛИЦЫ «{meta.get('properties', {}).get('title')}» ===")
            for s in meta.get('sheets', []):
                p = s['properties']
                mark = ' <-- сюда пишем' if p['title'] == SHEET_NAME else ''
                print(f"  gid={p['sheetId']}  «{p['title']}»{mark}")

            # Что уже лежит на целевом листе — чтобы не затереть чужие данные.
            cur = sheets_service().values().get(
                spreadsheetId=SPREADSHEET_ID, range=f"'{SHEET_NAME}'!A1:J5").execute()
            vals = cur.get('values') or []
            print(f"\nЛист «{SHEET_NAME}» сейчас: "
                  + (f"{len(vals)} строк в A1:J5" if vals else "ПУСТО"))
            for v in vals:
                print(f"  {v}")
        except Exception as ex:
            print(f"Не смог прочитать таблицу: {ex}")

    pages = int(os.environ.get("PROBE_PAGES") or "30")
    print(f"\n=== ПРОБА СОБЫТИЙ (до {pages} страниц) ===")
    params = {
        'limit': AMO_PAGE_LIMIT,
        'filter[created_at][from]': ts_from,
        'filter[created_at][to]': ts_to,
    }
    url, page, total = '/api/v4/events', 0, 0
    seen, counts = {}, {}
    while url and page < pages:
        data = amo_get(url, params if page == 0 else None)
        evs = (data.get('_embedded') or {}).get('events') or []
        if not evs:
            break
        total += len(evs)
        for e in evs:
            t = e.get('type')
            counts[t] = counts.get(t, 0) + 1
            seen.setdefault(t, e)
        page += 1
        url = ((data.get('_links') or {}).get('next') or {}).get('href')
        time.sleep(REQUEST_INTERVAL)

    print(f"Просмотрено событий: {total}, типов: {len(seen)}\n")
    print("=== ЧАСТОТА ТИПОВ ===")
    for t, c in sorted(counts.items(), key=lambda x: -x[1]):
        known = 'OK ' if (t in EVENT_RU or CF_TYPE_RE.match(str(t))) else '?? '
        print(f"  {known}{c:5d}  {t}")

    print("\n=== ПРИМЕРЫ (только незнакомые/значимые) ===")
    for t, e in sorted(seen.items(), key=lambda x: str(x[0])):
        if CF_TYPE_RE.match(str(t)) and t != next((k for k in seen if CF_TYPE_RE.match(str(k))), None):
            continue  # кастомные поля показываем один раз — структура одинаковая
        print(f"\n--- {t} ---")
        print(json.dumps(e, ensure_ascii=False, indent=2)[:900])


# ============================================================
#  main
# ============================================================

def main():
    missing = [n for n, v in [('AMO_TOKEN', AMO_TOKEN)] if not v]
    if not PROBE and not GOOGLE_SA_JSON:
        missing.append('GOOGLE_SERVICE_ACCOUNT_JSON')
    if missing:
        print("ОШИБКА: нет переменных окружения: " + ", ".join(missing))
        sys.exit(1)

    ts_from, ts_to, d_from, d_to = period_bounds()
    print(f"Период: {d_from.strftime('%d.%m.%Y %H:%M')} — {d_to.strftime('%d.%m.%Y %H:%M')} (МСК)")
    print(f"Таблица: {SPREADSHEET_ID}, лист «{SHEET_NAME}»"
          + (" [DRY_RUN]" if DRY_RUN else "") + (" [PROBE]" if PROBE else ""))

    if PROBE:
        probe(ts_from, ts_to)
        return {'events': 0, 'from': d_from, 'to': d_to}

    ctx = build_context()

    author_ids = None
    if AUTHORS:
        author_ids, missing = resolve_author_ids(ctx['users'], AUTHORS)
        if missing:
            raise RuntimeError(f"Не нашёл менеджеров в amo: {', '.join(missing)}. "
                               f"Проверь имена в AUTHORS (как в списке пользователей amo).")
        picked = ', '.join(ctx['users'][str(i)] for i in author_ids)
        print(f"Выгружаем только события менеджеров ({len(author_ids)}): {picked}")

    events = fetch_events(ts_from, ts_to, author_ids)
    print(f"Событий получено: {len(events)}")

    by_author = {}
    for e in events:
        who = ctx['users'].get(str(e.get('created_by'))) or str(e.get('created_by'))
        by_author[who] = by_author.get(who, 0) + 1
    print("Событий по авторам:")
    for who, cnt in sorted(by_author.items(), key=lambda x: -x[1]):
        print(f"  {cnt:6d}  {who}")

    if MANAGERS_ONLY:
        events = [e for e in events if str(e.get('created_by') or '0') != '0']
        print(f"Оставляю только действия менеджеров (без ботов): {len(events)}")

    if EVENTS_SCOPE == 'key':
        events = [e for e in events
                  if e.get('type') in KEY_TYPES or CF_TYPE_RE.match(str(e.get('type') or ''))]
        print(f"Оставляю только ключевые типы событий: {len(events)}")

    if GROUP_HOURLY:
        events = group_hourly(events)
        print(f"После группировки по часу (сущность+автор): {len(events)} строк")

    print("Догружаю то, чего нет в самих событиях (тексты, названия):")
    ctx['tasks'] = fetch_tasks(events)
    ctx['notes'] = fetch_notes(events)
    names = fetch_entity_names(events, ctx['tasks'])
    rows = build_rows(events, ctx, names)
    authors = len({r['Автор'] for r in rows if r['Автор']})
    print(f"Строк к записи: {len(rows)}, авторов: {authors}")

    if DRY_RUN:
        print("DRY_RUN — в таблицу ничего не писали.")
        for r in rows[:10]:
            print(f"  {r['Дата']} | {r['Автор']} | {r['Объект']} | {r['Событие']} | "
                  f"{r['Значение до'][:40]} → {r['Значение после'][:40]}")
        return {'events': len(rows), 'authors': authors, 'from': d_from, 'to': d_to}

    svc = sheets_service()
    write_sheet(svc, rows)
    print(f"ГОТОВО. Событий: {len(rows)}.")
    return {'events': len(rows), 'authors': authors, 'from': d_from, 'to': d_to}


if __name__ == '__main__':
    try:
        s = main()
        if not DRY_RUN and not PROBE:
            send_telegram(
                f"✅ amoCRM события: выгружено\n"
                f"Период: {s['from'].strftime('%d.%m.%Y')} — {s['to'].strftime('%d.%m.%Y')}\n"
                f"Событий: {s['events']}, менеджеров: {s['authors']}\n"
                f"Таблица: https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}"
                + run_url_line()
            )
    except Exception as e:
        send_telegram(
            f"❌ amoCRM события: выгрузка упала\n"
            f"Ошибка: {type(e).__name__}: {str(e)[:300]}"
            + run_url_line()
        )
        raise
