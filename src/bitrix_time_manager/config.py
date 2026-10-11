"""Credential loading and verified portal field mapping."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from pydantic import SecretStr

from bitrix_time_manager.errors import ConfigurationError


@dataclass(frozen=True)
class PortalSchema:
    """Mapping verified by read-only REST calls on 2026-10-07."""

    day_type: int = 1208
    time_type: int = 1218
    remote_type: int = 1250
    day_category: int = 61
    time_category: int = 63
    remote_category: int = 73
    day_date: str = "ufCrm46_1742342657"
    day_start: str = "ufCrm46_1742341635"
    day_end: str = "ufCrm46_1742341622"
    day_employee: str = "ufCrm46_1742341577"
    day_comment: str = "ufCrm46_1742341646"
    day_source: str = "ufCrm46_1743019660"
    day_kind: str = "ufCrm46_1742341877"
    normal_day_kind_id: int = 351
    manual_day_source_id: int = 192
    time_contract: str = "ufCrm48_1742996936"
    time_description: str = "ufCrm48_1744239302"
    time_hours: str = "ufCrm48_1742996959"
    time_minutes: str = "ufCrm48_1743029063"
    minute_zero_id: int = 193
    minute_thirty_id: int = 194
    remote_start: str = "ufCrm56_1743854949"
    remote_end: str = "ufCrm56_1743854986"
    remote_comment: str = "ufCrm56_1743855000"
    remote_employee: str = "ufCrm56UserId"
    contract_types: tuple[int, ...] = (1048, 1052, 1298)


@dataclass(frozen=True)
class Settings:
    """Runtime settings. Webhook secrets are masked in object representations."""

    webhook_url: SecretStr = field(repr=False)
    timezone: str = "Europe/Moscow"
    state_path: Path = Path(".state/operations.sqlite3")
    timeout: float = 20.0
    read_retries: int = 2
    allow_writes: bool = False
    schema: PortalSchema = field(default_factory=PortalSchema)

    def __post_init__(self) -> None:
        url = self.webhook_url.get_secret_value()
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or not re.fullmatch(r"/rest/[1-9]\d*/[A-Za-z0-9_-]+/", parts.path)
            or "REPLACE" in parts.path.upper()
        ):
            raise ConfigurationError("Нужен базовый HTTPS URL вебхука вида https://host/rest/user_id/secret/.")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError:
            raise ConfigurationError("Неизвестный часовой пояс BITRIX_TIMEZONE.") from None
        if not 0 < self.timeout <= 120 or not 0 <= self.read_retries <= 5:
            raise ConfigurationError("Timeout должен быть 0–120 секунд, read_retries — 0–5.")

    @property
    def user_id(self) -> int:
        """Return the webhook owner ID used for all self-service operations."""
        return int(urlsplit(self.webhook_url.get_secret_value()).path.split("/")[2])

    @property
    def portal_url(self) -> str:
        """Return the portal origin without credentials."""
        parts = urlsplit(self.webhook_url.get_secret_value())
        return f"{parts.scheme}://{parts.netloc}"

    @classmethod
    def from_env(cls, env_file: str | Path | None = ".env", *, allow_writes: bool | None = None) -> "Settings":
        """Load settings from the environment; an explicit env file never overrides it."""
        if env_file is not None:
            load_dotenv(env_file, override=False)
        webhook = os.getenv("BITRIX_WEBHOOK_URL", "").strip()
        if not webhook:
            raise ConfigurationError("Задайте BITRIX_WEBHOOK_URL в окружении или локальном .env.")
        raw_writes = os.getenv("BITRIX_ALLOW_WRITES", "false").lower()
        if raw_writes not in {"true", "false", "1", "0"}:
            raise ConfigurationError("BITRIX_ALLOW_WRITES должен быть true или false.")
        try:
            timeout = float(os.getenv("BITRIX_TIMEOUT", "20"))
            retries = int(os.getenv("BITRIX_READ_RETRIES", "2"))
        except ValueError:
            raise ConfigurationError("BITRIX_TIMEOUT и BITRIX_READ_RETRIES должны быть числами.") from None
        return cls(
            webhook_url=SecretStr(webhook),
            timezone=os.getenv("BITRIX_TIMEZONE", "Europe/Moscow"),
            state_path=Path(os.getenv("BITRIX_STATE_PATH", ".state/operations.sqlite3")),
            timeout=timeout,
            read_retries=retries,
            allow_writes=allow_writes if allow_writes is not None else raw_writes in {"true", "1"},
        )
