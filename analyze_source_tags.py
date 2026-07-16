#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Анализатор источников и тегов сделок amoCRM (сидинг «базы правил»).

Разовый скрипт для ЭТАПА 1 проекта «проверка тегов и источника».
Ничего в amoCRM и в таблицах НЕ меняет — только читает сделки и считает статистику,
чтобы на реальных данных увидеть:
  • какие бывают значения поля «Источник» и как часто;
  • какие бывают теги и как часто;
  • какие теги с какими источниками встречаются вместе (матрица источник × тег);
  • сколько сделок без источника / без тегов / без того и другого.

Результат кладётся в папку ./analysis/*.csv  — по ним собирается черновик правил.

Запуск локально:
    # 1) создать .env (см. .env.example), заполнить AMO_TOKEN
    # 2) при желании: pip install python-dotenv
    python analyze_source_tags.py                # окно по умолчанию — как у выгрузки (3 мес.)
    python analyze_source_tags.py --months 6     # взять более широкое окно
    python analyze_source_tags.py --days 30      # или последние N дней

Клиент amoCRM и логику разбора полей переиспользуем из amo_export.py — чтобы
«Источник» и «Теги» считались ровно так же, как в основной выгрузке.
"""

import os
import sys
import csv
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# --- подхватываем .env до импорта amo_export (он читает AMO_TOKEN на импорте) ---
def _load_env_file():
    """Минимальный ридер .env без внешних зависимостей (python-dotenv не обязателен)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    _load_env_file()  # запасной путь без python-dotenv

from dateutil.relativedelta import relativedelta

# Переиспользуем готовый клиент и разбор полей из основной выгрузки
import amo_export as ax


OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "analysis")


def parse_args():
    p = argparse.ArgumentParser(description="Анализ источников и тегов сделок amoCRM")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--months", type=int, help="окно анализа в месяцах до вчера (по умолчанию как у выгрузки)")
    g.add_argument("--days", type=int, help="окно анализа в днях до вчера")
    return p.parse_args()


def split_tags(tags_str):
    """'a, b' -> ['a','b']; '' -> []. Совпадает с форматом get_tags()."""
    if not tags_str:
        return []
    return [t.strip() for t in str(tags_str).split(",") if t.strip()]


def write_csv(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def main():
    args = parse_args()

    if not ax.AMO_TOKEN:
        print("ОШИБКА: не задан AMO_TOKEN. Заполни .env (см. .env.example) или переменную окружения.")
        sys.exit(1)

    msk = ZoneInfo(ax.TIMEZONE)
    yesterday = datetime.now(msk) - timedelta(days=1)
    date_to_dt = yesterday.replace(hour=23, minute=59, second=59, microsecond=0)
    if args.days:
        date_from_dt = (yesterday - timedelta(days=args.days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
        window = f"последние {args.days} дн."
    else:
        months = args.months or ax.MONTHS_BACK
        date_from_dt = (yesterday - relativedelta(months=months)).replace(hour=0, minute=0, second=0, microsecond=0)
        window = f"последние {months} мес."
    date_from_ts = int(date_from_dt.timestamp())
    date_to_ts = int(date_to_dt.timestamp())

    print(f"Окно анализа: {window}  ({date_from_dt:%d.%m.%Y} — {date_to_dt:%d.%m.%Y}, created_at, МСК)")
    print(f"Воронки: {ax.PIPELINE_IDS}")

    # --- справочники (для читаемых имён воронок) ---
    pipelines_data = ax.amo_get("/api/v4/leads/pipelines")
    pipelines = (pipelines_data.get("_embedded") or {}).get("pipelines") or []
    pipeline_map = {str(p["id"]): (p.get("name") or "") for p in pipelines}

    # --- сделки (без контактов — для анализа они не нужны) ---
    print("Тяну сделки...")
    params = {
        "limit": ax.AMO_PAGE_LIMIT,
        "filter[created_at][from]": date_from_ts,
        "filter[created_at][to]": date_to_ts,
        "order[created_at]": "asc",
    }
    for i, pid in enumerate(ax.PIPELINE_IDS):
        params[f"filter[pipeline_id][{i}]"] = pid
    leads = ax.amo_fetch_all("/api/v4/leads", params, "leads")

    allowed = set(ax.PIPELINE_IDS)
    leads = [l for l in leads
             if date_from_ts <= int(l.get("created_at") or 0) <= date_to_ts
             and int(l.get("pipeline_id") or 0) in allowed]
    print(f"  сделок в анализе: {len(leads)}")

    # --- агрегация ---
    total = len(leads)
    source_counter = Counter()               # источник -> кол-во
    tag_counter = Counter()                  # тег -> кол-во
    src_tag = defaultdict(Counter)           # источник -> Counter(тег -> кол-во)
    tag_src = defaultdict(Counter)           # тег -> Counter(источник -> кол-во)
    src_by_pipeline = defaultdict(Counter)   # воронка -> Counter(источник)

    n_no_source = n_no_tags = n_no_both = 0
    EMPTY = "(пусто)"

    for l in leads:
        src = ax.get_lead_field(l, "Источник")
        src = str(src).strip() if src not in (None, "") else ""
        tags = split_tags(ax.get_tags(l))
        pname = pipeline_map.get(str(l.get("pipeline_id")), str(l.get("pipeline_id")))

        skey = src if src else EMPTY
        source_counter[skey] += 1
        src_by_pipeline[pname][skey] += 1

        if not src:
            n_no_source += 1
        if not tags:
            n_no_tags += 1
        if not src and not tags:
            n_no_both += 1

        for t in tags:
            tag_counter[t] += 1
            src_tag[skey][t] += 1
            tag_src[t][skey] += 1

    # --- запись результатов ---
    os.makedirs(OUT_DIR, exist_ok=True)

    # 1) частота источников
    write_csv(
        os.path.join(OUT_DIR, "source_frequency.csv"),
        ["Источник", "Кол-во сделок", "Доля %"],
        [[s, c, f"{(c / total * 100):.1f}" if total else "0"]
         for s, c in source_counter.most_common()],
    )

    # 2) частота тегов
    write_csv(
        os.path.join(OUT_DIR, "tag_frequency.csv"),
        ["Тег", "Кол-во сделок", "Доля %"],
        [[t, c, f"{(c / total * 100):.1f}" if total else "0"]
         for t, c in tag_counter.most_common()],
    )

    # 3) матрица источник × тег (какие теги встречаются при источнике)
    matrix_rows = []
    for s, _ in source_counter.most_common():
        s_total = source_counter[s]
        if src_tag[s]:
            for t, c in src_tag[s].most_common():
                matrix_rows.append([s, s_total, t, c, f"{(c / s_total * 100):.1f}"])
        else:
            matrix_rows.append([s, s_total, "(без тегов)", "", ""])
    write_csv(
        os.path.join(OUT_DIR, "source_tag_matrix.csv"),
        ["Источник", "Всего сделок с источником", "Тег", "Сделок с этим тегом", "Доля тега при источнике %"],
        matrix_rows,
    )

    # 4) обратная связь тег -> источник (для восстановления источника по тегу)
    tag_rows = []
    for t, _ in tag_counter.most_common():
        t_total = tag_counter[t]
        for s, c in tag_src[t].most_common():
            tag_rows.append([t, t_total, s, c, f"{(c / t_total * 100):.1f}"])
    write_csv(
        os.path.join(OUT_DIR, "tag_to_source.csv"),
        ["Тег", "Всего сделок с тегом", "Источник", "Сделок", "Доля источника при теге %"],
        tag_rows,
    )

    # 5) сводка качества
    write_csv(
        os.path.join(OUT_DIR, "quality_summary.csv"),
        ["Показатель", "Кол-во", "Доля %"],
        [
            ["Всего сделок в анализе", total, "100.0"],
            ["Без источника", n_no_source, f"{(n_no_source / total * 100):.1f}" if total else "0"],
            ["Без тегов", n_no_tags, f"{(n_no_tags / total * 100):.1f}" if total else "0"],
            ["Без источника И без тегов", n_no_both, f"{(n_no_both / total * 100):.1f}" if total else "0"],
            ["Уникальных источников", len([s for s in source_counter if s != EMPTY]), ""],
            ["Уникальных тегов", len(tag_counter), ""],
        ],
    )

    # --- краткий вывод в консоль ---
    print("\n==== СВОДКА ====")
    print(f"Всего сделок: {total}")
    print(f"Без источника: {n_no_source} ({(n_no_source/total*100):.1f}%)" if total else "нет данных")
    print(f"Без тегов: {n_no_tags} ({(n_no_tags/total*100):.1f}%)" if total else "")
    print(f"Без источника и тегов: {n_no_both} ({(n_no_both/total*100):.1f}%)" if total else "")
    print(f"Уникальных источников: {len([s for s in source_counter if s != EMPTY])}, тегов: {len(tag_counter)}")
    print("\nТоп источников:")
    for s, c in source_counter.most_common(15):
        print(f"  {c:5d}  {s}")
    print("\nФайлы записаны в:", OUT_DIR)
    for name in ("source_frequency.csv", "tag_frequency.csv", "source_tag_matrix.csv",
                 "tag_to_source.csv", "quality_summary.csv"):
        print("  -", name)
    print("\nГотово. Пришли содержимое папки analysis/ — соберу черновик базы правил.")


if __name__ == "__main__":
    main()
