"""Async REST transport with read-only retries and credential redaction."""

import asyncio
import logging
from typing import Any, Self

import httpx

from bitrix_time_manager.config import Settings
from bitrix_time_manager.errors import BitrixAPIError, SchemaError, TransportError, WritesDisabledError, redact

READ_METHODS = frozenset(
    {"profile", "method.get", "crm.item.fields", "crm.item.get", "crm.item.list", "crm.category.list"}
)


class _WebhookLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


class BitrixClient:
    """REST client whose mutating methods are disabled unless explicitly enabled."""

    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self._http = httpx.AsyncClient(
            timeout=settings.timeout,
            transport=transport,
            follow_redirects=False,
            headers={"Accept": "application/json"},
        )
        for name in ("httpx", "httpcore"):
            logger = logging.getLogger(name)
            if not any(isinstance(f, _WebhookLogFilter) for f in logger.filters):
                logger.addFilter(_WebhookLogFilter())

    async def __aenter__(self) -> Self:
        """Use the client as an async context manager."""
        return self

    async def __aexit__(self, *args: object) -> None:
        """Close the HTTP connection pool."""
        await self.aclose()

    async def aclose(self) -> None:
        """Release network resources."""
        await self._http.aclose()

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call an allowed REST method; only read methods may be retried.

        Transport errors suppress the original exception to keep the secret URL
        out of tracebacks. A failed add must be reconciled by the service.
        """
        read_only = method in READ_METHODS
        if not read_only and method != "crm.item.add":
            raise WritesDisabledError("This SDK allows read and create operations only.")
        if not read_only and not self.settings.allow_writes:
            raise WritesDisabledError("Writes are disabled. Enable --apply or set BITRIX_ALLOW_WRITES=true.")
        attempts = self.settings.read_retries + 1 if read_only else 1
        for attempt in range(attempts):
            try:
                response = await self._http.post(
                    self.settings.webhook_url.get_secret_value() + method + ".json", json=params or {}
                )
            except httpx.TransportError:
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.3 * 2**attempt)
                    continue
                raise TransportError("Failed to get a response from Bitrix; check network and VPN.") from None
            if read_only and (response.status_code == 429 or response.status_code >= 500) and attempt + 1 < attempts:
                await asyncio.sleep(0.3 * 2**attempt)
                continue
            try:
                body = response.json()
            except ValueError:
                raise TransportError(f"Bitrix returned non-JSON (HTTP {response.status_code}).") from None
            if not isinstance(body, dict):
                raise TransportError("Bitrix returned invalid JSON.")
            if "error" in body:
                api_code = str(body["error"])
                if read_only and api_code == "QUERY_LIMIT_EXCEEDED" and attempt + 1 < attempts:
                    await asyncio.sleep(0.3 * 2**attempt)
                    continue
                raise BitrixAPIError(
                    api_code,
                    str(body.get("error_description", "API error")),
                    outcome_unknown=response.status_code >= 500,
                )
            if response.status_code >= 400 or response.status_code < 200 or response.status_code >= 300:
                raise TransportError(f"Unexpected Bitrix HTTP status: {response.status_code}.")
            if "result" not in body:
                raise TransportError("The Bitrix response has no result.")
            return body
        raise AssertionError("Unreachable retry state")

    async def get_item(self, entity_type_id: int, item_id: int) -> dict[str, Any]:
        """Retrieve one CRM item."""
        body = await self.call("crm.item.get", {"entityTypeId": entity_type_id, "id": item_id})
        result = body.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("item"), dict):
            raise SchemaError("crm.item.get returned an unexpected format.")
        return dict(result["item"])

    async def list_items(
        self,
        entity_type_id: int,
        *,
        filters: dict[str, Any] | None = None,
        select: list[str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Read list pages until exhaustion or a requested limit, detecting bad cursors."""
        if limit is not None and limit < 1:
            raise ValueError("limit must be positive.")
        items: list[dict[str, Any]] = []
        start = 0
        seen: set[int] = set()
        while True:
            if start in seen:
                raise SchemaError("Bitrix repeated the page cursor.")
            seen.add(start)
            params: dict[str, Any] = {
                "entityTypeId": entity_type_id,
                "filter": filters or {},
                "order": {"id": "ASC"},
                "start": start,
                "select": select if select is not None else ["*"],
            }
            body = await self.call("crm.item.list", params)
            result = body.get("result")
            if not isinstance(result, dict) or not isinstance(result.get("items"), list):
                raise SchemaError("crm.item.list returned an unexpected format.")
            page = result["items"]
            if any(not isinstance(item, dict) for item in page):
                raise SchemaError("crm.item.list returned an invalid item.")
            items.extend(page)
            if limit is not None and len(items) >= limit:
                return items[:limit]
            if "next" not in body:
                return items
            try:
                start = int(body["next"])
            except (TypeError, ValueError):
                raise SchemaError("Invalid Bitrix page cursor.") from None
