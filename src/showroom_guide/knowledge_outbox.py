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


@dataclass(frozen=True)
class KnowledgeEntry:
    id: str
    content: str
    filename: str
    state: OutboxState
    attempts: int
    next_attempt_at: float
    last_error: str | None


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
        timestamp = datetime.fromtimestamp(
            self._clock(),
            tz=timezone.utc,
        ).strftime("%Y%m%dT%H%M%SZ")
        filename = f"voice-knowledge-{timestamp}-{entry_id}.md"
        entry = KnowledgeEntry(
            id=entry_id,
            content=normalized,
            filename=filename,
            state=OutboxState.PENDING,
            attempts=0,
            next_attempt_at=self._clock(),
            last_error=None,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO knowledge_outbox (
                    id, content, filename, state, attempts,
                    next_attempt_at, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.id,
                    entry.content,
                    entry.filename,
                    entry.state.value,
                    entry.attempts,
                    entry.next_attempt_at,
                    entry.last_error,
                ),
            )
        return entry

    def list_due(self, limit: int = 20) -> list[KnowledgeEntry]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, content, filename, state, attempts,
                       next_attempt_at, last_error
                FROM knowledge_outbox
                WHERE next_attempt_at <= ?
                ORDER BY rowid
                LIMIT ?
                """,
                (self._clock(), limit),
            ).fetchall()
        return [self._entry(row) for row in rows]

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

    def defer(self, entry_id: str, *, retry_after_seconds: float) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE knowledge_outbox SET next_attempt_at = ? WHERE id = ?",
                (self._clock() + retry_after_seconds, entry_id),
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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS knowledge_outbox (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    filename TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL,
                    last_error TEXT
                )
                """
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
        assignments = ["next_attempt_at = ?", "last_error = ?"]
        values: list[object] = [
            self._clock() + retry_after_seconds,
            last_error,
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
        )
