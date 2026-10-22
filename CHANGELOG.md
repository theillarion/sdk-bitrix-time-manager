# Changelog

Все заметные изменения проекта документируются в этом файле.

Формат — [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/), версионирование —
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). До версии 1.0.0 совместимость
не гарантируется: ломающие изменения возможны с ростом minor-версии.

Единственный источник версии — `__version__` в `src/bitrix_time_manager/__init__.py`;
`pyproject.toml` подхватывает её автоматически. Все изменения по ходу коммитов
фиксируются в секции `[Unreleased]`; она превращается в релиз `[X.Y.Z] - дата`
только по явной команде владельца: обновить `__version__`, переименовать секцию
и поставить git-тег `vX.Y.Z`.

## [Unreleased]

## [0.2.0] - 2026-10-22

### Added

- Шапка с бейджами в README.md.

### Changed

- README, шапка: добавлен бейдж `stars`, убраны бейджи
  `code style: ruff` и `typing: mypy strict`; итоговый набор:
  release, python, license, stars, tests.

### Removed

- Ручное создание рабочего дня: модель `WorkdayCreate`, метод
  `TimeManager.create_workday`, CLI-команда `bitrix-time workday` и значение
  `kind="workday"` в `OperationResult`. Рабочий день создаётся порталом
  автоматически (проход СКУД или робот удалённого дня); чтение дня
  (`get_workday`/`day`) сохранено. Breaking change до 1.0.0.

## [0.1.1] - 2026-10-22

### Added

- Раздел «Возможности» и содержание в README.md.
- `AGENTS.md` и роли агентов (`docs/agents/*/ROLE.md`): пайплайн
  planner → implementer → tester → verifier, команды и границы.

### Changed

- Документация и тесты используют только нейтральные значения вместо
  идентификаторов конкретного портала.

## [0.1.0] - 2026-10-20

Начальная версия: асинхронный Python SDK и CLI `bitrix-time` для Time-менеджера
портала Bitrix24 (CRM-смарт-процессы, а не методы `timeman.*`).

### Added

- Создание удалённой работы, ручного рабочего дня и учета времени; каждая операция
  сначала доступна как предварительная проверка (preview/dry-run) без записи.
- Чтение рабочего дня, его учета времени и удалёнки; поиск договоров и шаблонов.
- Обнаружение дублей и конфликтов, идемпотентные ключи и стабильные `xmlId`.
- Журнал операций в SQLite: ровно одна отправка записи, блокировка автоматических
  повторов после потери ответа, ручной `resolve` после проверки.
- Запись по умолчанию запрещена: Python требует `allow_writes=True` и `dry_run=False`,
  CLI — флаг `--apply`.
- CLI `bitrix-time` с JSON-выводом, кодами 0/2 и флагом `--version`.
- Секрет вебхука маскируется в ошибках и логах; принимаются только HTTPS-вебхуки.
- Python 3.11–3.14, зависимости httpx и pydantic, строгая типизация (`py.typed`).
- Тесты уровней unit/mock/integration, опциональные live-тесты и CI на GitHub Actions.
- Лицензия MIT.

[Unreleased]: https://github.com/theillarion/sdk-bitrix-time-manager/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/theillarion/sdk-bitrix-time-manager/releases/tag/v0.2.0
[0.1.1]: https://github.com/theillarion/sdk-bitrix-time-manager/releases/tag/v0.1.1
[0.1.0]: https://github.com/theillarion/sdk-bitrix-time-manager/releases/tag/v0.1.0
