import asyncio
import io
import wave
from unittest.mock import AsyncMock, MagicMock

import pytest
import httpx

from showroom_guide.device import InvalidDeviceAudio, NoSpeechDetected
from showroom_guide.knowledge_capture import KnowledgeDraft
from showroom_guide.knowledge_mode import KnowledgeModeState, KnowledgeModeWorkflow


def make_wav(*, seconds=1.0, sample=3000):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(int(sample).to_bytes(2, "little", signed=True) * int(16000 * seconds))
    return output.getvalue()


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
    capture = MagicMock()
    capture.has_draft = False
    capture.review = AsyncMock(
        return_value=KnowledgeDraft("确认后的知识。", b"review-wav")
    )
    capture.accept = MagicMock()
    capture.save = MagicMock()
    capture.clear = MagicMock()
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

    capture.review.assert_awaited_once()
    audio.play.assert_awaited_once_with(b"review-wav")
    capture.accept.assert_called_once()
    assert workflow.state is KnowledgeModeState.CONFIRMING


@pytest.mark.asyncio
async def test_failed_rerecord_returns_to_confirmation_with_old_draft():
    workflow, audio, capture = make_workflow()
    await workflow.enter()
    await workflow.short_press()
    await workflow.short_press()
    capture.has_draft = True
    capture.accept.reset_mock()
    capture.review.side_effect = NoSpeechDetected("没有听清")
    await workflow.short_press()

    with pytest.raises(NoSpeechDetected):
        await workflow.short_press()

    assert workflow.state is KnowledgeModeState.CONFIRMING
    capture.accept.assert_not_called()
    audio.play_no_speech_prompt.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_long_press_saves_confirmed_draft_then_exits():
    workflow, _, capture = make_workflow()
    await workflow.enter()
    await workflow.short_press()
    await workflow.short_press()
    capture.has_draft = True

    should_exit = await workflow.long_press()

    assert should_exit is True
    capture.save.assert_called_once_with()
    workflow._audio.play_prompt.assert_awaited_with("knowledge-saved")
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
    capture.review.assert_awaited_once()
    assert workflow.state is KnowledgeModeState.CONFIRMING


@pytest.mark.asyncio
async def test_remote_review_failure_restores_state_and_plays_local_prompt():
    workflow, audio, capture = make_workflow()
    capture.review.side_effect = httpx.ReadTimeout("speech unavailable")
    await workflow.enter()
    await workflow.short_press()

    with pytest.raises(httpx.ReadTimeout):
        await workflow.short_press()

    assert workflow.state is KnowledgeModeState.READY
    audio.play_prompt.assert_awaited_with("guide-unavailable")


@pytest.mark.asyncio
async def test_close_cancels_timeout_review_that_is_still_processing():
    workflow, _, capture = make_workflow(max_recording_seconds=0.01)
    review_started = asyncio.Event()
    never_release = asyncio.Event()

    async def blocked_review(_audio):
        review_started.set()
        await never_release.wait()

    capture.review.side_effect = blocked_review
    await workflow.enter()
    await workflow.short_press()
    await review_started.wait()

    await asyncio.wait_for(workflow.aclose(), timeout=0.2)

    assert workflow.state is KnowledgeModeState.INACTIVE
