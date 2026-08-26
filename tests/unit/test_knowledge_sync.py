import asyncio
import re
from functools import partial
from types import FrameType, TracebackType
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from showroom_guide.clients.xzkb_auth import XzkbAuthenticationError
from showroom_guide.clients.xzkb_knowledge import (
    DocumentProcessingState,
    XzkbKnowledgeAuthenticationError,
    XzkbKnowledgePermissionError,
    XzkbKnowledgeUnavailableError,
)
from showroom_guide.knowledge_outbox import KnowledgeOutbox, OutboxState
from showroom_guide.knowledge_sync import (
    KnowledgeSyncService,
    _safe_sync_error_stack,
)
from showroom_guide.async_outbox import AsyncKnowledgeOutbox


@pytest.mark.asyncio
async def test_sync_once_awaits_outbox_reads():
    outbox = AsyncMock()
    outbox.list_due.return_value = []
    service = KnowledgeSyncService(outbox, AsyncMock(), poll_seconds=10)

    await service.sync_once()

    outbox.list_due.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_sync_worker_continues_after_unexpected_cycle_failure():
    outbox = AsyncMock()
    outbox.list_due.side_effect = [OSError("disk busy"), []]
    client = AsyncMock()
    service = KnowledgeSyncService(outbox, client, poll_seconds=0.01)
    service.start()

    for _ in range(50):
        if outbox.list_due.await_count >= 2:
            break
        await asyncio.sleep(0.01)

    assert outbox.list_due.await_count >= 2
    assert service.is_running is True
    await service.aclose()


class Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


def _raise_sync_error_with_sensitive_locals(error_sink, *_args) -> None:
    top_secret_body = "TOP_SECRET_BODY"
    token = "https://xzkb.example/upload?token=TOP_SECRET_TOKEN"
    error = RuntimeError(f"request failed: {token}; body={top_secret_body}")
    error_sink.append(error)
    raise error


@pytest.mark.asyncio
async def test_pending_entry_uploads_then_waits_for_processing(tmp_path):
    clock = Clock()
    outbox = KnowledgeOutbox(tmp_path / "knowledge.sqlite3", clock=clock)
    entry = outbox.enqueue("知识正文")
    client = AsyncMock()
    client.document_state.return_value = DocumentProcessingState.NOT_FOUND
    service = KnowledgeSyncService(
        AsyncKnowledgeOutbox(outbox), client, poll_seconds=10, clock=clock
    )

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
    service = KnowledgeSyncService(
        AsyncKnowledgeOutbox(outbox), client, poll_seconds=10, clock=clock
    )

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
    service = KnowledgeSyncService(
        AsyncKnowledgeOutbox(outbox), client, poll_seconds=10, clock=clock
    )

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
    service = KnowledgeSyncService(
        AsyncKnowledgeOutbox(outbox), client, poll_seconds=10, clock=clock
    )

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
    service = KnowledgeSyncService(
        AsyncKnowledgeOutbox(outbox), client, poll_seconds=10, clock=clock
    )

    await service.sync_once()

    assert outbox.count() == 1
    assert outbox.list_due() == []
    failed = outbox.get(entry.id)
    assert failed is not None
    assert failed.state is OutboxState.UPLOADING
    assert failed.attempts == 1
    assert failed.last_error == "XZKB 同步失败，知识已本地保存，等待同步"
    assert failed.next_attempt_at == clock.now + 10
    clock.now += 10
    failed = outbox.list_due()[0]
    assert failed.attempts == 1
    assert failed.last_error == "XZKB 同步失败，知识已本地保存，等待同步"


