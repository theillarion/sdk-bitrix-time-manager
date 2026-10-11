"""Configuration boundaries and environment loading without I/O to Bitrix."""

from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import SecretStr

from bitrix_time_manager import Settings
from bitrix_time_manager.errors import ConfigurationError


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/rest/0/secret/",
        "https://example.test/rest/500/secret",
        "https://name:password@example.test/rest/500/secret/",
        "https://example.test/rest/500/secret/?token=secret",
        "https://example.test/rest/500/secret/#fragment",
        "https://example.test/rest/500/REPLACE_LOCALLY/",
        "https:///rest/500/secret/",
    ],
)
def test_invalid_webhook(settings: Settings, url: str) -> None:
    with pytest.raises(ConfigurationError):
        replace(settings, webhook_url=SecretStr(url))


@pytest.mark.parametrize("timeout", [0, -1, 121])
def test_timeout_boundaries(settings: Settings, timeout: float) -> None:
    with pytest.raises(ConfigurationError):
        replace(settings, timeout=timeout)


@pytest.mark.parametrize("retries", [-1, 6])
def test_retry_boundaries(settings: Settings, retries: int) -> None:
    with pytest.raises(ConfigurationError):
        replace(settings, read_retries=retries)


def test_defaults_and_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BITRIX_WEBHOOK_URL", "https://example.test/rest/123/secret/")
    settings = Settings.from_env(None)
    assert settings.user_id == 123 and settings.portal_url == "https://example.test"
    assert settings.timezone == "Europe/Moscow"
    assert settings.state_path == Path(".state/operations.sqlite3")
    assert settings.timeout == 20 and settings.read_retries == 2
    assert not settings.allow_writes


def test_missing_webhook() -> None:
    with pytest.raises(ConfigurationError, match="BITRIX_WEBHOOK_URL"):
        Settings.from_env(None)


@pytest.mark.parametrize(
    ("name", "value"),
    [("BITRIX_ALLOW_WRITES", "yes"), ("BITRIX_TIMEOUT", "slow"), ("BITRIX_READ_RETRIES", "many")],
)
def test_invalid_environment(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    monkeypatch.setenv("BITRIX_WEBHOOK_URL", "https://example.test/rest/500/secret/")
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigurationError):
        Settings.from_env(None)


@pytest.mark.parametrize("value", ["true", "1", "TRUE", "false", "0"])
def test_write_setting(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("BITRIX_WEBHOOK_URL", "https://example.test/rest/500/secret/")
    monkeypatch.setenv("BITRIX_ALLOW_WRITES", value)
    assert Settings.from_env(None).allow_writes == (value.lower() in {"true", "1"})
    assert not Settings.from_env(None, allow_writes=False).allow_writes
    assert Settings.from_env(None, allow_writes=True).allow_writes


def test_env_file_does_not_override_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "test.env"
    path.write_text(
        "BITRIX_WEBHOOK_URL=https://file.test/rest/500/file-secret/\n"
        "BITRIX_TIMEOUT=10\nBITRIX_READ_RETRIES=1\nBITRIX_ALLOW_WRITES=true\n"
        "BITRIX_TIMEZONE=UTC\nBITRIX_STATE_PATH=/tmp/test-journal.sqlite3\n"
    )
    monkeypatch.setenv("BITRIX_WEBHOOK_URL", "https://process.test/rest/800/process-secret/")
    monkeypatch.setenv("BITRIX_TIMEOUT", "30")
    config = Settings.from_env(path)
    assert config.portal_url == "https://process.test" and config.user_id == 800
    assert config.timeout == 30 and config.read_retries == 1
    assert config.allow_writes and config.timezone == "UTC"
    assert config.state_path == Path("/tmp/test-journal.sqlite3")
