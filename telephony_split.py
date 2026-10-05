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
  3. Абонентка = акт BMI − сумма платных звонков; делится 50/50 между 1-й и 2-й линией
     (решение Никиты, 05.10.2026). Звонки без отдела и не найденные в АТС тоже 50/50.
  4. Итог по отделам и доли, детализация по менеджерам → лист «Телефония расход» от O21.

Запуск: MONTHS=2026-08,2026-09 python telephony_split.py   (по умолчанию — прошлый месяц)
DRY_RUN=1 — только лог.
"""

import os
import io
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


def read_staff(sheet):
    """→ {ext: (имя, отдел|None)} из A:G листа «Телефония расход»."""
    staff = {}
    for row in sheet.get(STAFF_RANGE):
        row = row + [""] * (7 - len(row))
        ext = digits(row[3])
        if not ext:
            continue
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


def compute(y, m, staff):
    first, last = month_bounds(y, m)
    label = f"{MONTHS_RU[m - 1]} {y}"
    print(f"\n===== {label}")
    bmi_calls = fetch_bmi_calls(first, last)
    pbx_calls = fetch_pbx_calls(first, last)
    acts = bmi.fetch_acts(first, last)
    act_total = acts.get(f"{y}-{m:02d}")
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

    # по отделам
    per_dept = {d: {"calls": 0, "sec": 0, "paid": 0.0} for d in DEPTS + ("без отдела", "не сопоставлено")}
    ext_rows = []
    for ext, a in sorted(per_ext.items(), key=lambda kv: -kv[1]["paid"]):
        if ext == "не сопоставлено":
            name, dept = "—", "не сопоставлено"
        else:
            name, dept = staff.get(ext, ("?", None))
            dept = dept or "без отдела"
        per_dept[dept]["calls"] += a["calls"]
        per_dept[dept]["sec"] += a["sec"]
        per_dept[dept]["paid"] += a["paid"]
        ext_rows.append([label, ext, name, dept, a["calls"], round(a["sec"] / 60), a["paid_calls"], round(a["paid"], 2)])

    paid_total = sum(b["price"] for b in bmi_calls)
    subscription = (act_total - paid_total) if act_total is not None else None
    # всё, что не привязано к отделу (абонентка, без отдела, не сопоставлено) — 50/50 между линиями
    pool = (subscription or 0.0) + per_dept["без отдела"]["paid"] + per_dept["не сопоставлено"]["paid"]
    totals = {}
    for d in DEPTS:
        share_pool = pool / 2 if d in LINES else 0.0
        totals[d] = per_dept[d]["paid"] + share_pool
    grand = sum(totals.values())

    dept_rows = []
    for d in DEPTS + ("без отдела", "не сопоставлено"):
        p = per_dept[d]
        pool_part = (pool / 2) if d in LINES else ""
        total = totals.get(d, "")
        share = (total / grand) if (d in DEPTS and grand) else ""
        dept_rows.append([label, d, p["calls"], round(p["sec"] / 60), round(p["paid"], 2), pool_part, total, share])
    dept_rows.append([label, "итого", len(bmi_calls), round(sum(b["sec"] for b in bmi_calls) / 60),
                      round(paid_total, 2), subscription if subscription is not None else "нет акта",
                      round(grand, 2), 1 if grand else ""])

    print(f"платные звонки {paid_total:,.0f} ₽, абонентка {subscription if subscription is not None else '?'}, "
          f"итого {grand:,.0f} ₽".replace(",", " "))
    for r in dept_rows:
        print("  ", r[1:], )
    return {"label": label, "dept_rows": dept_rows, "ext_rows": ext_rows,
            "act": act_total, "paid": paid_total, "subscription": subscription,
            "matched": matched, "unmatched": unmatched, "n_bmi": len(bmi_calls), "n_pbx": len(pbx_calls)}


# ============================================================
#  ВЫВОД НА ЛИСТ
# ============================================================

def build_matrix(results):
    stamp = dt.datetime.now(MSK).strftime("%d.%m.%Y %H:%M")
    rows = [[f"Телефония по факту: BMI × АТС (обновлено {stamp}). Абонентка = акт BMI − платные звонки, "
             f"делится 50/50 между линиями; звонки без отдела и не найденные в АТС — тоже 50/50."]]
    rows.append(["Месяц", "Отдел", "Звонков", "Минут", "Платные звонки ₽", "Доля абонентки и прочего ₽",
                 "Итого ₽", "Доля", "", "Акт BMI ₽", "Сопоставлено / не найдено"])
    for res in results:
        first = True
        for r in res["dept_rows"]:
            extra = [""] * 3
            if first:
                extra = ["", res["act"] if res["act"] is not None else "нет акта", f"{res['matched']} / {res['unmatched']}"]
                first = False
            rows.append(r + extra)
        rows.append([])
    rows.append(["По менеджерам"])
    rows.append(["Месяц", "Номер", "Менеджер", "Отдел", "Звонков", "Минут", "Платных звонков", "Платные ₽"])
    for res in results:
        rows.extend(res["ext_rows"])
        rows.append([])
    return rows


def main():
    months = months_from_env()
    sheet = Sheet()
    staff = read_staff(sheet)
    missing = [e for e, (n, d) in staff.items() if not d]
    print(f"Сотрудников в списке: {len(staff)}, без отдела: {missing}")
    results = [compute(y, m, staff) for y, m in months]
    matrix = build_matrix(results)
    if DRY_RUN:
        print("\n--- DRY_RUN: на лист не пишу ---")
        for r in matrix[:40]:
            print(r)
        return
    sheet.clear(OUT_CLEAR)
    sheet.write(f"{OUT_COL_FROM}{OUT_ANCHOR_ROW}", matrix)
    print(f"\nЗаписано на «{TEL_SHEET}» от {OUT_COL_FROM}{OUT_ANCHOR_ROW}: {len(matrix)} строк.")


if __name__ == "__main__":
    main()
