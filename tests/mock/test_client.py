import json
import logging
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

from bitrix_time_manager import BitrixClient, Settings
from bitrix_time_manager.errors import BitrixAPIError, SchemaError, TransportError, WritesDisabledError


@pytest.mark.asyncio
async def test_read_retries_and_pagination(settings: Settings, no_retry_delay: AsyncMock) -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"error": "BUSY"})
        page = json.loads(request.content)
        assert page["select"] == ["*"]
        if page["start"] == 0:
            return httpx.Response(200, json={"result": {"items": [{"id": 1}]}, "next": 50})
        return httpx.Response(200, json={"result": {"items": [{"id": 2}]}})

    async with BitrixClient(replace(settings, read_retries=1), transport=httpx.MockTransport(respond)) as client:
        assert await client.list_items(1208) == [{"id": 1}, {"id": 2}]
    assert calls == 3
    no_retry_delay.assert_awaited_once_with(0.3)


@pytest.mark.asyncio
async def test_write_never_retried_and_secrets_redacted(settings: Settings, caplog: pytest.LogCaptureFixture) -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout(f"failed {request.url}", request=request)

    async with BitrixClient(replace(settings, read_retries=5), transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(TransportError) as error:
            await client.call("crm.item.add", {"fields": {}})
        assert "test-secret" not in str(error.value) and error.value.__suppress_context__
    assert calls == 1
    with caplog.at_level(logging.INFO, logger="httpx"):
        logging.getLogger("httpx").info("URL %s", settings.webhook_url.get_secret_value())
    assert "test-secret" not in caplog.text and "***" in caplog.text


@pytest.mark.asyncio
async def test_cursor_loop_and_unknown_methods(settings: Settings) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": {"items": []}, "next": 0})

    async with BitrixClient(settings, transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(SchemaError, match="курсор"):
            await client.list_items(1208)
        with pytest.raises(WritesDisabledError):
            await client.call("crm.item.delete", {"id": 1})


@pytest.mark.parametrize("failure", ["timeout", "http_429", "http_500", "api_limit"])
async def test_read_retries_transient_failure(settings: Settings, no_retry_delay: AsyncMock, failure: str) -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("temporary", request=request)
            if failure == "api_limit":
                return httpx.Response(200, json={"error": "QUERY_LIMIT_EXCEEDED"})
            return httpx.Response(429 if failure == "http_429" else 500, json={"error": "BUSY"})
        return httpx.Response(200, json={"result": {"ID": "500"}})

    async with BitrixClient(replace(settings, read_retries=2), transport=httpx.MockTransport(respond)) as client:
        assert (await client.call("profile"))["result"]["ID"] == "500"
    assert calls == 2
    no_retry_delay.assert_awaited_once_with(0.3)


async def test_read_retry_budget_and_backoff(settings: Settings, no_retry_delay: AsyncMock) -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("no connection", request=request)

    async with BitrixClient(replace(settings, read_retries=2), transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(TransportError):
            await client.call("profile")
    assert calls == 3
    assert [call.args for call in no_retry_delay.await_args_list] == [(0.3,), (0.6,)]


async def test_api_limit_has_a_bounded_retry_budget(settings: Settings, no_retry_delay: AsyncMock) -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"error": "QUERY_LIMIT_EXCEEDED"})

    async with BitrixClient(replace(settings, read_retries=2), transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(BitrixAPIError) as caught:
            await client.call("profile")
    assert calls == 3 and no_retry_delay.await_count == 2
    assert caught.value.api_code == "QUERY_LIMIT_EXCEEDED" and not caught.value.outcome_unknown


async def test_offline_guard_rejects_real_transport(settings: Settings) -> None:
    async with BitrixClient(settings) as client:
        with pytest.raises(AssertionError, match="Real HTTP is forbidden"):
            await client.call("profile")


@pytest.mark.parametrize(
    ("status", "content", "message"),
    [
        (200, b"not JSON", "не JSON"),
        (200, b"[]", "некорректный JSON"),
        (200, b"null", "некорректный JSON"),
        (200, b"{}", "не содержит result"),
        (302, b'{"result": {}}', "HTTP-статус"),
        (400, b'{"result": {}}', "HTTP-статус"),
    ],
)
async def test_invalid_responses(settings: Settings, status: int, content: bytes, message: str) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(status, content=content))
    async with BitrixClient(settings, transport=transport) as client:
        with pytest.raises(TransportError, match=message):
            await client.call("profile")


@pytest.mark.parametrize("status", [400, 500])
async def test_api_error_metadata_and_single_write(settings: Settings, status: int) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, json={"error": "REJECTED", "error_description": "failed /rest/500/secret/"})

    async with BitrixClient(replace(settings, read_retries=5), transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(BitrixAPIError) as caught:
            await client.call("crm.item.add", {"entityTypeId": 1250, "fields": {}})
    assert len(requests) == 1 and "secret" not in str(caught.value)
    assert caught.value.api_code == "REJECTED" and caught.value.outcome_unknown == (status == 500)


@pytest.mark.parametrize("method", ["crm.item.add", "crm.item.update", "batch", "timeman.open"])
async def test_forbidden_calls_do_not_reach_transport(settings: Settings, method: str) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"result": {}})

    async with BitrixClient(replace(settings, allow_writes=False), transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(WritesDisabledError):
            await client.call(method)
    assert requests == []


@pytest.mark.parametrize("result", [None, [], {}, {"item": []}, {"item": None}])
async def test_malformed_get_response(settings: Settings, result: object) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"result": result}))
    async with BitrixClient(settings, transport=transport) as client:
        with pytest.raises(SchemaError):
            await client.get_item(1208, 1)


@pytest.mark.parametrize("result", [None, [], {}, {"items": {}}, {"items": [None]}])
async def test_malformed_list_response(settings: Settings, result: object) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"result": result}))
    async with BitrixClient(settings, transport=transport) as client:
        with pytest.raises(SchemaError):
            await client.list_items(1208)


@pytest.mark.parametrize("cursor", [None, "invalid", {}])
async def test_invalid_page_cursor(settings: Settings, cursor: object) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"result": {"items": []}, "next": cursor}))
    async with BitrixClient(settings, transport=transport) as client:
        with pytest.raises(SchemaError, match="курсор"):
            await client.list_items(1208)


async def test_select_limit_and_early_stop(settings: Settings) -> None:
    requests: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"result": {"items": [{"id": 1}, {"id": 2}]}, "next": 50})

    async with BitrixClient(settings, transport=httpx.MockTransport(respond)) as client:
        assert await client.list_items(1208, filters={"assignedById": 500}, select=["id"], limit=1) == [{"id": 1}]
        with pytest.raises(ValueError):
            await client.list_items(1208, limit=0)
    assert len(requests) == 1
    assert requests[0]["select"] == ["id"] and requests[0]["filter"] == {"assignedById": 500}


async def test_client_closes_transport_on_exception(settings: Settings) -> None:
    class ClosingTransport(httpx.AsyncBaseTransport):
        closed = False

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"result": {"ID": "500"}})

        async def aclose(self) -> None:
            self.closed = True

    transport = ClosingTransport()
    with pytest.raises(RuntimeError, match="handler failed"):
        async with BitrixClient(settings, transport=transport) as client:
            await client.call("profile")
            raise RuntimeError("handler failed")
    assert transport.closed
