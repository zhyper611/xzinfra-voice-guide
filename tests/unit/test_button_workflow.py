import asyncio
import io
import wave
from unittest.mock import AsyncMock, MagicMock

import pytest

from showroom_guide.button_workflow import ButtonInteractionMode, DeviceButtonWorkflow
from showroom_guide.knowledge_capture import KnowledgeDraft
from showroom_guide.knowledge_mode import (
    KnowledgeLongPressResult,
    KnowledgeModeState,
    KnowledgeModeWorkflow,
)


def make_wav():
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes((3000).to_bytes(2, "little", signed=True) * 16000)
    return output.getvalue()


class StatefulCapture:
    def __init__(self):
        self._draft = None
        self.save_calls = 0
        self.saved_entry = MagicMock(id="entry-id")

    @property
    def has_draft(self):
        return self._draft is not None

    @property
    def draft_text(self):
        return self._draft.text if self._draft is not None else None

    async def transcribe(self, _audio):
        return "确认后的知识。"

    async def synthesize_review(self, text):
        return KnowledgeDraft(text, b"review-wav")

    def accept(self, draft):
        self._draft = draft

    def save(self):
        self.save_calls += 1
        self._draft = None
        return self.saved_entry

    def clear(self):
        self._draft = None


def make_real_workflow():
    audio = MagicMock()
    audio.start_recording = AsyncMock()
    audio.stop_recording = AsyncMock(return_value=make_wav())
    audio.abort_recording = AsyncMock()
    audio.play = AsyncMock()
    audio.play_start_cue = AsyncMock()
    audio.play_stop_cue = AsyncMock()
    audio.play_no_speech_prompt = AsyncMock()
    audio.play_prompt = AsyncMock()
    capture = StatefulCapture()
    knowledge = KnowledgeModeWorkflow(audio, capture)
    local_device = MagicMock(is_idle=True, is_recording=False)
    return DeviceButtonWorkflow(local_device, knowledge), knowledge, audio, capture


@pytest.mark.asyncio
async def test_short_press_toggles_dialogue_recording():
    local_device = MagicMock()
    local_device.is_recording = False
    local_device.is_idle = True
    local_device.start_recording = AsyncMock()
    local_device.stop_recording = AsyncMock()
    knowledge = MagicMock()
    workflow = DeviceButtonWorkflow(local_device, knowledge)

    await workflow.short_press()
    local_device.start_recording.assert_awaited_once_with()

    local_device.is_recording = True
    await workflow.short_press()
    local_device.stop_recording.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_long_press_enters_knowledge_mode_and_confirmation_saves_once():
    local_device = MagicMock(is_idle=True, is_recording=False)
    knowledge = MagicMock()
    knowledge.enter = AsyncMock()
    saved_entry = MagicMock(id="entry-id")
    saved = KnowledgeLongPressResult(exited=True, saved_entry=saved_entry)
    knowledge.long_press = AsyncMock(return_value=saved)
    workflow = DeviceButtonWorkflow(local_device, knowledge)

    await workflow.long_press()

    assert workflow.mode is ButtonInteractionMode.KNOWLEDGE
    knowledge.enter.assert_awaited_once_with()

    result = await workflow.long_press()

    knowledge.long_press.assert_awaited_once_with()
    assert result is saved
    assert workflow.mode is ButtonInteractionMode.DIALOGUE


@pytest.mark.asyncio
async def test_long_press_keeps_knowledge_mode_when_workflow_does_not_exit():
    local_device = MagicMock(is_idle=True, is_recording=False)
    knowledge = MagicMock()
    knowledge.enter = AsyncMock()
    result = KnowledgeLongPressResult(exited=False, saved_entry=None)
    knowledge.long_press = AsyncMock(return_value=result)
    workflow = DeviceButtonWorkflow(local_device, knowledge)
    await workflow.long_press()

    returned = await workflow.long_press()

    assert returned is result
    assert workflow.mode is ButtonInteractionMode.KNOWLEDGE


@pytest.mark.asyncio
async def test_enter_prompt_cancellation_rolls_both_workflows_back():
    workflow, knowledge, audio, _ = make_real_workflow()
    prompt_started = asyncio.Event()
    never_release = asyncio.Event()

    async def block_enter_prompt(name):
        assert name == "knowledge-mode"
        prompt_started.set()
        await never_release.wait()

    audio.play_prompt.side_effect = block_enter_prompt
    entering = asyncio.create_task(workflow.long_press())
    await prompt_started.wait()

    entering.cancel()

    with pytest.raises(asyncio.CancelledError):
        await entering
    assert workflow.mode is ButtonInteractionMode.DIALOGUE
    assert knowledge.state is KnowledgeModeState.INACTIVE


