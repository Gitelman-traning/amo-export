#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Разведка для автозакрытия застрявших сделок Первой линии (read-only).
Показывает статусы воронки, поле «Причина отказа» с enum-ами и масштаб (сколько
сделок подпадёт под задачу на 27-й день и под закрытие на 30-й). Ничего не меняет."""

import os
from datetime import datetime, timezone
import amo_export as ax

PIPELINE_ID = 8733326  # Первая линия


def main():
    # 1. Статусы воронки
    data = ax.amo_get(f'/api/v4/leads/pipelines/{PIPELINE_ID}')
    statuses = (data.get('_embedded') or {}).get('statuses') or []
    print(f"=== Воронка {PIPELINE_ID} «{data.get('name')}» — статусы ===")
    for s in statuses:
        print(f"  id={s['id']}  type={s.get('type')}  «{s.get('name')}»")

    # 2. Поле «Причина отказа» + enum-ы
    print("\n=== Поле «Причина отказа» ===")
    page = 1
    found = False
    while page <= 20 and not found:
        d = ax.amo_get('/api/v4/leads/custom_fields', {'limit': 250, 'page': page})
        fs = (d.get('_embedded') or {}).get('custom_fields') or []
        if not fs:
            break
        for f in fs:
            if 'причина отказа' in str(f.get('name') or '').strip().lower():
                found = True
                print(f"  field_id={f['id']}  «{f['name']}»  type={f.get('type')}")
                for e in (f.get('enums') or []):
                    print(f"    enum_id={e['id']}  «{e['value']}»")
        page += 1
    if not found:
        print("  поле не найдено на сделках")

    # 3. Масштаб: открытые сделки воронки, возраст по created_at (МСК)
    now = datetime.now(timezone.utc)
    buckets = {'age27': 0, 'age28_29': 0, 'age30plus': 0, 'total_open': 0}
    ex27, ex30 = [], []
    months30 = {}          # распределение 30+ дневных по месяцу создания
    min30, max30 = None, None
    params = {'limit': 250, 'filter[pipeline_id][0]': PIPELINE_ID}
    url, page = '/api/v4/leads', 0
    first = True
    while url and page < 500:
        dd = ax.amo_get(url, params if first else None)
        first = False
        leads = (dd.get('_embedded') or {}).get('leads') or []
        if not leads:
            break
        for l in leads:
            st = l.get('status_id')
            if st in (142, 143):   # финальные (успех / закрыто-не реализовано)
                continue
            buckets['total_open'] += 1
            created = int(l.get('created_at') or 0)
            age = (now - datetime.fromtimestamp(created, tz=timezone.utc)).days
            if age == 27:
                buckets['age27'] += 1
                if len(ex27) < 5:
                    ex27.append((l['id'], age, l.get('name')))
            elif 28 <= age <= 29:
                buckets['age28_29'] += 1
            elif age >= 30:
                buckets['age30plus'] += 1
                cdt = datetime.fromtimestamp(created, tz=timezone.utc)
                months30[f"{cdt.year}-{cdt.month:02d}"] = months30.get(f"{cdt.year}-{cdt.month:02d}", 0) + 1
                if min30 is None or created < min30:
                    min30 = created
                if max30 is None or created > max30:
                    max30 = created
                if len(ex30) < 5:
                    ex30.append((l['id'], age, l.get('name')))
        url = ((dd.get('_links') or {}).get('next') or {}).get('href')
        page += 1

    print("\n=== Масштаб (открытые сделки Первой линии, по возрасту created_at) ===")
    print(f"  всего открытых (не 142/143): {buckets['total_open']}")
    print(f"  ровно 27 дней (→ задача):    {buckets['age27']}")
    print(f"  28-29 дней:                  {buckets['age28_29']}")
    print(f"  30+ дней (→ закрытие):       {buckets['age30plus']}")
    print("  примеры 27-дневных:", ex27)
    print("  примеры 30+ дневных:", ex30)

    def d(ts):
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%d.%m.%Y') if ts else '-'
    print("\n=== 30+ дневные: даты создания (для ручного разбора) ===")
    print(f"  самая ранняя: {d(min30)}   самая поздняя: {d(max30)}")
    print("  по месяцам создания:")
    for k in sorted(months30):
        print(f"    {k}: {months30[k]}")


if __name__ == '__main__':
    main()
