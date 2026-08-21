import asyncio
import io
import sqlite3
import wave
from unittest.mock import AsyncMock, MagicMock

import pytest

from showroom_guide.device import InvalidDeviceAudio, NoSpeechDetected
from showroom_guide.knowledge_capture import (
    KnowledgeAsrUnavailable,
    KnowledgeDraft,
    KnowledgeTtsUnavailable,
)
from showroom_guide.knowledge_mode import (
    KnowledgeLongPressResult,
    KnowledgeModeState,
    KnowledgeModeWorkflow,
    KnowledgeProcessingStage,
)
from showroom_guide.local_audio import LocalAudioError


def make_wav(*, seconds=1.0, sample=3000):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(int(sample).to_bytes(2, "little", signed=True) * int(16000 * seconds))
    return output.getvalue()


class FakeKnowledgeCapture:
    def __init__(self):
        self.transcribe = AsyncMock(return_value="确认后的知识。")
        self.synthesize_review = AsyncMock(
            return_value=KnowledgeDraft("确认后的知识。", b"review-wav")
        )
        self.accepted_drafts = []
        self.saved_texts = []
        self.clear_calls = 0
        self.save_error = None
        self.saved_entry = MagicMock(id="entry-id")
        self._draft = None

    @property
    def has_draft(self):
        return self._draft is not None

    @property
    def draft_text(self):
        return self._draft.text if self._draft is not None else None

    def accept(self, draft):
        self.accepted_drafts.append(draft)
        self._draft = draft

    def save(self):
        if self.save_error is not None:
            raise self.save_error
        if self._draft is None:
            raise ValueError("当前没有可保存的知识草稿")
        self.saved_texts.append(self._draft.text)
        self._draft = None
        return self.saved_entry

    def clear(self):
        self.clear_calls += 1
        self._draft = None


def make_workflow(captured=None, *, max_recording_seconds=60.0):
    audio = MagicMock()
    audio.start_recording = AsyncMock()
    audio.stop_recording = AsyncMock(return_value=captured or make_wav())
    audio.abort_recording = AsyncMock()
    audio.play = AsyncMock()
    audio.play_start_cue = AsyncMock()
    audio.play_stop_cue = AsyncMock()
    audio.play_no_speech_prompt = AsyncMock()
    audio.play_prompt = AsyncMock()
    capture = FakeKnowledgeCapture()
    workflow = KnowledgeModeWorkflow(
        audio,
        capture,
        max_recording_seconds=max_recording_seconds,
    )
    return workflow, audio, capture


@pytest.mark.asyncio
async def test_short_press_records_reviews_and_accepts_after_playback():
    workflow, audio, capture = make_workflow()
    await workflow.enter()
    audio.play_prompt.assert_awaited_once_with("knowledge-mode")

    await workflow.short_press()
    assert workflow.state is KnowledgeModeState.RECORDING
    audio.start_recording.assert_awaited_once_with()

    await workflow.short_press()

    capture.transcribe.assert_awaited_once()
    capture.synthesize_review.assert_awaited_once_with("确认后的知识。")
    audio.play.assert_awaited_once_with(b"review-wav")
    assert capture.draft_text == "确认后的知识。"
    assert workflow.state is KnowledgeModeState.CONFIRMING


@pytest.mark.asyncio
async def test_failed_rerecord_returns_to_confirmation_with_old_draft():
    workflow, audio, capture = make_workflow()
    await workflow.enter()
    await workflow.short_press()
    await workflow.short_press()
    old_draft = capture._draft
    capture.transcribe.side_effect = NoSpeechDetected("没有听清")
    await workflow.short_press()

    with pytest.raises(NoSpeechDetected):
        await workflow.short_press()

    assert workflow.state is KnowledgeModeState.CONFIRMING
    assert capture._draft is old_draft
    assert capture.draft_text == "确认后的知识。"
    audio.play_no_speech_prompt.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_long_press_saves_confirmed_draft_then_exits():
    workflow, _, capture = make_workflow()
    await workflow.enter()
    await workflow.short_press()
    await workflow.short_press()

    result = await workflow.long_press()

    assert result == KnowledgeLongPressResult(
        exited=True,
        saved_entry=capture.saved_entry,
    )
    assert capture.saved_texts == ["确认后的知识。"]
    assert capture.draft_text is None
    workflow._audio.play_prompt.assert_awaited_with("knowledge-saved")
    assert workflow.state is KnowledgeModeState.INACTIVE


