import asyncio
import threading
import time

import pytest

from showroom_guide.async_outbox import AsyncKnowledgeOutbox


class RecordingStorage:
    def __init__(self) -> None:
        self.thread_ids: set[int] = set()
        self.active_calls = 0
        self.max_active_calls = 0
        self.failed_call = None
        self.closed = False

    def enqueue(self, content: str) -> str:
        self.thread_ids.add(threading.get_ident())
        self.active_calls += 1
        self.max_active_calls = max(self.max_active_calls, self.active_calls)
        time.sleep(0.01)
        self.active_calls -= 1
        return content

    def mark_failed(
        self,
        entry_id: str,
        message: str,
        *,
        retry_after_seconds: float,
        requeue: bool = False,
    ) -> None:
        self.thread_ids.add(threading.get_ident())
        self.failed_call = (
            entry_id,
            message,
            retry_after_seconds,
            requeue,
        )

    def list_due(self, limit: int = 20) -> list[object]:
        self.thread_ids.add(threading.get_ident())
        return []

    def close(self) -> None:
        self.thread_ids.add(threading.get_ident())
        self.closed = True


@pytest.mark.asyncio
async def test_calls_share_one_non_event_loop_worker_thread():
    storage = RecordingStorage()
    outbox = AsyncKnowledgeOutbox(storage)
    event_loop_thread = threading.get_ident()

    first, second = await asyncio.gather(
        outbox.enqueue("第一条知识"),
        outbox.enqueue("第二条知识"),
    )

    assert (first, second) == ("第一条知识", "第二条知识")
    assert len(storage.thread_ids) == 1
    assert event_loop_thread not in storage.thread_ids
    assert storage.max_active_calls == 1
    await outbox.aclose()


@pytest.mark.asyncio
async def test_keyword_arguments_are_forwarded_and_ping_uses_worker():
    storage = RecordingStorage()
    outbox = AsyncKnowledgeOutbox(storage)

    await outbox.mark_failed(
        "entry-id",
        "network failed",
        retry_after_seconds=5,
        requeue=True,
    )

    assert storage.failed_call == ("entry-id", "network failed", 5, True)
    assert await outbox.ping() is True
    await outbox.aclose()
    assert storage.closed is True
