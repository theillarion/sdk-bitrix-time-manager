# Architecture — sdk-bitrix-time-manager

> Проверено: 2026-10-21, коммит 4b99a10.

## Границы системы

- Внутри: REST-транспорт, сервис операций, SQLite-журнал, модели, схема портала (`src/bitrix_time_manager/`).
- Снаружи: портал Bitrix24 (REST CRM через вебхук), вызывающее приложение `bitrix-auto-manage`, файл журнала SQLite как внешний артефакт.
- Telegram и мессенджеры в SDK не входят и не упоминаются в публичном API.

## Компоненты и ответственность

- `client.py` — транспорт: whitelist read-методов (client.py:13), запись — только `crm.item.add` и без ретраев (client.py:59), повторы только для чтения, маскирование секретов в логах (client.py:19).
- `service.py` — `TimeManager`: сверка владельца и схемы через `profile`/`method.get` (service.py:149); две операции с `dry_run=True` по умолчанию (service.py:247,268); identity операции — sha256 от портала, пользователя и параметров (service.py:90); фактическая отправка — единственный `crm.item.add` (service.py:390).
- `journal.py` — SQLite-журнал: `reserve` резервирует операцию или возвращает завершенную (journal.py:63-75), состояние `pending/uncertain` блокирует повтор (journal.py:71), `resolve` допустим только для pending/uncertain (journal.py:116-117).
- `config.py` — `Settings` (HTTPS-вебхук, `user_id` из URL, timeout 0–120 с, read_retries 0–5, `allow_writes=False`: config.py:55-57,78-79) и `PortalSchema` — схема конкретного портала (config.py:17,20,39,45).
- `models.py` — pydantic-контракты операций; `errors.py` — иерархия ошибок и `redact` (errors.py:6).

## Потоки данных

`create_*` (service.py:247+) → проверка схемы и поиск дублей (`crm.item.list/get`) → `journal.reserve` (journal.py:63) → при `dry_run=False` и `allow_writes=True` — единственный `crm.item.add` (service.py:390) → `created`/`existing`; потерянный ответ → `uncertain`, разблокировка — ручной `resolve`. Чтение (`get_workday`, `list_*`) — только read-методы с ретраями (client.py:59).

## Внешние зависимости и контракты

- Портал: REST-методы `profile`, `method.get`, `crm.item.fields/list/get/add`; смарт-процессы 1208/1218/1250, справочник минут 193/194, договоры 1048/1052/1298 (config.py:20,39,45).
- Приложение: контракт — публичный Python API `TimeManager`/`Settings`; стабильный `xmlId` выводится из портала, пользователя и параметров (service.py:91).

## Инварианты

- `crm.item.add` — один запрос на операцию (client.py:59,434); повтор возвращает `existing` или блокируется как `uncertain`.
- Запись требует одновременно `allow_writes=True` и `dry_run=False` (config.py:57; service.py:366).
- `xmlId` детерминирован: портал + пользователь + параметры (service.py:91).
- Секрет вебхука не попадает в логи и тексты ошибок (client.py:19; errors.py:6).

## Нефункциональные ограничения

- Тайм-аут ≤120 с, read-ретраи ≤5 (config.py:78-79).
- Покрытие: фактически 99% по модулям (замер 2026-10-21); порог в конфигурации репо не закреплен — TODO.
- Журнал хранится бессрочно, ретеншн не определен — TODO.

## ADR

ADR не заведены. TODO: создать `docs/adr/` при первом существенном решении (политика ретеншна журнала, закрепление порога покрытия).
