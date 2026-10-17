"""Сторож баланса телефонии BMI: на сколько дней хватит денег.

Зачем: 17.10.2026 баланс BMI закончился, звонки встали, а узнали мы об этом
от менеджеров. Скрипт берёт доступную до блокировки сумму (остаток + кредит),
делит на средний расход в день за последнюю неделю и пишет в Telegram, если
денег осталось меньше чем на ALERT_DAYS дней.

Расход в день = платные звонки за 7 дней / 7. Абонентская плата списывается
разом в начале месяца, поэтому учитываем её отдельно: если до 1-го числа
осталось меньше ALERT_DAYS дней, к нужной сумме добавляется месячная АП.

Запуск — шаг в bmi-export.yml (ночью) и отдельный bmi-balance.yml днём.
Переменные: BMI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
BMI_ALERT_DAYS (по умолчанию 3), BMI_BALANCE_REPORT=1 — слать сводку всегда.
"""
import os
from datetime import date, datetime, timedelta

from bmi_export import MSK, bmi_get, num, run_url_line, send_telegram, _currency_code

ALERT_DAYS = float(os.environ.get("BMI_ALERT_DAYS", "") or 3)
ALWAYS_REPORT = os.environ.get("BMI_BALANCE_REPORT", "").strip() in ("1", "true", "yes")
SPEND_DAYS = 7


def money(v, cur):
    return f"{num(v):,.0f}".replace(",", " ") + (f" {cur}" if cur else "")


def main():
    today = datetime.now(MSK).date()
    ov = bmi_get("/billing/overview")
    cur = _currency_code(ov.get("currency")) or ""
    available = num(ov.get("available"))

    # Неделя до вчера включительно: сегодняшние звонки ещё не все посчитаны.
    d_to = today - timedelta(days=1)
    d_from = d_to - timedelta(days=SPEND_DAYS - 1)
    sp = bmi_get("/billing/spend", {"date_from": d_from.isoformat(), "date_to": d_to.isoformat()})
    calls_week = num(sp.get("calls_cost"))
    monthly_fee = num(sp.get("monthly_fee"))
    per_day = calls_week / SPEND_DAYS

    next_month = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
    days_to_fee = (next_month - today).days
    days_left = available / per_day if per_day > 0 else float("inf")

    # Сколько нужно на ближайшие ALERT_DAYS дней (с АП, если она попадает в это окно).
    need = per_day * ALERT_DAYS + (monthly_fee if days_to_fee <= ALERT_DAYS else 0)
    low = available < need

    lines = [
        f"Доступно до блокировки: {money(available, cur)}",
        f"Расход на звонки: {money(per_day, cur)} в день (за {d_from:%d.%m}–{d_to:%d.%m}: {money(calls_week, cur)})",
        f"Хватит примерно на {days_left:.1f} дн." if days_left != float("inf") else "Платных звонков за неделю не было",
        f"Абонплата {money(monthly_fee, cur)} спишется {next_month:%d.%m} (через {days_to_fee} дн.)",
    ]
    print("\n".join(lines))

    if low:
        week_need = per_day * 7 + (monthly_fee if days_to_fee <= 7 else 0)
        topup = max(week_need - available, 0)
        send_telegram(
            f"⚠️ Телефония BMI: денег меньше чем на {ALERT_DAYS:g} дн.\n" + "\n".join(lines)
            + f"\nЧтобы хватило на неделю, пополнить на ~{money(topup, cur)}" + run_url_line()
        )
    elif ALWAYS_REPORT:
        send_telegram("☎️ Телефония BMI: баланс в порядке\n" + "\n".join(lines))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        send_telegram(f"❌ Сторож баланса BMI упал\nОшибка: {type(e).__name__}: {str(e)[:300]}" + run_url_line())
        raise
