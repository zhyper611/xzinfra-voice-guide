import asyncio
import inspect
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import AsyncIterator


class QueueWaitTimeout(TimeoutError):
    pass


class AsyncGate:
    def __init__(self, limit: int, timeout_seconds: float) -> None:
        self._semaphore = asyncio.Semaphore(limit)
        self._timeout_seconds = timeout_seconds

    @asynccontextmanager
    async def slot(
        self,
        on_wait: Callable[[], object] | None = None,
    ) -> AsyncIterator[None]:
        if self._semaphore.locked() and on_wait is not None:
            result = on_wait()
            if inspect.isawaitable(result):
                await result
        try:
            await asyncio.wait_for(
                self._semaphore.acquire(),
                timeout=self._timeout_seconds,
            )
        except TimeoutError as error:
            raise QueueWaitTimeout from error
        try:
            yield
        finally:
            self._semaphore.release()
