import asyncio
import logging
import sqlite3

import pytest

from showroom_guide.button_workflow import ButtonInteractionMode, DeviceButtonWorkflow
from showroom_guide.device import InvalidDeviceAudio, NoSpeechDetected
from showroom_guide.knowledge_capture import (
    KnowledgeAsrUnavailable,
    KnowledgeTtsUnavailable,
)
from showroom_guide.knowledge_mode import (
    KnowledgeLongPressResult,
    KnowledgeModeState,
    KnowledgeProcessingStage,
)
from showroom_guide.knowledge_outbox import KnowledgeEntry, KnowledgeOutbox, OutboxState
from showroom_guide.knowledge_web import (
    KnowledgeControlState,
    KnowledgeSyncState,
    KnowledgeWebController,
    KnowledgeWebError,
)
from showroom_guide.local_audio import LocalAudioError


class FakeClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ControlledSleep:
    def __init__(self) -> None:
        self.waiters: list[asyncio.Future[None]] = []

    async def __call__(self, _seconds: float) -> None:
        waiter = asyncio.get_running_loop().create_future()
        self.waiters.append(waiter)
        await waiter

    def release_pending(self) -> None:
        for waiter in self.waiters:
            if not waiter.done():
                waiter.set_result(None)


class CancellationBlockingSleep:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cleanup_started = asyncio.Event()
        self.never_release = asyncio.Event()

    async def __call__(self, _seconds: float) -> None:
        self.started.set()
        try:
            await self.never_release.wait()
        finally:
            self.cleanup_started.set()
            await self.never_release.wait()


class StatefulKnowledge:
    def __init__(self) -> None:
        self.state = KnowledgeModeState.INACTIVE
        self.processing_stage = None
        self.draft_text = None
        self.short_error = None
        self.long_error = None
        self.cancel_error = None
        self.cancel_clears_on_error = True
        self.cancel_calls = 0
        self.long_calls = 0
        self.enter_started = asyncio.Event()
        self.enter_release: asyncio.Event | None = None
        self.short_started = asyncio.Event()
        self.short_release: asyncio.Event | None = None
        self.saved_entry = KnowledgeEntry(
            id="entry-id",
            content="展厅知识正文",
            filename="entry.md",
            state=OutboxState.PENDING,
            attempts=0,
            next_attempt_at=0.0,
            last_error=None,
            updated_at=0.0,
        )

    async def enter(self) -> None:
        self.enter_started.set()
        if self.enter_release is not None:
            await self.enter_release.wait()
        self.state = KnowledgeModeState.READY

    async def short_press(self) -> None:
        self.short_started.set()
        if self.short_release is not None:
            await self.short_release.wait()
        if self.short_error is not None:
            raise self.short_error
        if self.state in {KnowledgeModeState.READY, KnowledgeModeState.CONFIRMING}:
            self.state = KnowledgeModeState.RECORDING
            return
        if self.state is KnowledgeModeState.RECORDING:
            self.state = KnowledgeModeState.CONFIRMING
            self.draft_text = "展厅知识正文"

    async def long_press(self) -> KnowledgeLongPressResult:
        self.long_calls += 1
        if self.long_error is not None:
            raise self.long_error
        saved_entry = self.saved_entry if self.draft_text is not None else None
        self.state = KnowledgeModeState.INACTIVE
        self.draft_text = None
        return KnowledgeLongPressResult(exited=True, saved_entry=saved_entry)

    async def cancel(self) -> None:
        self.cancel_calls += 1
        if self.cancel_error is not None and not self.cancel_clears_on_error:
            raise self.cancel_error
        self.state = KnowledgeModeState.INACTIVE
        self.processing_stage = None
        self.draft_text = None
        if self.cancel_error is not None:
            raise self.cancel_error


def make_controller_with_buttons(
    tmp_path,
    *,
    enabled=True,
    lease_seconds=120.0,
    local_idle=True,
    sleep=None,
):
    clock = FakeClock()
    sleep = sleep or ControlledSleep()
    knowledge = StatefulKnowledge()
    local_device = type(
        "IdleLocalDevice",
        (),
        {"is_idle": local_idle, "is_recording": False},
    )()
    buttons = DeviceButtonWorkflow(local_device, knowledge)
    outbox = (
        KnowledgeOutbox(tmp_path / "knowledge.sqlite3", clock=clock)
        if enabled
        else None
    )
    controller = KnowledgeWebController(
        buttons,
        knowledge,
        outbox,
        lease_seconds=lease_seconds,
        clock=clock,
        sleep=sleep,
    )
    return controller, buttons, knowledge, outbox, clock, sleep


