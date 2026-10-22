import asyncio
from dataclasses import replace
from datetime import date

import httpx
import pytest
from pydantic import SecretStr

from bitrix_time_manager import BitrixClient, RemoteWorkCreate, Settings, TimeEntryCreate, TimeManager
from bitrix_time_manager.errors import (
    BitrixAPIError,
    ConflictError,
    NotFoundError,
    SchemaError,
    UncertainWriteError,
    WritesDisabledError,
)
from tests.fakes import Portal


def remote(**updates: object) -> RemoteWorkCreate:
    return RemoteWorkCreate.model_validate({"day": "2026-10-09", "start": "09:00", "end": "18:00", **updates})


def entry(**updates: object) -> TimeEntryCreate:
    return TimeEntryCreate.model_validate(
        {
            "day": "2026-10-05",
            "contract": {"entity_type_id": 1298, "item_id": 123},
            "description": "Работа по задаче",
            "hours": 8,
            **updates,
        }
    )


@pytest.mark.asyncio
async def test_readonly_check_date_and_preview(service: TimeManager, portal: Portal, settings: Settings) -> None:
    checked = await service.check_connection()
    assert checked["schema_ok"] and checked["user_id"] == 500
    day = await service.get_workday(date(2026, 10, 5))
    assert day and day.id == 123456
    contracts = await service.search_contracts("Пример")
    assert [(item.id, item.title, item.entity_type_id) for item in contracts] == [(123, "Пример карточки", 1298)]
    plan = await service.create_remote_work(remote())
    assert plan.status == "preview" and plan.item_id is None
    assert plan.fields[service.schema.remote_start] == "2026-10-09T09:00:00+03:00"
    assert "parentId1208" not in plan.fields and service.schema.remote_employee not in plan.fields
    assert not portal.adds and not settings.state_path.exists()


@pytest.mark.asyncio
async def test_time_entry_encoding_and_repeat(service: TimeManager, portal: Portal) -> None:
    spec = entry(hours=7, minutes=30)
    result = await service.create_time_entry(spec, dry_run=False, idempotency_key="task:1")
    assert result.status == "created" and result.workday_id == 123456
    fields = portal.adds[0]["fields"]
    assert fields[service.schema.time_contract] == "T512_123"
    assert fields[service.schema.time_minutes] == 194 and fields["parentId1208"] == 123456
    assert fields["categoryId"] == 63
    assert (await service.create_time_entry(spec, dry_run=False, idempotency_key="task:1")).status == "existing"
    assert len(portal.adds) == 1


@pytest.mark.parametrize("key", ["", "x" * 201])
async def test_invalid_request_key_never_writes(service: TimeManager, portal: Portal, key: str) -> None:
    with pytest.raises(ValueError, match="Idempotency-Key"):
        await service.create_remote_work(remote(), dry_run=False, idempotency_key=key)
    assert not portal.adds and service.journal.list_operations() == []


async def test_missing_day_list_is_an_error(service: TimeManager) -> None:
    with pytest.raises(NotFoundError):
        await service.list_time_entries(date(2026, 10, 12))


async def test_different_work_on_same_day_is_not_a_duplicate(service: TimeManager, portal: Portal) -> None:
    first = await service.create_time_entry(entry(description="First task"), dry_run=False, idempotency_key="task-one")
    second = await service.create_time_entry(
        entry(description="Second task"), dry_run=False, idempotency_key="task-two"
    )
    assert first.status == second.status == "created" and first.item_id != second.item_id
    again = await service.create_time_entry(entry(description="Second task"), dry_run=False, idempotency_key="task-two")
    assert again.status == "existing" and again.item_id == second.item_id
    assert len(portal.adds) == 2


@pytest.mark.parametrize(
    ("query", "type_id", "limit"), [(" ", 1298, 20), ("Task", 1208, 20), ("Task", 1298, 0), ("Task", 1298, 101)]
)
async def test_invalid_contract_search(
    service: TimeManager, portal: Portal, query: str, type_id: int, limit: int
) -> None:
    with pytest.raises(ValueError):
        await service.search_contracts(query, entity_type_id=type_id, limit=limit)
    assert portal.calls == []


async def test_unknown_contract_does_not_write(service: TimeManager, portal: Portal) -> None:
    spec = entry(contract={"entity_type_id": 1298, "item_id": 999})
    with pytest.raises(BitrixAPIError):
        await service.create_time_entry(spec, dry_run=False)
    assert not portal.adds and service.journal.list_operations() == []


