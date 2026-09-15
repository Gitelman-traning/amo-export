import os, amo_export as ax
data = ax.amo_get('/api/v4/leads', {'limit': 20, 'filter[pipeline_id][0]': 11304934,
                                    'order[created_at]': 'desc', 'with': 'contacts'})
leads = (data.get('_embedded') or {}).get('leads') or []
print(f"Сделок в воронке 11304934 (последние {len(leads)}):")
for l in leads:
    cs = [c.get('id') for c in ((l.get('_embedded') or {}).get('contacts') or [])]
    print(f"  #{l['id']} «{l.get('name')}» статус={l.get('status_id')} отв={l.get('responsible_user_id')} "
          f"создана={ax.fmt_msk_datetime(l.get('created_at'))} контакты={cs}")