def make_controller(
    tmp_path,
    *,
    enabled=True,
    lease_seconds=120.0,
    local_idle=True,
    sleep=None,
):
    controller, _, knowledge, outbox, clock, sleep = make_controller_with_buttons(
        tmp_path,
        enabled=enabled,
        lease_seconds=lease_seconds,
        local_idle=local_idle,
        sleep=sleep,
    )
    return controller, knowledge, outbox, clock, sleep


@pytest.mark.asyncio
async def test_first_page_acquires_control_and_observer_is_denied(tmp_path):
    controller, _, _, _, _ = make_controller(tmp_path)

    token, state = await controller.acquire()

    assert token
    assert state.enabled is True
    assert state.control_state is KnowledgeControlState.OWNED
    assert state.mode_state is KnowledgeModeState.READY
    with pytest.raises(KnowledgeWebError) as caught:
        await controller.acquire()
    assert caught.value.code == "knowledge_control_busy"
    assert caught.value.status_code == 409
    assert caught.value.state.control_state is KnowledgeControlState.OBSERVED
    await controller.aclose()


@pytest.mark.asyncio
async def test_second_acquire_is_busy_while_first_is_entering_mode(tmp_path):
    controller, knowledge, _, _, _ = make_controller(tmp_path)
    knowledge.enter_release = asyncio.Event()
    acquiring = asyncio.create_task(controller.acquire())
    await knowledge.enter_started.wait()

    with pytest.raises(KnowledgeWebError) as caught:
        await controller.acquire()

    assert caught.value.code == "knowledge_control_busy"
    assert caught.value.status_code == 409
    knowledge.enter_release.set()
    await acquiring
    await controller.aclose()


@pytest.mark.asyncio
async def test_acquire_does_not_issue_lease_when_device_is_busy(tmp_path):
    controller, knowledge, _, _, _ = make_controller(
        tmp_path,
        local_idle=False,
    )

    try:
        with pytest.raises(KnowledgeWebError) as caught:
            await controller.acquire()
    finally:
        await controller.aclose()

    assert caught.value.code == "knowledge_control_busy"
    assert caught.value.status_code == 409
    assert caught.value.state.control_state is KnowledgeControlState.AVAILABLE
    assert knowledge.state is KnowledgeModeState.INACTIVE
    assert (await controller.state()).control_state is KnowledgeControlState.AVAILABLE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "active_state",
    [KnowledgeModeState.READY, KnowledgeModeState.CONFIRMING],
)
async def test_acquire_does_not_mutate_physical_knowledge_session(
    tmp_path,
    active_state,
):
    controller, buttons, knowledge, _, _, _ = make_controller_with_buttons(tmp_path)
    await buttons.long_press()
    knowledge.state = active_state
    knowledge.draft_text = "实体会话草稿" if active_state is KnowledgeModeState.CONFIRMING else None

    try:
        with pytest.raises(KnowledgeWebError) as caught:
            await controller.acquire()

        assert caught.value.code == "knowledge_control_busy"
        assert buttons.mode is ButtonInteractionMode.KNOWLEDGE
        assert knowledge.state is active_state
        assert knowledge.draft_text == (
            "实体会话草稿"
            if active_state is KnowledgeModeState.CONFIRMING
            else None
        )
        assert knowledge.long_calls == 0
        assert (await controller.state()).control_state is KnowledgeControlState.AVAILABLE
    finally:
        await buttons.cancel_knowledge()
        await controller.aclose()


@pytest.mark.asyncio
async def test_owner_sees_draft_but_observer_state_is_redacted(tmp_path):
    controller, knowledge, _, _, _ = make_controller(tmp_path)
    token, _ = await controller.acquire()
    knowledge.state = KnowledgeModeState.CONFIRMING
    knowledge.processing_stage = KnowledgeProcessingStage.SYNTHESIZING
    knowledge.draft_text = "不能泄露的知识正文"

    owner = await controller.state(token)
    with pytest.raises(KnowledgeWebError) as caught:
        await controller.acquire()
    observer = caught.value.state

    assert owner.draft_text == "不能泄露的知识正文"
    assert owner.lease_expires_at is not None
    assert observer.draft_text is None
    assert observer.lease_expires_at is None
    assert observer.mode_state is KnowledgeModeState.CONFIRMING
    assert observer.processing_stage is KnowledgeProcessingStage.SYNTHESIZING
    await controller.aclose()


