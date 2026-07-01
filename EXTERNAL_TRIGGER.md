# Внешний запуск по расписанию (cron-job.org → GitHub API)

Собственный cron GitHub на этом репозитории оказался ненадёжным (срабатывал не вовремя),
поэтому запуск по времени делает внешний сервис **cron-job.org**: он в нужное время
дёргает GitHub API, а тот запускает воркфлоу. Так точное время гарантировано.

`Публикация отчётов в ТГ` отдельно НЕ дёргается — она запускается автоматически после
выгрузки amoCRM (триггер `workflow_run`).

`BMI бюджет и расходы` тоже отдельно НЕ дёргается — запускается автоматически сразу после
ежедневной выгрузки звонков OnlinePBX (триггер `workflow_run`), т.е. обновляется каждый день
вместе со звонками.

---

## Шаг 1. Создать токен GitHub (PAT)

1. GitHub → справа вверху аватар → **Settings** → внизу слева **Developer settings**.
2. **Personal access tokens → Fine-grained tokens → Generate new token**.
3. Заполнить:
   - **Token name:** `cron-amo-export`
   - **Expiration:** на ваш выбор (например, 1 год; перед истечением надо будет обновить).
   - **Repository access:** *Only select repositories* → выбрать **amo-export**.
   - **Permissions → Repository permissions → Actions:** *Read and write*.
     (Этого достаточно, чтобы запускать воркфлоу.)
4. **Generate token** → скопировать строку токена (показывается один раз!).

---

## Шаг 2. Создать задания на cron-job.org

1. Зарегистрироваться на [cron-job.org](https://cron-job.org) (бесплатно).
2. В настройках аккаунта выставить часовой пояс **Europe/Moscow** (тогда время заданий
   указываем сразу по Москве, без пересчёта).
3. Создать **4 задания** (Create cronjob). У всех общие настройки запроса:

   - **Request method:** `POST`
   - **Headers** (добавить три):
     - `Authorization` = `Bearer ВАШ_ТОКЕН`
     - `Accept` = `application/vnd.github+json`
     - `X-GitHub-Api-Version` = `2022-11-28`
   - **Request body:** `{"ref":"main"}`
   - Успешный ответ GitHub — код **204** (No Content). В cron-job.org можно отметить
     ожидаемый статус и включить уведомление о сбоях.

   Различаются только **URL** и **расписание**:

   | Задание | URL | Расписание (МСК) |
   |---|---|---|
   | Выгрузка amoCRM | `https://api.github.com/repos/Gitelman-traning/amo-export/actions/workflows/amo-export.yml/dispatches` | каждый день **01:10** |
   | Звонки OnlinePBX | `https://api.github.com/repos/Gitelman-traning/amo-export/actions/workflows/pbx-export.yml/dispatches` | каждый день **01:15** |
   | Контакты+сделки 2-й линии | `https://api.github.com/repos/Gitelman-traning/amo-export/actions/workflows/second-line.yml/dispatches` | каждый день **01:20** |
   | Напоминание о таблице | `https://api.github.com/repos/Gitelman-traning/amo-export/actions/workflows/monthly-reminder.yml/dispatches` | **1-го числа** каждого месяца, **09:00** |

   > BMI отдельного задания НЕ требует — идёт по цепочке после звонков OnlinePBX (см. выше).

4. Сохранить. Готово — теперь запуск идёт точно по времени.

### Быстрее: Import from cURL

На странице создания задания есть кнопка **IMPORT FROM CURL** — она сама заполнит
URL, метод, заголовки и тело. Вставьте команду (замените `ВАШ_ТОКЕН`), затем задайте
расписание через **Custom** (crontab, часовой пояс Europe/Moscow):

| Задание | Crontab | cURL (хвост URL) |
|---|---|---|
| amoCRM | `10 1 * * *` | `.../workflows/amo-export.yml/dispatches` |
| OnlinePBX | `15 1 * * *` | `.../workflows/pbx-export.yml/dispatches` |
| 2-я линия | `20 1 * * *` | `.../workflows/second-line.yml/dispatches` |
| Напоминание | `0 9 1 * *` | `.../workflows/monthly-reminder.yml/dispatches` |

Шаблон команды:
```
curl -X POST \
  -H "Authorization: Bearer ВАШ_ТОКЕН" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  -H "Content-Type: application/json" \
  -d '{"ref":"main"}' \
  https://api.github.com/repos/Gitelman-traning/amo-export/actions/workflows/amo-export.yml/dispatches
```

Кнопка **TEST RUN** на cron-job.org сразу проверит запрос (ожидаемый ответ — **204**).

---

## Как проверить, что работает

- На cron-job.org у задания есть **History** — там виден код ответа (нужен **204**).
- В GitHub: вкладка **Actions** — появятся запуски с событием `workflow_dispatch` в нужное время.
- В Telegram придут обычные отбивки об успехе каждого процесса.

## Если перестало запускаться
- Проверьте History на cron-job.org: если код **401** — протух/неверный токен (пересоздать PAT, обновить в заголовке `Authorization`). Если **404** — проверьте URL/имя файла воркфлоу.
- Токен PAT имеет срок действия — не забудьте обновить перед истечением.
