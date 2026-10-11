"""Time-manager business API for Python applications and the CLI."""

import asyncio
import hashlib
import json
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from zoneinfo import ZoneInfo

from bitrix_time_manager.client import BitrixClient
from bitrix_time_manager.errors import (
    BitrixAPIError,
    ConflictError,
    NotFoundError,
    SchemaError,
    TimeManagerError,
    TransportError,
    UncertainWriteError,
    WritesDisabledError,
)
from bitrix_time_manager.journal import OperationJournal
from bitrix_time_manager.models import (
    DayInterval,
    ItemSummary,
    OperationResult,
    RemoteWorkCreate,
    TimeEntryCreate,
    Workday,
    WorkdayCreate,
)

Kind = Literal["workday", "time_entry", "remote_work"]


class TimeManager:
    """Self-service API for the webhook owner, with preview and idempotent create.

    Reuse the client and journal for an application lifespan. For multiple
    workers, use one shared journal path; independent journals cannot provide
    an atomic uniqueness constraint inside Bitrix itself.
    """

    def __init__(self, client: BitrixClient, journal: OperationJournal | None = None) -> None:
        self.client = client
        self.settings = client.settings
        self.schema = self.settings.schema
        self.journal = journal or OperationJournal(self.settings.state_path)
        self._checked_types: set[int] = set()

    def _stamp(self, spec: DayInterval, *, end: bool = False) -> str:
        value = datetime.combine(spec.day, spec.end if end else spec.start, ZoneInfo(self.settings.timezone))
        # A timezone with a daylight-saving gap may not accept the requested clock time.
        if value.astimezone(ZoneInfo("UTC")).astimezone(value.tzinfo).replace(tzinfo=None) != value.replace(
            tzinfo=None
        ):
            raise ValueError("Указанное локальное время не существует в этом часовом поясе.")
        return value.isoformat(timespec="seconds")

    @staticmethod
    def _same_stamp(left: object, right: object) -> bool:
        if not isinstance(left, str) or not isinstance(right, str):
            return left == right
        try:
            return datetime.fromisoformat(left.replace("Z", "+00:00")) == datetime.fromisoformat(
                right.replace("Z", "+00:00")
            )
        except ValueError:
            return False

    @staticmethod
    def _number(value: object) -> Decimal:
        try:
            return Decimal(str(value))
        except InvalidOperation:
            raise SchemaError("Bitrix вернул некорректную числовую длительность.") from None

    def _url(self, kind: Kind, entity_type_id: int, item_id: int) -> str:
        section = "udalennaya_rabota" if kind == "remote_work" else "rabochiy_den"
        return f"{self.settings.portal_url}/page/timemenedzher/{section}/type/{entity_type_id}/details/{item_id}/"

    def _plan(
        self, kind: Kind, spec: DayInterval | TimeEntryCreate, entity_type_id: int, fields: dict[str, Any]
    ) -> OperationResult:
        identity = {
            "portal": self.settings.portal_url,
            "user_id": self.settings.user_id,
            "kind": kind,
            "input": spec.model_dump(mode="json"),
        }
        operation_id = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        fields = {**fields, "xmlId": f"btm_{operation_id}", "assignedById": self.settings.user_id, "opened": "N"}
        return OperationResult(
            status="preview", kind=kind, operation_id=operation_id, entity_type_id=entity_type_id, fields=fields
        )

    async def _check_schema(self, entity_type_id: int) -> None:
        if entity_type_id in self._checked_types:
            return
        body = await self.client.call("crm.item.fields", {"entityTypeId": entity_type_id})
        result = body.get("result")
        fields = result.get("fields") if isinstance(result, dict) else None
        if not isinstance(fields, dict):
            raise SchemaError("Не удалось прочитать поля смарт-процесса.")
        expected: dict[str, str]
        s = self.schema
        if entity_type_id == s.day_type:
            expected = {
                s.day_date: "date",
                s.day_start: "datetime",
                s.day_end: "datetime",
                s.day_employee: "employee",
                s.day_comment: "string",
                s.day_source: "enumeration",
                s.day_kind: "iblock_element",
            }
        elif entity_type_id == s.time_type:
            expected = {
                s.time_contract: "crm",
                s.time_description: "string",
                s.time_hours: "double",
                s.time_minutes: "enumeration",
                "parentId1208": "crm_entity",
            }
        elif entity_type_id == s.remote_type:
            expected = {s.remote_start: "datetime", s.remote_end: "datetime", s.remote_comment: "string"}
        else:
            raise SchemaError("Неизвестный тип смарт-процесса.")
        for name, expected_type in expected.items():
            meta = fields.get(name)
            if not isinstance(meta, dict) or meta.get("type") != expected_type or meta.get("isReadOnly"):
                raise SchemaError(f"Поле {name} отсутствует, изменило тип или доступно только для чтения.")
        if entity_type_id == s.time_type:
            choices = {str(item["ID"]): str(item["VALUE"]) for item in fields[s.time_minutes].get("items", [])}
            if choices.get(str(s.minute_zero_id)) != "0" or choices.get(str(s.minute_thirty_id)) != "30":
                raise SchemaError("Справочник минут изменился; обновите PortalSchema.")
            allowed = fields[s.time_contract].get("settings", {})
            if any(allowed.get(f"DYNAMIC_{type_id}") != "Y" for type_id in s.contract_types):
                raise SchemaError("Типы допустимых договоров изменились; обновите PortalSchema.")
        self._checked_types.add(entity_type_id)

    async def check_connection(self) -> dict[str, Any]:
        """Verify identity, field mapping and REST availability without adding records."""
        body = await self.client.call("profile")
        profile = body.get("result")
        if not isinstance(profile, dict) or int(profile.get("ID", 0)) != self.settings.user_id:
            raise SchemaError("Вебхук принадлежит другому пользователю.")
        for entity_type_id in (self.schema.day_type, self.schema.time_type, self.schema.remote_type):
            await self._check_schema(entity_type_id)
        method = await self.client.call("method.get", {"name": "crm.item.add"})
        return {
            "portal": self.settings.portal_url,
            "user_id": self.settings.user_id,
            "timezone": self.settings.timezone,
            "schema_ok": True,
            "create_method": method["result"],
            "writes_enabled": self.settings.allow_writes,
            "note": "Доступность метода не подтверждает права добавления и поведение роботов; запись не выполнялась.",
        }

    async def get_workday(self, day: date) -> Workday | None:
        """Find the owner's workday by the portal's technical date field."""
        s = self.schema
        items = await self.client.list_items(
            s.day_type,
            filters={s.day_employee: self.settings.user_id, s.day_date: day.isoformat()},
            select=["id", "title", s.day_date, s.day_employee, s.day_start, s.day_end],
            limit=2,
        )
        if len(items) > 1:
            raise ConflictError(
                f"За {day} найдены несколько рабочих дней. Сначала устраните неоднозначность на портале."
            )
        if not items:
            return None
        item = items[0]
        if (
            str(item.get(s.day_date, ""))[:10] != day.isoformat()
            or int(item.get(s.day_employee, 0)) != self.settings.user_id
        ):
            raise SchemaError("Bitrix вернул рабочий день, не соответствующий фильтру.")
        return Workday(
            id=int(item["id"]),
            title=str(item["title"]),
            day=day,
            employee_id=self.settings.user_id,
            start=item.get(s.day_start),
            end=item.get(s.day_end),
        )

    async def list_time_entries(self, day: date) -> list[dict[str, Any]]:
        """Read contract work for the owner's day; absence of a day is an error."""
        workday = await self.get_workday(day)
        if workday is None:
            raise NotFoundError(
                f"Рабочий день за {day} не найден. Сначала создайте день или дождитесь роботов удалёнки."
            )
        return await self.client.list_items(
            self.schema.time_type,
            filters={"parentId1208": workday.id, "categoryId": self.schema.time_category},
            select=[
                "id",
                "title",
                "xmlId",
                "categoryId",
                "parentId1208",
                self.schema.time_contract,
                self.schema.time_description,
                self.schema.time_hours,
                self.schema.time_minutes,
            ],
        )

    async def list_remote_work(self, day: date) -> list[dict[str, Any]]:
        """Read remote intervals starting on this local date for the webhook owner."""
        zone = ZoneInfo(self.settings.timezone)
        begin = datetime.combine(day, datetime.min.time(), zone)
        finish = datetime.combine(day + timedelta(days=1), datetime.min.time(), zone)
        return await self.client.list_items(
            self.schema.remote_type,
            filters={
                "assignedById": self.settings.user_id,
                f">={self.schema.remote_start}": begin.isoformat(),
                f"<{self.schema.remote_start}": finish.isoformat(),
            },
            select=[
                "id",
                "title",
                "xmlId",
                "categoryId",
                "assignedById",
                "parentId1208",
                self.schema.remote_start,
                self.schema.remote_end,
                self.schema.remote_comment,
            ],
        )

    async def search_contracts(self, query: str, *, entity_type_id: int = 1298, limit: int = 20) -> list[ItemSummary]:
        """Search one allowed contract type; templates use entity_type_id=1298."""
        if entity_type_id not in self.schema.contract_types or not query.strip() or not 1 <= limit <= 100:
            raise ValueError("Укажите непустой поиск, тип 1048/1052/1298 и limit от 1 до 100.")
        items = await self.client.list_items(
            entity_type_id, filters={"%title": query.strip()}, select=["id", "title"], limit=limit
        )
        return [
            ItemSummary(id=int(item["id"]), title=str(item["title"]), entity_type_id=entity_type_id) for item in items
        ]

    async def create_workday(
        self, spec: WorkdayCreate, *, dry_run: bool = True, idempotency_key: str | None = None
    ) -> OperationResult:
        """Preview/create a regular manual day. A differing existing day is a conflict."""
        s = self.schema
        await self._check_schema(s.day_type)
        fields = {
            "categoryId": s.day_category,
            s.day_date: spec.day.isoformat(),
            s.day_start: self._stamp(spec),
            s.day_end: self._stamp(spec, end=True),
            s.day_employee: self.settings.user_id,
            s.day_comment: spec.comment,
            s.day_source: s.manual_day_source_id,
            s.day_kind: s.normal_day_kind_id,
        }
        plan = self._plan("workday", spec, s.day_type, fields)
        plan = plan.model_copy(
            update={
                "warnings": ["Ручной день создаётся с источником «Другое»; поведение роботов требует ручной проверки."]
            }
        )
        return await self._finish(plan, spec, dry_run=dry_run, idempotency_key=idempotency_key)

    async def create_time_entry(
        self, spec: TimeEntryCreate, *, dry_run: bool = True, idempotency_key: str | None = None
    ) -> OperationResult:
        """Preview/create contract work; this never implicitly creates a missing day."""
        s = self.schema
        await self._check_schema(s.time_type)
        workday = await self.get_workday(spec.day)
        if workday is None:
            raise NotFoundError(f"Рабочий день за {spec.day} не найден. Сначала создайте его отдельно.")
        await self.client.get_item(spec.contract.entity_type_id, spec.contract.item_id)
        fields = {
            "categoryId": s.time_category,
            "parentId1208": workday.id,
            s.time_contract: spec.contract.crm_value,
            s.time_description: spec.description,
            s.time_hours: spec.hours,
            s.time_minutes: s.minute_thirty_id if spec.minutes else s.minute_zero_id,
        }
        plan = self._plan("time_entry", spec, s.time_type, fields).model_copy(update={"workday_id": workday.id})
        return await self._finish(plan, spec, dry_run=dry_run, idempotency_key=idempotency_key)

    async def create_remote_work(
        self, spec: RemoteWorkCreate, *, dry_run: bool = True, idempotency_key: str | None = None
    ) -> OperationResult:
        """Preview/create remote work; leave automatic day creation/linking to Bitrix."""
        s = self.schema
        await self._check_schema(s.remote_type)
        fields = {
            "categoryId": s.remote_category,
            s.remote_start: self._stamp(spec),
            s.remote_end: self._stamp(spec, end=True),
            s.remote_comment: spec.comment,
        }
        plan = self._plan("remote_work", spec, s.remote_type, fields)
        plan = plan.model_copy(
            update={
                "warnings": [
                    "Рабочий день привязывают роботы портала; утилита не создаёт второй день самостоятельно.",
                    "Сохранение будущих дат и поведение роботов ещё требуют ручной проверки.",
                ]
            }
        )
        return await self._finish(plan, spec, dry_run=dry_run, idempotency_key=idempotency_key)

    async def _find_duplicate(
        self, plan: OperationResult, spec: DayInterval | TimeEntryCreate
    ) -> dict[str, Any] | None:
        s = self.schema
        ours = await self.client.list_items(
            plan.entity_type_id, filters={"xmlId": plan.fields["xmlId"], "assignedById": self.settings.user_id}, limit=2
        )
        if len(ours) > 1:
            raise ConflictError("На сервере несколько записей с идентификатором операции.")
        if ours:
            if not self._core_matches(plan, ours[0]):
                raise ConflictError("Запись этой операции уже существует, но её поля изменились на портале.")
            return ours[0]
        if plan.kind == "workday":
            workday = await self.get_workday(spec.day)
            if workday is None:
                return None
            existing = await self.client.get_item(s.day_type, workday.id)
            if self._core_matches(plan, existing):
                return existing
            raise ConflictError(f"Рабочий день за {spec.day} уже существует с другими параметрами.")
        if plan.kind == "time_entry":
            for item in await self.list_time_entries(spec.day):
                if self._core_matches(plan, item):
                    return item
            return None
        overlapping = await self.client.list_items(
            s.remote_type,
            filters={
                "assignedById": self.settings.user_id,
                f"<{s.remote_start}": plan.fields[s.remote_end],
                f">{s.remote_end}": plan.fields[s.remote_start],
            },
        )
        # An unfinished manual remote interval on the same day is also a conflict.
        same_day = await self.list_remote_work(spec.day)
        by_id = {int(item["id"]): item for item in overlapping + same_day}
        overlapping_ids = {int(item["id"]) for item in overlapping}
        for item in by_id.values():
            if self._core_matches(plan, item):
                return item
            if not item.get(s.remote_end) or int(item["id"]) in overlapping_ids:
                raise ConflictError("Уже есть пересекающаяся удалённая работа с другими параметрами.")
        return None

    def _core_matches(self, plan: OperationResult, item: dict[str, Any]) -> bool:
        s = self.schema
        if plan.kind == "workday":
            return (
                str(item.get(s.day_date, ""))[:10] == str(plan.fields[s.day_date])
                and int(item.get(s.day_employee) or 0) == self.settings.user_id
                and self._same_stamp(item.get(s.day_start), plan.fields[s.day_start])
                and self._same_stamp(item.get(s.day_end), plan.fields[s.day_end])
                and (item.get(s.day_comment) or "") == plan.fields[s.day_comment]
                and int(item.get(s.day_kind) or 0) == s.normal_day_kind_id
            )
        if plan.kind == "remote_work":
            return (
                int(item.get("assignedById") or 0) == self.settings.user_id
                and self._same_stamp(item.get(s.remote_start), plan.fields[s.remote_start])
                and self._same_stamp(item.get(s.remote_end), plan.fields[s.remote_end])
                and (item.get(s.remote_comment) or "") == plan.fields[s.remote_comment]
            )
        return (
            int(item.get("parentId1208") or 0) == plan.workday_id
            and item.get(s.time_contract) == plan.fields[s.time_contract]
            and (item.get(s.time_description) or "").strip() == plan.fields[s.time_description]
            and self._number(item.get(s.time_hours)) == self._number(plan.fields[s.time_hours])
            and str(item.get(s.time_minutes)) == str(plan.fields[s.time_minutes])
            and int(item.get("categoryId") or s.time_category) == s.time_category
        )

    def _with_item(self, plan: OperationResult, item: dict[str, Any], status: str) -> OperationResult:
        item_id = int(item["id"])
        parent_id = item.get("parentId1208")
        return plan.model_copy(
            update={
                "status": status,
                "item_id": item_id,
                "item_url": self._url(plan.kind, plan.entity_type_id, item_id),
                "workday_id": item_id if plan.kind == "workday" else int(parent_id) if parent_id else plan.workday_id,
            }
        )

    async def _finish(
        self,
        plan: OperationResult,
        spec: DayInterval | TimeEntryCreate,
        *,
        dry_run: bool,
        idempotency_key: str | None,
    ) -> OperationResult:
        if not dry_run and not self.settings.allow_writes:
            raise WritesDisabledError("Запись отключена. Включите --apply или BITRIX_ALLOW_WRITES=true.")
        if idempotency_key is not None and not 1 <= len(idempotency_key) <= 200:
            raise ValueError("Idempotency-Key должен содержать от 1 до 200 символов.")
        duplicate = await self._find_duplicate(plan, spec)
        if dry_run:
            return self._with_item(plan, duplicate, "preview") if duplicate else plan
        if idempotency_key is not None:
            scope = f"{self.settings.portal_url}:{self.settings.user_id}:"
            key = hashlib.sha256((scope + idempotency_key).encode()).hexdigest()
            self.journal.bind_key(key, plan.operation_id)
        if duplicate:
            result = self._with_item(plan, duplicate, "existing")
            self.journal.complete(
                plan.operation_id, plan.entity_type_id, plan.fields["xmlId"], result.model_dump(mode="json")
            )
            return result
        saved = self.journal.reserve(plan.operation_id, plan.entity_type_id, plan.fields["xmlId"])
        if saved:
            # Do not recreate a manually deleted record from a completed operation.
            raise ConflictError(
                "Операция уже завершалась, но её запись больше не найдена или была изменена. Проверьте портал."
            )
        try:
            body = await self.client.call("crm.item.add", {"entityTypeId": plan.entity_type_id, "fields": plan.fields})
            payload = body.get("result")
            item = payload.get("item") if isinstance(payload, dict) else None
            if not isinstance(item, dict) or not str(item.get("id", "")).isdigit() or int(item["id"]) <= 0:
                raise TransportError("Создание вернуло ответ без ID; результат записи неизвестен.")
        except BitrixAPIError as error:
            self.journal.set_state(plan.operation_id, "uncertain" if error.outcome_unknown else "failed")
            if error.outcome_unknown:
                raise UncertainWriteError(plan.operation_id) from None
            raise
        except asyncio.CancelledError:
            self.journal.set_state(plan.operation_id, "uncertain")
            raise
        except TransportError:
            self.journal.set_state(plan.operation_id, "uncertain")
            raise UncertainWriteError(plan.operation_id) from None
        result = self._with_item(plan, item, "created")
        # Persist the known ID before a verification read can fail or be cancelled.
        self.journal.complete(
            plan.operation_id, plan.entity_type_id, plan.fields["xmlId"], result.model_dump(mode="json")
        )
        try:
            fresh = await self.client.get_item(plan.entity_type_id, int(item["id"]))
            result = self._with_item(plan, fresh, "created")
            if not self._core_matches(plan, fresh):
                result = result.model_copy(
                    update={
                        "warnings": [
                            *result.warnings,
                            "После сохранения поля отличаются от запроса; проверьте карточку и роботов.",
                        ]
                    }
                )
        except TimeManagerError:
            result = result.model_copy(
                update={"warnings": [*result.warnings, "ID создания получен, но повторное чтение карточки не удалось."]}
            )
        if plan.kind == "remote_work" and result.workday_id is None:
            result = result.model_copy(
                update={"warnings": [*result.warnings, "Связанный рабочий день пока не появился; проверьте его позже."]}
            )
        self.journal.complete(
            plan.operation_id, plan.entity_type_id, plan.fields["xmlId"], result.model_dump(mode="json")
        )
        return result
