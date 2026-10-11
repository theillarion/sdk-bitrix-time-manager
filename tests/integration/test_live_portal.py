"""Opt-in, read-only checks of the supported portal's actual REST contract."""

import os
from collections.abc import AsyncIterator
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import SecretStr

from bitrix_time_manager import BitrixClient, Settings, TimeManager
from bitrix_time_manager.errors import NotFoundError

pytestmark = pytest.mark.live


@pytest.fixture
async def live_service(tmp_path: Path) -> AsyncIterator[TimeManager]:
    webhook = os.getenv("BITRIX_TEST_WEBHOOK_URL", "").strip()
    if not webhook:
        pytest.skip("Set BITRIX_TEST_WEBHOOK_URL explicitly to enable read-only portal tests.")
    settings = Settings(
        webhook_url=SecretStr(webhook),
        timezone=os.getenv("BITRIX_TEST_TIMEZONE", "Europe/Moscow"),
        state_path=tmp_path / "unused-journal.sqlite3",
        allow_writes=False,
    )
    async with BitrixClient(settings) as client:
        yield TimeManager(client)


@pytest.fixture
def live_day() -> date:
    raw = os.getenv("BITRIX_TEST_DAY")
    if raw:
        return date.fromisoformat(raw)
    return datetime.now(ZoneInfo(os.getenv("BITRIX_TEST_TIMEZONE", "Europe/Moscow"))).date()


async def test_live_connection_and_schema(live_service: TimeManager) -> None:
    result = await live_service.check_connection()
    assert result["schema_ok"] and result["user_id"] == live_service.settings.user_id
    assert not result["writes_enabled"] and not live_service.settings.state_path.exists()


async def test_live_own_workday_and_entries(live_service: TimeManager, live_day: date) -> None:
    day = await live_service.get_workday(live_day)
    if day is None:
        with pytest.raises(NotFoundError):
            await live_service.list_time_entries(live_day)
    else:
        assert day.employee_id == live_service.settings.user_id and day.day == live_day
        entries = await live_service.list_time_entries(live_day)
        assert all(int(item["parentId1208"]) == day.id for item in entries)
    assert not live_service.settings.state_path.exists()


async def test_live_remote_intervals_and_contract_search(live_service: TimeManager, live_day: date) -> None:
    intervals = await live_service.list_remote_work(live_day)
    assert all(int(item["assignedById"]) == live_service.settings.user_id for item in intervals)
    contracts = await live_service.search_contracts(os.getenv("BITRIX_TEST_CONTRACT_QUERY", "Пример"))
    assert all(item.id > 0 and item.entity_type_id == 1298 for item in contracts)
    assert not live_service.settings.state_path.exists()