@pytest.mark.asyncio
async def test_document_state_failure_preserves_uploaded_retry_state(tmp_path):
    clock = Clock()
    outbox = KnowledgeOutbox(tmp_path / "knowledge.sqlite3", clock=clock)
    entry = outbox.enqueue("知识正文")
    outbox.mark_uploaded(entry.id, retry_after_seconds=0)
    client = AsyncMock()
    client.document_state.side_effect = httpx.ConnectError("status offline")
    service = KnowledgeSyncService(
        AsyncKnowledgeOutbox(outbox), client, poll_seconds=10, clock=clock
    )

    await service.sync_once()

    failed = outbox.get(entry.id)
    assert failed is not None
    assert failed.state is OutboxState.UPLOADED
    assert failed.attempts == 1
    assert failed.last_error == "XZKB 同步失败，知识已本地保存，等待同步"
    assert failed.next_attempt_at == clock.now + 10


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_message"),
    [
        (
            XzkbKnowledgeAuthenticationError(
                "https://xzkb.example/login?token=authentication-secret"
                "&body=不可写入错误状态的业务正文"
            ),
            "XZKB 专用账号登录失败，知识已本地保存，等待同步",
        ),
        (
            XzkbAuthenticationError(
                "https://xzkb.example/login-local?token=local-auth-secret"
                "&body=不可写入错误状态的业务正文"
            ),
            "XZKB 专用账号登录失败，知识已本地保存，等待同步",
        ),
        (
            XzkbKnowledgePermissionError(
                "https://xzkb.example/kb/private?token=permission-secret"
                "&body=不可写入错误状态的业务正文"
            ),
            "XZKB 专用账号无目标知识库写入权限，知识已本地保存",
        ),
        (
            XzkbKnowledgeUnavailableError(
                "https://xzkb.example/status?token=unavailable-secret"
                "&body=不可写入错误状态的业务正文"
            ),
            "XZKB 暂时不可用，知识已本地保存，等待同步",
        ),
        (
            RuntimeError(
                "https://xzkb.example/internal?token=unknown-secret"
                "&body=不可写入错误状态的业务正文"
            ),
            "XZKB 同步失败，知识已本地保存，等待同步",
        ),
    ],
)
async def test_sync_failure_persists_only_safe_message(
    tmp_path,
    caplog,
    error,
    expected_message,
):
    clock = Clock()
    outbox = KnowledgeOutbox(tmp_path / "knowledge.sqlite3", clock=clock)
    entry = outbox.enqueue("不可写入错误状态的业务正文")
    client = AsyncMock()
    client.upload.side_effect = error
    service = KnowledgeSyncService(
        AsyncKnowledgeOutbox(outbox), client, poll_seconds=10, clock=clock
    )

    await service.sync_once()

    failed = outbox.get(entry.id)
    assert failed is not None
    assert failed.last_error == expected_message
    assert "https://xzkb.example" not in failed.last_error
    assert "token=" not in failed.last_error
    assert entry.content not in failed.last_error
    failure_logs = [
        record
        for record in caplog.records
        if record.getMessage().startswith("knowledge_sync_failed")
    ]
    assert len(failure_logs) == 1
    assert failure_logs[0].entry_id == entry.id
    assert failure_logs[0].attempts == entry.attempts
    assert failure_logs[0].exc_info is not None
    assert failure_logs[0].exc_info[2] is None
    logged_error = failure_logs[0].exc_info[1]
    assert logged_error.__context__ is None
    assert logged_error.__cause__ is None
    assert expected_message in caplog.text
    assert "https://xzkb.example" not in caplog.text
    assert "token=" not in caplog.text
    assert entry.content not in caplog.text


