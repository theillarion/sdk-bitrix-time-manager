import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from bitrix_time_manager import BitrixClient, cli
from bitrix_time_manager.journal import OperationJournal
from tests.fakes import Portal


@pytest.mark.parametrize("apply", [False, True])
def test_cli_preview_and_apply(
    apply: bool, portal: Portal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BITRIX_WEBHOOK_URL", "https://example.test/rest/500/test-secret/")
    monkeypatch.setenv("BITRIX_STATE_PATH", str(tmp_path / "state.sqlite3"))
    # An enabled SDK write setting must not turn a CLI preview into a write.
    monkeypatch.setenv("BITRIX_ALLOW_WRITES", "true")
    monkeypatch.setattr(
        cli, "BitrixClient", lambda settings: BitrixClient(settings, transport=httpx.MockTransport(portal.respond))
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bitrix-time",
            "--env-file",
            str(tmp_path / "absent.env"),
            "remote",
            "--date",
            "2026-10-09",
            "--from",
            "09:00",
            "--to",
            "18:00",
            *(["--apply"] if apply else []),
        ],
    )
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out)["status"] == ("created" if apply else "preview")
    assert len(portal.adds) == int(apply)


def test_cli_configuration_error_is_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BITRIX_WEBHOOK_URL", "http://example.test/rest/500/test-secret/")
    monkeypatch.setattr(sys, "argv", ["bitrix-time", "--env-file", str(tmp_path / "absent.env"), "check"])
    assert cli.main() == 2
    error = capsys.readouterr().err
    assert json.loads(error)["error"] == "configuration_error" and "test-secret" not in error


@pytest.fixture
def cli_context(portal: Portal, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "journal.sqlite3"
    monkeypatch.setenv("BITRIX_WEBHOOK_URL", "https://example.test/rest/500/test-secret/")
    monkeypatch.setenv("BITRIX_STATE_PATH", str(path))
    monkeypatch.setattr(
        cli, "BitrixClient", lambda settings: BitrixClient(settings, transport=httpx.MockTransport(portal.respond))
    )
    return path


@pytest.mark.parametrize(
    "arguments",
    [
        ["check"],
        ["day", "--date", "2026-10-05"],
        ["day", "--date", "2026-10-12"],
        ["entries", "--date", "2026-10-05"],
        ["remote-list", "--date", "2026-10-09"],
        ["contracts", "Пример", "--type", "1298"],
        ["journal"],
    ],
)
def test_read_commands(
    cli_context: Path,
    portal: Portal,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["bitrix-time", "--env-file", str(tmp_path / "absent.env"), *arguments])
    assert cli.main() == 0
    result = json.loads(capsys.readouterr().out)
    if arguments[0] == "check":
        assert result["schema_ok"]
    elif arguments[0] == "day":
        assert (result["id"] == 123456) if arguments[-1] == "2026-10-05" else result is None
    elif arguments[0] == "contracts":
        assert result[0]["id"] == 123
    else:
        assert result == []
    assert not portal.adds and not cli_context.exists()


@pytest.mark.parametrize("apply", [False, True])
@pytest.mark.parametrize("kind", ["workday", "entry"])
def test_other_create_commands(
    cli_context: Path,
    portal: Portal,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    apply: bool,
    kind: str,
) -> None:
    arguments = (
        ["workday", "--date", "2026-10-12", "--from", "09:00", "--to", "18:00"]
        if kind == "workday"
        else [
            "entry",
            "--date",
            "2026-10-05",
            "--contract-type",
            "1298",
            "--contract-id",
            "123",
            "--description",
            "Task",
            "--hours",
            "7",
            "--minutes",
            "30",
        ]
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bitrix-time",
            "--env-file",
            str(tmp_path / "absent.env"),
            *arguments,
            "--idempotency-key",
            "cli-run",
            *(["--apply"] if apply else []),
        ],
    )
    assert cli.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == ("created" if apply else "preview")
    assert result["kind"] == ("workday" if kind == "workday" else "time_entry")
    assert len(portal.adds) == int(apply)


def test_validation_failure_is_json(
    cli_context: Path,
    portal: Portal,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bitrix-time",
            "--env-file",
            str(tmp_path / "absent.env"),
            "remote",
            "--date",
            "2026-10-09",
            "--from",
            "18:00",
            "--to",
            "09:00",
            "--apply",
        ],
    )
    assert cli.main() == 2
    captured = capsys.readouterr()
    assert not captured.out and json.loads(captured.err)["error"] == "invalid_request"
    assert not portal.calls and not cli_context.exists()


def test_keyboard_interrupt_has_distinct_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def interrupt(args: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run", interrupt)
    monkeypatch.setattr(sys, "argv", ["bitrix-time", "check"])
    assert cli.main() == 130
    captured = capsys.readouterr()
    assert not captured.out and "journal" in captured.err


def test_cli_in_new_process_reads_and_resolves_persistent_journal(tmp_path: Path) -> None:
    path = tmp_path / "operations.sqlite3"
    config = tmp_path / "test.env"
    config.write_text(f"BITRIX_WEBHOOK_URL=https://example.test/rest/500/test-secret/\nBITRIX_STATE_PATH={path}\n")
    journal = OperationJournal(path)
    journal.reserve("operation", 1250, "xml-id")

    def run(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "bitrix_time_manager", "--env-file", str(config), *arguments],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            timeout=15,
        )

    before = run("journal")
    assert before.returncode == 0, before.stderr
    assert json.loads(before.stdout)[0]["state"] == "pending"
    denied = run("resolve", "operation")
    assert denied.returncode == 2 and journal.list_operations()[0]["state"] == "pending"
    resolved = run("resolve", "operation", "--confirmed-not-created")
    assert resolved.returncode == 0, resolved.stderr
    assert json.loads(resolved.stdout)["state"] == "failed"
    assert journal.list_operations()[0]["state"] == "failed"


def test_module_entrypoint_help_in_subprocess(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "bitrix_time_manager", "--help"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=15,
    )
    assert result.returncode == 0 and "remote-list" in result.stdout and not result.stderr
