# Операционная шпаргалка — автоматизации Gitelman (замена n8n)

Единый гайд по системе: что где работает, как управлять, что помнить.
Обновлено: 08.07.2026.

---

## 1. Общая картина

Все процессы по расписанию, что раньше крутились в **n8n**, переехали на **GitHub Actions**.
Запуск по времени делает внешний сервис **cron-job.org** (дёргает GitHub API), т.к. родной
cron GitHub на новом репозитории был ненадёжен. n8n по расписанию больше ничего не выполняет.

- **Репозиторий:** https://github.com/Gitelman-traning/amo-export (приватный, аккаунт GitHub `Gitelman-traning`)
- **Оркестратор:** GitHub Actions (воркфлоу в `.github/workflows/`)
- **Планировщик:** cron-job.org (5 заданий → GitHub API `workflow_dispatch`)
- **Локальная папка с кодом:** `C:\Users\Nikita\Documents\Gitelman\Local\amo-export`

---

## 2. Процессы

| Процесс | Файл | Расписание (МСК) | Что делает | Куда пишет |
|---|---|---|---|---|
| Выгрузка amoCRM | `amo_export.py` | 01:10 | сделки+контакты amoCRM (3 воронки, 3 мес.) | месячная таблица `SPREADSHEET_ID`, лист «общая выгрузка от Никиты» |
| Звонки OnlinePBX | `pbx_export.py` | 01:15 | звонки за месяц | тот же файл, лист «…ЗВОНКИ» |
| Контакты 2026 (2-я линия) | `second_line.py` | 01:20 | целевые контакты (с 01.01, оборот>30, сотр.>5) + их сделки | таблица `1_cI3G94U…`, листы «Контакты/Сделки 2026» (и 2025 разово) |
| Публикация отчётов в ТГ | `tg_report.py` | после выгрузки amoCRM (по цепочке) | читает лист `tg_reports`, шлёт готовые тексты в Telegram-чаты | Telegram |
| Транскрибация зумов | `zoom_transcribe.py` | 01:20 | новые записи Zoom → Apify+Deepgram → Google Doc | таблица «маркетинг» `1gbj-…`, лист «дпд» (ссылка на док) |
| Напоминание о новой таблице | `monthly_reminder.py` | 1-го числа 09:00 | напоминание в ТГ завести таблицу на месяц | Telegram |

Плюс в репо есть `BMI.io → Google Sheets` (выгрузка бюджета), запускается по цепочке.

---

## 3. Запуск по расписанию (cron-job.org)

Личный кабинет: https://console.cron-job.org — **5 заданий**, у всех:
- **Method:** POST
- **URL:** `https://api.github.com/repos/Gitelman-traning/amo-export/actions/workflows/<ФАЙЛ>.yml/dispatches`
- **Headers:** `Authorization: Bearer <PAT>`, `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`, `Content-Type: application/json`
- **Body:** `{"ref":"main"}`
- **Timezone:** Europe/Moscow
- Успех = ответ **204**.

| Задание (Title) | workflow-файл | Crontab |
|---|---|---|
| Выгрузка amoCRM | `amo-export.yml` | `10 1 * * *` |
| Звонки OnlinePBX | `pbx-export.yml` | `15 1 * * *` |
| Контакты 2026 | `second-line.yml` | `20 1 * * *` |
| Транскрибация зумов | `zoom-transcribe.yml` | `20 1 * * *` |
| Добавить новый ID таблицы | `monthly-reminder.yml` | `0 9 1 * *` |

Отчёты в ТГ (`tg-report.yml`) отдельным заданием НЕ дёргаются — они идут **по цепочке**
(триггер `workflow_run`) сразу после успешной выгрузки amoCRM.

---

## 4. Секреты и переменные GitHub

Settings репозитория → Secrets and variables → Actions.

**Секреты (значения скрыты):**
- `AMO_TOKEN` — долгоживущий токен amoCRM (шлётся как `Authorization: Bearer`)
- `GOOGLE_SERVICE_ACCOUNT_JSON` — JSON сервис-аккаунта (для чтения/записи Google Sheets)
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — бот и чат для отбивок (бот @ZoomYoutubeUpload_bot, чат -1003982575444)
- `ONLINEPBX_AUTH_KEY` — ключ интеграции OnlinePBX
- `APIFY_TOKEN`, `DEEPGRAM_TOKEN` — для транскрибации зумов
- `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` / `GOOGLE_OAUTH_REFRESH_TOKEN` — OAuth аккаунта `gitelmanteam1@gmail.com` для СОЗДАНИЯ Google Docs

**Переменная (Variables, не секрет):**
- `SPREADSHEET_ID` — ID месячной таблицы для amo-выгрузки/звонков/отчётов. **Меняется каждый месяц.**

---

## 5. Доступы Google (важно)

- **Таблицы** читает/пишет **сервис-аккаунт** `clode-60@amo-export-500512.iam.gserviceaccount.com`.
  Ему нужен доступ **Редактор** ко всем рабочим таблицам:
  - месячная таблица amo (`SPREADSHEET_ID`)
  - таблица 2-й линии `1_cI3G94UuGEDoHK58xSCX8Gb5UQSRB78SKVo9f321KY`
  - таблица «маркетинг» `1gbj-FGKRnc5Cm4s5_qFfHpx-gHqL3Bn37FQbR8gTpuY`