@pytest.mark.asyncio
async def test_sync_failure_logs_safe_immutable_stack_without_original_error(
    tmp_path,
    caplog,
):
    clock = Clock()
    outbox = KnowledgeOutbox(tmp_path / "knowledge.sqlite3", clock=clock)
    outbox.enqueue("TOP_SECRET_BODY")
    client = AsyncMock()
    original_errors = []
    client.upload.side_effect = partial(
        _raise_sync_error_with_sensitive_locals,
        original_errors,
    )
    service = KnowledgeSyncService(
        AsyncKnowledgeOutbox(outbox), client, poll_seconds=10, clock=clock
    )

    await service.sync_once()

    record = next(
        record
        for record in caplog.records
        if record.getMessage().startswith("knowledge_sync_failed")
    )
    assert isinstance(record.error_stack, tuple)
    assert record.error_stack
    assert all(isinstance(item, str) for item in record.error_stack)
    assert any(
        re.fullmatch(
            r"test_knowledge_sync\.py:\d+ in "
            r"_raise_sync_error_with_sensitive_locals",
            item,
        )
        for item in record.error_stack
    )
    helper_frame = next(
        item
        for item in record.error_stack
        if "_raise_sync_error_with_sensitive_locals" in item
    )
    assert f"error_stack={record.error_stack[0]}" in record.getMessage()
    assert helper_frame in record.getMessage()
    assert helper_frame in caplog.text
    assert "TOP_SECRET_BODY" not in repr(record.error_stack)
    assert "TOP_SECRET_TOKEN" not in repr(record.error_stack)
    assert "https://xzkb.example" not in repr(record.error_stack)
    assert record.exc_info is not None
    assert record.exc_info[2] is None
    assert record.exc_info[1].__context__ is None
    assert record.exc_info[1].__cause__ is None
    original_error = original_errors[0]
    original_traceback = original_error.__traceback__
    assert record.exc_info[1] is not original_error
    assert not any(
        isinstance(value, (TracebackType, FrameType))
        for value in vars(record).values()
    )
    assert original_error not in vars(record).values()
    assert original_traceback not in vars(record).values()


def test_safe_sync_error_stack_is_empty_without_traceback():
    error = RuntimeError("https://xzkb.example?token=TOP_SECRET_TOKEN")

    assert error.__traceback__ is None
    assert _safe_sync_error_stack(error) == ()


@pytest.mark.asyncio
async def test_sync_failure_logs_none_when_error_stack_is_empty(
    tmp_path,
    caplog,
    monkeypatch,
):
    clock = Clock()
    outbox = KnowledgeOutbox(tmp_path / "knowledge.sqlite3", clock=clock)
    entry = outbox.enqueue("TOP_SECRET_BODY")
    client = AsyncMock()
    client.upload.side_effect = RuntimeError(
        "https://xzkb.example/upload?token=TOP_SECRET_TOKEN"
    )

    def extract_after_discarding_traceback(error):
        error.with_traceback(None)
        return _safe_sync_error_stack(error)

    monkeypatch.setattr(
        "showroom_guide.knowledge_sync._safe_sync_error_stack",
        extract_after_discarding_traceback,
    )
    service = KnowledgeSyncService(
        AsyncKnowledgeOutbox(outbox), client, poll_seconds=10, clock=clock
    )

    await service.sync_once()

    record = next(
        record
        for record in caplog.records
        if record.getMessage().startswith("knowledge_sync_failed")
    )
    assert record.getMessage() == "knowledge_sync_failed error_stack=<none>"
    assert record.error_stack == ()
    assert record.entry_id == entry.id
    assert record.attempts == entry.attempts
    assert record.exc_info is not None
    assert record.exc_info[2] is None
    assert "TOP_SECRET_BODY" not in caplog.text
    assert "TOP_SECRET_TOKEN" not in caplog.text
    assert "https://xzkb.example" not in caplog.text


@pytest.mark.asyncio
async def test_uploading_entry_already_accepted_remotely_is_not_uploaded_again(
    tmp_path,
):
    clock = Clock()
    outbox = KnowledgeOutbox(tmp_path / "knowledge.sqlite3", clock=clock)
    entry = outbox.enqueue("知识正文")
    outbox.mark_uploading(entry.id)
    client = AsyncMock()
    client.document_state.return_value = DocumentProcessingState.PENDING
    service = KnowledgeSyncService(
        AsyncKnowledgeOutbox(outbox), client, poll_seconds=10, clock=clock
    )

    await service.sync_once()

    client.document_state.assert_awaited_once_with(entry.filename)
    client.upload.assert_not_awaited()
    clock.now += 10
    assert outbox.list_due()[0].state is OutboxState.UPLOADED