async def test_duplicate_xml_id_is_a_conflict(service: TimeManager, portal: Portal) -> None:
    await service.create_remote_work(remote(), dry_run=False)
    portal.items[1250].append({**portal.items[1250][0], "id": 9999})
    with pytest.raises(ConflictError, match="Multiple records"):
        await service.create_remote_work(remote(), dry_run=False)
    assert len(portal.adds) == 1


async def test_changed_duplicate_is_not_overwritten(service: TimeManager, portal: Portal) -> None:
    await service.create_remote_work(remote(), dry_run=False)
    portal.items[1250][0][service.schema.remote_comment] = "changed on portal"
    with pytest.raises(ConflictError, match="fields changed"):
        await service.create_remote_work(remote(), dry_run=False)
    assert len(portal.adds) == 1


async def test_verification_read_failure_preserves_known_success(settings: Settings, portal: Portal) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("crm.item.get.json"):
            raise httpx.ReadTimeout("verification lost", request=request)
        return portal.respond(request)

    async with BitrixClient(settings, transport=httpx.MockTransport(respond)) as client:
        manager = TimeManager(client)
        result = await manager.create_remote_work(remote(), dry_run=False)
        assert result.status == "created" and result.item_id is not None
        assert any("re-reading the item failed" in warning for warning in result.warnings)
        assert manager.journal.list_operations()[0]["state"] == "succeeded"
        assert (await manager.create_remote_work(remote(), dry_run=False)).status == "existing"
    assert len(portal.adds) == 1


async def test_portal_changes_after_add_are_reported(settings: Settings, portal: Portal) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        response = portal.respond(request)
        if request.url.path.endswith("crm.item.add.json"):
            portal.items[1250][-1][settings.schema.remote_comment] = "robot changed it"
            portal.items[1250][-1]["parentId1208"] = 123
        return response

    async with BitrixClient(settings, transport=httpx.MockTransport(respond)) as client:
        manager = TimeManager(client)
        result = await manager.create_remote_work(remote(), dry_run=False)
        assert result.status == "created" and result.workday_id == 123
        assert any("fields differ from the request" in warning for warning in result.warnings)
        assert manager.journal.list_operations()[0]["state"] == "succeeded"
    assert len(portal.adds) == 1


async def test_server_error_is_unknown_and_blocks_replay(settings: Settings, portal: Portal) -> None:
    add_count = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal add_count
        if request.url.path.endswith("crm.item.add.json"):
            add_count += 1
            return httpx.Response(500, json={"error": "INTERNAL", "error_description": "outcome unknown"})
        return portal.respond(request)

    async with BitrixClient(settings, transport=httpx.MockTransport(respond)) as client:
        manager = TimeManager(client)
        for _ in range(2):
            with pytest.raises(UncertainWriteError):
                await manager.create_remote_work(remote(), dry_run=False)
        assert manager.journal.list_operations()[0]["state"] == "uncertain"
    assert add_count == 1


async def test_manual_duplicate_accepts_equivalent_utc_timestamp(service: TimeManager, portal: Portal) -> None:
    s = service.schema
    portal.items[1250] = [
        {
            "id": 123,
            "assignedById": 500,
            s.remote_start: "2026-10-09T06:00:00Z",
            s.remote_end: "2026-10-09T15:00:00Z",
            s.remote_comment: "",
        }
    ]
    result = await service.create_remote_work(remote(), dry_run=False)
    assert result.status == "existing" and result.item_id == 123 and not portal.adds


async def test_manual_entry_duplicate_accepts_numeric_hours(service: TimeManager, portal: Portal) -> None:
    s = service.schema
    portal.items[1218] = [
        {
            "id": 321,
            "parentId1208": 123456,
            "categoryId": s.time_category,
            s.time_contract: "T512_123",
            s.time_description: "  Работа по задаче  ",
            s.time_hours: "8.0",
            s.time_minutes: str(s.minute_zero_id),
        }
    ]
    result = await service.create_time_entry(entry(), dry_run=False)
    assert result.status == "existing" and result.item_id == 321 and not portal.adds


async def test_invalid_numeric_hours_on_portal_is_schema_error(service: TimeManager, portal: Portal) -> None:
    s = service.schema
    portal.items[1218] = [
        {
            "id": 321,
            "parentId1208": 123456,
            "categoryId": s.time_category,
            s.time_contract: "T512_123",
            s.time_description: "Работа по задаче",
            s.time_hours: "not a number",
            s.time_minutes: s.minute_zero_id,
        }
    ]
    with pytest.raises(SchemaError, match="numeric duration"):
        await service.create_time_entry(entry(), dry_run=False)
    assert not portal.adds


