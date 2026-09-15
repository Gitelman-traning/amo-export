#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only: детали сделки + поиск контакта по телефону (диагностика входящего хука)."""
import os
import json
import amo_export as ax

LEAD_ID = os.environ.get("LEAD_ID", "").strip()
PHONE = os.environ.get("PHONE", "").strip()


def main():
    if LEAD_ID:
        lead = ax.amo_get(f'/api/v4/leads/{LEAD_ID}', {'with': 'contacts'})
        emb = (lead.get('_embedded') or {})
        contacts = emb.get('contacts') or []
        print(f"=== Сделка {LEAD_ID} ===")
        print(f"  название: «{lead.get('name')}»")
        print(f"  воронка: {lead.get('pipeline_id')}  статус: {lead.get('status_id')}")
        print(f"  ответственный: {lead.get('responsible_user_id')}")
        print(f"  создана: {ax.fmt_msk_datetime(lead.get('created_at'))}")
        print(f"  контактов привязано: {len(contacts)} → {[c.get('id') for c in contacts]}")
        cf = lead.get('custom_fields_values') or []
        if cf:
            print("  заполненные поля сделки:")
            for f in cf:
                vals = ', '.join(str(v.get('value')) for v in (f.get('values') or []))
                print(f"    {f.get('field_name')}: {vals}")

    if PHONE:
        digits = ''.join(ch for ch in PHONE if ch.isdigit())[-10:]
        print(f"\n=== Поиск контактов по «{digits}» ===")
        data = ax.amo_get('/api/v4/contacts', {'query': digits, 'limit': 50, 'with': 'leads'})
        items = ((data.get('_embedded') or {}).get('contacts') or [])
        print(f"  найдено: {len(items)}")
        for c in items:
            phones = []
            for f in (c.get('custom_fields_values') or []):
                if f.get('field_code') == 'PHONE':
                    phones = [v.get('value') for v in (f.get('values') or [])]
            leads = [l.get('id') for l in ((c.get('_embedded') or {}).get('leads') or [])]
            print(f"    id={c['id']} «{c.get('name')}» тел={phones} создан={ax.fmt_msk_datetime(c.get('created_at'))} сделки={leads}")


if __name__ == '__main__':
    main()
