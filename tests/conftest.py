"""Shared fixtures; offline tests cannot access the real portal."""

from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr

from bitrix_time_manager import BitrixClient, Settings, TimeManager
from bitrix_time_manager.config import PortalSchema
from tests.fakes import Portal


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Give each test exactly one level based on its directory."""
    for item in items:
        for level in ("unit", "mock", "integration"):
            if level in item.path.parts:
                item.add_marker(getattr(pytest.mark, level))
                break


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Do not inherit SDK credentials or write permissions from the shell."""
    for name in (
        "BITRIX_WEBHOOK_URL",
        "BITRIX_TIMEZONE",
        "BITRIX_STATE_PATH",
        "BITRIX_TIMEOUT",
        "BITRIX_READ_RETRIES",
        "BITRIX_ALLOW_WRITES",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Block real HTTP transports, except for explicitly selected live tests."""
    if request.node.get_closest_marker("live") is not None:
        return

    def reject(*args: object, **kwargs: object) -> None:
        raise AssertionError("Real HTTP is forbidden in offline tests; use MockTransport.")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", reject)


@pytest.fixture
def no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Keep transport retry tests fast without skipping the retry path."""
    sleep = AsyncMock()
    monkeypatch.setattr("bitrix_time_manager.client.asyncio.sleep", sleep)
    return sleep


@pytest.fixture
def portal() -> Portal:
    s = PortalSchema()
    return Portal(
        items={
            1208: [
                {
                    "id": 123456,
                    "title": "Workday",
                    "categoryId": 61,
                    "assignedById": 500,
                    s.day_date: "2026-10-05T03:00:00+03:00",
                    s.day_employee: 500,
                    s.day_start: "2026-10-05T09:00:00+03:00",
                    s.day_end: "2026-10-05T18:00:00+03:00",
                    s.day_comment: "",
                    s.day_kind: 351,
                }
            ],
            1298: [{"id": 123, "title": "Пример карточки"}],
        }
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        webhook_url=SecretStr("https://example.test/rest/500/test-secret/"),
        state_path=tmp_path / "operations.sqlite3",
        allow_writes=True,
        read_retries=0,
    )


@pytest.fixture
async def service(settings: Settings, portal: Portal) -> AsyncIterator[TimeManager]:
    async with BitrixClient(settings, transport=httpx.MockTransport(portal.respond)) as client:
        yield TimeManager(client)
