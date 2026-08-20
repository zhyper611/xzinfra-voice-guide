from unittest.mock import AsyncMock

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
async def test_uploaded_entry_is_deleted_only_after_remote_success(tmp_path):
    clock = Clock()
    outbox = KnowledgeOutbox(tmp_path / "knowledge.sqlite3", clock=clock)
    entry = outbox.enqueue("知识正文")
    outbox.mark_uploaded(entry.id, retry_after_seconds=0)
    client = AsyncMock()
    client.document_state.return_value = DocumentProcessingState.SUCCESS
    service = KnowledgeSyncService(outbox, client, poll_seconds=10, clock=clock)

    await service.sync_once()

    client.document_state.assert_awaited_once_with(entry.filename)
    assert outbox.count() == 0


@pytest.mark.asyncio
async def test_sync_failure_keeps_entry_and_uses_exponential_backoff(tmp_path):
    clock = Clock()
    outbox = KnowledgeOutbox(tmp_path / "knowledge.sqlite3", clock=clock)
    outbox.enqueue("知识正文")
    client = AsyncMock()
    client.upload.side_effect = httpx.ConnectError("offline")
    client.document_state.return_value = DocumentProcessingState.NOT_FOUND
    service = KnowledgeSyncService(outbox, client, poll_seconds=10, clock=clock)

    await service.sync_once()

    assert outbox.count() == 1
    assert outbox.list_due() == []
    clock.now += 10
    failed = outbox.list_due()[0]
    assert failed.attempts == 1
    assert failed.last_error == "offline"


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
