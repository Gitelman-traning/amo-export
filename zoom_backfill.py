#!/usr/bin/env python3
"""One-off Zoom recovery: two recognition workers, one Google Sheets writer."""
import concurrent.futures
import json
import os
import re
import time
from pathlib import Path

import requests
import zoom_transcribe as z

JOURNAL = Path('zoom-backfill-results.json')


def parse_ids(value):
    parts = [p.strip() for p in value.split(',') if p.strip()]
    if not parts or len(parts) > 200 or any(not p.isdigit() for p in parts):
        raise ValueError('Provide 1–200 numeric deal IDs separated by commas')
    return list(dict.fromkeys(parts))


def read_dpd(sheets):
    rows = sheets.get(spreadsheetId=z.MARKETING_SHEET_ID,
                      range=f"'{z.DPD_TAB}'!1:100000").execute().get('values', [])
    headers = rows[0]
    id_col, tr_col = headers.index('ID'), headers.index('transcribation')
    indexed = {}
    for num, row in enumerate(rows[1:], 2):
        rid = str(row[id_col]).strip() if id_col < len(row) else ''
        if not rid:
            continue
        ready = bool(tr_col < len(row) and str(row[tr_col]).strip())
        if rid not in indexed or ready:
            indexed[rid] = (num, ready)
    return headers, indexed


def save_result(sheets, headers, indexed, row, doc_url):
    values = {'ID': row['ID'], 'first_name': row['first_name'],
              'zoom_share_url': row['share_url'], 'zoom_passcode': row['passcode'],
              'transcribation': doc_url}
    rid = row['ID']
    if rid in indexed:
        num = indexed[rid][0]
        data = [{'range': f"'{z.DPD_TAB}'!{z.col_letter(headers.index(k))}{num}",
                 'values': [[z.safe_cell(v)]]} for k, v in values.items() if k in headers]
        sheets.batchUpdate(spreadsheetId=z.MARKETING_SHEET_ID,
                           body={'valueInputOption': 'USER_ENTERED', 'data': data}).execute()
    else:
        cells = [''] * len(headers)
        for key, value in values.items():
            if key in headers:
                cells[headers.index(key)] = z.safe_cell(value)
        sheets.append(spreadsheetId=z.MARKETING_SHEET_ID, range=f"'{z.DPD_TAB}'!A1",
                      valueInputOption='USER_ENTERED', insertDataOption='INSERT_ROWS',
                      body={'values': [cells]}).execute()


def prepare(row, nofunds):
    rid = row['ID']
    audio_path = None
    result = {'deal': rid}
    try:
        transcript = z.shtab_transcript(rid)
        if transcript:
            return {**result, 'text': transcript, 'source': 'shtab'}
        if nofunds:
            return {**result, 'status': 'error', 'reason': 'DEEPGRAM_NO_BALANCE'}
        response = requests.post(
            f'https://api.apify.com/v2/actor-tasks/{z.APIFY_TASK}/runs',
            params={'token': z.APIFY_TOKEN, 'timeout': 600},
            json={'shareUrl': row['share_url'], 'passcode': row['passcode']}, timeout=60)
        response.raise_for_status()
        run = response.json()['data']['id']
        result['apify_run'] = run
        print(f'[{rid}] collecting audio; Apify {run}', flush=True)
        for _ in range(65):
            response = requests.get(f'https://api.apify.com/v2/actor-runs/{run}',
                                    params={'token': z.APIFY_TOKEN}, timeout=60)
            response.raise_for_status()
            status = response.json()['data']['status']
            if status not in ('READY', 'RUNNING', 'TIMING-OUT'):
                break
            time.sleep(10)
        else:
            return {**result, 'status': 'error', 'reason': 'APIFY_POLL_TIMEOUT'}
        if status != 'SUCCEEDED':
            return {**result, 'status': 'error', 'reason': 'APIFY_' + status}
        item = z.apify_result(run)
        if not item.get('audio_url'):
            reason = str(item.get('error') or 'NO_AUDIO_URL')
            # Only publish an error code, never Zoom URLs, cookies or passcodes.
            reason = reason if re.fullmatch(r'[A-Z_0-9-]{1,100}', reason) else 'NO_AUDIO_URL'
            return {**result, 'status': 'error', 'reason': reason}
        print(f'[{rid}] audio ready; recognizing', flush=True)
        audio_path = z.download_audio(item['audio_url'], item.get('finalUrl'),
                                      item.get('cookieHeader'), rid)
        dg = z.deepgram_transcribe(audio_path)
        transcript = dg['results']['channels'][0]['alternatives'][0]['transcript']
        if not transcript.strip():
            return {**result, 'status': 'error', 'reason': 'EMPTY_TRANSCRIPT'}
        z.shtab_share(rid, dg, row['share_url'])
        minutes = round(float((dg.get('metadata') or {}).get('duration') or 0) / 60, 1)
        return {**result, 'text': transcript, 'source': 'deepgram', 'minutes': minutes}
    except Exception as exc:
        code = getattr(getattr(exc, 'response', None), 'status_code', None)
        return {**result, 'status': 'error', 'reason': type(exc).__name__ + (f'_HTTP_{code}' if code else '')}
    finally:
        if audio_path:
            Path(audio_path).unlink(missing_ok=True)