async def test_nonexistent_local_time_is_rejected_before_add(settings: Settings, portal: Portal) -> None:
    async with BitrixClient(
        replace(settings, timezone="Europe/Berlin"), transport=httpx.MockTransport(portal.respond)
    ) as client:
        with pytest.raises(ValueError, match="does not exist"):
            await TimeManager(client).create_remote_work(
                remote(day="2026-03-29", start="02:30", end="04:00"), dry_run=False
            )
    assert not portal.adds


@pytest.mark.parametrize("separate_portal", [False, True])
async def test_clients_share_journal_without_mixing_operations(
    settings: Settings, portal: Portal, separate_portal: bool
) -> None:
    url = (
        "https://other.test/rest/500/other-secret/"
        if separate_portal
        else "https://example.test/rest/501/other-secret/"
    )
    second_portal = Portal()
    async with (
        BitrixClient(settings, transport=httpx.MockTransport(portal.respond)) as first,
        BitrixClient(
            replace(settings, webhook_url=SecretStr(url)), transport=httpx.MockTransport(second_portal.respond)
        ) as second,
    ):
        a = TimeManager(first)
        b = TimeManager(second)
        results = await asyncio.gather(
            a.create_remote_work(remote(), dry_run=False, idempotency_key="same-key"),
            b.create_remote_work(remote(), dry_run=False, idempotency_key="same-key"),
        )
        assert all(result.status == "created" for result in results)
        assert results[0].operation_id != results[1].operation_id
        assert len(a.journal.list_operations()) == 2
        assert (await a.create_remote_work(remote(), dry_run=False, idempotency_key="same-key")).status == "existing"
        assert (await b.create_remote_work(remote(), dry_run=False, idempotency_key="same-key")).status == "existing"
    assert len(portal.adds) == len(second_portal.adds) == 1


async def test_new_client_reconciles_lost_response_after_restart(settings: Settings, portal: Portal) -> None:
    portal.lose_add_response = True
    async with BitrixClient(settings, transport=httpx.MockTransport(portal.respond)) as client:
        with pytest.raises(UncertainWriteError):
            await TimeManager(client).create_remote_work(remote(), dry_run=False, idempotency_key="persistent-run")
    portal.lose_add_response = False
    async with BitrixClient(settings, transport=httpx.MockTransport(portal.respond)) as client:
        manager = TimeManager(client)
        result = await manager.create_remote_work(remote(), dry_run=False, idempotency_key="persistent-run")
        assert result.status == "existing" and manager.journal.list_operations()[0]["state"] == "succeeded"
    assert len(portal.adds) == 1


@pytest.mark.asyncio
async def test_missing_day_does_not_create_implicitly(service: TimeManager, portal: Portal) -> None:
    with pytest.raises(NotFoundError):
        await service.create_time_entry(entry(day="2026-10-12"), dry_run=False)
    assert not portal.adds


@pytest.mark.asyncio
async def test_remote_overlap_and_nonoverlap(service: TimeManager, portal: Portal) -> None:
    first = await service.create_remote_work(remote(start="09:00", end="12:00"), dry_run=False)
    assert first.status == "created"
    assert (await service.create_remote_work(remote(start="09:00", end="12:00"), dry_run=False)).status == "existing"
    with pytest.raises(ConflictError):
        await service.create_remote_work(remote(start="11:00", end="15:00"), dry_run=False)
    next_interval = await service.create_remote_work(remote(start="12:00", end="18:00"), dry_run=False)
    assert next_interval.status == "created" and len(portal.adds) == 2


@pytest.mark.asyncio
async def test_uncertain_write_recovers_from_server_without_retry(service: TimeManager, portal: Portal) -> None:
    portal.lose_add_response = True
    with pytest.raises(UncertainWriteError):
        await service.create_remote_work(remote(), dry_run=False)
    assert len(portal.adds) == 1
    assert service.journal.list_operations()[0]["state"] == "uncertain"
    result = await service.create_remote_work(remote(), dry_run=False)
    assert result.status == "existing" and len(portal.adds) == 1
    assert service.journal.list_operations()[0]["state"] == "succeeded"


@pytest.mark.asyncio
async def test_uncertain_and_deleted_records_not_recreated(service: TimeManager, portal: Portal) -> None:
    portal.lose_add_response = True
    with pytest.raises(UncertainWriteError):
        await service.create_remote_work(remote(), dry_run=False)
    portal.items[1250].clear()
    with pytest.raises(UncertainWriteError):
        await service.create_remote_work(remote(), dry_run=False)
    assert len(portal.adds) == 1
    operation = service.journal.list_operations()[0]["id"]
    service.journal.confirm_not_created(operation)
    portal.lose_add_response = False
    await service.create_remote_work(remote(), dry_run=False)
    portal.items[1250].clear()
    with pytest.raises(ConflictError):
        await service.create_remote_work(remote(), dry_run=False)
    assert len(portal.adds) == 2


