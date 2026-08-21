import asyncio
import logging
import secrets
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from showroom_guide.button_workflow import ButtonInteractionMode
from showroom_guide.device import InvalidDeviceAudio, NoSpeechDetected
from showroom_guide.knowledge_capture import (
    KnowledgeAsrUnavailable,
    KnowledgeTtsUnavailable,
)
from showroom_guide.knowledge_mode import KnowledgeModeState, KnowledgeProcessingStage
from showroom_guide.knowledge_outbox import KnowledgeEntry, OutboxState
from showroom_guide.local_audio import LocalAudioError


logger = logging.getLogger(__name__)


class KnowledgeControlState(StrEnum):
    AVAILABLE = "available"
    OWNED = "owned"
    OBSERVED = "observed"


class KnowledgeSyncState(StrEnum):
    LOCAL_SAVED = "local_saved"
    UPLOADING = "uploading"
    PROCESSING = "processing"
    RETRYING = "retrying"
    SYNCED = "synced"


@dataclass(frozen=True)
class KnowledgeWebState:
    enabled: bool
    mode_state: KnowledgeModeState
    processing_stage: KnowledgeProcessingStage | None
    control_state: KnowledgeControlState
    lease_expires_at: float | None
    draft_text: str | None
    last_entry_id: str | None


@dataclass(frozen=True)
class KnowledgeEntrySnapshot:
    entry_id: str
    sync_state: KnowledgeSyncState
    attempts: int
    last_error: str | None
    next_attempt_at: float
    updated_at: float


