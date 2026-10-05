#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Проба для расчёта телефонии по отделам: печатает, как выглядят сырые данные
BMI (/stats/calls/export) и OnlinePBX (mongo_history/search) за пару дней.
Номера маскируются (видны только последние 4 цифры). Никуда не пишет.

PROBE_FROM / PROBE_TO — даты YYYY-MM-DD (по умолчанию 1–2 сентября 2026).
"""

import io
import os
import re
import csv
import json
from collections import Counter
from datetime import datetime, time, timedelta

import requests
import openpyxl

import bmi_export as bmi
import pbx_export as pbx

D_FROM = os.environ.get("PROBE_FROM", "2026-09-01").strip()
D_TO = os.environ.get("PROBE_TO", "2026-09-02").strip()


def mask(v):
    s = "" if v is None else str(v)
    return re.sub(r"\d(?=\d{4})", "•", s)


def probe_bmi():
    print(f"\n===== BMI /stats/calls/export {D_FROM}..{D_TO}")
    j = bmi.bmi_get("/stats/calls/export", {"date_from": D_FROM, "date_to": D_TO})
    print("ответ API:", {k: v for k, v in j.items() if k != "file_url"})
    url = j.get("file_url")
    if not url:
        print("файла нет")
        return
    data = requests.get(url, timeout=300).content
    fmt = (j.get("format") or "xlsx").lower()
    if fmt == "csv":
        rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig", errors="replace")), delimiter=";"))
    else:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        rows = [list(r) for r in wb.active.iter_rows(values_only=True)]
        wb.close()
    header, body = rows[0], rows[1:]
    print("колонки:", header)
    print(f"строк: {len(body)}")
    idx = {h: i for i, h in enumerate(header)}
    dir_col = idx.get(bmi.COL_DIRECTION)
    if dir_col is not None:
        print("направления:", Counter(str(r[dir_col]) for r in body))
    for r in body[:6]:
        print("  ", [mask(c) for c in r])
    # какие колонки похожи на номера/время/цену
    for name in header:
        i = idx[name]
        sample = [r[i] for r in body[:50] if r[i] not in (None, "")]
        if sample:
            print(f"  {name!r}: пример {mask(sample[0])!r}, тип {type(sample[0]).__name__}")


def probe_pbx():
    print(f"\n===== OnlinePBX mongo_history/search {D_FROM}..{D_TO}")
    key = pbx.pbx_auth()
    d0 = datetime.strptime(D_FROM, "%Y-%m-%d").date()
    d1 = datetime.strptime(D_TO, "%Y-%m-%d").date()
    c_from = int(datetime.combine(d0, time(0, 0, 1), pbx.MSK).timestamp())
    c_to = int(datetime.combine(d1, time(23, 59, 59), pbx.MSK).timestamp())
    calls = pbx.fetch_calls(key, c_from, c_to)
    print(f"звонков: {len(calls)}")
    if not calls:
        return
    print("ключи записи:", sorted(calls[0].keys()))
    print("типы (accountcode):", Counter(c.get("accountcode") for c in calls))
    print("шлюзы (gateway):", Counter(mask(c.get("gateway")) for c in calls).most_common(10))
    print("caller_id_name исходящих:", Counter(c.get("caller_id_name") for c in calls if c.get("accountcode") == "outbound").most_common(15))
    print("caller_id_number исходящих:", Counter(mask(c.get("caller_id_number")) for c in calls if c.get("accountcode") == "outbound").most_common(10))
    out = [c for c in calls if c.get("accountcode") == "outbound"][:6]
    for c in out:
        slim = {k: (mask(v) if isinstance(v, (str, int)) and re.search(r"\d{5,}", str(v)) else v)
                for k, v in c.items() if k in ("accountcode", "caller_id_name", "caller_id_number", "destination_number",
                                               "gateway", "start_stamp", "duration", "user_talk_time", "hangup_cause",
                                               "billsec", "end_stamp", "answer_stamp", "uuid", "direction")}
        if c.get("start_stamp"):
            slim["start_msk"] = pbx.format_ts(c["start_stamp"])
        print("  ", json.dumps(slim, ensure_ascii=False))


def main():
    probe_bmi()
    probe_pbx()


if __name__ == "__main__":
    main()
