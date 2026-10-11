"""Service handling of portal ownership, schema drift and malformed responses."""

from datetime import date
from unittest.mock import AsyncMock

import httpx
import pytest

from bitrix_time_manager import BitrixClient, RemoteWorkCreate, Settings, TimeEntryCreate, TimeManager
from bitrix_time_manager.errors import ConflictError, SchemaError
from tests.fakes import Portal


@pytest.mark.parametrize("profile", [{"ID": "501"}, None, []])
async def test_connection_rejects_wrong_or_missing_owner(settings: Settings, profile: object) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"result": profile})

    async with BitrixClient(settings, transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(SchemaError, match="different user"):
            await TimeManager(client).check_connection()
    assert len(requests) == 1


@pytest.mark.parametrize("result", [None, {}, {"fields": []}])
async def test_unreadable_schema(settings: Settings, result: object) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"result": result}))
    async with BitrixClient(settings, transport=transport) as client:
        spec = RemoteWorkCreate.model_validate({"day": "2026-10-09", "start": "09:00", "end": "18:00"})
        with pytest.raises(SchemaError, match="Failed to read smart-process fields"):
            await TimeManager(client).create_remote_work(spec)


@pytest.mark.parametrize("change", ["type", "read_only", "minutes", "contracts"])
async def test_schema_changes_block_creation(
    service: TimeManager, portal: Portal, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    original = portal.fields
    s = service.schema

    def fields(type_id: int) -> dict[str, object]:
        value = original(type_id)
        if type_id == s.time_type:
            if change == "type":
                value[s.time_hours]["type"] = "string"
            elif change == "read_only":
                value[s.time_hours]["isReadOnly"] = True
            elif change == "minutes":
                value[s.time_minutes]["items"][1]["VALUE"] = "15"
            else:
                value[s.time_contract]["settings"]["DYNAMIC_1298"] = "N"
        return value

    monkeypatch.setattr(portal, "fields", fields)
    spec = TimeEntryCreate.model_validate(
        {
            "day": "2026-10-05",
            "contract": {"entity_type_id": 1298, "item_id": 123},
            "description": "Task",
            "hours": 8,
        }
    )
    with pytest.raises(SchemaError):
        await service.create_time_entry(spec, dry_run=False)
    assert not portal.adds and not service.settings.state_path.exists()


async def test_ambiguous_workday_is_rejected(service: TimeManager, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service.client, "list_items", AsyncMock(return_value=[{"id": 1}, {"id": 2}]))
    with pytest.raises(ConflictError, match="Multiple workdays"):
        await service.get_workday(date(2026, 10, 5))


@pytest.mark.parametrize("wrong", ["date", "employee"])
async def test_workday_filter_mismatch_is_rejected(
    service: TimeManager, portal: Portal, monkeypatch: pytest.MonkeyPatch, wrong: str
) -> None:
    item = dict(portal.items[1208][0])
    item[service.schema.day_date if wrong == "date" else service.schema.day_employee] = (
        "2026-10-06" if wrong == "date" else 501
    )
    monkeypatch.setattr(service.client, "list_items", AsyncMock(return_value=[item]))
    with pytest.raises(SchemaError, match="match the filter"):
        await service.get_workday(date(2026, 10, 5))


async def test_schema_is_cached_per_service(service: TimeManager, portal: Portal) -> None:
    spec = RemoteWorkCreate.model_validate({"day": "2026-10-09", "start": "09:00", "end": "18:00"})
    await service.create_remote_work(spec)
    await service.create_remote_work(spec)
    calls = [payload for method, payload in portal.calls if method == "crm.item.fields"]
    assert len(calls) == 1 and not portal.adds


async def test_invalid_existing_timestamp_is_conflict(service: TimeManager, portal: Portal) -> None:
    s = service.schema
    portal.items[1250] = [
        {
            "id": 123,
            "assignedById": 500,
            s.remote_start: "2026-10-09T09:bad",
            s.remote_end: None,
            s.remote_comment: "",
        }
    ]
    spec = RemoteWorkCreate.model_validate({"day": "2026-10-09", "start": "09:00", "end": "18:00"})
    with pytest.raises(ConflictError):
        await service.create_remote_work(spec)
    assert not portal.adds
