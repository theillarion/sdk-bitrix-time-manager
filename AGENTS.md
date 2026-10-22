# sdk-bitrix-time-manager
Асинхронный Python SDK и CLI (`bitrix-time`) для Time-менеджера Bitrix24 через REST вебхук владельца; операции ограничены сотрудником-владельцем. На SDK опирается приложение [bitrix-auto-manage](https://github.com/theillarion/bitrix-auto-manage). Инвариант: `crm.item.add` отправляется один раз через SQLite-журнал с детерминированным `xmlId` — повторы и потерянные ответы не создают дублей.

## Commands

| Команда | Зачем |
| --- | --- |
| `python3 -m venv .venv` | создать окружение (один раз) |
| `source .venv/bin/activate` | включить окружение |
| `pip install -e '.[dev]'` | поставить пакет и dev-инструменты (один раз) |
| `pytest -q --cov=bitrix_time_manager --cov-report=term` | все тесты + покрытие по модулям |
| `pytest -q tests/unit/test_config.py -k invalid_webhook` | один тест: файл + фильтр по имени |
| `ruff check src tests examples` | линт |
| `ruff format --check src tests examples` | формат (шаг CI) |
| `mypy src` | типы (strict) |

## Boundaries

Never:
- реальные данные портала (домен, USER_ID, ID дней и карточек, названия задач, URL вебхуков) — только плейсхолдеры (`example.test/rest/500/…`);
- `.env`, `.state/`, `reports/`, секрет вебхука в логах и ошибках;
- `PortalSchema` в `src/bitrix_time_manager/config.py` (ID смарт-процессов, категории, справочники, типы договоров) — схема конкретного портала: не менять;
- повторы `crm.item.add`, удаление или обход журнала, force push и переписывание истории без явной команды.
Ask first:
- новые зависимости в `pyproject.toml`;
- обращения к реальному порталу (live-тесты — только с явно заданным `BITRIX_TEST_WEBHOOK_URL`);
- правки смежного приложения `bitrix-auto-manage`.

## Agents

Главный агент только делегирует: не читает, не правит, не запускает.

### Pipeline

```
planner → implementer → tester → verifier → PASS
              ↑__________________|
                FAIL (max 3)
```

### Roles

| Роль | Файл | Вход | Выход |
| --- | --- | --- | --- |
| planner | `docs/agents/planner/ROLE.md` | задача, критерии приёмки | `spec.md`, `hypotheses.md` |
| implementer | `docs/agents/implementer/ROLE.md` | `spec.md`, `hypotheses.md` | код, `claims.md` |
| tester | `docs/agents/tester/ROLE.md` | `spec.md`, `hypotheses.md`, diff | проверки |
| verifier | `docs/agents/verifier/ROLE.md` | `spec.md`, `hypotheses.md`, `claims.md`, diff | `verdicts.md`, PASS/FAIL |

### Artifacts

| Файл | Кто пишет | Кто читает |
| --- | --- | --- |
| `.agents-cache/<task>/spec.md` | planner | implementer, tester, verifier |
| `.agents-cache/<task>/hypotheses.md` | planner, verifier | implementer, tester, verifier |
| `.agents-cache/<task>/claims.md` | implementer | verifier |
| `.agents-cache/<task>/verdicts.md` | verifier | implementer, человек |

### Rules

- Полный пайплайн: >1 файла, >~50 строк, критичные пути. Мелочи: `implementer` + `verifier`.
- Нет исполняемых проверок — `tester` не вызывается.
- FAIL → `implementer` с `verdicts.md`; максимум 3 раунда, дальше человек.
- `.agents-cache/` в `.gitignore`.

## Conventions

- Документация — на русском; сообщения логов и ошибок в коде — на английском; коммиты — английские, короткие; изменения — в `[Unreleased]` `CHANGELOG.md`, релиз — по явной команде.
- Новая функциональность — с тестами и покрытием модуля ≥95%; pydantic-модели запрещают неизвестные поля; зависимости минимальны; офлайн-тесты блокируют сеть к Bitrix — не отключать.
- Меняя публичный API или поведение: убедиться, что потребитель [bitrix-auto-manage](https://github.com/theillarion/bitrix-auto-manage) не сломался (его тесты и `mypy` зелёные); ломающие изменения предлагать в двух вариантах — с сохранением обратной совместимости и без; до 1.0.0 ломать совместимость допустимо.

## Checklist

| Проверка | Команда | Зеленый, когда |
| --- | --- | --- |
| lint | `ruff check src tests examples` | exit 0 |
| typecheck | `mypy src` | exit 0 |
| tests | `pytest -q --cov=bitrix_time_manager` | 0 failed, порог покрытия пройден |
| регрессионный тест | pytest на base: `git worktree add /tmp/base <ref>` | падает на base, проходит сейчас |
| verifier | `verdicts.md` | PASS: 0 REFUTED, все CRITICAL CONFIRMED |
| evidence | `.agents-cache/<task>/verdicts.md` | по каждой гипотезе |
| secrets / generated | `git status --porcelain` + grep по `Boundaries` | чисто |
| scope | `git diff` против `spec.md` | diff только из spec |

Любая красная строка — задача не готова.

## Deeper context

- `README.md` — установка, CLI, поведение портала и повторных запросов.
- `docs/technical-requirements.md` и `docs/limitations.md` — требования, схема портала, CRM-ссылки, известные ограничения.
- `docs/architecture.md` — границы, компоненты, потоки, инварианты.
