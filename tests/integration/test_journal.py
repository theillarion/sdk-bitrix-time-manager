"""Durability, locking and reservations against real SQLite files and processes."""

import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from bitrix_time_manager.errors import ConflictError, UncertainWriteError
from bitrix_time_manager.journal import OperationJournal


def test_absent_journal_read_creates_nothing(tmp_path: Path) -> None:
    path = tmp_path / "new-directory" / "operations.sqlite3"
    assert OperationJournal(path).list_operations() == []
    assert not path.parent.exists()


def test_pending_and_uncertain_survive_reopening(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "operations.sqlite3"
    assert OperationJournal(path).reserve("operation", 1250, "xml-id") is None
    with pytest.raises(UncertainWriteError):
        OperationJournal(path).reserve("operation", 1250, "xml-id")
    OperationJournal(path).set_state("operation", "uncertain")
    with pytest.raises(UncertainWriteError):
        OperationJournal(path).reserve("operation", 1250, "xml-id")
    assert OperationJournal(path).list_operations()[0]["state"] == "uncertain"


def test_completed_result_survives_reopening(tmp_path: Path) -> None:
    path = tmp_path / "operations.sqlite3"
    result = {"status": "created", "item_id": 123, "warnings": ["Проверить роботов"]}
    journal = OperationJournal(path)
    journal.reserve("operation", 1250, "xml-id")
    journal.complete("operation", 1250, "xml-id", result)
    assert OperationJournal(path).reserve("operation", 1250, "xml-id") == result
    assert OperationJournal(path).list_operations()[0]["state"] == "succeeded"


def test_request_key_binding_is_durable_and_rolls_back_conflict(tmp_path: Path) -> None:
    path = tmp_path / "operations.sqlite3"
    OperationJournal(path).bind_key("key", "first")
    OperationJournal(path).bind_key("key", "first")
    with pytest.raises(ConflictError, match="Idempotency-Key"):
        OperationJournal(path).bind_key("key", "second")
    # The failed transaction must not leave a lock or change the original binding.
    OperationJournal(path).bind_key("key", "first")
    OperationJournal(path).bind_key("another", "second")


def test_manual_release_only_allows_unknown_operations(tmp_path: Path) -> None:
    journal = OperationJournal(tmp_path / "operations.sqlite3")
    with pytest.raises(ConflictError):
        journal.confirm_not_created("absent")
    journal.reserve("operation", 1250, "xml-id")
    journal.confirm_not_created("operation")
    assert journal.list_operations()[0]["state"] == "failed"
    assert journal.reserve("operation", 1250, "xml-id") is None
    journal.complete("operation", 1250, "xml-id", {"item_id": 123})
    with pytest.raises(ConflictError):
        journal.confirm_not_created("operation")
    with pytest.raises(ValueError):
        journal.set_state("operation", "invalid")


def test_sqlite_lock_is_reported_without_overwriting_state(tmp_path: Path) -> None:
    path = tmp_path / "operations.sqlite3"
    journal = OperationJournal(path)
    journal.reserve("first", 1250, "xml-first")
    connection = sqlite3.connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(ConflictError, match="занят"):
            journal.reserve("second", 1250, "xml-second")
    finally:
        connection.rollback()
        connection.close()
    assert journal.reserve("second", 1250, "xml-second") is None
    assert len(journal.list_operations()) == 2


def test_parameterized_journal_does_not_interpret_identifiers_as_sql(tmp_path: Path) -> None:
    journal = OperationJournal(tmp_path / "operations.sqlite3")
    operation = "'; DROP TABLE operations; --"
    journal.bind_key("' OR 1=1 --", operation)
    journal.reserve(operation, 1250, "xml-id")
    assert journal.list_operations()[0]["id"] == operation
    with pytest.raises(UncertainWriteError):
        journal.reserve(operation, 1250, "xml-id")


def test_process_failure_leaves_a_durable_pending_reservation(tmp_path: Path) -> None:
    path = tmp_path / "operations.sqlite3"
    script = (
        "import sys\nfrom pathlib import Path\n"
        "from bitrix_time_manager.journal import OperationJournal\n"
        "OperationJournal(Path(sys.argv[1])).reserve('operation', 1250, 'xml-id')\n"
        "raise RuntimeError('worker crashed after reservation')\n"
    )
    result = subprocess.run([sys.executable, "-c", script, str(path)], capture_output=True, text=True, timeout=15)
    assert result.returncode != 0 and "worker crashed" in result.stderr
    assert OperationJournal(path).list_operations()[0]["state"] == "pending"
    with pytest.raises(UncertainWriteError):
        OperationJournal(path).reserve("operation", 1250, "xml-id")


def test_processes_cannot_reserve_the_same_operation_twice(tmp_path: Path) -> None:
    path = tmp_path / "operations.sqlite3"
    journal = OperationJournal(path)
    journal.bind_key("key", "operation")  # Initialize schema before competing workers.
    script = (
        "import sys\nfrom pathlib import Path\n"
        "from bitrix_time_manager.journal import OperationJournal\n"
        "from bitrix_time_manager.errors import ConflictError, UncertainWriteError\n"
        "try:\n"
        "    OperationJournal(Path(sys.argv[1])).reserve('operation', 1250, 'xml-id')\n"
        "except (ConflictError, UncertainWriteError):\n"
        "    print('blocked')\n"
        "else:\n"
        "    print('reserved')\n"
    )

    def reserve_in_process(index: int) -> str:
        result = subprocess.run([sys.executable, "-c", script, str(path)], capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(reserve_in_process, range(4)))
    assert results.count("reserved") == 1 and results.count("blocked") == 3
    assert len(journal.list_operations()) == 1
