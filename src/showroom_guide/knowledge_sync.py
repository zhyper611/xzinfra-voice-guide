import asyncio
import logging
import time
import traceback
from contextlib import suppress
from typing import Callable

from showroom_guide.clients.xzkb_auth import XzkbAuthenticationError
from showroom_guide.clients.xzkb_knowledge import (
    DocumentProcessingState,
    XzkbKnowledgeAuthenticationError,
    XzkbKnowledgePermissionError,
    XzkbKnowledgeUnavailableError,
)
from showroom_guide.knowledge_outbox import KnowledgeOutbox, OutboxState


logger = logging.getLogger(__name__)


def _safe_sync_error_message(error: Exception) -> str:
    messages = {
        (XzkbAuthenticationError, XzkbKnowledgeAuthenticationError): (
            "XZKB 专用账号登录失败，知识已本地保存，等待同步"
        ),
        XzkbKnowledgePermissionError: (
            "XZKB 专用账号无目标知识库写入权限，知识已本地保存"
        ),
        XzkbKnowledgeUnavailableError: ("XZKB 暂时不可用，知识已本地保存，等待同步"),
    }
    for error_type, message in messages.items():
        if isinstance(error, error_type):
            return message
    return "XZKB 同步失败，知识已本地保存，等待同步"


def _safe_sync_exc_info(
    safe_message: str,
) -> tuple[type[RuntimeError], RuntimeError, None]:
    return RuntimeError, RuntimeError(safe_message), None


def _safe_sync_error_stack(error: Exception) -> tuple[str, ...]:
    if error.__traceback__ is None:
        return ()
    return tuple(
        "{}:{} in {}".format(
            frame.filename.replace("\\", "/").rsplit("/", 1)[-1],
            frame.lineno,
            frame.name,
        )
        for frame in traceback.extract_tb(error.__traceback__)
    )


class KnowledgeSyncService:
    def __init__(
        self,
        outbox: KnowledgeOutbox,
        client,
        *,
        poll_seconds: float = 30.0,
        max_backoff_seconds: float = 3600.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._outbox = outbox
        self._client = client
        self._poll_seconds = poll_seconds
        self._max_backoff_seconds = max_backoff_seconds
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(),
                name="knowledge-outbox-sync",
            )

    def wake(self) -> None:
        self._wake.set()

    async def sync_once(self) -> None:
        for entry in self._outbox.list_due():
            try:
                if entry.state is OutboxState.PENDING:
                    self._outbox.mark_uploading(entry.id)
                    await self._client.upload(entry)
                    self._outbox.mark_uploaded(
                        entry.id,
                        retry_after_seconds=self._poll_seconds,
                    )
                    continue
                if entry.state is OutboxState.UPLOADING:
                    state = await self._client.document_state(entry.filename)
                    if state is DocumentProcessingState.SUCCESS:
                        self._mark_synced(entry.id)
                        continue
                    if state is DocumentProcessingState.PENDING:
                        self._outbox.mark_uploaded(
                            entry.id,
                            retry_after_seconds=self._poll_seconds,
                        )
                        continue
                    await self._client.upload(entry)
                    self._outbox.mark_uploaded(
                        entry.id,
                        retry_after_seconds=self._poll_seconds,
                    )
                    continue
                state = await self._client.document_state(entry.filename)
                if state is DocumentProcessingState.SUCCESS:
                    self._mark_synced(entry.id)
                elif state is DocumentProcessingState.FAILURE:
                    self._outbox.mark_failed(
                        entry.id,
                        "XZKB 文档解析或向量化失败",
                        retry_after_seconds=self._backoff(entry.attempts),
                        requeue=True,
                    )
                elif state is DocumentProcessingState.NOT_FOUND:
                    self._outbox.mark_failed(
                        entry.id,
                        "XZKB 中未找到已上传文档",
                        retry_after_seconds=self._backoff(entry.attempts),
                        requeue=True,
                    )
                else:
                    self._outbox.defer(
                        entry.id,
                        retry_after_seconds=self._poll_seconds,
                    )
            except Exception as error:
                error_stack = _safe_sync_error_stack(error)
                error_stack_text = " <- ".join(error_stack) or "<none>"
                safe_message = _safe_sync_error_message(error)
                logger.warning(
                    "knowledge_sync_failed error_stack=%s",
                    error_stack_text,
                    extra={
                        "entry_id": entry.id,
                        "attempts": entry.attempts,
                        "error_stack": error_stack,
                    },
                    exc_info=_safe_sync_exc_info(safe_message),
                )
                self._outbox.mark_failed(
                    entry.id,
                    safe_message,
                    retry_after_seconds=self._backoff(entry.attempts),
                )

    async def aclose(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await self._client.aclose()
        self._outbox.close()

    async def _run(self) -> None:
        while True:
            await self.sync_once()
            self._wake.clear()
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=self._poll_seconds,
                )
            except TimeoutError:
                pass

    def _backoff(self, attempts: int) -> float:
        return min(
            self._poll_seconds * (2**attempts),
            self._max_backoff_seconds,
        )

    def _mark_synced(self, entry_id: str) -> None:
        self._outbox.mark_synced(entry_id)
        try:
            self._outbox.prune_synced(keep=50)
        except Exception:
            logger.warning(
                "knowledge_sync_prune_failed",
                extra={"entry_id": entry_id},
            )
