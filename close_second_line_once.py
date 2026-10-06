#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
РАЗОВАЯ уборка Второй линии (воронка 9701010): закрыть брошенные сделки.

Правило (согласовано с Никитой 30.09.2026):
  • этапы «диагностика проведена» (77332734), «"Иные" лиды» (77332730)
    и «наз на сегодня диаг. (не проведены)» (77332970);
  • нет движения STALE_DAYS дней (по updated_at) И нет ни одной открытой задачи;
  • переводим в 143 «Закрыто и не реализовано» и вешаем тег CLOSE_TAG.
Денежные этапы (счёт, предоплата, чек, комитет) НЕ трогаем — их разбирают руками.

Перед каждым прогоном пишется файл отката: id сделки + её прежний статус.

БЕЗОПАСНОСТЬ:
  DRY_RUN=1 (по умолчанию) — только показать. MAX_CLOSE_PER_RUN — предохранитель.
  Пропускаем сделки с тегом «не автозакрывать» и из exclude_leads.txt.
"""

import os
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

AMO_BASE_URL = "https://pavelgitelman.amocrm.ru"
PIPELINE_ID = 9701010
TARGET_STATUSES = {
    77332734: 'диагностика проведена',
    77332730: '"Иные" лиды',
    77332970: 'наз на сегодня диаг. (не проведены)',
}
LOSS_STATUS_ID = 143
CLOSE_TAG = os.environ.get("CLOSE_TAG", "").strip() or "закрытие 30.09"
STALE_DAYS = int(os.environ.get("STALE_DAYS") or "30")

MSK = ZoneInfo("Europe/Moscow")
REQUEST_INTERVAL = 0.2
BATCH = 50

# Токен интеграции Team_Training_CrmOps (служебные процессы: правки сделок);
# AMO_TOKEN — общий запасной вариант, см. DOPPLER.md.
AMO_TOKEN = (os.environ.get("AMO_TOKEN_CRMOPS") or os.environ.get("AMO_TOKEN", "")).strip()
if AMO_TOKEN[:7].lower() == "bearer ":
    AMO_TOKEN = AMO_TOKEN[7:].strip()
DRY_RUN = (os.environ.get("DRY_RUN", "1").strip().lower() in ("1", "true", "yes"))
MAX_CLOSE_PER_RUN = int(os.environ.get("MAX_CLOSE_PER_RUN") or "1000")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

EXCLUDE_TAG = "не автозакрывать"
HERE = os.path.dirname(os.path.abspath(__file__))
EXCLUDE_FILE = os.path.join(HERE, "exclude_leads.txt")
ROLLBACK_FILE = os.path.join(HERE, f"rollback_second_line_{datetime.now(MSK):%Y%m%d_%H%M}.json")


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


def fetch_all(path, params, key):
    out, url, page = [], path, 0
    while url and page < 200:
        d = amo_request('GET', url, params=params if page == 0 else None)
        got = (d.get('_embedded') or {}).get(key) or []
        if not got:
            break
        out.extend(got)
        page += 1
        url = ((d.get('_links') or {}).get('next') or {}).get('href')
        time.sleep(REQUEST_INTERVAL)
    return out


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


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                            "disable_web_page_preview": True}, timeout=30)
    except Exception as ex:
        print(f"Telegram ошибка: {ex}")


def main():
    if not AMO_TOKEN:
        raise SystemExit("Нет AMO_TOKEN")
    now = int(time.time())
    exclude = load_exclude_ids()
    print(f"Режим: {'ХОЛОСТОЙ (DRY_RUN)' if DRY_RUN else 'БОЕВОЙ'}; тег «{CLOSE_TAG}»; "
          f"порог {STALE_DAYS} дн.; исключений в файле: {len(exclude)}")

    leads = fetch_all('/api/v4/leads',
                      {'limit': 250, 'filter[pipeline_id][0]': PIPELINE_ID, 'with': 'tags'},
                      'leads')
    print(f"Сделок в воронке: {len(leads)}")

    tasks = fetch_all('/api/v4/tasks',
                      {'limit': 250, 'filter[entity_type]': 'leads', 'filter[is_completed]': 0},
                      'tasks')
    with_task = {t.get('entity_id') for t in tasks}
    print(f"Сделок с открытой задачей (весь аккаунт): {len(with_task)}")

    picked, skipped_tag, skipped_excl = [], 0, 0
    for l in leads:
        if l.get('status_id') not in TARGET_STATUSES:
            continue
        if (now - int(l.get('updated_at') or 0)) // 86400 < STALE_DAYS:
            continue
        if l['id'] in with_task:
            continue
        if l['id'] in exclude:
            skipped_excl += 1
            continue
        tags = [t for t in ((l.get('_embedded') or {}).get('tags') or [])]
        if any(str(t.get('name') or '').strip().lower() == EXCLUDE_TAG for t in tags):
            skipped_tag += 1
            continue
        picked.append({
            'id': l['id'],
            'name': l.get('name'),
            'status_id': l.get('status_id'),
            'status': TARGET_STATUSES[l['status_id']],
            'updated': datetime.fromtimestamp(int(l.get('updated_at') or 0), MSK).strftime('%d.%m.%Y'),
            'days': (now - int(l.get('updated_at') or 0)) // 86400,
            'tags': [t.get('name') for t in tags],
        })

    by_stage = {}
    for p in picked:
        by_stage[p['status']] = by_stage.get(p['status'], 0) + 1
    print(f"\nК закрытию: {len(picked)}")
    for s, n in sorted(by_stage.items(), key=lambda x: -x[1]):
        print(f"  {n:>5}  {s}")
    print(f"Пропущено по тегу «{EXCLUDE_TAG}»: {skipped_tag}, по файлу исключений: {skipped_excl}")

    if len(picked) > MAX_CLOSE_PER_RUN:
        print(f"ВНИМАНИЕ: предохранитель MAX_CLOSE_PER_RUN={MAX_CLOSE_PER_RUN}, "
              f"беру первые {MAX_CLOSE_PER_RUN}")
        picked = picked[:MAX_CLOSE_PER_RUN]

    with open(ROLLBACK_FILE, 'w', encoding='utf-8') as f:
        json.dump(picked, f, ensure_ascii=False, indent=1)
    print(f"Файл отката: {ROLLBACK_FILE}")

    if DRY_RUN:
        print("\nDRY_RUN — ничего не меняю. Примеры:")
        for p in picked[:15]:
            print(f"  {p['id']} «{p['name']}» [{p['status']}] обновлена {p['updated']} "
                  f"({p['days']} дн. назад)")
        return

    done = 0
    for i in range(0, len(picked), BATCH):
        chunk = picked[i:i + BATCH]
        payload = [{
            'id': p['id'],
            'status_id': LOSS_STATUS_ID,
            '_embedded': {'tags': [{'name': t} for t in p['tags']] + [{'name': CLOSE_TAG}]},
        } for p in chunk]
        amo_request('PATCH', '/api/v4/leads', payload=payload)
        done += len(chunk)
        print(f"  закрыто {done}/{len(picked)}", flush=True)
        time.sleep(REQUEST_INTERVAL)

    print(f"Готово: закрыто {done}")
    send_telegram(f"Разовая уборка Второй линии: закрыто {done} брошенных сделок "
                  f"(этапы: {', '.join(f'{s} — {n}' for s, n in by_stage.items())}); "
                  f"тег «{CLOSE_TAG}», откат по файлу {os.path.basename(ROLLBACK_FILE)}.")


if __name__ == '__main__':
    main()