def main():
    wanted = parse_ids(os.environ.get('BACKFILL_DEALS', ''))
    for name in ('APIFY_TOKEN', 'DEEPGRAM_TOKEN', 'GOOGLE_SERVICE_ACCOUNT_JSON',
                 'GOOGLE_OAUTH_CLIENT_ID', 'GOOGLE_OAUTH_CLIENT_SECRET', 'GOOGLE_OAUTH_REFRESH_TOKEN'):
        if not os.environ.get(name):
            raise ValueError('Missing ' + name)
    sheets, docs, drive = z.google_clients()
    _, source = z.read_tab(sheets, z.ZOOM_TAB, z.ZOOM_HEADER_ROW)
    _, existing = read_dpd(sheets)
    by_id = {}
    for item in source:
        rid = str(item.get('ID') or '').strip()
        parsed = z.parse_row(item)
        if rid in wanted and parsed and rid not in by_id:
            by_id[rid] = {'ID': rid, 'first_name': item.get('Основной контакт') or '', **parsed}
    results, pending = [], []

    def record(result):
        # Full recognition text stays in memory / approved destinations, not in artifacts.
        clean = {k: v for k, v in result.items() if k != 'text'}
        results.append(clean)
        temporary = JOURNAL.with_suffix('.tmp')
        temporary.write_text(json.dumps({'requested': wanted, 'results': results}, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(JOURNAL)
        print(json.dumps(clean, ensure_ascii=False), flush=True)

    for rid in wanted:
        if existing.get(rid, (None, False))[1]:
            record({'deal': rid, 'status': 'already_ready'})
        elif rid not in by_id:
            record({'deal': rid, 'status': 'error', 'reason': 'NO_ZOOM_LINK'})
        else:
            pending.append(by_id[rid])
    print(f'BACKFILL requested={len(wanted)} pending={len(pending)} workers=2', flush=True)
    nofunds = not z.deepgram_ok() if pending else False
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        jobs = {pool.submit(prepare, row, nofunds): row for row in pending}
        for future in concurrent.futures.as_completed(jobs):
            row, result = jobs[future], future.result()
            if 'text' not in result:
                record(result)
                continue
            try:
                # Appends can insert rows inside the existing sheet. Always re-read
                # IDs before the sole writer creates a document and writes its link.
                headers, current = read_dpd(sheets)
                if current.get(row['ID'], (None, False))[1]:
                    record({'deal': row['ID'], 'status': 'already_ready'})
                    continue
                doc_id = z.create_doc(docs, drive, row['ID'], result['text'])
                result['doc_url'] = f'https://docs.google.com/document/d/{doc_id}/edit'
                result['chars'] = len(result.pop('text'))
                save_result(sheets, headers, current, row, result['doc_url'])
                _, verified = read_dpd(sheets)
                if not verified.get(row['ID'], (None, False))[1]:
                    raise RuntimeError('LINK_NOT_SAVED')
                result['status'] = 'ready'
            except Exception as exc:
                result.pop('text', None)
                result['status'], result['reason'] = 'error', 'GOOGLE_' + type(exc).__name__
            record(result)
    totals = {key: sum(r['status'] == key for r in results) for key in ('ready', 'already_ready', 'error')}
    print('BACKFILL_DONE ' + json.dumps(totals), flush=True)
    return 1 if totals['error'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
