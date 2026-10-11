"""Local operation journal shared by the Python API and CLI."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bitrix_time_manager.errors import ConflictError, UncertainWriteError


class OperationJournal:
    """Reserve a logical operation before sending its single write request.

    SQLite serializes reservations across processes sharing the same file.
    A pending record left by a crash is treated as an unknown outcome, never
    automatically reclaimed on a timer.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=0)
        connection.row_factory = sqlite3.Row
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY, entity_type_id INTEGER NOT NULL,
                    xml_id TEXT NOT NULL, state TEXT NOT NULL,
                    result TEXT, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS request_keys (
                    key TEXT PRIMARY KEY, operation_id TEXT NOT NULL
                );
                """
            )
            yield connection
        except sqlite3.OperationalError:
            raise ConflictError("The operation journal is busy or unavailable. Check BITRIX_STATE_PATH.") from None
        finally:
            connection.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def bind_key(self, key: str, operation_id: str) -> None:
        """Bind a caller idempotency key to one immutable logical operation."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT operation_id FROM request_keys WHERE key = ?", (key,)).fetchone()
            if row is not None and row["operation_id"] != operation_id:
                raise ConflictError("This Idempotency-Key has already been used with different parameters.")
            connection.execute("INSERT OR IGNORE INTO request_keys VALUES (?, ?)", (key, operation_id))
            connection.commit()

    def reserve(self, operation_id: str, entity_type_id: int, xml_id: str) -> dict[str, Any] | None:
        """Reserve or return a completed operation; refuse pending/uncertain replays."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM operations WHERE id = ?", (operation_id,)).fetchone()
            if row is not None:
                if row["state"] == "succeeded":
                    return dict(json.loads(row["result"]))
                if row["state"] in {"pending", "uncertain"}:
                    raise UncertainWriteError(operation_id)
            connection.execute(
                "INSERT INTO operations VALUES (?, ?, ?, 'pending', NULL, ?) "
                "ON CONFLICT(id) DO UPDATE SET state='pending', result=NULL, updated_at=excluded.updated_at",
                (operation_id, entity_type_id, xml_id, self._now()),
            )
            connection.commit()
            return None

    def complete(self, operation_id: str, entity_type_id: int, xml_id: str, result: dict[str, Any]) -> None:
        """Record a response or a server-side recovery even if this DB is new."""
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO operations VALUES (?, ?, ?, 'succeeded', ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET state='succeeded', result=excluded.result, updated_at=excluded.updated_at",
                (operation_id, entity_type_id, xml_id, json.dumps(result, ensure_ascii=False), self._now()),
            )
            connection.commit()

    def set_state(self, operation_id: str, state: str) -> None:
        """Mark a known rejection or an ambiguous network outcome."""
        if state not in {"failed", "uncertain"}:
            raise ValueError("Invalid operation state.")
        with self._connection() as connection:
            connection.execute(
                "UPDATE operations SET state=?, updated_at=? WHERE id=?", (state, self._now(), operation_id)
            )
            connection.commit()

    def list_operations(self) -> list[dict[str, Any]]:
        """Read journal states without creating a file if the journal is absent."""
        if not self.path.exists():
            return []
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id, entity_type_id, xml_id, state, updated_at FROM operations ORDER BY updated_at DESC"
            ).fetchall()
            return [dict(row) for row in rows]

    def confirm_not_created(self, operation_id: str) -> None:
        """Release an uncertain operation only after a human has checked the portal."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state FROM operations WHERE id=?", (operation_id,)).fetchone()
            if row is None or row["state"] not in {"pending", "uncertain"}:
                raise ConflictError("Only an existing pending/uncertain operation can be released.")
            connection.execute(
                "UPDATE operations SET state='failed', updated_at=? WHERE id=?", (self._now(), operation_id)
            )
            connection.commit()