@pytest.mark.asyncio
async def test_reusing_key_for_other_payload_is_conflict(service: TimeManager, portal: Portal) -> None:
    await service.create_remote_work(remote(), dry_run=False, idempotency_key="job:1")
    with pytest.raises(ConflictError, match="Idempotency-Key"):
        await service.create_remote_work(remote(day="2026-10-12"), dry_run=False, idempotency_key="job:1")
    assert len(portal.adds) == 1


@pytest.mark.asyncio
async def test_write_gate_and_schema_drift(settings: Settings, portal: Portal) -> None:
    async with BitrixClient(
        replace(settings, allow_writes=False), transport=httpx.MockTransport(portal.respond)
    ) as client:
        with pytest.raises(WritesDisabledError):
            await TimeManager(client).create_remote_work(remote(), dry_run=False)
    portal.schema_drift = True
    async with BitrixClient(settings, transport=httpx.MockTransport(portal.respond)) as client:
        with pytest.raises(SchemaError):
            await TimeManager(client).create_remote_work(remote(), dry_run=False)
    assert not portal.adds


@pytest.mark.asyncio
async def test_rejected_add_and_malformed_result(service: TimeManager, portal: Portal) -> None:
    portal.reject_add = True
    with pytest.raises(BitrixAPIError):
        await service.create_remote_work(remote(), dry_run=False)
    assert service.journal.list_operations()[0]["state"] == "failed"
    portal.reject_add = False
    portal.malformed_add = True
    with pytest.raises(UncertainWriteError):
        await service.create_remote_work(remote(), dry_run=False)
    assert service.journal.list_operations()[0]["state"] == "uncertain"


@pytest.mark.asyncio
async def test_concurrent_same_operation_single_add(settings: Settings, portal: Portal) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("crm.item.add.json"):
            entered.set()
            await release.wait()
        return portal.respond(request)

    async with BitrixClient(settings, transport=httpx.MockTransport(respond)) as client:
        first = TimeManager(client)
        second = TimeManager(client)
        task = asyncio.create_task(first.create_remote_work(remote(), dry_run=False))
        await asyncio.wait_for(entered.wait(), timeout=5)
        with pytest.raises(UncertainWriteError):
            await second.create_remote_work(remote(), dry_run=False)
        release.set()
        assert (await task).status == "created"
    assert len(portal.adds) == 1


@pytest.mark.asyncio
async def test_cancel_write_preserves_cancellation_and_blocks_retry(settings: Settings, portal: Portal) -> None:
    entered = asyncio.Event()

    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("crm.item.add.json"):
            entered.set()
            await asyncio.Event().wait()
        return portal.respond(request)

    async with BitrixClient(settings, transport=httpx.MockTransport(respond)) as client:
        service = TimeManager(client)
        task = asyncio.create_task(service.create_remote_work(remote(), dry_run=False))
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert service.journal.list_operations()[0]["state"] == "uncertain"
        with pytest.raises(UncertainWriteError):
            await service.create_remote_work(remote(), dry_run=False)


@pytest.mark.asyncio
async def test_manual_remote_duplicate_and_unfinished_interval(service: TimeManager, portal: Portal) -> None:
    s = service.schema
    portal.items[1250] = [
        {
            "id": 123,
            "title": "Manual",
            "assignedById": 500,
            "categoryId": 73,
            s.remote_start: "2026-10-09T09:00:00+03:00",
            s.remote_end: "2026-10-09T18:00:00+03:00",
            s.remote_comment: "",
        }
    ]
    assert (await service.create_remote_work(remote(), dry_run=False)).item_id == 123
    assert not portal.adds
    portal.items[1250][0][s.remote_end] = None
    with pytest.raises(ConflictError):
        await service.create_remote_work(remote(), dry_run=False)
    assert not portal.adds


@pytest.mark.asyncio
async def test_known_id_survives_cancelled_verification(settings: Settings, portal: Portal) -> None:
    verify_started = asyncio.Event()

    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("crm.item.get.json"):
            verify_started.set()
            await asyncio.Event().wait()
        return portal.respond(request)

    async with BitrixClient(settings, transport=httpx.MockTransport(respond)) as client:
        service = TimeManager(client)
        task = asyncio.create_task(service.create_remote_work(remote(), dry_run=False))
        await asyncio.wait_for(verify_started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert service.journal.list_operations()[0]["state"] == "succeeded"
        assert (await service.create_remote_work(remote(), dry_run=False)).status == "existing"
    assert len(portal.adds) == 1
