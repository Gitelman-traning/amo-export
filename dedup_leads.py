#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Дубли СДЕЛОК: две и более АКТИВНЫХ сделок одного контакта в одной воронке.

Что делает: оставляет одну сделку (мастер), остальные закрывает —
статус 143 «Закрыто и не реализовано» + причина отказа «Дубль».
amoCRM объединять сделки не умеет, поэтому именно закрытие (обратимо, история цела).

Мастер выбирается так: дальше по воронке (больший sort этапа), при равенстве — свежее.

БЕЗОПАСНОСТЬ (это запись в боевой amoCRM!):
  • DRY_RUN=1 — только показать, что закрылось бы. В workflow это значение по умолчанию.
  • MAX_CLOSE_PER_RUN — предохранитель.
  • Пропускаем сделки с тегом «не автозакрывать» и id из exclude_leads.txt.
  • Активные = статус не 142 (успех) и не 143 (закрыто).

Переменные: AMO_TOKEN, TELEGRAM_* (необяз.), PIPELINE_IDS (через запятую, пусто = все),
            DAYS_BACK (период создания, 0 = вся база), DRY_RUN, MAX_CLOSE_PER_RUN.
"""

import os
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

AMO_BASE_URL = "https://pavelgitelman.amocrm.ru"
FINAL_STATUSES = {142, 143}          # 142 — успех, 143 — закрыто и не реализовано
LOSS_STATUS_ID = 143
REASON_FIELD_ID = 1426303            # «Причина отказа»
REASON_ENUM_DUP = 1220969            # значение «Дубль»
EXCLUDE_TAG = "не автозакрывать"

TIMEZONE = "Europe/Moscow"
MSK = ZoneInfo(TIMEZONE)
REQUEST_INTERVAL = 0.2

# Ограничения выборки (ускоряют скан)
_p = os.environ.get("PIPELINE_IDS", "").replace(';', ',')
PIPELINE_IDS = [int(x) for x in _p.split(',') if x.strip().isdigit()]
DAYS_BACK = int(os.environ.get("DAYS_BACK") or "0")      # 0 = без ограничения по дате

MAX_CLOSE_PER_RUN = int(os.environ.get("MAX_CLOSE_PER_RUN") or "300")
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")

AMO_TOKEN = os.environ.get("AMO_TOKEN", "").strip()
if AMO_TOKEN[:7].lower() == "bearer ":
    AMO_TOKEN = AMO_TOKEN[7:].strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

EXCLUDE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "exclude_leads.txt")


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


def load_exclude_ids():
    ids = set()
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


def fetch_status_sort():
    """{status_id: sort} — насколько далеко этап в воронке."""
    data = amo_get('/api/v4/leads/pipelines')
    sort = {}
    for p in ((data.get('_embedded') or {}).get('pipelines') or []):
        for s in ((p.get('_embedded') or {}).get('statuses') or []):
            sort[int(s['id'])] = int(s.get('sort') or 0)
    return sort


def fetch_active_leads():
    """Активные сделки (не 142/143) с контактами, опционально по воронкам и периоду."""
    out = []
    params = {'limit': 250, 'with': 'contacts'}
    for i, pid in enumerate(PIPELINE_IDS):
        params[f'filter[pipeline_id][{i}]'] = pid
    if DAYS_BACK:
        since = int((datetime.now(MSK) - timedelta(days=DAYS_BACK)).timestamp())
        params['filter[created_at][from]'] = since
    url, first, page = '/api/v4/leads', True, 0
    while url and page < 2000:
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
        if page % 20 == 0:
            print(f"  ...страница {page}, активных набрано {len(out)}")
        time.sleep(REQUEST_INTERVAL)
    return out


def main_contact_id(lead):
    contacts = (lead.get('_embedded') or {}).get('contacts') or []
    main = next((c for c in contacts if c.get('is_main')), None)
    return (main or (contacts[0] if contacts else {})).get('id')


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

def main():
    if not AMO_TOKEN:
        print("ОШИБКА: нет AMO_TOKEN")
        sys.exit(1)

    print(f"Воронки: {PIPELINE_IDS or 'все'}; период создания: "
          f"{('последние ' + str(DAYS_BACK) + ' дн.') if DAYS_BACK else 'вся база'}; "
          f"лимит закрытий {MAX_CLOSE_PER_RUN}" + (" [DRY_RUN]" if DRY_RUN else ""))

    exclude_ids = load_exclude_ids()
    sort_by_status = fetch_status_sort()

    leads = fetch_active_leads()
    print(f"Активных сделок: {len(leads)}")

    # группируем: (контакт, воронка) → сделки
    groups = {}
    skipped = 0
    for l in leads:
        if l['id'] in exclude_ids or has_exclude_tag(l):
            skipped += 1
            continue
        cid = main_contact_id(l)
        if not cid:
            continue      # без контакта дубль не определить
        groups.setdefault((cid, int(l.get('pipeline_id') or 0)), []).append(l)
    print(f"Пропущено по исключениям/тегу: {skipped}")

    dup_groups = {k: v for k, v in groups.items() if len(v) > 1}
    to_close = []
    for (cid, pid), items in dup_groups.items():
        # мастер: дальше по воронке, при равенстве — свежее
        items.sort(key=lambda x: (sort_by_status.get(int(x.get('status_id') or 0), 0),
                                  int(x.get('created_at') or 0)), reverse=True)
        master, dups = items[0], items[1:]
        for d in dups:
            to_close.append((d, master, cid, pid))

    print(f"Групп с дублями: {len(dup_groups)}; сделок к закрытию: {len(to_close)}")

    capped = to_close[:MAX_CLOSE_PER_RUN]
    if len(to_close) > MAX_CLOSE_PER_RUN:
        print(f"⚠ лимит {MAX_CLOSE_PER_RUN} — закрою первые {MAX_CLOSE_PER_RUN}")

    if DRY_RUN:
        print("\n[DRY_RUN] Закрылись бы (первые 25):")
        for d, master, cid, pid in capped[:25]:
            print(f"  #{d['id']} «{d.get('name')}» (контакт {cid}, воронка {pid}) "
                  f"→ остаётся #{master['id']} «{master.get('name')}»")
        return {'groups': len(dup_groups), 'closed': 0, 'planned': len(to_close)}

    payload = [{
        'id': d['id'],
        'pipeline_id': pid,
        'status_id': LOSS_STATUS_ID,
        'custom_fields_values': [{'field_id': REASON_FIELD_ID,
                                  'values': [{'enum_id': REASON_ENUM_DUP}]}],
    } for d, master, cid, pid in capped]

    closed = 0
    for chunk in chunked(payload, 50):
        amo_request('PATCH', '/api/v4/leads', payload=chunk)
        closed += len(chunk)
        print(f"  закрыто: {closed}/{len(payload)}")
        time.sleep(REQUEST_INTERVAL)

    print(f"ГОТОВО. Закрыто дублей: {closed} (групп: {len(dup_groups)}).")
    return {'groups': len(dup_groups), 'closed': closed, 'planned': len(to_close)}


if __name__ == '__main__':
    try:
        s = main()
        tail = f"\n⚠ осталось на след. прогон: {s['planned'] - s['closed']}" if s['planned'] > s['closed'] else ""
        send_telegram(("🧪 [DRY_RUN] " if DRY_RUN else "✅ ") +
                      "Дубли сделок\n"
                      f"Групп с дублями: {s['groups']}\n"
                      f"{'Закрылось бы' if DRY_RUN else 'Закрыто'}: {s['closed'] or s['planned']}"
                      + tail + run_url_line())
    except Exception as e:
        send_telegram(f"❌ Дубли сделок: упало\n{type(e).__name__}: {str(e)[:300]}" + run_url_line())
        raise
