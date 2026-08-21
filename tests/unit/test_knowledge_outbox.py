import sqlite3

import pytest

from showroom_guide.knowledge_outbox import KnowledgeOutbox, OutboxState


class Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_pending_entry_survives_reopen(tmp_path):
    path = tmp_path / "knowledge.sqlite3"
    clock = Clock()
    first = KnowledgeOutbox(path, clock=clock)

    entry = first.enqueue("总装车间采用柔性生产线。")
    first.close()

    reopened = KnowledgeOutbox(path, clock=clock)
    stored = reopened.list_due()

    assert len(stored) == 1
    assert stored[0].id == entry.id
    assert stored[0].content == "总装车间采用柔性生产线。"
    assert stored[0].filename.endswith(".md")
    assert stored[0].state is OutboxState.PENDING


def test_uploaded_entry_is_retained_until_remote_processing_succeeds(tmp_path):
    clock = Clock()
    outbox = KnowledgeOutbox(tmp_path / "knowledge.sqlite3", clock=clock)
    entry = outbox.enqueue("知识正文")

    outbox.mark_uploaded(entry.id, retry_after_seconds=30)
    assert outbox.count() == 1
    assert outbox.list_due() == []

    clock.now += 30
    uploaded = outbox.list_due()[0]
    assert uploaded.state is OutboxState.UPLOADED

    outbox.delete(entry.id)
    assert outbox.count() == 0


def test_failure_increments_attempts_and_applies_backoff(tmp_path):
    clock = Clock()
    outbox = KnowledgeOutbox(tmp_path / "knowledge.sqlite3", clock=clock)
    entry = outbox.enqueue("知识正文")

    outbox.mark_failed(entry.id, "network unavailable", retry_after_seconds=60)

    assert outbox.list_due() == []
    clock.now += 60
    failed = outbox.list_due()[0]
    assert failed.attempts == 1
    assert failed.last_error == "network unavailable"


def test_existing_database_adds_and_backfills_updated_at(tmp_path):
    path = tmp_path / "knowledge.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE knowledge_outbox (
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
        connection.execute(
            """
            INSERT INTO knowledge_outbox (
                id, content, filename, state, attempts,
                next_attempt_at, last_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-entry",
                "旧知识",
                "legacy.md",
                OutboxState.PENDING.value,
                0,
                1000.0,
                None,
            ),
        )

    clock = Clock()
    clock.now = 2000.0
    outbox = KnowledgeOutbox(path, clock=clock)

    entry = outbox.get("legacy-entry")

    assert entry is not None
    assert entry.updated_at == 2000.0


def test_existing_nullable_updated_at_is_backfilled_after_interrupted_migration(
    tmp_path,
):
    path = tmp_path / "knowledge.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE knowledge_outbox (
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
        connection.execute(
            "ALTER TABLE knowledge_outbox ADD COLUMN updated_at REAL"
        )
        connection.execute(
            """
            INSERT INTO knowledge_outbox (
                id, content, filename, state, attempts,
                next_attempt_at, last_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "interrupted-entry",
                "迁移中断知识",
                "interrupted.md",
                OutboxState.PENDING.value,
                0,
                1000.0,
                None,
            ),
        )

    clock = Clock()
    clock.now = 3000.0
    outbox = KnowledgeOutbox(path, clock=clock)

    entry = outbox.get("interrupted-entry")

    assert entry is not None
    assert entry.updated_at == 3000.0


def test_updated_at_tracks_enqueue_defer_and_state_updates(tmp_path):
    clock = Clock()
    outbox = KnowledgeOutbox(tmp_path / "knowledge.sqlite3", clock=clock)

    entry = outbox.enqueue("知识正文")
    assert entry.updated_at == 1000.0

    clock.now = 1010.0
    outbox.defer(entry.id, retry_after_seconds=5)
    assert outbox.get(entry.id).updated_at == 1010.0

    clock.now = 1020.0
    outbox.mark_uploading(entry.id)
    assert outbox.get(entry.id).updated_at == 1020.0

    clock.now = 1030.0
    outbox.mark_uploaded(entry.id, retry_after_seconds=5)
    assert outbox.get(entry.id).updated_at == 1030.0

    clock.now = 1040.0
    outbox.mark_failed(entry.id, "失败", retry_after_seconds=5, requeue=True)
    assert outbox.get(entry.id).updated_at == 1040.0

    clock.now = 1050.0
    outbox.mark_synced(entry.id)
    assert outbox.get(entry.id).updated_at == 1050.0


def test_synced_entry_is_queryable_but_never_due(tmp_path):
    clock = Clock()
    outbox = KnowledgeOutbox(tmp_path / "knowledge.sqlite3", clock=clock)
    entry = outbox.enqueue("知识正文")

    outbox.mark_synced(entry.id)

    synced = outbox.get(entry.id)
    assert synced is not None
    assert synced.state is OutboxState.SYNCED
    assert outbox.list_due() == []
    assert outbox.get("missing-entry") is None


def test_prune_synced_keeps_latest_fifty_and_other_states(tmp_path):
    clock = Clock()
    outbox = KnowledgeOutbox(tmp_path / "knowledge.sqlite3", clock=clock)
    synced_ids = []

    for index in range(55):
        entry = outbox.enqueue(f"知识正文 {index}")
        clock.now += 1
        outbox.mark_synced(entry.id)
        synced_ids.append(entry.id)

    pending = outbox.enqueue("待处理知识")
    outbox.prune_synced()

    assert all(outbox.get(entry_id) is None for entry_id in synced_ids[:5])
    assert all(outbox.get(entry_id) is not None for entry_id in synced_ids[5:])
    assert outbox.get(pending.id) is not None
    assert outbox.count() == 51


def test_prune_synced_with_zero_removes_all_synced_entries(tmp_path):
    clock = Clock()
    outbox = KnowledgeOutbox(tmp_path / "knowledge.sqlite3", clock=clock)
    synced = outbox.enqueue("已同步知识")
    outbox.mark_synced(synced.id)
    pending = outbox.enqueue("待处理知识")

    outbox.prune_synced(keep=0)

    assert outbox.get(synced.id) is None
    assert outbox.get(pending.id) is not None


def test_prune_synced_rejects_negative_keep(tmp_path):
    outbox = KnowledgeOutbox(tmp_path / "knowledge.sqlite3", clock=Clock())

    with pytest.raises(ValueError):
        outbox.prune_synced(keep=-1)
