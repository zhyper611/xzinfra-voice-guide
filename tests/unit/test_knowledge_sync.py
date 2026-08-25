from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from showroom_guide.clients.xzkb_knowledge import DocumentProcessingState
from showroom_guide.knowledge_outbox import KnowledgeOutbox, OutboxState
from showroom_guide.knowledge_sync import KnowledgeSyncService


class Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_pending_entry_uploads_then_waits_for_processing(tmp_path):
    clock = Clock()
    outbox = KnowledgeOutbox(tmp_path / "knowledge.sqlite3", clock=clock)
    entry = outbox.enqueue("知识正文")
    client = AsyncMock()
    client.document_state.return_value = DocumentProcessingState.NOT_FOUND
    service = KnowledgeSyncService(outbox, client, poll_seconds=10, clock=clock)

    await service.sync_once()

    client.upload.assert_awaited_once()
    assert outbox.count() == 1
    clock.now += 10
    assert outbox.list_due()[0].state is OutboxState.UPLOADED


@pytest.mark.asyncio
async def test_uploaded_entry_is_marked_synced_and_prunes_old_history(tmp_path):
    clock = Clock()
    outbox = KnowledgeOutbox(tmp_path / "knowledge.sqlite3", clock=clock)
    old_entries = []
    for index in range(50):
        old_entry = outbox.enqueue(f"历史知识 {index}")
        outbox.mark_synced(old_entry.id)
        old_entries.append(old_entry)
    entry = outbox.enqueue("知识正文")
    outbox.mark_uploaded(entry.id, retry_after_seconds=0)
    client = AsyncMock()
    client.document_state.return_value = DocumentProcessingState.SUCCESS
    service = KnowledgeSyncService(outbox, client, poll_seconds=10, clock=clock)

    await service.sync_once()

    client.document_state.assert_awaited_once_with(entry.filename)
    synced = outbox.get(entry.id)
    assert synced is not None
    assert synced.state is OutboxState.SYNCED
    assert outbox.list_due() == []
    assert outbox.count() == 50
    assert outbox.get(old_entries[0].id) is None


@pytest.mark.asyncio
async def test_uploading_entry_remote_success_is_marked_synced_without_reupload(
    tmp_path,
):
    clock = Clock()
    outbox = KnowledgeOutbox(tmp_path / "knowledge.sqlite3", clock=clock)
    entry = outbox.enqueue("知识正文")
    outbox.mark_uploading(entry.id)
    client = AsyncMock()
    client.document_state.return_value = DocumentProcessingState.SUCCESS
    service = KnowledgeSyncService(outbox, client, poll_seconds=10, clock=clock)

    await service.sync_once()

    client.document_state.assert_awaited_once_with(entry.filename)
    client.upload.assert_not_awaited()
    synced = outbox.get(entry.id)
    assert synced is not None
    assert synced.state is OutboxState.SYNCED
    assert outbox.list_due() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "initial_state",
    [OutboxState.UPLOADING, OutboxState.UPLOADED],
)
async def test_prune_failure_does_not_turn_remote_success_into_sync_failure(
    tmp_path,
    monkeypatch,
    caplog,
    initial_state,
):
    clock = Clock()
    outbox = KnowledgeOutbox(tmp_path / "knowledge.sqlite3", clock=clock)
    entry = outbox.enqueue("敏感知识正文")
    if initial_state is OutboxState.UPLOADING:
        outbox.mark_uploading(entry.id)
    else:
        outbox.mark_uploaded(entry.id, retry_after_seconds=0)
    prune_synced = Mock(side_effect=RuntimeError("prune failed"))
    monkeypatch.setattr(outbox, "prune_synced", prune_synced)
    client = AsyncMock()
    client.document_state.return_value = DocumentProcessingState.SUCCESS
    service = KnowledgeSyncService(outbox, client, poll_seconds=10, clock=clock)

    await service.sync_once()

    client.document_state.assert_awaited_once_with(entry.filename)
    client.upload.assert_not_awaited()
    prune_synced.assert_called_once_with(keep=50)
    synced = outbox.get(entry.id)
    assert synced is not None
    assert synced.state is OutboxState.SYNCED
    assert synced.attempts == 0
    assert synced.last_error is None
    assert outbox.list_due() == []
    prune_logs = [
        record
        for record in caplog.records
        if record.getMessage() == "knowledge_sync_prune_failed"
    ]
    assert len(prune_logs) == 1
    assert prune_logs[0].entry_id == entry.id
    assert entry.content not in caplog.text


@pytest.mark.asyncio
async def test_sync_failure_keeps_entry_and_uses_exponential_backoff(tmp_path):
    clock = Clock()
    outbox = KnowledgeOutbox(tmp_path / "knowledge.sqlite3", clock=clock)
    entry = outbox.enqueue("知识正文")
    client = AsyncMock()
    client.upload.side_effect = httpx.ConnectError("offline")
    client.document_state.return_value = DocumentProcessingState.NOT_FOUND
    service = KnowledgeSyncService(outbox, client, poll_seconds=10, clock=clock)

    await service.sync_once()

    assert outbox.count() == 1
    assert outbox.list_due() == []
    failed = outbox.get(entry.id)
    assert failed is not None
    assert failed.state is OutboxState.UPLOADING
    assert failed.attempts == 1
    assert failed.last_error == "offline"
    assert failed.next_attempt_at == clock.now + 10
    clock.now += 10
    failed = outbox.list_due()[0]
    assert failed.attempts == 1
    assert failed.last_error == "offline"


@pytest.mark.asyncio
async def test_document_state_failure_preserves_uploaded_retry_state(tmp_path):
    clock = Clock()
    outbox = KnowledgeOutbox(tmp_path / "knowledge.sqlite3", clock=clock)
    entry = outbox.enqueue("知识正文")
    outbox.mark_uploaded(entry.id, retry_after_seconds=0)
    client = AsyncMock()
    client.document_state.side_effect = httpx.ConnectError("status offline")
    service = KnowledgeSyncService(outbox, client, poll_seconds=10, clock=clock)

    await service.sync_once()

    failed = outbox.get(entry.id)
    assert failed is not None
    assert failed.state is OutboxState.UPLOADED
    assert failed.attempts == 1
    assert failed.last_error == "status offline"
    assert failed.next_attempt_at == clock.now + 10


@pytest.mark.asyncio
async def test_uploading_entry_already_accepted_remotely_is_not_uploaded_again(tmp_path):
    clock = Clock()
    outbox = KnowledgeOutbox(tmp_path / "knowledge.sqlite3", clock=clock)
    entry = outbox.enqueue("知识正文")
    outbox.mark_uploading(entry.id)
    client = AsyncMock()
    client.document_state.return_value = DocumentProcessingState.PENDING
    service = KnowledgeSyncService(outbox, client, poll_seconds=10, clock=clock)

    await service.sync_once()

    client.document_state.assert_awaited_once_with(entry.filename)
    client.upload.assert_not_awaited()
    clock.now += 10
    assert outbox.list_due()[0].state is OutboxState.UPLOADED
