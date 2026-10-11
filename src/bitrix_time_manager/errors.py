"""Domain errors that do not expose credentials or raw transport exceptions."""

import re


def redact(text: str) -> str:
    """Hide a webhook secret in text, including URLs returned by Bitrix."""
    return re.sub(r"(/rest/\d+/)[^/\s\"'?#]+", r"\1***", text)


class TimeManagerError(Exception):
    """Base exception for errors safe to display to a caller."""

    code = "time_manager_error"


class ConfigurationError(TimeManagerError):
    """Configuration is missing or invalid."""

    code = "configuration_error"


class BitrixAPIError(TimeManagerError):
    """Bitrix explicitly rejected a request."""

    code = "bitrix_api_error"

    def __init__(self, api_code: str, description: str, *, outcome_unknown: bool = False) -> None:
        self.api_code = redact(api_code)
        self.outcome_unknown = outcome_unknown
        super().__init__(f"Bitrix {self.api_code}: {redact(description)}")


class TransportError(TimeManagerError):
    """The request outcome cannot be determined from the transport response."""

    code = "transport_error"


class SchemaError(TimeManagerError):
    """The portal schema or response does not match the configured mapping."""

    code = "schema_error"


class NotFoundError(TimeManagerError):
    """A requested day or related item is absent or inaccessible."""

    code = "not_found"


class ConflictError(TimeManagerError):
    """The operation conflicts with existing data or an idempotency key."""

    code = "conflict"


class WritesDisabledError(TimeManagerError):
    """An apply request was made through a client with writes disabled."""

    code = "writes_disabled"


class UncertainWriteError(TimeManagerError):
    """A write may have reached Bitrix and must be reconciled before retrying."""

    code = "uncertain_write"

    def __init__(self, operation_id: str) -> None:
        self.operation_id = operation_id
        super().__init__(
            f"Результат записи неизвестен (операция {operation_id}). "
            "Повторите тот же запрос для сверки. Если запись не найдена, сначала проверьте портал вручную; "
            "автоматическая повторная отправка заблокирована."
        )