@pytest.mark.asyncio
async def test_owner_poll_renews_lease(tmp_path):
    controller, knowledge, _, clock, _ = make_controller(tmp_path)
    token, acquired = await controller.acquire()
    initial_deadline = acquired.lease_expires_at
    clock.advance(30)

    renewed = await controller.state(token)

    assert renewed.lease_expires_at == initial_deadline + 30
    assert knowledge.cancel_calls == 0
    await controller.aclose()


@pytest.mark.asyncio
async def test_short_press_crossing_deadline_renews_from_completion(tmp_path):
    controller, knowledge, _, clock, sleep = make_controller(tmp_path)
    token, _ = await controller.acquire()
    await asyncio.sleep(0)
    initial_expiry_waiter = sleep.waiters[0]
    knowledge.state = KnowledgeModeState.RECORDING
    knowledge.short_release = asyncio.Event()

    running = asyncio.create_task(controller.short_press(token))
    await knowledge.short_started.wait()
    await asyncio.sleep(0)
    try:
        assert initial_expiry_waiter.cancelled()
        assert len(sleep.waiters) >= 2

        clock.advance(121)
        sleep.release_pending()
        await asyncio.sleep(0)
        knowledge.short_release.set()
        completed = await running
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert completed.control_state is KnowledgeControlState.OWNED
        assert completed.mode_state is KnowledgeModeState.CONFIRMING
        assert completed.lease_expires_at == clock.now + 120
        assert knowledge.state is KnowledgeModeState.CONFIRMING
        assert knowledge.cancel_calls == 0
    finally:
        knowledge.short_release.set()
        if not running.done():
            await running
        await controller.aclose()


@pytest.mark.asyncio
async def test_state_accepts_renewal_while_waiting_on_expired_generation(tmp_path):
    controller, knowledge, _, clock, _ = make_controller(
        tmp_path,
        lease_seconds=10,
    )
    token, _ = await controller.acquire()
    knowledge.state = KnowledgeModeState.RECORDING
    knowledge.short_release = asyncio.Event()
    running = asyncio.create_task(controller.short_press(token))
    await knowledge.short_started.wait()
    clock.advance(11)

    polling = asyncio.create_task(controller.state(token))
    await asyncio.sleep(0)
    assert polling.done() is False

    try:
        knowledge.short_release.set()
        completed = await running
        polled = await polling

        assert completed.control_state is KnowledgeControlState.OWNED
        assert completed.mode_state is KnowledgeModeState.CONFIRMING
        assert polled.control_state is KnowledgeControlState.OWNED
        assert polled.mode_state is KnowledgeModeState.CONFIRMING
        assert polled.lease_expires_at == clock.now + 10
        assert (await controller.state(token)).control_state is KnowledgeControlState.OWNED
        assert knowledge.cancel_calls == 0
    finally:
        knowledge.short_release.set()
        if not running.done():
            await running
        if not polling.done():
            polling.cancel()
            with pytest.raises(asyncio.CancelledError):
                await polling
        await controller.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation_name", ["short_press", "long_press", "release"])
async def test_failed_owned_operation_renews_lease_for_retry(
    tmp_path,
    operation_name,
):
    controller, knowledge, _, clock, _ = make_controller(tmp_path)
    token, _ = await controller.acquire()
    clock.advance(30)
    if operation_name == "short_press":
        knowledge.short_error = KnowledgeAsrUnavailable("asr unavailable")
    elif operation_name == "long_press":
        knowledge.long_error = sqlite3.OperationalError("database unavailable")
    else:
        knowledge.cancel_clears_on_error = False
        knowledge.cancel_error = LocalAudioError("abort failed")

    try:
        with pytest.raises(KnowledgeWebError) as caught:
            await getattr(controller, operation_name)(token)

        assert caught.value.state.control_state is KnowledgeControlState.OWNED
        assert caught.value.state.lease_expires_at == clock.now + 120
    finally:
        knowledge.short_error = None
        knowledge.long_error = None
        knowledge.cancel_error = None
        try:
            await controller.release(token)
        except KnowledgeWebError:
            pass
        await controller.aclose()


