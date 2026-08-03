#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Автозакрытие застрявших сделок Первой линии + задача-предупреждение.

Правило (согласовано с Никитой):
  • 27 календарных дней от даты создания → создать ответственному задачу «сделка будет
    закрыта через 3 дня», срок — сегодня. Создаётся один раз (идемпотентно).
  • 30 календарных дней → закрыть сделку: статус «Закрыто и не реализовано» (143)
    + кастомное поле «Причина отказа» = «Пропал/Не отвечает».
  • Берём воронку 8733326, все открытые статусы КРОМЕ финальных 142 «Встреча проведена»
    и 143 «Закрыто и не реализовано».

БЕЗОПАСНОСТЬ (это запись в боевой amoCRM!):
  • DRY_RUN=1 — ничего не меняет, только печатает, что сделал бы. Прогоняйте первым.
  • MAX_CLOSE_PER_RUN — предохранитель: не закрывать больше N сделок за прогон.
  • CLOSE_MAX_AGE_DAYS — не трогать сделки старше N дней (старый архив разбирается вручную).
    0 = без верхней границы.
  • CREATED_FROM (ДД.ММ.ГГГГ) — трогать только сделки, созданные не раньше этой даты. Пусто = без.

Запуск: ежедневно утром (cron-job.org). Локально/вручную — через workflow с DRY_RUN.
"""

import os
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

# ============================================================
#  НАСТРОЙКИ
# ============================================================

AMO_BASE_URL = "https://pavelgitelman.amocrm.ru"
PIPELINE_ID = 8733326                 # Первая линия
FINAL_STATUSES = {142, 143}           # 142 «Встреча проведена» (успех), 143 «Закрыто и не реализовано»
LOSS_STATUS_ID = 143                  # куда переводим при автозакрытии

WARN_DAYS = 27                        # день создания задачи-предупреждения
CLOSE_DAYS = 30                       # день автозакрытия

REASON_FIELD_ID = 1426303             # кастомное поле «Причина отказа» (select)
REASON_ENUM_ID = 1220963             # значение «Пропал/Не отвечает»

TASK_TEXT = "Сделка будет закрыта через 3 дня (нет движения 27 дней) — свяжитесь с клиентом"
TASK_MARKER = "будет закрыта через 3 дня"   # по этой подстроке проверяем, что задача уже есть
TASK_TYPE_ID = 1                      # обычная задача

# Предохранители
MAX_CLOSE_PER_RUN = int(os.environ.get("MAX_CLOSE_PER_RUN") or "500")
CLOSE_MAX_AGE_DAYS = int(os.environ.get("CLOSE_MAX_AGE_DAYS") or "0")   # 0 = без верхней границы
CREATED_FROM = os.environ.get("CREATED_FROM", "").strip()               # ДД.ММ.ГГГГ или пусто

# Исключения: id сделок, которые НЕ трогаем (ни задача, ни закрытие).
# Читаем из файла exclude_leads.txt рядом со скриптом (по одному id в строке, # — комментарий).
EXCLUDE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "exclude_leads.txt")

# Тег в amoCRM: сделки с этим тегом тоже пропускаем (гибкая защита из самой CRM).
EXCLUDE_TAG = "не автозакрывать"


def load_exclude_ids():
    ids = set()
    for x in os.environ.get("EXCLUDE_IDS", "").replace(';', ',').split(','):
        if x.strip().isdigit():
            ids.add(int(x.strip()))
    if os.path.exists(EXCLUDE_FILE):
        with open(EXCLUDE_FILE, encoding='utf-8') as f:
            for line in f:
                line = line.split('#', 1)[0].strip()
                if line.isdigit():
                    ids.add(int(line))
    return ids


def has_exclude_tag(lead):
    for t in ((lead.get('_embedded') or {}).get('tags') or []):
        if str(t.get('name') or '').strip().lower() == EXCLUDE_TAG:
            return True
    return False

TIMEZONE = "Europe/Moscow"
MSK = ZoneInfo(TIMEZONE)
REQUEST_INTERVAL = 0.2

# ---- Секреты / режим ----
AMO_TOKEN = os.environ.get("AMO_TOKEN", "").strip()
if AMO_TOKEN[:7].lower() == "bearer ":
    AMO_TOKEN = AMO_TOKEN[7:].strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")


# ============================================================
#  amoCRM
# ============================================================

def amo_request(method, path, params=None, payload=None):
    url = path if path.startswith('http') else AMO_BASE_URL + path
    headers = {'Authorization': f'Bearer {AMO_TOKEN}', 'Accept': 'application/json',
               'Content-Type': 'application/json'}
    for attempt in range(5):
        r = requests.request(method, url, headers=headers, params=params, json=payload, timeout=90)
        if r.status_code in (200, 201):
            return r.json() if r.text else {}
        if r.status_code == 204:
            return {}
        if r.status_code in (429, 500, 502, 503, 504):
            wait = 2 ** attempt
            print(f"  amo {r.status_code}, повтор через {wait}с...")
            time.sleep(wait)
            continue
        raise RuntimeError(f"amoCRM {method} {r.status_code}: {r.text[:300]} ({url})")
    raise RuntimeError(f"amoCRM: не удалось {method} {url}")


def amo_get(path, params=None):
    return amo_request('GET', path, params=params)


def fetch_open_leads():
    """Все сделки воронки, кроме финальных статусов."""
    out = []
    params = {'limit': 250, 'filter[pipeline_id][0]': PIPELINE_ID}
    url, first, page = '/api/v4/leads', True, 0
    while url and page < 500:
        data = amo_get(url, params if first else None)
        first = False
        leads = (data.get('_embedded') or {}).get('leads') or []
        if not leads:
            break
        for l in leads:
            if l.get('status_id') not in FINAL_STATUSES:
                out.append(l)
        url = ((data.get('_links') or {}).get('next') or {}).get('href')
        page += 1
        time.sleep(REQUEST_INTERVAL)
    return out


def leads_with_open_marker_task(lead_ids):
    """Множество id сделок, у которых уже есть НЕвыполненная задача с нашим маркером."""
    have = set()
    ids = list(lead_ids)
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        params = {'limit': 250, 'filter[entity_type]': 'leads', 'filter[is_completed]': 0}
        for j, cid in enumerate(chunk):
            params[f'filter[entity_id][{j}]'] = cid
        data = amo_get('/api/v4/tasks', params)
        for t in ((data.get('_embedded') or {}).get('tasks') or []):
            if TASK_MARKER.lower() in str(t.get('text') or '').lower():
                have.add(t.get('entity_id'))
        time.sleep(REQUEST_INTERVAL)
    return have


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
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


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# ============================================================
#  main
# ============================================================

def age_days(created_ts, today):
    created_date = datetime.fromtimestamp(int(created_ts or 0), tz=MSK).date()
    return (today - created_date).days


def main():
    if not AMO_TOKEN:
        print("ОШИБКА: нет AMO_TOKEN")
        sys.exit(1)

    today = datetime.now(MSK).date()
    created_from_date = None
    if CREATED_FROM:
        d = datetime.strptime(CREATED_FROM, '%d.%m.%Y')
        created_from_date = datetime(d.year, d.month, d.day).date()

    print(f"Сегодня (МСК): {today}. WARN={WARN_DAYS}д, CLOSE={CLOSE_DAYS}д, "
          f"max_age={CLOSE_MAX_AGE_DAYS or '∞'}, created_from={CREATED_FROM or '—'}, "
          f"лимит закрытий={MAX_CLOSE_PER_RUN}" + (" [DRY_RUN]" if DRY_RUN else ""))

    exclude_ids = load_exclude_ids()
    print(f"Исключений из файла/env: {len(exclude_ids)}; тег-исключение: «{EXCLUDE_TAG}»")

    leads = fetch_open_leads()
    print(f"Открытых сделок Первой линии: {len(leads)}")

    to_task, to_close = [], []
    skipped_excl = 0
    for l in leads:
        if l['id'] in exclude_ids or has_exclude_tag(l):
            skipped_excl += 1
            continue
        age = age_days(l.get('created_at'), today)
        cdate = datetime.fromtimestamp(int(l.get('created_at') or 0), tz=MSK).date()
        if created_from_date and cdate < created_from_date:
            continue
        if CLOSE_MAX_AGE_DAYS and age > CLOSE_MAX_AGE_DAYS:
            continue   # старый архив — не трогаем
        if age >= CLOSE_DAYS:
            to_close.append(l)
        elif WARN_DAYS <= age < CLOSE_DAYS:
            to_task.append(l)

    print(f"Пропущено по исключениям: {skipped_excl}")
    print(f"Кандидатов на задачу (27-29д): {len(to_task)}; на закрытие (30д+): {len(to_close)}")

    # --- Задачи (идемпотентно) ---
    task_ids = [l['id'] for l in to_task]
    already = leads_with_open_marker_task(task_ids) if task_ids else set()
    new_tasks = [l for l in to_task if l['id'] not in already]
    print(f"Задачи: уже есть у {len(already)}, создать {len(new_tasks)}")

    end_of_today = int(datetime(today.year, today.month, today.day, 23, 59, 0, tzinfo=MSK).timestamp())
    task_payload = [{
        'text': TASK_TEXT,
        'task_type_id': TASK_TYPE_ID,
        'complete_till': end_of_today,
        'entity_id': l['id'],
        'entity_type': 'leads',
        'responsible_user_id': l.get('responsible_user_id'),
    } for l in new_tasks]

    # --- Закрытие (с предохранителем) ---
    capped_close = to_close[:MAX_CLOSE_PER_RUN]
    if len(to_close) > MAX_CLOSE_PER_RUN:
        print(f"⚠ на закрытие {len(to_close)}, но лимит {MAX_CLOSE_PER_RUN} — "
              f"закрою {MAX_CLOSE_PER_RUN}, остальные в следующий прогон")
    close_payload = [{
        'id': l['id'],
        'pipeline_id': PIPELINE_ID,
        'status_id': LOSS_STATUS_ID,
        'custom_fields_values': [{'field_id': REASON_FIELD_ID,
                                  'values': [{'enum_id': REASON_ENUM_ID}]}],
    } for l in capped_close]

    if DRY_RUN:
        print("\n[DRY_RUN] Задачи создались бы для:")
        for l in new_tasks[:15]:
            print(f"  #{l['id']} возраст {age_days(l.get('created_at'), today)}д, отв. {l.get('responsible_user_id')}")
        print("[DRY_RUN] Закрылись бы (первые 15):")
        for l in capped_close[:15]:
            print(f"  #{l['id']} возраст {age_days(l.get('created_at'), today)}д «{l.get('name')}»")
        # Полный список ID под закрытие — для формирования файла исключений.
        all_close_ids = [str(l['id']) for l in to_close]
        print(f"[DRY_RUN] ВСЕ id под закрытие ({len(all_close_ids)}):")
        print(','.join(all_close_ids))
        return {'tasks': len(task_payload), 'closed': len(close_payload), 'to_close_total': len(to_close)}

    # реальная запись
    created = 0
    for chunk in chunked(task_payload, 250):
        amo_request('POST', '/api/v4/tasks', payload=chunk)
        created += len(chunk)
        time.sleep(REQUEST_INTERVAL)
    print(f"Создано задач: {created}")

    closed = 0
    for chunk in chunked(close_payload, 50):
        amo_request('PATCH', '/api/v4/leads', payload=chunk)
        closed += len(chunk)
        print(f"  закрыто: {closed}/{len(close_payload)}")
        time.sleep(REQUEST_INTERVAL)

    # verify: перечитываем первые закрытые и подтверждаем статус + причину
    for l in capped_close[:5]:
        d = amo_get(f"/api/v4/leads/{l['id']}")
        reason = ''
        for f in (d.get('custom_fields_values') or []):
            if f.get('field_id') == REASON_FIELD_ID:
                reason = ', '.join(str(v.get('value')) for v in (f.get('values') or []))
        print(f"  ✓ #{l['id']}: status_id={d.get('status_id')} причина=«{reason}»")

    print(f"ГОТОВО. Задач: {created}, закрыто: {closed}.")
    return {'tasks': created, 'closed': closed, 'to_close_total': len(to_close)}


if __name__ == '__main__':
    try:
        s = main()
        tail = ""
        if s['to_close_total'] > s['closed']:
            tail = f"\n⚠ осталось на след. прогон: {s['to_close_total'] - s['closed']}"
        send_telegram(
            ("🧪 [DRY_RUN] " if DRY_RUN else "✅ ") +
            "Автозакрытие Первой линии\n"
            f"Задач-предупреждений: {s['tasks']}\n"
            f"{'Закрылось бы' if DRY_RUN else 'Закрыто'}: {s['closed']}"
            + tail + run_url_line())
    except Exception as e:
        send_telegram(f"❌ Автозакрытие Первой линии: упало\n"
                      f"Ошибка: {type(e).__name__}: {str(e)[:300]}" + run_url_line())
        raise