@pytest.mark.asyncio
async def test_long_press_ready_without_draft_exits_without_saved_entry():
    workflow, _, capture = make_workflow()
    await workflow.enter()

    result = await workflow.long_press()

    assert result == KnowledgeLongPressResult(exited=True, saved_entry=None)
    assert capture.saved_texts == []
    assert workflow.state is KnowledgeModeState.INACTIVE


@pytest.mark.asyncio
async def test_short_recording_is_rejected_without_spoken_error():
    workflow, audio, _ = make_workflow(captured=make_wav(seconds=0.1))
    await workflow.enter()
    await workflow.short_press()

    with pytest.raises(InvalidDeviceAudio, match="太短"):
        await workflow.short_press()

    assert workflow.state is KnowledgeModeState.READY
    audio.play_no_speech_prompt.assert_not_awaited()


@pytest.mark.asyncio
async def test_recording_timeout_runs_review_pipeline_and_reaches_confirmation():
    workflow, audio, capture = make_workflow(max_recording_seconds=0.01)
    await workflow.enter()

    await workflow.short_press()
    for _ in range(20):
        if workflow.state is KnowledgeModeState.CONFIRMING:
            break
        await asyncio.sleep(0.01)

    audio.stop_recording.assert_awaited_once_with()
    capture.transcribe.assert_awaited_once()
    assert workflow.state is KnowledgeModeState.CONFIRMING


@pytest.mark.asyncio
async def test_asr_failure_restores_state_and_plays_dedicated_prompt():
    workflow, audio, capture = make_workflow()
    capture.transcribe.side_effect = KnowledgeAsrUnavailable()
    await workflow.enter()
    await workflow.short_press()

    with pytest.raises(KnowledgeAsrUnavailable):
        await workflow.short_press()

    assert workflow.state is KnowledgeModeState.READY
    audio.play_prompt.assert_awaited_with("asr-unavailable")


@pytest.mark.asyncio
async def test_tts_failure_restores_old_draft_and_plays_dedicated_prompt():
    workflow, audio, capture = make_workflow()
    capture.synthesize_review.side_effect = KnowledgeTtsUnavailable()
    await workflow.enter()
    old_draft = KnowledgeDraft("旧草稿。", b"old-review-wav")
    capture.accept(old_draft)
    await workflow.short_press()

    with pytest.raises(KnowledgeTtsUnavailable):
        await workflow.short_press()

    assert workflow.state is KnowledgeModeState.CONFIRMING
    assert capture._draft is old_draft
    assert capture.draft_text == "旧草稿。"
    audio.play_prompt.assert_awaited_with("tts-unavailable")


@pytest.mark.asyncio
async def test_successful_rerecord_replaces_old_draft_after_pipeline():
    workflow, audio, capture = make_workflow()
    stages = []

    async def transcribe(_audio):
        stages.append(workflow.processing_stage)
        return "新知识。"

    async def synthesize(text):
        stages.append(workflow.processing_stage)
        return KnowledgeDraft(text, b"review-wav")

    async def play(_audio):
        stages.append(workflow.processing_stage)

    capture.transcribe.side_effect = transcribe
    capture.synthesize_review.side_effect = synthesize
    audio.play.side_effect = play
    await workflow.enter()
    capture.accept(KnowledgeDraft("旧草稿。", b"old-review-wav"))
    await workflow.short_press()
    await workflow.short_press()

    assert stages == [
        KnowledgeProcessingStage.TRANSCRIBING,
        KnowledgeProcessingStage.SYNTHESIZING,
        KnowledgeProcessingStage.PLAYING_REVIEW,
    ]
    assert workflow.processing_stage is None
    assert workflow.draft_text == "新知识。"


@pytest.mark.asyncio
async def test_playback_failure_does_not_accept_new_draft_and_keeps_old_draft():
    workflow, audio, capture = make_workflow()
    audio.play.side_effect = LocalAudioError("speaker failed")
    await workflow.enter()
    old_draft = KnowledgeDraft("旧草稿。", b"old-review-wav")
    capture.accept(old_draft)
    await workflow.short_press()

    with pytest.raises(LocalAudioError):
        await workflow.short_press()

    assert workflow.state is KnowledgeModeState.CONFIRMING
    assert capture._draft is old_draft
    assert workflow.draft_text == "旧草稿。"
    assert workflow.processing_stage is None