@pytest.mark.asyncio
async def test_expired_lease_cancels_mode_and_rejects_old_token(tmp_path):
    controller, knowledge, _, clock, sleep = make_controller(
        tmp_path,
        lease_seconds=10,
    )
    token, _ = await controller.acquire()
    await asyncio.sleep(0)
    clock.advance(11)

    sleep.release_pending()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert knowledge.cancel_calls == 1
    assert knowledge.state is KnowledgeModeState.INACTIVE
    with pytest.raises(KnowledgeWebError) as caught:
        await controller.state(token)
    assert caught.value.code == "knowledge_lease_expired"
    assert caught.value.status_code == 409
    await controller.aclose()


@pytest.mark.asyncio
async def test_long_press_returns_entry_id_and_releases_lease(tmp_path):
    controller, knowledge, _, _, _ = make_controller(tmp_path)
    token, _ = await controller.acquire()
    knowledge.state = KnowledgeModeState.CONFIRMING
    knowledge.draft_text = "可保存知识"

    state = await controller.long_press(token)

    assert state.last_entry_id == "entry-id"
    assert state.control_state is KnowledgeControlState.AVAILABLE
    assert state.mode_state is KnowledgeModeState.INACTIVE
    with pytest.raises(KnowledgeWebError) as caught:
        await controller.state(token)
    assert caught.value.code == "knowledge_lease_expired"
    await controller.aclose()


@pytest.mark.asyncio
async def test_concurrent_operation_is_rejected_without_waiting(tmp_path):
    controller, knowledge, _, _, _ = make_controller(tmp_path)
    token, _ = await controller.acquire()
    knowledge.short_release = asyncio.Event()
    running = asyncio.create_task(controller.short_press(token))
    await knowledge.short_started.wait()

    with pytest.raises(KnowledgeWebError) as caught:
        await controller.short_press(token)
    assert caught.value.code == "knowledge_operation_busy"
    assert caught.value.status_code == 409

    knowledge.short_release.set()
    await running
    await controller.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation_name",
    ["acquire", "short_press", "long_press", "release"],
)
async def test_expired_lease_does_not_wait_behind_active_operation(
    tmp_path,
    operation_name,
):
    controller, knowledge, _, clock, _ = make_controller(
        tmp_path,
        lease_seconds=10,
    )
    token, _ = await controller.acquire()
    knowledge.short_release = asyncio.Event()
    running = asyncio.create_task(controller.short_press(token))
    await knowledge.short_started.wait()
    clock.advance(11)

    operation = getattr(controller, operation_name)
    contender = asyncio.create_task(
        operation() if operation_name == "acquire" else operation(token)
    )
    try:
        await asyncio.sleep(0)
        assert contender.done(), "过期租约的控制操作不应等待 operation lock"
    finally:
        knowledge.short_release.set()
        await running
        if not contender.done():
            contender.cancel()
        try:
            await contender
        except KnowledgeWebError:
            pass
        except asyncio.CancelledError:
            pass
        await controller.aclose()

    with pytest.raises(KnowledgeWebError) as caught:
        await contender
    assert caught.value.code == "knowledge_operation_busy"
    assert caught.value.status_code == 409


@pytest.mark.asyncio
async def test_aclose_propagates_caller_cancellation_while_waiting_for_lease_task(
    tmp_path,
):
    sleep = CancellationBlockingSleep()
    controller, knowledge, _, _, _ = make_controller(tmp_path, sleep=sleep)
    token, _ = await controller.acquire()
    await sleep.started.wait()
    closing = asyncio.create_task(controller.aclose())
    await sleep.cleanup_started.wait()

    closing.cancel()

    with pytest.raises(asyncio.CancelledError):
        await closing
    assert knowledge.state is KnowledgeModeState.INACTIVE
    assert (await controller.state()).control_state is KnowledgeControlState.AVAILABLE


