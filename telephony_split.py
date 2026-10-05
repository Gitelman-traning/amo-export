#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Телефония по факту: кто из отделов сколько потратил у BMI за месяц.

Источники:
  • BMI /stats/calls/export — каждый исходящий звонок: время (UTC), B-номер, длительность, цена.
    Цена пустая = звонок внутри пакета; цена есть = сверх пакета / за рубеж.
  • BMI /documents?type=act — сумма акта за месяц (всё начисление: связь + абонентка).
  • OnlinePBX mongo_history — каждый звонок: внутренний номер менеджера, номер назначения,
    время (МСК). Через BMI идут звонки со шлюзом «Carousel».
  • Лист «Телефония расход» A:G — внутренний номер → имя → отдел (колонка F, правит Никита).

Как считаем:
  1. Звонок BMI ищем в АТС по номеру назначения (последние 10 цифр) и времени (±MATCH_TOLERANCE с).
  2. Платные звонки (цена из BMI) относим на менеджера → отдел.
  3. Абонентка = акт BMI − сумма платных звонков; делится по доле ЗВОНКОВ отдела
     (решение Никиты, 05.10.2026: 60 % звонков у 1-й линии → 60 % абонентки ей).
     Не найденные в АТС звонки делятся так же; номер без отдела в списке → прочее.
  4. Итог по отделам и доли, детализация по менеджерам → лист «Телефония расход» от O21.

