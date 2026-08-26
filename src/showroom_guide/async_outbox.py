import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

from showroom_guide.knowledge_outbox import KnowledgeEntry, KnowledgeOutbox


class AsyncKnowledgeOutbox:
    def __init__(self, storage: KnowledgeOutbox) -> None:
        self._storage = storage
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="knowledge-outbox",
        )
        self._closed = False

    async def enqueue(self, content: str) -> KnowledgeEntry:
        return await self._call(self._storage.enqueue, content)

    async def list_due(self, limit: int = 20) -> list[KnowledgeEntry]:
        return await self._call(self._storage.list_due, limit)

    async def get(self, entry_id: str) -> KnowledgeEntry | None:
        return await self._call(self._storage.get, entry_id)

    async def mark_uploaded(
        self,
        entry_id: str,
        *,
        retry_after_seconds: float,
    ) -> None:
        await self._call(
            self._storage.mark_uploaded,
            entry_id,
            retry_after_seconds=retry_after_seconds,
        )

    async def mark_uploading(self, entry_id: str) -> None:
        await self._call(self._storage.mark_uploading, entry_id)

    async def mark_synced(self, entry_id: str) -> None:
        await self._call(self._storage.mark_synced, entry_id)

    async def defer(
        self,
        entry_id: str,
        *,
        retry_after_seconds: float,
    ) -> None:
        await self._call(
            self._storage.defer,
            entry_id,
            retry_after_seconds=retry_after_seconds,
        )

    async def mark_failed(
        self,
        entry_id: str,
        message: str,
        *,
        retry_after_seconds: float,
        requeue: bool = False,
    ) -> None:
        await self._call(
            self._storage.mark_failed,
            entry_id,
            message,
            retry_after_seconds=retry_after_seconds,
            requeue=requeue,
        )

    async def delete(self, entry_id: str) -> None:
        await self._call(self._storage.delete, entry_id)

    async def prune_synced(self, keep: int = 50) -> None:
        await self._call(self._storage.prune_synced, keep=keep)

    async def count(self) -> int:
        return await self._call(self._storage.count)

    async def ping(self) -> bool:
        await self._call(self._storage.list_due, 1)
        return True

    async def aclose(self) -> None:
        if self._closed:
            return
        await self._call(self._storage.close)
        self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=True)

    async def _call(self, operation, /, *args, **kwargs) -> Any:
        if self._closed:
            raise RuntimeError("knowledge outbox is closed")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor,
            partial(operation, *args, **kwargs),
        )
