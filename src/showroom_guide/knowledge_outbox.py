import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Callable


class OutboxState(StrEnum):
    PENDING = "pending"
    UPLOADING = "uploading"
    UPLOADED = "uploaded"
    SYNCED = "synced"


@dataclass(frozen=True)
class KnowledgeEntry:
    id: str
    content: str
    filename: str
    state: OutboxState
    attempts: int
    next_attempt_at: float
    last_error: str | None
    updated_at: float = 0.0


class KnowledgeOutbox:
    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = Path(path)
        self._clock = clock
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def enqueue(self, content: str) -> KnowledgeEntry:
        normalized = content.strip()
        if not normalized:
            raise ValueError("知识内容不能为空")
        entry_id = uuid.uuid4().hex
        now = self._clock()
        timestamp = datetime.fromtimestamp(
            now,
            tz=timezone.utc,
        ).strftime("%Y%m%dT%H%M%SZ")
        filename = f"voice-knowledge-{timestamp}-{entry_id}.md"
        entry = KnowledgeEntry(
            id=entry_id,
            content=normalized,
            filename=filename,
            state=OutboxState.PENDING,
            attempts=0,
            next_attempt_at=now,
            last_error=None,
            updated_at=now,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO knowledge_outbox (
                    id, content, filename, state, attempts,
                    next_attempt_at, last_error, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.id,
                    entry.content,
                    entry.filename,
                    entry.state.value,
                    entry.attempts,
                    entry.next_attempt_at,
                    entry.last_error,
                    entry.updated_at,
                ),
            )
        return entry

    def list_due(self, limit: int = 20) -> list[KnowledgeEntry]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, content, filename, state, attempts,
                       next_attempt_at, last_error, updated_at
                FROM knowledge_outbox
                WHERE next_attempt_at <= ? AND state != ?
                ORDER BY rowid
                LIMIT ?
                """,
                (self._clock(), OutboxState.SYNCED.value, limit),
            ).fetchall()
        return [self._entry(row) for row in rows]

    def get(self, entry_id: str) -> KnowledgeEntry | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, content, filename, state, attempts,
                       next_attempt_at, last_error, updated_at
                FROM knowledge_outbox
                WHERE id = ?
                """,
                (entry_id,),
            ).fetchone()
        return self._entry(row) if row is not None else None

    def mark_uploaded(self, entry_id: str, *, retry_after_seconds: float) -> None:
        self._update_schedule(
            entry_id,
            state=OutboxState.UPLOADED,
            retry_after_seconds=retry_after_seconds,
            last_error=None,
            increment_attempts=False,
        )

    def mark_uploading(self, entry_id: str) -> None:
        self._update_schedule(
            entry_id,
            state=OutboxState.UPLOADING,
            retry_after_seconds=0,
            last_error=None,
            increment_attempts=False,
        )

    def mark_synced(self, entry_id: str) -> None:
        self._update_schedule(
            entry_id,
            state=OutboxState.SYNCED,
            retry_after_seconds=0,
            last_error=None,
            increment_attempts=False,
        )

    def defer(self, entry_id: str, *, retry_after_seconds: float) -> None:
        now = self._clock()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE knowledge_outbox
                SET next_attempt_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now + retry_after_seconds, now, entry_id),
            )

    def mark_failed(
        self,
        entry_id: str,
        message: str,
        *,
        retry_after_seconds: float,
        requeue: bool = False,
    ) -> None:
        self._update_schedule(
            entry_id,
            state=OutboxState.PENDING if requeue else None,
            retry_after_seconds=retry_after_seconds,
            last_error=message[:1000],
            increment_attempts=True,
        )

    def delete(self, entry_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM knowledge_outbox WHERE id = ?",
                (entry_id,),
            )

    def prune_synced(self, keep: int = 50) -> None:
        if keep < 0:
            raise ValueError("keep 不能小于 0")
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM knowledge_outbox
                WHERE state = ?
                  AND id NOT IN (
                      SELECT id
                      FROM knowledge_outbox
                      WHERE state = ?
                      ORDER BY updated_at DESC, rowid DESC
                      LIMIT ?
                  )
                """,
                (OutboxState.SYNCED.value, OutboxState.SYNCED.value, keep),
            )

    def count(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM knowledge_outbox"
            ).fetchone()
        return int(row[0])

    def close(self) -> None:
        pass

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS knowledge_outbox (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    filename TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL,
                    last_error TEXT,
                    updated_at REAL NOT NULL
                )
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(knowledge_outbox)"
                ).fetchall()
            }
            if "updated_at" not in columns:
                connection.execute(
                    "ALTER TABLE knowledge_outbox ADD COLUMN updated_at REAL"
                )
            connection.execute(
                """
                UPDATE knowledge_outbox
                SET updated_at = ?
                WHERE updated_at IS NULL
                """,
                (self._clock(),),
            )

    def _update_schedule(
        self,
        entry_id: str,
        *,
        state: OutboxState | None,
        retry_after_seconds: float,
        last_error: str | None,
        increment_attempts: bool,
    ) -> None:
        now = self._clock()
        assignments = [
            "next_attempt_at = ?",
            "last_error = ?",
            "updated_at = ?",
        ]
        values: list[object] = [
            now + retry_after_seconds,
            last_error,
            now,
        ]
        if state is not None:
            assignments.append("state = ?")
            values.append(state.value)
        if increment_attempts:
            assignments.append("attempts = attempts + 1")
        values.append(entry_id)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE knowledge_outbox SET {', '.join(assignments)} WHERE id = ?",
                values,
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=5)

    @staticmethod
    def _entry(row: tuple[object, ...]) -> KnowledgeEntry:
        return KnowledgeEntry(
            id=str(row[0]),
            content=str(row[1]),
            filename=str(row[2]),
            state=OutboxState(str(row[3])),
            attempts=int(row[4]),
            next_attempt_at=float(row[5]),
            last_error=str(row[6]) if row[6] is not None else None,
            updated_at=float(row[7]),
        )