@pytest.mark.asyncio
async def test_aclose_defers_cancellation_while_waiting_for_operation_lock(tmp_path):
    controller, knowledge, _, _, sleep = make_controller(tmp_path)
    token, _ = await controller.acquire()
    await asyncio.sleep(0)
    lease_waiter = sleep.waiters[0]
    knowledge.short_release = asyncio.Event()
    running = asyncio.create_task(controller.short_press(token))
    await knowledge.short_started.wait()
    closing = asyncio.create_task(controller.aclose())
    for _ in range(3):
        await asyncio.sleep(0)
    assert lease_waiter.cancelled()

    closing.cancel()
    try:
        await asyncio.sleep(0)
        assert not closing.done(), "aclose 必须等待活动操作结束并完成清理"
    finally:
        knowledge.short_release.set()
        await running
        try:
            await closing
        except asyncio.CancelledError:
            pass
        if (await controller.state()).control_state is not KnowledgeControlState.AVAILABLE:
            await controller.aclose()

    assert knowledge.state is KnowledgeModeState.INACTIVE
    assert (await controller.state()).control_state is KnowledgeControlState.AVAILABLE


@pytest.mark.asyncio
async def test_entry_maps_all_outbox_states(tmp_path):
    controller, _, outbox, clock, _ = make_controller(tmp_path)
    local = outbox.enqueue("本地知识")
    retrying = outbox.enqueue("重试知识")
    outbox.mark_failed(
        retrying.id,
        "network failed",
        retry_after_seconds=10,
        requeue=True,
    )
    uploading = outbox.enqueue("上传知识")
    outbox.mark_uploading(uploading.id)
    retrying_upload = outbox.enqueue("上传重试知识")
    outbox.mark_uploading(retrying_upload.id)
    outbox.mark_failed(
        retrying_upload.id,
        "upload failed",
        retry_after_seconds=20,
    )
    retrying_upload_due = clock.now + 20
    processing = outbox.enqueue("处理知识")
    outbox.mark_uploaded(processing.id, retry_after_seconds=10)
    retrying_processing = outbox.enqueue("处理重试知识")
    outbox.mark_uploaded(retrying_processing.id, retry_after_seconds=10)
    outbox.mark_failed(
        retrying_processing.id,
        "document state failed",
        retry_after_seconds=30,
    )
    retrying_processing_due = clock.now + 30
    synced = outbox.enqueue("同步知识")
    clock.advance(1)
    outbox.mark_synced(synced.id)

    local_snapshot = await controller.entry(local.id)
    retry_snapshot = await controller.entry(retrying.id)
    uploading_snapshot = await controller.entry(uploading.id)
    retrying_upload_snapshot = await controller.entry(retrying_upload.id)
    processing_snapshot = await controller.entry(processing.id)
    retrying_processing_snapshot = await controller.entry(retrying_processing.id)
    synced_snapshot = await controller.entry(synced.id)

    assert local_snapshot.sync_state is KnowledgeSyncState.LOCAL_SAVED
    assert retry_snapshot.sync_state is KnowledgeSyncState.RETRYING
    assert retry_snapshot.last_error == "network failed"
    assert uploading_snapshot.sync_state is KnowledgeSyncState.UPLOADING
    assert retrying_upload_snapshot.sync_state is KnowledgeSyncState.RETRYING
    assert retrying_upload_snapshot.attempts == 1
    assert retrying_upload_snapshot.last_error == "upload failed"
    assert retrying_upload_snapshot.next_attempt_at == retrying_upload_due
    assert processing_snapshot.sync_state is KnowledgeSyncState.PROCESSING
    assert retrying_processing_snapshot.sync_state is KnowledgeSyncState.RETRYING
    assert retrying_processing_snapshot.attempts == 1
    assert retrying_processing_snapshot.last_error == "document state failed"
    assert retrying_processing_snapshot.next_attempt_at == retrying_processing_due
    assert synced_snapshot.sync_state is KnowledgeSyncState.SYNCED
    assert synced_snapshot.updated_at == clock.now
    await controller.aclose()


@pytest.mark.asyncio
async def test_missing_entry_has_stable_not_found_error(tmp_path):
    controller, _, _, _, _ = make_controller(tmp_path)

    with pytest.raises(KnowledgeWebError) as caught:
        await controller.entry("missing-entry")

    assert caught.value.code == "knowledge_entry_not_found"
    assert caught.value.status_code == 404
    await controller.aclose()