@pytest.mark.asyncio
async def test_save_failure_keeps_confirming_state_and_draft():
    workflow, _, capture = make_workflow()
    await workflow.enter()
    await workflow.short_press()
    await workflow.short_press()
    capture.save_error = sqlite3.OperationalError("sqlite unavailable")

    with pytest.raises(sqlite3.OperationalError, match="sqlite unavailable"):
        await workflow.long_press()

    assert workflow.state is KnowledgeModeState.CONFIRMING
    assert workflow.draft_text == "确认后的知识。"


@pytest.mark.asyncio
async def test_cancel_aborts_active_recording_and_clears_workflow():
    workflow, audio, capture = make_workflow()
    await workflow.enter()
    await workflow.short_press()
    clear_calls = capture.clear_calls

    await workflow.cancel()

    audio.abort_recording.assert_awaited_once_with()
    assert capture.clear_calls == clear_calls + 1
    assert workflow.state is KnowledgeModeState.INACTIVE
    assert workflow.processing_stage is None


@pytest.mark.asyncio
async def test_cancel_clears_workflow_even_when_aborting_recording_fails():
    workflow, audio, capture = make_workflow()
    audio.abort_recording.side_effect = LocalAudioError("abort failed")
    await workflow.enter()
    await workflow.short_press()
    clear_calls = capture.clear_calls

    with pytest.raises(LocalAudioError, match="abort failed"):
        await workflow.cancel()

    assert capture.clear_calls == clear_calls + 1
    assert workflow.state is KnowledgeModeState.INACTIVE
    assert workflow.processing_stage is None


@pytest.mark.asyncio
async def test_cancel_waits_for_processing_critical_section_then_clears_draft():
    workflow, _, capture = make_workflow()
    processing_started = asyncio.Event()
    release_processing = asyncio.Event()

    async def blocked_transcribe(_audio):
        processing_started.set()
        await release_processing.wait()
        return "新知识。"

    capture.transcribe.side_effect = blocked_transcribe
    await workflow.enter()
    await workflow.short_press()
    processing = asyncio.create_task(workflow.short_press())
    await processing_started.wait()

    cancelling = asyncio.create_task(workflow.cancel())
    await asyncio.sleep(0)
    assert cancelling.done() is False
    release_processing.set()
    await processing
    await asyncio.wait_for(cancelling, timeout=0.2)

    assert capture.draft_text is None
    assert workflow.state is KnowledgeModeState.INACTIVE
    assert workflow.processing_stage is None


@pytest.mark.asyncio
async def test_close_cancels_timeout_review_that_is_still_processing():
    workflow, _, capture = make_workflow(max_recording_seconds=0.01)
    review_started = asyncio.Event()
    never_release = asyncio.Event()

    async def blocked_transcribe(_audio):
        review_started.set()
        await never_release.wait()

    capture.transcribe.side_effect = blocked_transcribe
    await workflow.enter()
    await workflow.short_press()
    await review_started.wait()

    await asyncio.wait_for(workflow.aclose(), timeout=0.2)

    assert workflow.state is KnowledgeModeState.INACTIVE


@pytest.mark.asyncio
async def test_cancel_propagates_caller_cancellation_while_awaiting_timeout_task():
    workflow, _, capture = make_workflow()
    timeout_cleanup_started = asyncio.Event()
    never_release = asyncio.Event()

    async def timeout_worker():
        try:
            await never_release.wait()
        finally:
            timeout_cleanup_started.set()
            await never_release.wait()

    await workflow.enter()
    capture.accept(KnowledgeDraft("未保存草稿。", b"review-wav"))
    timeout_task = asyncio.create_task(timeout_worker())
    workflow._timeout_task = timeout_task
    cancelling = asyncio.create_task(workflow.cancel())
    await timeout_cleanup_started.wait()

    cancelling.cancel()

    with pytest.raises(asyncio.CancelledError):
        await cancelling
    assert timeout_task.done()
    assert workflow._timeout_task is None
    assert workflow.state is KnowledgeModeState.INACTIVE
    assert capture.draft_text is None
