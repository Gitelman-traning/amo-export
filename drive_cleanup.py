#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Чистка файлового хранилища amoCRM от копий рассылок.

Зачем. Рассылка Salebot загружает один и тот же файл заново для каждого
получателя: 06.10.2026 за сутки прилетело 11 копий одного PDF на 187 МБ.
Хранилище (≈5 ГБ) забивается за полторы-две недели, и менеджеры перестают
прикреплять скриншоты. Проверено инвентаризацией: из 933 МБ занятого места
569 МБ — повторы одних и тех же материалов.

Правило (согласовано с Никитой 06.10.2026):
  • файлы группируем по имени и размеру — совпало, значит это та же рассылка,
    сколько бы раз её ни отправляли (автор не годится: amo записывает файл на
    получателя, поэтому копии числятся и за ботом, и за контактами);
  • в каждой группе оставляем САМУЮ РАННЮЮ копию, остальные удаляем;
  • файлы моложе MIN_AGE_HOURS не трогаем — свежие диалоги остаются целыми;
  • загруженное менеджерами вручную (created_by.type = internal) не трогаем;
  • перед удалением пишем журнал: что, когда, какого размера.

Удаление окончательное (DELETE /v1.0/files), нужен scope «Удаление файлов».

Запуск:
  DRY_RUN=1 python drive_cleanup.py      # показать, ничего не трогая
  python drive_cleanup.py                # боевой прогон
Переменные: AMO_TOKEN_CRMOPS (или AMO_TOKEN), TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
            MIN_AGE_HOURS (24), MAX_DELETE_PER_RUN (2000), KEEP_COPIES (1).
"""

import os
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

DRIVE = "https://drive-b.amocrm.ru"
MSK = ZoneInfo("Europe/Moscow")
PAGE = 250
BATCH = 50               # сколько uuid отправляем в одном DELETE
REQUEST_INTERVAL = 0.2   # пауза между запросами: лимиты amo, см. DOPPLER.md

AMO_TOKEN = (os.environ.get("AMO_TOKEN_CRMOPS") or os.environ.get("AMO_TOKEN", "")).strip()
if AMO_TOKEN[:7].lower() == "bearer ":
    AMO_TOKEN = AMO_TOKEN[7:].strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")
MIN_AGE_HOURS = int(os.environ.get("MIN_AGE_HOURS") or "24")
MAX_DELETE_PER_RUN = int(os.environ.get("MAX_DELETE_PER_RUN") or "2000")
KEEP_COPIES = max(1, int(os.environ.get("KEEP_COPIES") or "1"))

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, f"drive_cleanup_{datetime.now(MSK):%Y%m%d_%H%M}.json")
H = {"Authorization": f"Bearer {AMO_TOKEN}", "Accept": "application/json"}


def mb(n):
    return f"{n / 1024 / 1024:,.1f} МБ".replace(",", " ")


def request(method, url, **kw):
    for attempt in range(5):
        r = requests.request(method, url, headers={**H, **kw.pop("headers", {})}, timeout=90, **kw)
        if r.status_code in (200, 201, 204):
            return r
        if r.status_code in (429, 500, 502, 503, 504):
            wait = 2 ** attempt
            print(f"  amo {r.status_code}, повтор через {wait}с...")
            time.sleep(wait)
            continue
        raise RuntimeError(f"{method} {url} → {r.status_code}: {r.text[:300]}")
    raise RuntimeError(f"не удалось {method} {url}")


def stats():
    d = request("GET", f"{DRIVE}/v1.0/files/stats").json()
    return d.get("used", 0), d.get("limit", 0)


def fetch_files():
    out, url = [], f"{DRIVE}/v1.0/files?limit={PAGE}"
    while url:
        d = request("GET", url).json()
        items = (d.get("_embedded") or {}).get("files") or []
        if not items:
            break
        out.extend(items)
        url = ((d.get("_links") or {}).get("next") or {}).get("href")
        time.sleep(REQUEST_INTERVAL)
    return out


def pick(files, now):
    """Что удаляем: лишние копии, старше MIN_AGE_HOURS, не от менеджеров."""
    groups = {}
    for f in files:
        if f.get("is_trashed"):
            continue
        key = (f.get("sanitized_name") or f.get("name"), f.get("size"))
        groups.setdefault(key, []).append(f)

    doomed = []
    for key, items in groups.items():
        if len(items) <= KEEP_COPIES:
            continue
        items.sort(key=lambda x: x.get("created_at") or 0)
        for f in items[KEEP_COPIES:]:                 # самые ранние остаются
            by = f.get("created_by") or {}
            if by.get("type") == "internal":          # менеджер приложил руками
                continue
            if (now - int(f.get("created_at") or 0)) < MIN_AGE_HOURS * 3600:
                continue
            doomed.append(f)
    doomed.sort(key=lambda x: -(x.get("size") or 0))
    return groups, doomed


def delete(files):
    done = 0
    for i in range(0, len(files), BATCH):
        chunk = files[i:i + BATCH]
        request("DELETE", f"{DRIVE}/v1.0/files",
                headers={"Content-Type": "application/json"},
                json=[{"uuid": f["uuid"]} for f in chunk])
        done += len(chunk)
        print(f"  удалено {done}/{len(files)}", flush=True)
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
        raise SystemExit("Нет AMO_TOKEN_CRMOPS / AMO_TOKEN")
    now = int(time.time())
    used, limit = stats()
    print(f"Режим: {'ХОЛОСТОЙ (DRY_RUN)' if DRY_RUN else 'БОЕВОЙ'}; "
          f"занято {mb(used)} из {mb(limit)} ({used / limit * 100:.1f}%)")

    files = fetch_files()
    print(f"Файлов в хранилище: {len(files)}")
    groups, doomed = pick(files, now)
    free = sum(f.get("size") or 0 for f in doomed)
    print(f"Групп по имени+размеру: {len(groups)}")
    print(f"К удалению: {len(doomed)} шт., освободится {mb(free)}")

    if len(doomed) > MAX_DELETE_PER_RUN:
        print(f"Предохранитель: беру первые {MAX_DELETE_PER_RUN}")
        doomed = doomed[:MAX_DELETE_PER_RUN]
        free = sum(f.get("size") or 0 for f in doomed)

    journal = [{
        "uuid": f["uuid"], "name": f.get("name"), "size": f.get("size"),
        "created": datetime.fromtimestamp(f.get("created_at") or 0, MSK).strftime("%d.%m.%Y %H:%M"),
        "created_by": f.get("created_by"), "source_id": f.get("source_id"),
    } for f in doomed]
    with open(LOG_PATH, "w", encoding="utf-8") as fh:
        json.dump(journal, fh, ensure_ascii=False, indent=1)
    print(f"Журнал: {LOG_PATH}")

    print("\nСамое крупное к удалению:")
    for f in doomed[:10]:
        print(f"  {mb(f.get('size') or 0):>10}  {f.get('name')}  "
              f"({datetime.fromtimestamp(f.get('created_at') or 0, MSK):%d.%m %H:%M})")

    if DRY_RUN:
        print("\nDRY_RUN — ничего не удаляю.")
        return
    if not doomed:
        print("Удалять нечего.")
        return

    done = delete(doomed)
    used2, _ = stats()
    print(f"Готово: удалено {done}, занято теперь {mb(used2)} "
          f"({used2 / limit * 100:.1f}%)")
    send_telegram(
        f"Чистка файлов amo: удалено {done} копий, освободилось {mb(used - used2)}.\n"
        f"Занято {mb(used2)} из {mb(limit)} ({used2 / limit * 100:.0f}%).")


if __name__ == "__main__":
    main()