@pytest.mark.asyncio
async def test_empty_exit_prompt_cancellation_exits_both_workflows():
    workflow, knowledge, audio, _ = make_real_workflow()
    await workflow.long_press()
    cue_started = asyncio.Event()
    never_release = asyncio.Event()

    async def block_stop_cue():
        cue_started.set()
        await never_release.wait()

    audio.play_stop_cue.side_effect = block_stop_cue
    exiting = asyncio.create_task(workflow.long_press())
    await cue_started.wait()

    exiting.cancel()

    with pytest.raises(asyncio.CancelledError):
        await exiting
    assert workflow.mode is ButtonInteractionMode.DIALOGUE
    assert knowledge.state is KnowledgeModeState.INACTIVE


@pytest.mark.asyncio
async def test_saved_prompt_cancellation_keeps_single_save_and_exits_both_workflows():
    workflow, knowledge, audio, capture = make_real_workflow()
    await workflow.long_press()
    await workflow.short_press()
    await workflow.short_press()
    prompt_started = asyncio.Event()
    never_release = asyncio.Event()

    async def block_saved_prompt(name):
        assert name == "knowledge-saved"
        prompt_started.set()
        await never_release.wait()

    audio.play_prompt.side_effect = block_saved_prompt
    exiting = asyncio.create_task(workflow.long_press())
    await prompt_started.wait()
    assert capture.save_calls == 1
    assert capture.draft_text is None

    exiting.cancel()

    with pytest.raises(asyncio.CancelledError):
        await exiting
    assert capture.save_calls == 1
    assert workflow.mode is ButtonInteractionMode.DIALOGUE
    assert knowledge.state is KnowledgeModeState.INACTIVE


@pytest.mark.asyncio
async def test_cancel_knowledge_only_cancels_active_knowledge_mode():
    local_device = MagicMock(is_idle=True, is_recording=False)
    knowledge = MagicMock()
    knowledge.enter = AsyncMock()
    knowledge.cancel = AsyncMock()
    workflow = DeviceButtonWorkflow(local_device, knowledge)

    await workflow.cancel_knowledge()
    knowledge.cancel.assert_not_awaited()

    await workflow.long_press()
    await workflow.cancel_knowledge()

    knowledge.cancel.assert_awaited_once_with()
    assert workflow.mode is ButtonInteractionMode.DIALOGUE


@pytest.mark.asyncio
async def test_cancel_knowledge_restores_dialogue_mode_when_cancel_fails():
    local_device = MagicMock(is_idle=True, is_recording=False)
    knowledge = MagicMock()
    knowledge.enter = AsyncMock()
    knowledge.cancel = AsyncMock(side_effect=RuntimeError("cancel failed"))
    workflow = DeviceButtonWorkflow(local_device, knowledge)
    await workflow.long_press()

    with pytest.raises(RuntimeError, match="cancel failed"):
        await workflow.cancel_knowledge()

    assert workflow.mode is ButtonInteractionMode.DIALOGUE


@pytest.mark.asyncio
async def test_control_owner_blocks_physical_presses_until_release():
    workflow, knowledge, audio, _ = make_real_workflow()
    owner = object()

    acquired = await workflow.acquire_knowledge_control(owner)
    await workflow.short_press()
    await workflow.long_press()

    assert acquired is True
    assert workflow.mode is ButtonInteractionMode.KNOWLEDGE
    assert knowledge.state is KnowledgeModeState.READY
    audio.start_recording.assert_not_awaited()

    released = await workflow.release_knowledge_control(owner)
    assert released is True
    assert workflow.mode is ButtonInteractionMode.DIALOGUE
    assert knowledge.state is KnowledgeModeState.INACTIVE

    await workflow.long_press()
    assert workflow.mode is ButtonInteractionMode.KNOWLEDGE
    assert knowledge.state is KnowledgeModeState.READY


@pytest.mark.asyncio
async def test_cancelled_control_acquire_does_not_leave_owner_gate():
    workflow, knowledge, audio, _ = make_real_workflow()
    prompt_started = asyncio.Event()
    never_release = asyncio.Event()

    async def block_enter_prompt(name):
        assert name == "knowledge-mode"
        prompt_started.set()
        await never_release.wait()

    audio.play_prompt.side_effect = block_enter_prompt
    acquiring = asyncio.create_task(workflow.acquire_knowledge_control(object()))
    await prompt_started.wait()
    acquiring.cancel()

    with pytest.raises(asyncio.CancelledError):
        await acquiring
    assert workflow.mode is ButtonInteractionMode.DIALOGUE
    assert knowledge.state is KnowledgeModeState.INACTIVE

    audio.play_prompt.side_effect = None
    await workflow.long_press()
    assert workflow.mode is ButtonInteractionMode.KNOWLEDGE
    await workflow.cancel_knowledge()


@pytest.mark.asyncio
async def test_web_operation_is_rejected_while_in_knowledge_mode():
    local_device = MagicMock(is_idle=True, is_recording=False)
    knowledge = MagicMock()
    knowledge.enter = AsyncMock()
    workflow = DeviceButtonWorkflow(local_device, knowledge)
    await workflow.long_press()
    operation = AsyncMock()

    with pytest.raises(RuntimeError, match="知识补充模式"):
        await workflow.run_dialogue(operation)

    operation.assert_not_awaited()