@pytest.mark.asyncio
async def test_disabled_controller_rejects_acquire(tmp_path):
    controller, knowledge, _, _, _ = make_controller(tmp_path, enabled=False)

    with pytest.raises(KnowledgeWebError) as caught:
        await controller.acquire()

    assert caught.value.code == "knowledge_capture_disabled"
    assert caught.value.status_code == 503
    assert caught.value.state.enabled is False
    assert knowledge.state is KnowledgeModeState.INACTIVE
    await controller.aclose()


@pytest.mark.asyncio
async def test_disabled_controller_supports_absent_workflows():
    controller = KnowledgeWebController(None, None, None)

    state = await controller.state()
    with pytest.raises(KnowledgeWebError) as caught:
        await controller.acquire()

    assert state.enabled is False
    assert state.mode_state is KnowledgeModeState.INACTIVE
    assert caught.value.code == "knowledge_capture_disabled"
    await controller.aclose()


@pytest.mark.asyncio
async def test_release_cancels_mode_and_releases_control(tmp_path):
    controller, knowledge, _, _, _ = make_controller(tmp_path)
    token, _ = await controller.acquire()

    released = await controller.release(token)

    assert knowledge.cancel_calls == 1
    assert released.control_state is KnowledgeControlState.AVAILABLE
    assert released.mode_state is KnowledgeModeState.INACTIVE
    with pytest.raises(KnowledgeWebError) as caught:
        await controller.release(token)
    assert caught.value.code == "knowledge_lease_expired"
    await controller.aclose()


@pytest.mark.asyncio
async def test_release_error_state_reflects_cleared_lease(tmp_path):
    controller, knowledge, _, _, _ = make_controller(tmp_path)
    token, _ = await controller.acquire()
    knowledge.draft_text = "不能出现在错误状态中的正文"
    knowledge.cancel_error = LocalAudioError("abort failed")

    with pytest.raises(KnowledgeWebError) as caught:
        await controller.release(token)

    assert caught.value.code == "playback_unavailable"
    assert caught.value.state.control_state is KnowledgeControlState.AVAILABLE
    assert caught.value.state.lease_expires_at is None
    assert caught.value.state.draft_text is None
    assert (await controller.state()).control_state is KnowledgeControlState.AVAILABLE
    await controller.aclose()


@pytest.mark.asyncio
async def test_release_failure_keeps_gate_until_cleanup_can_retry(tmp_path):
    controller, buttons, knowledge, _, _, _ = make_controller_with_buttons(tmp_path)
    token, _ = await controller.acquire()
    knowledge.cancel_clears_on_error = False
    knowledge.cancel_error = LocalAudioError("abort failed")

    with pytest.raises(KnowledgeWebError) as caught:
        await controller.release(token)

    assert caught.value.code == "playback_unavailable"
    assert caught.value.state.control_state is KnowledgeControlState.OWNED
    await buttons.short_press()
    await buttons.long_press()
    assert knowledge.state is KnowledgeModeState.READY
    assert knowledge.short_started.is_set() is False
    assert knowledge.long_calls == 0

    knowledge.cancel_error = None
    released = await controller.release(token)
    assert released.control_state is KnowledgeControlState.AVAILABLE

    await buttons.long_press()
    assert buttons.mode is ButtonInteractionMode.KNOWLEDGE
    assert knowledge.state is KnowledgeModeState.READY
    await buttons.cancel_knowledge()
    await controller.aclose()


@pytest.mark.asyncio
async def test_expiry_cancel_failure_keeps_controlled_state_and_safe_log(
    tmp_path,
    caplog,
):
    controller, knowledge, _, clock, sleep = make_controller(
        tmp_path,
        lease_seconds=10,
    )
    token, _ = await controller.acquire()
    knowledge.draft_text = "不能泄露的后台草稿"
    knowledge.cancel_clears_on_error = False
    knowledge.cancel_error = RuntimeError(
        f"cancel failed token={token} draft={knowledge.draft_text}"
    )
    loop = asyncio.get_running_loop()
    unhandled = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    try:
        await asyncio.sleep(0)
        clock.advance(11)
        with caplog.at_level(logging.WARNING):
            sleep.release_pending()
            await asyncio.sleep(0)
            await asyncio.sleep(0)

            observed = await controller.state()
            with pytest.raises(KnowledgeWebError) as caught:
                await controller.acquire()

        assert observed.control_state is KnowledgeControlState.OBSERVED
        assert observed.lease_expires_at is None
        assert observed.draft_text is None
        assert knowledge.state is KnowledgeModeState.READY
        assert caught.value.code == "knowledge_control_busy"
        assert unhandled == []
        messages = "\n".join(record.getMessage() for record in caplog.records)
        assert "error_type=RuntimeError" in messages
        assert "mode_state=ready" in messages
        assert token not in messages
        assert "不能泄露的后台草稿" not in messages
    finally:
        loop.set_exception_handler(previous_handler)
        knowledge.cancel_error = None
        await controller.aclose()

    assert knowledge.state is KnowledgeModeState.INACTIVE