- **Google Docs** (транскрипты) создаёт **OAuth реального аккаунта** `gitelmanteam1@gmail.com`
  (у сервис-аккаунта нет своего Drive → он файлы создавать не может). Аккаунту нужен
  **Редактор** на папку транскриптов `1F_SdcscyOKhXUe79Va65m18d9ScfviVj` («ММ_июль»).
- В Google Cloud проекте `amo-export-500512` включены API: **Sheets, Docs, Drive**.

---

## 6. Как управлять

- **Запустить вручную:** GitHub → вкладка **Actions** → выбрать процесс → **Run workflow**.
  Или на cron-job.org → **TEST RUN** у нужного задания.
- **Поменять время:** на cron-job.org у задания → Crontab (в поясе Europe/Moscow). GitHub-крон в воркфлоу отключён намеренно.
- **Посмотреть, что пошло не так:** GitHub → Actions → красный прогон → шаг со скриптом → лог.
  Ещё проще — отбивки в Telegram (✅/❌ по каждому процессу, со ссылкой на лог).
- **Настройки внутри процессов:** блок «НАСТРОЙКИ» в начале каждого `*.py`
  (воронки, пороги, ID таблиц, лимиты). После правки — commit + push в `main`.
- **Транскрибация, лимит за прогон:** по умолчанию 21 (env `MAX_PER_RUN` / поле `limit` при ручном запуске). Берёт новейшие записи, старый бэклог не грызёт.

---

## 7. Ежемесячная рутина (смена таблицы amo)

1-го числа приходит напоминание в ТГ. Что сделать:
1. Создать новую Google-таблицу на месяц (лист назвать так же — «общая выгрузка от Никиты»,
   для звонков «…ЗВОНКИ»; блок `key/value` с `export_date` тоже нужен).
2. Дать **Редактор** сервис-аккаунту `clode-60@...` на новый файл.
3. Вписать ID новой таблицы в переменную **`SPREADSHEET_ID`**
   (GitHub → Settings → Secrets and variables → Actions → Variables).

---

## 8. Токен cron-job.org (продлевать!)

Задания cron-job.org ходят по **GitHub fine-grained PAT** (права: репо `amo-export`, Actions: Read and write).
- Срок жизни fine-grained максимум **~1 год**. Последнее продление: **08.07.2026**.
- Когда истечёт — **все 5 заданий встанут** (будут ловить 401).
- Продлить: https://github.com/settings/personal-access-tokens → токен → **Regenerate** →
  новый срок → скопировать → обновить `Authorization: Bearer <новый>` во **всех 5 заданиях** cron-job.org → TEST RUN (204).
- Альтернатива, чтобы не продлевать: classic token с правами `repo`+`workflow` и «No expiration» (но шире по правам).

---

## 9. Грабли и особенности (чтобы не сломать)

- **Таймзона:** GitHub-раннеры в UTC. В скриптах даты считаются по `Europe/Moscow`. Crontab на cron-job.org — в поясе Europe/Moscow.
- **Формульная инъекция Sheets:** значения, начинающиеся с `= + - @`, экранируются апострофом (иначе `#ERROR`). Уже встроено во все пишущие скрипты.
- **Сервис-аккаунт и Drive:** он НЕ может создавать файлы (нет квоты) — поэтому доки создаёт OAuth `gitelmanteam1`. Таблицы (не его файлы) писать может.
- **Колонка `transcribation` в «дпд»** — smart-chip. Скрипт пишет обычной ссылкой (рабочая, просто не кнопка-чип). Дедуп зумов — по непустому `transcribation`; ключ дедупа — ID сделки.
- **Записи, добавленные в ZOOM после ночного прогона,** обработаются на следующем прогоне (раз в сутки). Хочешь чаще — поменяй crontab на несколько раз в день.
- **cron-job.org — единый источник запуска.** Родной cron GitHub в воркфлоу выключен, чтобы не было дублей/ложных срабатываний.

---

## 10. Что НЕ переезжало

Вебхук/телеграм-боты в n8n (Бот транскрибатор, sms-приёмник, zoom→youtube, «Файл после
диагностики») — это **не расписания**, их не трогали. Если решишь убрать и их с n8n —
это отдельный этап (нужен постоянный endpoint / мини-сервер).

---

## Ключевые идентификаторы (справочно)
- amoCRM: `pavelgitelman.amocrm.ru`, воронки `8733326 / 9701010 / 7295078`
- Apify actor-task: `gyglem~my-actor-1-task`
- Deepgram: модель `nova-3`, язык `ru`
- Сервис-аккаунт Google: `clode-60@amo-export-500512.iam.gserviceaccount.com`
- OAuth-аккаунт для доков: `gitelmanteam1@gmail.com`
- Таблицы: 2-я линия `1_cI3G94U…`, маркетинг/зумы `1gbj-…`, месячная amo — в переменной `SPREADSHEET_ID`
- Папка транскриптов: `1F_Sdc…` («ММ_июль»)