class KnowledgeWebError(RuntimeError):
    def __init__(
        self,
        code: str,
        detail: str,
        status_code: int,
        state: KnowledgeWebState,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status_code = status_code
        self.state = state


class KnowledgeWebController:
    def __init__(
        self,
        button_workflow,
        knowledge_mode,
        outbox,
        *,
        lease_seconds: float = 120.0,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._buttons = button_workflow
        self._knowledge = knowledge_mode
        self._outbox = outbox
        self._lease_seconds = lease_seconds
        self._clock = clock
        self._sleep = sleep
        self._operation_lock = asyncio.Lock()
        self._lease_token: str | None = None
        self._lease_expires_at: float | None = None
        self._lease_task: asyncio.Task[None] | None = None
        self._lease_generation = 0
        self._last_entry_id: str | None = None
        self._button_owner = object()

    async def acquire(self) -> tuple[str, KnowledgeWebState]:
        if self._outbox is None:
            raise self._error(
                "knowledge_capture_disabled",
                "知识补充功能未启用",
                503,
            )
        if self._operation_lock.locked():
            if self._lease_token is not None:
                self._raise_operation_busy()
            raise KnowledgeWebError(
                "knowledge_control_busy",
                "知识补充功能正由其他页面控制",
                409,
                self._state(KnowledgeControlState.OBSERVED),
            )
        await self._expire_elapsed_lease()
        if self._lease_token is not None:
            raise self._error(
                "knowledge_control_busy",
                "知识补充功能正由其他页面控制",
                409,
                observer=True,
            )
        if self._operation_lock.locked():
            raise KnowledgeWebError(
                "knowledge_control_busy",
                "知识补充功能正由其他页面控制",
                409,
                self._state(KnowledgeControlState.OBSERVED),
            )
        await self._acquire_operation()
        try:
            await self._expire_elapsed_lease_locked()
            if self._lease_token is not None:
                raise self._error(
                    "knowledge_control_busy",
                    "知识补充功能正由其他页面控制",
                    409,
                    observer=True,
                )
            try:
                acquired = await self._buttons.acquire_knowledge_control(
                    self._button_owner
                )
            except Exception as error:
                self._raise_mapped(error)
                raise
            if not acquired:
                raise self._error(
                    "knowledge_control_busy",
                    "设备当前无法进入知识补充模式",
                    409,
                )
            token = secrets.token_urlsafe(32)
            self._lease_token = token
            self._renew_lease()
            return token, self._state(KnowledgeControlState.OWNED)
        finally:
            self._operation_lock.release()

    async def state(self, token: str | None = None) -> KnowledgeWebState:
        if token is None:
            return self._observer_or_available_state()
        await self._require_owner(token)
        self._renew_lease()
        return self._state(KnowledgeControlState.OWNED)

    async def short_press(self, token: str) -> KnowledgeWebState:
        self._raise_operation_busy(token)
        await self._require_owner(token)
        await self._acquire_operation(token)
        try:
            await self._require_owner_locked(token)
            self._renew_lease()
            try:
                await self._buttons.knowledge_short_press(self._button_owner)
            except BaseException as error:
                self._renew_owned_lease(token)
                if isinstance(error, Exception):
                    self._raise_mapped(error, token)
                raise
            self._renew_owned_lease(token)
            return self._state(KnowledgeControlState.OWNED)
        finally:
            self._operation_lock.release()

    async def long_press(self, token: str) -> KnowledgeWebState:
        self._raise_operation_busy(token)
        await self._require_owner(token)
        await self._acquire_operation(token)
        try:
            await self._require_owner_locked(token)
            self._renew_lease()
            try:
                result = await self._buttons.knowledge_long_press(
                    self._button_owner
                )
            except BaseException as error:
                if self._knowledge_is_inactive():
                    self._clear_lease()
                else:
                    self._renew_owned_lease(token)
                if isinstance(error, Exception):
                    self._raise_mapped(error, token)
                raise
            if result is not None and result.saved_entry is not None:
                self._last_entry_id = result.saved_entry.id
            if result is not None and result.exited:
                self._clear_lease()
                return self._state(KnowledgeControlState.AVAILABLE)
            self._renew_owned_lease(token)
            return self._state(KnowledgeControlState.OWNED)
        finally:
            self._operation_lock.release()

    async def release(self, token: str) -> KnowledgeWebState:
        self._raise_operation_busy(token)
        await self._require_owner(token)
        await self._acquire_operation(token)
        try:
            await self._require_owner_locked(token)
            self._renew_lease()
            try:
                released = await self._buttons.release_knowledge_control(
                    self._button_owner
                )
            except BaseException as error:
                if self._knowledge_is_inactive():
                    self._clear_lease()
                else:
                    self._renew_owned_lease(token)
                if isinstance(error, Exception):
                    self._raise_mapped(error, token)
                raise
            if not released or not self._knowledge_is_inactive():
                self._renew_owned_lease(token)
                raise self._error(
                    "knowledge_control_busy",
                    "知识补充模式清理尚未完成",
                    409,
                    owner_token=token,
                )
            self._clear_lease()
            return self._state(KnowledgeControlState.AVAILABLE)
        finally:
            self._operation_lock.release()

    async def entry(self, entry_id: str) -> KnowledgeEntrySnapshot:
        if self._outbox is None:
            raise self._error(
                "knowledge_entry_not_found",
                "知识条目不存在",
                404,
            )
        try:
            entry = self._outbox.get(entry_id)
        except sqlite3.Error as error:
            raise self._mapped_error(error) from error
        if entry is None:
            raise self._error(
                "knowledge_entry_not_found",
                "知识条目不存在",
                404,
            )
        return self._entry_snapshot(entry)

    async def aclose(self) -> None:
        caller_cancellation = None
        task = self._lease_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError as error:
                if asyncio.current_task().cancelling():
                    caller_cancellation = error
        self._lease_task = None
        cleanup_task = asyncio.create_task(
            self._finish_close(),
            name="knowledge-web-close-cleanup",
        )
        cleanup_error = None
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError as error:
                if cleanup_task.cancelled():
                    cleanup_error = error
                    break
                if asyncio.current_task().cancelling():
                    caller_cancellation = caller_cancellation or error
            except Exception as error:
                cleanup_error = error
                break
        if cleanup_error is None and cleanup_task.done():
            try:
                cleanup_task.result()
            except BaseException as error:
                cleanup_error = error
        if caller_cancellation is not None:
            raise caller_cancellation
        if cleanup_error is not None:
            raise cleanup_error

    async def _finish_close(self) -> None:
        await self._operation_lock.acquire()
        try:
            if self._buttons is None:
                return
            try:
                released = await self._buttons.release_knowledge_control(
                    self._button_owner
                )
            except BaseException:
                if self._lease_token is not None and self._knowledge_is_inactive():
                    self._clear_lease()
                raise
            if self._lease_token is not None and released:
                self._clear_lease()
        finally:
            self._operation_lock.release()

    async def _require_owner(self, token: str) -> None:
        deadline = self._lease_expires_at
        generation = self._lease_generation
        if not self._matches_owner(token):
            raise self._lease_expired_error()
        if deadline is not None and deadline <= self._clock():
            await self._expire_lease(token, deadline, generation)
            current_deadline = self._lease_expires_at
            if (
                self._matches_owner(token)
                and self._lease_generation != generation
                and current_deadline is not None
                and current_deadline > self._clock()
            ):
                return
            raise self._lease_expired_error()

    async def _require_owner_locked(self, token: str) -> None:
        deadline = self._lease_expires_at
        if not self._matches_owner(token):
            raise self._lease_expired_error()
        if deadline is not None and deadline <= self._clock():
            await self._cancel_expired_knowledge_locked()
            raise self._lease_expired_error()

    async def _acquire_operation(self, token: str | None = None) -> None:
        self._raise_operation_busy(token)
        await self._operation_lock.acquire()

    def _raise_operation_busy(self, token: str | None = None) -> None:
        if not self._operation_lock.locked():
            return
        raise self._error(
            "knowledge_operation_busy",
            "知识补充操作正在进行",
            409,
            observer=True,
            owner_token=token,
        )

    def _renew_lease(self) -> None:
        token = self._lease_token
        if token is None:
            return
        deadline = self._clock() + self._lease_seconds
        self._lease_expires_at = deadline
        self._lease_generation += 1
        generation = self._lease_generation
        old_task = self._lease_task
        if old_task is not None and old_task is not asyncio.current_task():
            old_task.cancel()
        self._lease_task = asyncio.create_task(
            self._expire_after(token, deadline, generation),
            name="knowledge-web-lease-expiry",
        )

    async def _expire_after(
        self,
        token: str,
        deadline: float,
        generation: int,
    ) -> None:
        try:
            remaining = max(0.0, deadline - self._clock())
            await self._sleep(remaining)
            if self._clock() < deadline:
                await self._sleep(deadline - self._clock())
            await self._expire_lease(token, deadline, generation)
        except asyncio.CancelledError:
            raise

    async def _expire_elapsed_lease(self) -> None:
        token = self._lease_token
        deadline = self._lease_expires_at
        generation = self._lease_generation
        if token is None or deadline is None or deadline > self._clock():
            return
        await self._expire_lease(token, deadline, generation)

    async def _expire_lease(
        self,
        token: str,
        deadline: float,
        generation: int,
    ) -> None:
        await self._operation_lock.acquire()
        try:
            if (
                self._matches_owner(token)
                and self._lease_expires_at == deadline
                and self._lease_generation == generation
                and self._clock() >= deadline
            ):
                await self._cancel_expired_knowledge_locked()
        finally:
            self._operation_lock.release()

    async def _expire_elapsed_lease_locked(self) -> None:
        token = self._lease_token
        deadline = self._lease_expires_at
        generation = self._lease_generation
        if token is None or deadline is None or deadline > self._clock():
            return
        if (
            self._matches_owner(token)
            and self._lease_expires_at == deadline
            and self._lease_generation == generation
        ):
            await self._cancel_expired_knowledge_locked()

    async def _cancel_expired_knowledge_locked(self) -> bool:
        try:
            released = await self._buttons.release_knowledge_control(
                self._button_owner
            )
        except Exception as error:
            self._log_expiry_cancel_failure(type(error).__name__)
            if self._knowledge_is_inactive():
                self._clear_lease()
                return True
            return False
        if (
            not released
            or self._buttons.mode is not ButtonInteractionMode.DIALOGUE
            or self._knowledge.state is not KnowledgeModeState.INACTIVE
        ):
            self._log_expiry_cancel_failure("KnowledgeModeStillActive")
            return False
        self._clear_lease()
        return True

    def _knowledge_is_inactive(self) -> bool:
        return (
            self._buttons.mode is ButtonInteractionMode.DIALOGUE
            and self._knowledge.state is KnowledgeModeState.INACTIVE
        )

    def _log_expiry_cancel_failure(self, error_type: str) -> None:
        mode_state = (
            self._knowledge.state
            if self._knowledge is not None
            else KnowledgeModeState.INACTIVE
        )
        logger.warning(
            "knowledge_lease_cancel_failed error_type=%s mode_state=%s",
            error_type,
            mode_state,
        )

    def _clear_lease(self) -> None:
        task = self._lease_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        self._lease_task = None
        self._lease_token = None
        self._lease_expires_at = None
        self._lease_generation += 1

    def _renew_owned_lease(self, token: str) -> None:
        if self._matches_owner(token):
            self._renew_lease()

    def _matches_owner(self, token: str) -> bool:
        owner = self._lease_token
        return owner is not None and secrets.compare_digest(token, owner)

    def _observer_or_available_state(self) -> KnowledgeWebState:
        if self._lease_token is None:
            return self._state(KnowledgeControlState.AVAILABLE)
        return self._state(KnowledgeControlState.OBSERVED)

    def _state(self, control_state: KnowledgeControlState) -> KnowledgeWebState:
        is_owner = control_state is KnowledgeControlState.OWNED
        mode_state = (
            self._knowledge.state
            if self._knowledge is not None
            else KnowledgeModeState.INACTIVE
        )
        processing_stage = (
            self._knowledge.processing_stage
            if self._knowledge is not None
            else None
        )
        draft_text = (
            self._knowledge.draft_text
            if is_owner and self._knowledge is not None
            else None
        )
        return KnowledgeWebState(
            enabled=self._outbox is not None,
            mode_state=mode_state,
            processing_stage=processing_stage,
            control_state=control_state,
            lease_expires_at=self._lease_expires_at if is_owner else None,
            draft_text=draft_text,
            last_entry_id=self._last_entry_id,
        )

    def _error(
        self,
        code: str,
        detail: str,
        status_code: int,
        *,
        observer: bool = False,
        owner_token: str | None = None,
    ) -> KnowledgeWebError:
        if owner_token is not None and self._matches_owner(owner_token):
            control = KnowledgeControlState.OWNED
        elif observer and self._lease_token is not None:
            control = KnowledgeControlState.OBSERVED
        else:
            control = KnowledgeControlState.AVAILABLE
        return KnowledgeWebError(code, detail, status_code, self._state(control))

    def _lease_expired_error(self) -> KnowledgeWebError:
        return self._error(
            "knowledge_lease_expired",
            "知识补充控制权已过期",
            409,
            observer=self._lease_token is not None,
        )

    def _raise_mapped(self, error: Exception, token: str | None = None) -> None:
        mapped = self._mapped_error(error, token)
        if mapped is not None:
            raise mapped from error

    def _mapped_error(
        self,
        error: Exception,
        token: str | None = None,
    ) -> KnowledgeWebError | None:
        if isinstance(error, InvalidDeviceAudio):
            code, status = "recording_too_short", 422
        elif isinstance(error, NoSpeechDetected):
            code, status = "no_speech", 422
        elif isinstance(error, KnowledgeAsrUnavailable):
            code, status = "asr_unavailable", 503
        elif isinstance(error, KnowledgeTtsUnavailable):
            code, status = "tts_unavailable", 503
        elif isinstance(error, sqlite3.Error):
            code, status = "local_save_failed", 503
        elif isinstance(error, LocalAudioError):
            code, status = "playback_unavailable", 503
        else:
            return None
        control = (
            KnowledgeControlState.OWNED
            if token is not None and self._matches_owner(token)
            else KnowledgeControlState.AVAILABLE
        )
        return KnowledgeWebError(
            code,
            str(error),
            status,
            self._state(control),
        )

    @staticmethod
    def _entry_snapshot(entry: KnowledgeEntry) -> KnowledgeEntrySnapshot:
        if entry.state is not OutboxState.SYNCED and entry.last_error is not None:
            sync_state = KnowledgeSyncState.RETRYING
        elif entry.state is OutboxState.PENDING:
            sync_state = KnowledgeSyncState.LOCAL_SAVED
        elif entry.state is OutboxState.UPLOADING:
            sync_state = KnowledgeSyncState.UPLOADING
        elif entry.state is OutboxState.UPLOADED:
            sync_state = KnowledgeSyncState.PROCESSING
        else:
            sync_state = KnowledgeSyncState.SYNCED
        return KnowledgeEntrySnapshot(
            entry_id=entry.id,
            sync_state=sync_state,
            attempts=entry.attempts,
            last_error=entry.last_error,
            next_attempt_at=entry.next_attempt_at,
            updated_at=entry.updated_at,
        )