@pytest.mark.asyncio
async def test_expiry_error_after_cleanup_clears_lease_and_gate(tmp_path):
    controller, buttons, knowledge, _, clock, sleep = make_controller_with_buttons(
        tmp_path,
        lease_seconds=10,
    )
    token, _ = await controller.acquire()
    knowledge.cancel_error = LocalAudioError("prompt failed")
    await asyncio.sleep(0)
    clock.advance(11)

    sleep.release_pending()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert knowledge.state is KnowledgeModeState.INACTIVE
    assert (await controller.state()).control_state is KnowledgeControlState.AVAILABLE
    with pytest.raises(KnowledgeWebError):
        await controller.state(token)

    knowledge.cancel_error = None
    await buttons.long_press()
    assert buttons.mode is ButtonInteractionMode.KNOWLEDGE
    await buttons.cancel_knowledge()
    await controller.aclose()


@pytest.mark.asyncio
async def test_close_error_after_cleanup_does_not_leave_stale_lease(tmp_path):
    controller, buttons, knowledge, _, _, _ = make_controller_with_buttons(tmp_path)
    token, _ = await controller.acquire()
    knowledge.cancel_error = LocalAudioError("prompt failed")

    with pytest.raises(LocalAudioError, match="prompt failed"):
        await controller.aclose()

    assert knowledge.state is KnowledgeModeState.INACTIVE
    assert (await controller.state()).control_state is KnowledgeControlState.AVAILABLE
    with pytest.raises(KnowledgeWebError):
        await controller.state(token)

    knowledge.cancel_error = None
    await buttons.long_press()
    assert buttons.mode is ButtonInteractionMode.KNOWLEDGE
    await buttons.cancel_knowledge()
    await controller.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "code", "status_code"),
    [
        (InvalidDeviceAudio("too short"), "recording_too_short", 422),
        (NoSpeechDetected("no speech"), "no_speech", 422),
        (KnowledgeAsrUnavailable("asr"), "asr_unavailable", 503),
        (KnowledgeTtsUnavailable("tts"), "tts_unavailable", 503),
        (LocalAudioError("speaker"), "playback_unavailable", 503),
    ],
)
async def test_operation_maps_domain_errors(tmp_path, error, code, status_code):
    controller, knowledge, _, _, _ = make_controller(tmp_path)
    token, _ = await controller.acquire()
    knowledge.short_error = error

    with pytest.raises(KnowledgeWebError) as caught:
        await controller.short_press(token)

    assert caught.value.code == code
    assert caught.value.status_code == status_code
    assert caught.value.detail == str(error)
    await controller.aclose()


@pytest.mark.asyncio
async def test_long_press_maps_sqlite_save_failure(tmp_path):
    controller, knowledge, _, _, _ = make_controller(tmp_path)
    token, _ = await controller.acquire()
    knowledge.state = KnowledgeModeState.CONFIRMING
    knowledge.draft_text = "不能写入的正文"
    knowledge.long_error = sqlite3.OperationalError("database unavailable")

    with pytest.raises(KnowledgeWebError) as caught:
        await controller.long_press(token)

    assert caught.value.code == "local_save_failed"
    assert caught.value.status_code == 503
    await controller.aclose()


@pytest.mark.asyncio
async def test_error_logging_does_not_expose_token_or_draft(tmp_path, caplog):
    controller, knowledge, _, _, _ = make_controller(tmp_path)
    token, _ = await controller.acquire()
    knowledge.draft_text = "绝密知识正文"
    knowledge.short_error = KnowledgeAsrUnavailable("asr unavailable")

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(KnowledgeWebError):
            await controller.short_press(token)

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert token not in messages
    assert "绝密知识正文" not in messages
    await controller.aclose()
