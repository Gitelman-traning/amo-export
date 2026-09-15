#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only: показать этапы воронки (для настройки приёмника сделок от партнёра)."""
import os
import amo_export as ax

PIPELINE_ID = int(os.environ.get("PIPELINE_ID") or "11304934")


def main():
    data = ax.amo_get(f'/api/v4/leads/pipelines/{PIPELINE_ID}')
    print(f"Воронка {PIPELINE_ID}: «{data.get('name')}»  (is_main={data.get('is_main')})")
    st = (data.get('_embedded') or {}).get('statuses') or []
    print("Этапы (sort — порядок в воронке):")
    for s in sorted(st, key=lambda x: int(x.get('sort') or 0)):
        print(f"  id={s['id']:>10}  sort={s.get('sort'):>3}  type={s.get('type')}  «{s.get('name')}»")


if __name__ == '__main__':
    main()