Запуск: MONTHS=2026-08,2026-09 python telephony_split.py   (по умолчанию — прошлый месяц)
DRY_RUN=1 — только лог.
"""

import os
import io
import math
import csv
import json
import re
import datetime as dt
from collections import defaultdict
from datetime import datetime, date, time, timedelta, timezone

import requests
import openpyxl
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

import bmi_export as bmi
import pbx_export as pbx

# ============================================================
#  НАСТРОЙКИ
# ============================================================

SPREADSHEET_ID = os.environ.get("SERVICES_SPREADSHEET_ID", "").strip() or "1nbeuUG1IPAMcj-uBmI7grCzHTSdlKTy4BD5sCuxIGyw"
TEL_SHEET = "Телефония расход"
STAFF_RANGE = "A2:G60"            # №, Имя, Фамилия, Номер, Телефония, Отдел, Роль
OUT_ANCHOR_ROW = 21               # пишем от O21 (заголовок) / O22 (данные) вправо и вниз
OUT_COL_FROM, OUT_COL_TO = "O", "Y"
OUT_CLEAR = f"{OUT_COL_FROM}{OUT_ANCHOR_ROW}:{OUT_COL_TO}400"

MATCH_TOLERANCE = 240             # секунд между временем BMI и АТС
BANK_FEE_CELL = "L31"             # комиссия банка (например «5%») — ложится в прочее от итога по отделам
BANK_FEE_DEFAULT = 0.05
BMI_GATEWAY_HINT = "carousel"     # шлюз АТС, через который идут звонки BMI (для статистики)
LINES = ("1-линия", "2-линия")
DEPTS = ("1-линия", "2-линия", "прочее")

GOOGLE_SA_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")
MSK = pbx.MSK

MONTHS_RU = ["январь", "февраль", "март", "апрель", "май", "июнь",
             "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]


# ============================================================
#  УТИЛИТЫ
# ============================================================

def digits(v):
    return re.sub(r"\D", "", str(v or ""))


def key10(v):
    d = digits(v)
    return d[-10:] if len(d) >= 10 else d


def norm_dept(raw):
    s = str(raw or "").strip().lower().replace("—", "-").replace("–", "-")
    if s.startswith("1"):
        return "1-линия"
    if s.startswith("2"):
        return "2-линия"
    if s.startswith("проч"):
        return "прочее"
    return None


def months_from_env():
    env = os.environ.get("MONTHS", "").strip()
    if env:
        out = []
        for part in env.split(","):
            y, m = part.strip().split("-")
            out.append((int(y), int(m)))
        return out
    pay = os.environ.get("PAY_MONTH", "").strip()
    if pay:                                   # запуск из заявки: два месяца перед месяцем оплаты
        y, m = (int(x) for x in pay.split("-"))
        out = []
        for back in (2, 1):
            mm, yy = m - back, y
            while mm <= 0:
                mm, yy = mm + 12, yy - 1
            out.append((yy, mm))
        return out
    today = dt.datetime.now(MSK).date()
    first = today.replace(day=1)
    prev = first - timedelta(days=1)
    return [(prev.year, prev.month)]


def month_bounds(y, m):
    first = date(y, m, 1)
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    last = date(ny, nm, 1) - timedelta(days=1)
    return first, last


# ============================================================
#  ИСТОЧНИКИ
# ============================================================

def fetch_bmi_calls(first, last):
    """Исходящие звонки BMI за период: [{ts_msk, b, country, sec, price, tariff, answered}]."""
    j = bmi.bmi_get("/stats/calls/export", {"date_from": first.isoformat(), "date_to": last.isoformat()})
    url, count = j.get("file_url"), j.get("count") or 0
    if j.get("truncated"):
        print("  ⚠ BMI: экспорт обрезан (truncated=True) — часть звонков не попала")
    if not url or not count:
        return []
    data = requests.get(url, timeout=300).content
    if (j.get("format") or "xlsx").lower() == "csv":
        rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig", errors="replace")), delimiter=";"))
    else:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        rows = [list(r) for r in wb.active.iter_rows(values_only=True)]
        wb.close()
    header, body = rows[0], rows[1:]
    idx = {str(h).strip(): i for i, h in enumerate(header)}

    def cell(row, name):
        i = idx.get(name)
        return row[i] if i is not None and i < len(row) else None

    out = []
    for r in body:
        if (cell(r, "Направление") or "") != "Исходящий":
            continue
        raw_dt = cell(r, "Дата и время")
        if isinstance(raw_dt, datetime):
            t_utc = raw_dt.replace(tzinfo=timezone.utc)
        else:
            t_utc = datetime.strptime(str(raw_dt)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        out.append({
            "ts": int(t_utc.timestamp()),
            "b": key10(cell(r, "B-номер")),
            "b_full": digits(cell(r, "B-номер")),
            "country": cell(r, "Страна (куда)") or "—",
            "sec": int(bmi.num(cell(r, "Длительность, сек"))),
            "price": float(bmi.num(cell(r, "Цена"))),
            "tariff": cell(r, "Тариф") or "",
            "answered": (cell(r, "Ответ") or "") == "Да",
        })
    return out


def fetch_pbx_calls(first, last):
    """Исходящие звонки АТС за период: [{ts, ext, dst, gateway, dur, talk}]."""
    key = pbx.pbx_auth()
    out = []
    cur = first
    while cur <= last:
        c_end = min(cur + timedelta(days=pbx.CHUNK_DAYS - 1), last)
        c_from = int(datetime.combine(cur, time(0, 0, 1), MSK).timestamp())
        c_to = int(datetime.combine(c_end, time(23, 59, 59), MSK).timestamp())
        for c in pbx.fetch_calls(key, c_from, c_to):
            if c.get("accountcode") != "outbound":
                continue
            out.append({
                "ts": int(c.get("start_stamp") or 0),
                "ext": str(c.get("caller_id_name") or c.get("caller_id_number") or "").strip(),
                "dst": key10(c.get("destination_number")),
                "gateway": str(c.get("gateway") or ""),
                "dur": int(c.get("duration") or 0),
                "talk": int(c.get("user_talk_time") or 0),
                "used": False,
            })
        cur = c_end + timedelta(days=1)
    return out


# ============================================================
#  GOOGLE SHEETS
# ============================================================

class Sheet:
    def __init__(self):
        creds = Credentials.from_service_account_info(
            json.loads(GOOGLE_SA_JSON), scopes=["https://www.googleapis.com/auth/spreadsheets"])
        self.svc = build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()

    def get(self, rng):
        return self.svc.values().get(spreadsheetId=SPREADSHEET_ID, range=f"'{TEL_SHEET}'!{rng}").execute().get("values", [])

    def clear(self, rng):
        self.svc.values().clear(spreadsheetId=SPREADSHEET_ID, range=f"'{TEL_SHEET}'!{rng}").execute()

    def write(self, rng, values):
        self.svc.values().update(spreadsheetId=SPREADSHEET_ID, range=f"'{TEL_SHEET}'!{rng}",
                                 valueInputOption="USER_ENTERED", body={"values": values}).execute()

    def tables(self):
        """Таблицы Google (формат «Преобразовать в таблицу») на листе: [{tableId, name, range}]."""
        meta = self.svc.get(spreadsheetId=SPREADSHEET_ID,
                            fields="sheets(properties(title,sheetId),tables(tableId,name,range))").execute()
        for sh in meta.get("sheets", []):
            if sh["properties"]["title"] == TEL_SHEET:
                self.sheet_id = sh["properties"]["sheetId"]
                return sh.get("tables", []) or []
        return []

    def resize_table(self, table, n_rows, n_cols):
        """Подгоняет границы таблицы: n_rows строк данных (без шапки), n_cols колонок."""
        r = table["range"]
        new = {"sheetId": r["sheetId"], "startRowIndex": r["startRowIndex"], "endRowIndex": r["startRowIndex"] + 1 + n_rows,
               "startColumnIndex": r["startColumnIndex"], "endColumnIndex": r["startColumnIndex"] + n_cols}
        if new != r:
            self.svc.batchUpdate(spreadsheetId=SPREADSHEET_ID, body={"requests": [
                {"updateTable": {"table": {"tableId": table["tableId"], "range": new}, "fields": "range"}}]}).execute()
        return new


def read_bank_fee(sheet):
    """Комиссия банка из ячейки BANK_FEE_CELL: «5%» → 0.05, «0,05» → 0.05; пусто → BANK_FEE_DEFAULT."""
    try:
        raw = (sheet.get(f"{BANK_FEE_CELL}:{BANK_FEE_CELL}") or [[""]])[0][0]
    except IndexError:
        raw = ""
    txt = str(raw).strip().replace(",", ".").replace(" ", "")
    if not txt:
        return BANK_FEE_DEFAULT
    try:
        v = float(txt.rstrip("%"))
    except ValueError:
        return BANK_FEE_DEFAULT
    return v / 100 if ("%" in txt or v >= 1) else v


def read_staff(sheet):
    """→ {ext: (имя, отдел|None)} из A:G листа «Телефония расход»."""
    staff = {}
    for row in sheet.get(STAFF_RANGE):
        row = row + [""] * (7 - len(row))
        ext = digits(row[3])
        if len(ext) != 3:
            continue                                # внутренние номера трёхзначные; пустые строки и
                                                    # таблицы ниже (там в колонке D мелкие числа) пропускаем
        name = " ".join(x for x in (row[1].strip(), row[2].strip()) if x)
        staff[ext] = (name, norm_dept(row[5]))
    return staff


# ============================================================
#  РАСЧЁТ
# ============================================================

def match(bmi_calls, pbx_calls):
    """Каждому звонку BMI подбираем ближайший по времени звонок АТС на тот же номер."""
    by_dst = defaultdict(list)
    for c in pbx_calls:
        by_dst[c["dst"]].append(c)
    for lst in by_dst.values():
        lst.sort(key=lambda c: c["ts"])
    matched, unmatched = 0, 0
    for b in bmi_calls:
        best, best_d = None, None
        for c in by_dst.get(b["b"], []):
            if c["used"]:
                continue
            d = abs(c["ts"] - b["ts"])
            if d <= MATCH_TOLERANCE and (best is None or d < best_d):
                best, best_d = c, d
        if best:
            best["used"] = True
            b["ext"] = best["ext"]
            b["gateway"] = best["gateway"]
            matched += 1
        else:
            b["ext"] = None
            unmatched += 1
    return matched, unmatched


def compute(y, m, staff, bank_fee):
    first, last = month_bounds(y, m)
    label = f"{MONTHS_RU[m - 1]} {y}"
    yesterday = dt.datetime.now(MSK).date() - timedelta(days=1)
    scale, partial = 1.0, ""
    if last > yesterday:                      # месяц не закончился: берём по вчера и дотягиваем по дням
        days_total, days_have = last.day, max((yesterday - first).days + 1, 1)
        last = min(last, yesterday)
        scale = days_total / days_have
        partial = f" по {yesterday.strftime('%d.%m')}, ×{scale:.2f}"
        label = f"{label}{partial}"
    print(f"\n===== {label}")
    bmi_calls = fetch_bmi_calls(first, last)
    pbx_calls = fetch_pbx_calls(first, last)
    acts = bmi.fetch_acts(first, last)
    act_total = acts.get(f"{y}-{m:02d}")
    act_note = "акт"
    if act_total is None:
        # акта ещё нет (месяц не закрыт у BMI) — берём абонентку по текущему тарифу как оценку
        _, tariff_total, _ = bmi.fetch_tariff_composition()
        act_note = f"нет акта, абонентка по тарифу {tariff_total:,.0f} ₽ (оценка)".replace(",", " ")
        est_subscription = tariff_total
    else:
        est_subscription = None
    print(f"BMI исходящих: {len(bmi_calls)}, АТС исходящих: {len(pbx_calls)} "
          f"(через {BMI_GATEWAY_HINT}: {sum(1 for c in pbx_calls if BMI_GATEWAY_HINT in c['gateway'].lower())}), "
          f"акт BMI: {act_total}")
    matched, unmatched = match(bmi_calls, pbx_calls)
    print(f"сопоставлено {matched}, не найдено в АТС {unmatched}")

    # по менеджерам
    per_ext = defaultdict(lambda: {"calls": 0, "sec": 0, "paid": 0.0, "paid_calls": 0})
    for b in bmi_calls:
        ext = b["ext"] or "не сопоставлено"
        a = per_ext[ext]
        a["calls"] += 1
        a["sec"] += b["sec"]
        a["paid"] += b["price"]
        if b["price"]:
            a["paid_calls"] += 1

    # по отделам: номер без отдела в списке → прочее (решение Никиты, 05.10)
    per_dept = {d: {"calls": 0, "sec": 0, "paid": 0.0} for d in DEPTS}
    unmatched_paid, unmatched_calls = 0.0, 0
    ext_rows = []
    for ext, a in sorted(per_ext.items(), key=lambda kv: -kv[1]["paid"]):
        if ext == "не сопоставлено":
            name, dept = "—", "не сопоставлено"
            unmatched_paid += a["paid"]
            unmatched_calls += a["calls"]
        else:
            name, dept = staff.get(ext, ("?", None))
            dept = dept or "прочее"
            per_dept[dept]["calls"] += a["calls"]
            per_dept[dept]["sec"] += a["sec"]
            per_dept[dept]["paid"] += a["paid"]
        ext_rows.append([label, ext, name, dept, a["calls"], round(a["sec"] / 60), a["paid_calls"], round(a["paid"], 2)])

    if scale != 1.0:                          # экстраполяция неполного месяца
        for b in bmi_calls:
            b["price"] *= scale
        for a in per_ext.values():
            a["paid"] *= scale
        for d in per_dept.values():
            d["paid"] *= scale
        unmatched_paid *= scale
    paid_total = sum(b["price"] for b in bmi_calls)
    subscription = (act_total - paid_total) if act_total is not None else est_subscription
    # абонентка и не сопоставленные звонки делятся по доле ЗВОНКОВ отдела (решение Никиты, 05.10):
    # 60 % звонков у 1-й линии → 60 % абонентки ей
    pool = (subscription or 0.0) + unmatched_paid
    calls_known = sum(p["calls"] for p in per_dept.values()) or 1
    call_share = {d: per_dept[d]["calls"] / calls_known for d in DEPTS}
    totals = {d: per_dept[d]["paid"] + pool * call_share[d] for d in DEPTS}
    commission = sum(totals.values()) * bank_fee            # комиссия банка — в прочее
    totals["прочее"] += commission
    grand = sum(totals.values())

    row = {"label": f"{label} факт", "paid": round(paid_total),
           "subscription": round(subscription) if subscription is not None else "",
           "total": round(grand), "commission": round(commission), "bank_fee": bank_fee,
           "topup": round(grand - commission)}
    for d in DEPTS:
        row[d] = round(totals[d])
        row[d + "_pct"] = round(100 * totals[d] / grand, 1) if grand else ""
        row[d + "_calls_pct"] = round(100 * call_share[d], 1)
    row["note"] = (f"{act_note}; сопоставлено {matched}, не найдено {unmatched} ({unmatched_paid:,.0f} ₽)"
                   .replace(",", " "))

    print(f"платные звонки {paid_total:,.0f} ₽, абонентка {subscription if subscription is not None else '?'}, "
          f"итого {grand:,.0f} ₽".replace(",", " "))
    print("   доля звонков:", {d: f"{100 * call_share[d]:.1f}%" for d in DEPTS})
    print(f"   комиссия банка {bank_fee:.1%}: {commission:,.0f} ₽ → прочее".replace(",", " "))
    print("   итог по отделам:", {d: (row[d], row[d + "_pct"]) for d in DEPTS})
    return {"label": label, "row": row, "ext_rows": ext_rows, "y": y, "m": m}


def ceil_1000(v):
    return int(math.ceil(float(v) / 1000.0) * 1000) if v else 0


def forecast_row(fact, y, m):
    """Прогноз на следующий месяц. Итог К ОПЛАТЕ (с комиссией банка) округляется вверх до 1 000 ₽,
    пополнение BMI = итог / (1 + комиссия), комиссия — в прочее. Пополнение делится между
    отделами по долям факта (платные звонки + абонентка, без комиссии)."""
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    fee = fact.get("bank_fee", BANK_FEE_DEFAULT)
    base = {d: fact[d] for d in DEPTS}
    base["прочее"] -= fact.get("commission", 0)                 # прочее факта без комиссии
    base_sum = sum(base.values()) or 1.0
    total = ceil_1000(fact["total"])                            # к оплате, круглая сумма с комиссией
    topup = round(total / (1 + fee))                            # что вводить в пополнение BMI
    commission = total - topup
    row = {"label": f"{MONTHS_RU[nm - 1]} {ny} прогноз", "paid": fact["paid"], "subscription": fact["subscription"],
           "total": total, "topup": topup, "commission": commission}
    parts = {d: round(topup * base[d] / base_sum) for d in LINES}
    parts["прочее"] = topup - sum(parts.values())               # остаток — чтобы сумма сошлась
    for d in DEPTS:
        row[d] = parts[d] + (commission if d == "прочее" else 0)
        row[d + "_pct"] = round(100 * row[d] / total, 1) if total else ""
        row[d + "_calls_pct"] = fact[d + "_calls_pct"]
    return row


# ============================================================
#  ВЫВОД НА ЛИСТ
# ============================================================

SUMMARY_COLS = 12
TOPUP_HEADER = "Пополнение BMI ₽"
MANAGER_COLS = 8


def summary_rows(results):
    """Строки сводки: факт по каждому месяцу + прогноз на месяц после последнего."""
    rows = []
    for res in results:
        r = res["row"]
        rows.append(r)
    last = results[-1]
    rows.append(forecast_row(last["row"], last["y"], last["m"]))
    out = []
    for r in rows:
        out.append([r["label"], r["paid"], r["subscription"], r["total"],
                    r["1-линия"], r["1-линия_pct"], r["2-линия"], r["2-линия_pct"], r["прочее"], r["прочее_pct"],
                    f'{r["1-линия_calls_pct"]} / {r["2-линия_calls_pct"]} / {r["прочее_calls_pct"]}', r["topup"]])
    return out


def manager_rows(results):
    out = []
    for res in results:
        out.extend(res["ext_rows"])
    return out


def title_line():
    stamp = dt.datetime.now(MSK).strftime("%d.%m.%Y %H:%M")
    return (f"Телефония по факту: BMI × АТС (обновлено {stamp}). Платные звонки — по цене из BMI на менеджера → отдел; "
            f"абонентка = акт BMI − платные звонки (без акта — по тарифу) и делится по доле звонков отдела; "
            f"номер без отдела → прочее; комиссия банка ({BANK_FEE_CELL}) от итога — в прочее; "
            f"прогноз: итог к оплате (с комиссией) = факт, округлённый вверх до 1 000 ₽; пополнение BMI = итог / (1 + комиссия).")


def write_sheet(sheet, results):
    """Пишет значения ВНУТРЬ таблиц Google на листе (формат Никиты сохраняется), подгоняя их размер.
    Если таблиц нет — пишет блоками от O21 как раньше."""
    s_rows = summary_rows(results)
    m_rows = manager_rows(results)
    col0 = col_index(OUT_COL_FROM)
    tables = [t for t in sheet.tables() if t["range"].get("startColumnIndex") == col0
              and t["range"].get("startRowIndex", 0) >= OUT_ANCHOR_ROW - 1]
    tables.sort(key=lambda t: t["range"]["startRowIndex"])
    sheet.write(f"{OUT_COL_FROM}{OUT_ANCHOR_ROW}", [[title_line()]])
    if len(tables) >= 2:
        summ, mgr = tables[0], tables[1]
        for table, rows, n_cols, header in ((summ, s_rows, SUMMARY_COLS, None), (mgr, m_rows, MANAGER_COLS, None)):
            r = table["range"]
            old_rows, old_cols = r["endRowIndex"] - r["startRowIndex"] - 1, r["endColumnIndex"] - r["startColumnIndex"]
            first_data = r["startRowIndex"] + 2                      # 1-based номер первой строки данных
            # очищаем старые данные (и лишние колонки), затем пишем новые и подгоняем границы
            sheet.clear(f"{col_letter(col0)}{first_data}:{col_letter(col0 + max(old_cols, n_cols) - 1)}{first_data + max(old_rows, len(rows)) - 1}")
            # хвост справа от таблицы (старая колонка «Примечание» и т.п.) — чистим вместе с шапкой
            sheet.clear(f"{col_letter(col0 + n_cols)}{first_data - 1}:{col_letter(col0 + max(old_cols, n_cols) + 1)}{first_data + max(old_rows, len(rows)) - 1}")
            if n_cols > old_cols and table is summ:              # новая колонка — подписываем шапку
                sheet.write(f"{col_letter(col0 + old_cols)}{first_data - 1}", [[TOPUP_HEADER]])
            sheet.write(f"{col_letter(col0)}{first_data}", rows)
            sheet.resize_table(table, len(rows), n_cols)
            print(f"  таблица «{table.get('name')}»: {len(rows)} строк × {n_cols} колонок (было {old_rows} × {old_cols})")
        # если таблица менеджеров стоит слишком близко к сводке и сводка выросла — не наш случай, строк всегда ≤ 3
        return
    # запасной вариант: без таблиц Google
    rows = [[title_line()],
            ["Месяц", "Платные звонки ₽", "Абонентка ₽", "Итого ₽", "1 линия ₽", "1 линия %", "2 линия ₽", "2 линия %",
             "Прочее ₽", "Прочее %", "Доля звонков 1 / 2 / прочее", TOPUP_HEADER]] + s_rows + [[], ["По менеджерам"],
            ["Месяц", "Номер", "Менеджер", "Отдел", "Звонков", "Минут", "Платных звонков", "Платные ₽"]] + m_rows
    sheet.clear(OUT_CLEAR)
    sheet.write(f"{OUT_COL_FROM}{OUT_ANCHOR_ROW}", rows)
    print(f"  таблиц Google нет — записано блоками: {len(rows)} строк")


def col_index(letters):
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch.upper()) - 64)
    return n - 1


def col_letter(idx):
    out, i = "", idx + 1
    while i:
        i, r = divmod(i - 1, 26)
        out = chr(65 + r) + out
    return out


def main():
    months = months_from_env()
    sheet = Sheet()
    staff = read_staff(sheet)
    missing = [e for e, (n, d) in staff.items() if not d]
    print(f"Сотрудников в списке: {len(staff)}, без отдела: {missing}")
    bank_fee = read_bank_fee(sheet)
    print(f"Комиссия банка ({BANK_FEE_CELL}): {bank_fee:.2%}")
    results = [compute(y, m, staff, bank_fee) for y, m in months]
    print("\nСводка:")
    for r in summary_rows(results):
        print("  ", r)
    if DRY_RUN:
        print("\n--- DRY_RUN: на лист не пишу ---")
        print("таблицы на листе:", sheet.tables())
        return
    write_sheet(sheet, results)
    print(f"\nЗаписано на «{TEL_SHEET}».")


if __name__ == "__main__":
    main()
