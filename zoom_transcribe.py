#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Транскрибация записей Zoom → Google Docs, ссылка в лист «дпд».
Замена n8n-воркфлоу «Транскрибация зумов (G/H, по расписанию)» (onAVa33RGViML8Ey).

Логика 1-в-1:
  1. Читает лист «ZOOM» (заголовки в строке 1931), лист «дпд».
  2. Берёт строки, которых ещё нет в «дпд» с заполненным transcribation.
  3. Для каждой: чистит ссылку Zoom и код доступа (из ссылки, иначе из «Код доступа»).
  4. Apify-задача достаёт аудио записи → скачиваем mp4 → Deepgram (рус.) расшифровывает.
  5. Создаёт Google Doc с текстом, пишет ссылку на док в «дпд» (upsert по ID).

Запуск по расписанию (внешний триггер cron-job.org → GitHub API), 22:20 МСК.
"""

import os
import re
import sys
import time
import tempfile

import requests
from google.oauth2.service_account import Credentials
from google.oauth2.credentials import Credentials as UserCredentials
from googleapiclient.discovery import build

# ============================================================
#  НАСТРОЙКИ
# ============================================================

MARKETING_SHEET_ID = "1gbj-FGKRnc5Cm4s5_qFfHpx-gHqL3Bn37FQbR8gTpuY"  # таблица «маркетинг»
ZOOM_TAB = "ZOOM"
ZOOM_HEADER_ROW = 1931          # заголовки листа ZOOM в этой строке, данные — ниже
DPD_TAB = "дпд"
DOCS_FOLDER_ID = "1F_SdcscyOKhXUe79Va65m18d9ScfviVj"   # папка Google Drive для доков

APIFY_TASK = "gyglem~my-actor-1-task"
DEEPGRAM_URL = "https://api.deepgram.com/v1/listen?model=nova-3&language=ru&smart_format=true"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

POLL_INTERVAL = 60      # пауза между опросами статуса Apify, сек
MAX_POLLS = 30          # максимум опросов на одну запись (30 мин), потом пропуск
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN") or "21")  # максимум записей за прогон (≈2 дневные нормы);
                        # берём НОВЕЙШИЕ по «Дата Диагностика проведена», старый бэклог не грызём.
                        # Можно переопределить через ручной запуск (поле limit) для теста.

# ---- Секреты из окружения ----
APIFY_TOKEN = os.environ.get("APIFY_TOKEN", "").strip()
DEEPGRAM_TOKEN = os.environ.get("DEEPGRAM_TOKEN", "").strip()
GOOGLE_SA_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
# OAuth реального пользователя (gitelmanteam1) — для СОЗДАНИЯ Google Docs (у сервис-аккаунта нет Drive)
GOOGLE_OAUTH_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
GOOGLE_OAUTH_REFRESH_TOKEN = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# ============================================================
#  Хелперы
# ============================================================

def safe_cell(v):
    if v is None:
        return ''
    if isinstance(v, str) and v[:1] in ('=', '+', '-', '@'):
        return "'" + v
    return v


def col_letter(idx0):
    """0-based индекс колонки -> буква (0->A)."""
    s, n = '', idx0 + 1
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def strip_code_prefix(s):
    if s is None:
        return ''
    s = str(s).strip()
    s = re.sub(r'^.*?(?:секретн\w*\s*код|код\s*доступа|passcode|password|пароль|код)\s*[:\-–—]?\s*',
              '', s, count=1, flags=re.I)
    s = s.strip()
    s = re.sub(r'^[:\-–—\s]+', '', s)
    s = re.sub(r"[)\]'»]+$", '', s)
    return s.strip()


def diag_sort_key(s):
    """Дата вида dd.mm.yy(yy) -> целое yyyymmdd для сортировки (0 если не распарсилось)."""
    m = re.search(r'(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})', str(s or ''))
    if not m:
        return 0
    d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
    y = int('20' + y) if len(y) == 2 else int(y)
    return y * 10000 + mo * 100 + d


def parse_row(zoom_row):
    """Из строки листа ZOOM достаёт shareUrl + passcode (порт JS-ноды)."""
    g = str(zoom_row.get('Ссылка zoom запись') or '').strip()
    h = zoom_row.get('Код доступа')

    m = re.search(r"(https?://[^\s'<>]*zoom\.us[^\s'<>]*)", g, re.I)
    if not m:
        return None
    share_url = re.sub(r"[)\]'».,;]+$", '', m.group(0))

    code_from_g = None
    tail = g[m.end():].strip()
    if tail:
        kw = re.search(r"(?:секретн\w*\s*код|код\s*доступа|passcode|password|пароль|код)\s*[:\-–—]?\s*([^\s\r\n]+)",
                       tail, re.I)
        code_from_g = kw.group(1) if kw else tail.split()[-1]
        if code_from_g:
            code_from_g = re.sub(r"[)\]'»]+$", '', code_from_g)
    if code_from_g and not re.search(r'[A-Za-z0-9]', code_from_g):
        code_from_g = None

    code_from_h = strip_code_prefix(h)
    if code_from_h and not re.search(r'[A-Za-z0-9]', code_from_h):
        code_from_h = ''

    passcode = (code_from_g.strip() if code_from_g else '') or (code_from_h.strip() if code_from_h else '')
    return {'share_url': share_url, 'passcode': passcode}


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram-отбивка пропущена (нет секретов).")
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True},
                      timeout=30)
    except Exception as ex:
        print(f"Telegram ошибка: {ex}")


def run_url_line():
    server = os.environ.get("GITHUB_SERVER_URL", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    return f"\nЛог: {server}/{repo}/actions/runs/{run_id}" if (server and repo and run_id) else ""


# ============================================================
#  Apify
# ============================================================

def apify_start(share_url, passcode):
    r = requests.post(f"https://api.apify.com/v2/actor-tasks/{APIFY_TASK}/runs",
                      params={'token': APIFY_TOKEN},
                      json={'shareUrl': share_url, 'passcode': passcode}, timeout=60)
    r.raise_for_status()
    return r.json()['data']['id']


def apify_wait(run_id):
    """Опрашивает статус раз в POLL_INTERVAL. Возвращает финальный статус (SUCCEEDED и т.п.)."""
    for _ in range(MAX_POLLS):
        time.sleep(POLL_INTERVAL)
        r = requests.get(f"https://api.apify.com/v2/actor-runs/{run_id}",
                         params={'token': APIFY_TOKEN}, timeout=60)
        r.raise_for_status()
        st = r.json()['data']['status']
        if st in ('READY', 'RUNNING', 'TIMING-OUT'):
            continue
        return st
    return 'TIMEOUT'


def apify_result(run_id):
    r = requests.get(f"https://api.apify.com/v2/actor-runs/{run_id}/dataset/items",
                     params={'token': APIFY_TOKEN, 'clean': 'true'}, timeout=120)
    r.raise_for_status()
    items = r.json()
    return items[0] if items else {}


def download_audio(url, referer, cookie, filename):
    headers = {
        'Referer': referer or '',
        'Cookie': cookie or '',
        'Origin': 'https://us06web.zoom.us',
        'User-Agent': USER_AGENT,
        'Content-Disposition': f'attachment; filename="{filename}"',
    }
    r = requests.get(url, headers=headers, stream=True, timeout=600)
    r.raise_for_status()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.mp4')
    for chunk in r.iter_content(1 << 20):
        tmp.write(chunk)
    tmp.close()
    return tmp.name


def deepgram_transcribe(path):
    with open(path, 'rb') as f:
        r = requests.post(DEEPGRAM_URL,
                          headers={'Content-Type': 'video/mp4', 'Authorization': f'Token {DEEPGRAM_TOKEN}'},
                          data=f, timeout=1800)
    r.raise_for_status()
    j = r.json()
    return j['results']['channels'][0]['alternatives'][0]['transcript']


# ============================================================
#  Google
# ============================================================

def google_clients():
    import json
    # таблицы — сервис-аккаунт (у него есть доступ к «маркетинг»)
    sa = Credentials.from_service_account_info(
        json.loads(GOOGLE_SA_JSON), scopes=['https://www.googleapis.com/auth/spreadsheets'])
    sheets = build('sheets', 'v4', credentials=sa, cache_discovery=False).spreadsheets().values()
    # доки — OAuth реального пользователя (у сервис-аккаунта нет Drive для создания файлов)
    oauth = UserCredentials(
        None, refresh_token=GOOGLE_OAUTH_REFRESH_TOKEN,
        client_id=GOOGLE_OAUTH_CLIENT_ID, client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
        token_uri='https://oauth2.googleapis.com/token',
        scopes=['https://www.googleapis.com/auth/documents', 'https://www.googleapis.com/auth/drive.file'])
    docs = build('docs', 'v1', credentials=oauth, cache_discovery=False)
    drive = build('drive', 'v3', credentials=oauth, cache_discovery=False)
    return sheets, docs, drive


def read_tab(values, tab, header_row=1):
    resp = values.get(spreadsheetId=MARKETING_SHEET_ID,
                      range=f"'{tab}'!{header_row}:100000").execute()
    data = resp.get('values', [])
    if not data:
        return [], []
    headers = data[0]
    rows = [{headers[i]: (r[i] if i < len(r) else '') for i in range(len(headers))} for r in data[1:]]
    return headers, rows


def create_doc(docs, drive, title, text):
    meta = drive.files().create(
        body={'name': title, 'mimeType': 'application/vnd.google-apps.document', 'parents': [DOCS_FOLDER_ID]},
        fields='id', supportsAllDrives=True).execute()
    doc_id = meta['id']
    if text:
        docs.documents().batchUpdate(
            documentId=doc_id,
            body={'requests': [{'insertText': {'location': {'index': 1}, 'text': text}}]}).execute()
    return doc_id


# ============================================================
#  Основная логика
# ============================================================

def main():
    missing = [n for n, v in [('APIFY_TOKEN', APIFY_TOKEN), ('DEEPGRAM_TOKEN', DEEPGRAM_TOKEN),
                              ('GOOGLE_SERVICE_ACCOUNT_JSON', GOOGLE_SA_JSON),
                              ('GOOGLE_OAUTH_CLIENT_ID', GOOGLE_OAUTH_CLIENT_ID),
                              ('GOOGLE_OAUTH_CLIENT_SECRET', GOOGLE_OAUTH_CLIENT_SECRET),
                              ('GOOGLE_OAUTH_REFRESH_TOKEN', GOOGLE_OAUTH_REFRESH_TOKEN)] if not v]
    if missing:
        print("ОШИБКА: нет переменных окружения: " + ", ".join(missing))
        sys.exit(1)

    sheets, docs, drive = google_clients()

    # --- читаем ZOOM и дпд ---
    _, zoom_rows = read_tab(sheets, ZOOM_TAB, ZOOM_HEADER_ROW)
    dpd_resp = sheets.get(spreadsheetId=MARKETING_SHEET_ID, range=f"'{DPD_TAB}'!1:100000").execute()
    dpd_data = dpd_resp.get('values', [])
    dpd_headers = dpd_data[0] if dpd_data else ['amo_link', 'ID', 'first_name', 'last_name',
                                                'company', 'zoom_share_url', 'zoom_passcode', 'transcribation']
    id_col = dpd_headers.index('ID') if 'ID' in dpd_headers else -1
    tr_col = dpd_headers.index('transcribation') if 'transcribation' in dpd_headers else -1

    id_to_row, done = {}, set()
    for i, raw in enumerate(dpd_data[1:]):
        rownum = i + 2
        rid = str(raw[id_col]).strip() if 0 <= id_col < len(raw) else ''
        tr = str(raw[tr_col]).strip() if 0 <= tr_col < len(raw) else ''
        if rid:
            id_to_row[rid] = rownum
        if rid and tr:
            done.add(rid)

    # --- собираем очередь ---
    pending = []
    for zr in zoom_rows:
        rid = str(zr.get('ID') or '').strip()
        parsed = parse_row(zr)
        if not parsed:
            continue
        if rid and rid in done:
            continue
        pending.append({
            'ID': rid, 'amoTag': rid,
            'first_name': zr.get('Основной контакт') or '',
            'share_url': parsed['share_url'], 'passcode': parsed['passcode'],
            'zoom_share_url': parsed['share_url'], 'zoom_passcode': parsed['passcode'],
            '_diag': diag_sort_key(zr.get('Дата Диагностика проведена')),
        })

    # новейшие сначала, затем ограничиваем размер прогона
    pending.sort(key=lambda x: x['_diag'], reverse=True)
    total_pending = len(pending)
    deferred = 0
    if total_pending > MAX_PER_RUN:
        deferred = total_pending - MAX_PER_RUN
        pending = pending[:MAX_PER_RUN]

    print(f"Строк ZOOM: {len(zoom_rows)}, уже готово: {len(done)}, в очереди: {total_pending}, "
          f"беру за прогон: {len(pending)}" + (f", отложено: {deferred}" if deferred else ""))
    if not pending:
        return {'ok': 0, 'fail': 0, 'total': 0}

    def upsert(mapping):
        rid = str(mapping['ID']).strip()
        if rid in id_to_row:
            rownum = id_to_row[rid]
            data_updates = [{'range': f"'{DPD_TAB}'!{col_letter(dpd_headers.index(h))}{rownum}",
                             'values': [[safe_cell(v)]]}
                            for h, v in mapping.items() if h in dpd_headers]
            sheets.batchUpdate(spreadsheetId=MARKETING_SHEET_ID,
                               body={'valueInputOption': 'USER_ENTERED', 'data': data_updates}).execute()
        else:
            rowarr = [''] * len(dpd_headers)
            for h, v in mapping.items():
                if h in dpd_headers:
                    rowarr[dpd_headers.index(h)] = safe_cell(v)
            resp = sheets.append(spreadsheetId=MARKETING_SHEET_ID, range=f"'{DPD_TAB}'!A1",
                                 valueInputOption='USER_ENTERED', insertDataOption='INSERT_ROWS',
                                 body={'values': [rowarr]}).execute()
            try:
                rng = resp['updates']['updatedRange'].split('!')[1]
                id_to_row[rid] = int(re.search(r'(\d+)', rng).group(1))
            except Exception:
                pass

    ok = fail = 0
    for row in pending:
        rid = row['ID']
        audio_path = None
        try:
            print(f"[{rid}] запуск Apify...")
            run_id = apify_start(row['share_url'], row['passcode'])
            status = apify_wait(run_id)
            if status != 'SUCCEEDED':
                print(f"[{rid}] Apify статус {status} — пропуск")
                fail += 1
                continue
            item = apify_result(run_id)
            audio_url = item.get('audio_url')
            if not audio_url:
                print(f"[{rid}] нет audio_url — пропуск")
                fail += 1
                continue
            print(f"[{rid}] скачиваю аудио...")
            audio_path = download_audio(audio_url, item.get('finalUrl'), item.get('cookieHeader'), row['amoTag'])
            print(f"[{rid}] Deepgram...")
            transcript = deepgram_transcribe(audio_path)
            print(f"[{rid}] создаю Google Doc...")
            doc_id = create_doc(docs, drive, row['amoTag'], transcript)
            doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
            upsert({'ID': row['ID'], 'first_name': row['first_name'],
                    'zoom_share_url': row['zoom_share_url'], 'zoom_passcode': row['zoom_passcode'],
                    'transcribation': doc_url})
            print(f"[{rid}] готово: {doc_url}")
            ok += 1
        except Exception as e:
            print(f"[{rid}] ОШИБКА: {type(e).__name__}: {str(e)[:200]}")
            fail += 1
        finally:
            if audio_path and os.path.exists(audio_path):
                try:
                    os.remove(audio_path)
                except Exception:
                    pass

    print(f"ГОТОВО. Обработано: {ok}, ошибок: {fail} (за прогон {len(pending)}, "
          f"в очереди {total_pending}, отложено {deferred})")
    return {'ok': ok, 'fail': fail, 'total': len(pending), 'queue': total_pending, 'deferred': deferred}


if __name__ == '__main__':
    try:
        s = main()
        if s['total'] > 0:
            tail = f"\nОсталось на следующий прогон: {s['deferred']}" if s.get('deferred') else ""
            send_telegram(
                "✅ Транскрибация зумов\n"
                f"Обработано: {s['ok']}, ошибок: {s['fail']}\n"
                f"Всего было в очереди: {s.get('queue', s['total'])}{tail}"
                + run_url_line()
            )
    except Exception as e:
        send_telegram(
            "❌ Транскрибация зумов: ПРОГОН УПАЛ\n"
            f"Ошибка: {type(e).__name__}: {str(e)[:300]}"
            + run_url_line()
        )
        raise
