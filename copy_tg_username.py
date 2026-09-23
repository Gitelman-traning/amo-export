#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Разовая задача: продублировать значение поля контакта «tg_username» (1445897)
в поле «TelegramUsername_WZ» (1441143).

Правила:
  • пишем только если целевое поле ПУСТОЕ;
  • если совпадает (без учёта регистра) — пропускаем;
  • если заполнено ДРУГИМ значением — не трогаем, только показываем список;
  • значение приводится к нижнему регистру (как у карточек из Wazzup), «@» сохраняем.

Переменные окружения:
  AMO_TOKEN            — токен amo (обязателен)
  DRY_RUN=1            — холостой прогон (по умолчанию именно так)
  LIMIT=0              — ограничение на число изменяемых контактов (0 = без лимита)
  LOWERCASE=0          — копировать значение как есть, без приведения к нижнему регистру
"""
import os
import re
import sys
import time
import requests

AMO_BASE_URL = "https://pavelgitelman.amocrm.ru"
SRC_FIELD = 1445897   # tg_username
DST_FIELD = 1441143   # TelegramUsername_WZ
BATCH = 50
REQUEST_INTERVAL = 0.2

AMO_TOKEN = os.environ.get("AMO_TOKEN", "").strip()
if AMO_TOKEN[:7].lower() == "bearer ":
    AMO_TOKEN = AMO_TOKEN[7:].strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

DRY_RUN = os.environ.get("DRY_RUN", "1").strip().lower() in ("1", "true", "yes")
LIMIT = int(os.environ.get("LIMIT", "0") or "0")
LOWERCASE = os.environ.get("LOWERCASE", "1").strip().lower() in ("1", "true", "yes")


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


GARBAGE = {'array', 'null', 'none', 'false', 'true', '-', '—'}
NICK_RE = re.compile(r'^@?[A-Za-z0-9_]{4,32}$')


def clean_nick(raw):
    """Приводит значение к виду @nickname. Возвращает None для мусора."""
    v = str(raw or '').strip()
    if not v or v.lower() in GARBAGE:
        return None
    if not NICK_RE.match(v):
        return None
    if not v.startswith('@'):
        v = '@' + v
    return v.lower() if LOWERCASE else v


def field_value(contact, field_id):
    for f in (contact.get('custom_fields_values') or []):
        if f.get('field_id') == field_id:
            vals = [v.get('value') for v in (f.get('values') or []) if v.get('value') not in (None, '')]
            return str(vals[0]).strip() if vals else None
    return None


def scan():
    """Проходит всю базу контактов и раскладывает их по трём корзинам."""
    to_fill, same, conflict, garbage = [], [], [], []
    total = 0
    url, params, page = '/api/v4/contacts', {'limit': 250}, 0
    t0 = time.time()
    while url:
        data = amo_request('GET', url, params=params if page == 0 else None)
        items = (data.get('_embedded') or {}).get('contacts') or []
        if not items:
            break
        total += len(items)
        page += 1
        for c in items:
            src = field_value(c, SRC_FIELD)
            if not src:
                continue
            dst = field_value(c, DST_FIELD)
            row = {'id': c['id'], 'name': c.get('name') or '', 'src': src, 'dst': dst}
            row['value'] = clean_nick(src)
            if not row['value']:
                garbage.append(row)
            elif not dst:
                to_fill.append(row)
            elif dst.lstrip('@').lower() == row['value'].lstrip('@').lower():
                same.append(row)
            else:
                conflict.append(row)
        if page % 40 == 0:
            print(f"  ...стр {page}, контактов {total}, к заполнению {len(to_fill)}, "
                  f"{int(time.time() - t0)} c", flush=True)
        url = ((data.get('_links') or {}).get('next') or {}).get('href')
        if url:
            time.sleep(REQUEST_INTERVAL)
    print(f"Просмотрено контактов: {total} за {int(time.time() - t0)} c")
    return to_fill, same, conflict, garbage


def write(rows):
    """Батчами проставляет значение в TelegramUsername_WZ."""
    done = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        payload = [{
            'id': r['id'],
            'custom_fields_values': [{
                'field_id': DST_FIELD,
                'values': [{'value': r['value']}],
            }],
        } for r in chunk]
        amo_request('PATCH', '/api/v4/contacts', payload=payload)
        done += len(chunk)
        print(f"  записано {done}/{len(rows)}", flush=True)
        time.sleep(REQUEST_INTERVAL)
    return done


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
        sys.exit("Нет AMO_TOKEN")

    print(f"Режим: {'ХОЛОСТОЙ (DRY_RUN)' if DRY_RUN else 'БОЕВОЙ'}; "
          f"нижний регистр: {'да' if LOWERCASE else 'нет'}; лимит: {LIMIT or 'без лимита'}")
    to_fill, same, conflict, garbage = scan()

    print(f"\nС заполненным tg_username: {len(to_fill) + len(same) + len(conflict) + len(garbage)}")
    print(f"  целевое поле пустое      : {len(to_fill)}  ← заполняем")
    print(f"  уже совпадает            : {len(same)}")
    print(f"  занято другим значением  : {len(conflict)}  ← не трогаем")
    print(f"  мусор в tg_username      : {len(garbage)}  ← пропускаем (Array и подобное)")

    print("\nПримеры к заполнению:")
    for r in to_fill[:15]:
        print(f"  {r['id']} «{r['name']}»  {r['src']} → {r['value']}")
    if conflict:
        print("\nРасхождения (оставлены как есть):")
        for r in conflict[:20]:
            print(f"  {r['id']} «{r['name']}»  tg_username={r['src']}  TelegramUsername_WZ={r['dst']}")
        if len(conflict) > 20:
            print(f"  ...и ещё {len(conflict) - 20}")

    rows = to_fill[:LIMIT] if LIMIT else to_fill
    if DRY_RUN:
        print(f"\nDRY_RUN — ничего не записано. К записи готово: {len(rows)}")
        return

    print(f"\nЗаписываю {len(rows)} контактов...")
    done = write(rows)
    print(f"Готово: {done}")
    send_telegram(f"tg_username → TelegramUsername_WZ: заполнено {done} контактов; "
                  f"совпадало {len(same)}, расхождений {len(conflict)}, мусор {len(garbage)}.")


if __name__ == '__main__':
    main()
