import asyncio
import io
import wave
from unittest.mock import AsyncMock, MagicMock

import pytest

from showroom_guide.controller import QuestionInProgress
from showroom_guide.device import DeviceTurnResult, InvalidDeviceAudio
from showroom_guide.local_audio import LocalAudioError, LocalAudioNotRecording
from showroom_guide.local_device import LocalDeviceWorkflow
from showroom_guide.models import GuidePhase, GuideSnapshot


def make_wav(*, frames: int = 16000) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x01\x00" * frames)
    return output.getvalue()


class FakeSession:
    def __init__(self, result=None) -> None:
        self.snapshot = GuideSnapshot()
        self.result = result or DeviceTurnResult(
            transcript="介绍展项甲",
            answer="讲解内容",
            audio_id="audio-id",
        )
        self.begin_recording = AsyncMock(side_effect=self._begin_recording)
        self.process_recorded_wav = AsyncMock(return_value=self.result)
        self.process_wav = AsyncMock(return_value=self.result)
        self.get_audio = MagicMock(return_value=make_wav())
        self.finish_playback = AsyncMock(side_effect=self._finish_playback)
        self.fail_playback = AsyncMock(side_effect=self._fail_playback)
        self.fail_recording = AsyncMock(side_effect=self._fail_recording)
        self.reset = AsyncMock(side_effect=self._reset)
        self.clear = AsyncMock(side_effect=self._reset)

    async def _begin_recording(self):
        self.snapshot = GuideSnapshot(
            phase=GuidePhase.RECORDING,
            message="正在录音，再次点击后提交",
        )

    async def _finish_playback(self):
        self.snapshot = GuideSnapshot()

    async def _fail_playback(self):
        self.snapshot = GuideSnapshot(
            phase=GuidePhase.ERROR,
            message="扬声器播放失败，请检查默认输出设备",
        )

    async def _fail_recording(self, message):
        self.snapshot = GuideSnapshot(phase=GuidePhase.ERROR, message=message)

    async def _reset(self):
        self.snapshot = GuideSnapshot()


class FakeAudio:
    def __init__(self, captured=None) -> None:
        self.captured = captured or make_wav()
        self.start_recording = AsyncMock()
        self.stop_recording = AsyncMock(return_value=self.captured)
        self.abort_recording = AsyncMock()
        self.play = AsyncMock()
        self.stop_playback = AsyncMock()
        self.aclose = AsyncMock()


async def let_background_tasks_run() -> None:
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_start_stop_processes_and_plays_in_order():
    events = []
    session = FakeSession()
    audio = FakeAudio()
    audio.start_recording.side_effect = lambda: events.append("audio-start")

    async def begin_recording():
        events.append("state-recording")
        await session._begin_recording()

    session.begin_recording.side_effect = begin_recording
    audio.stop_recording.side_effect = lambda: events.append("audio-stop") or make_wav()

    async def process_recorded(source):
        events.append("pipeline")
        assert source == make_wav()
        return session.result

    session.process_recorded_wav.side_effect = process_recorded

    async def play(source):
        events.append("play")
        assert source == make_wav()

    audio.play.side_effect = play
    session.finish_playback.side_effect = lambda: events.append("finished")
    workflow = LocalDeviceWorkflow(session=session, audio=audio)

    snapshot = await workflow.start_recording()
    result = await workflow.stop_recording()
    await let_background_tasks_run()

    assert snapshot.phase is GuidePhase.RECORDING
    assert result is session.result
    assert events == [
        "audio-start",
        "state-recording",
        "audio-stop",
        "pipeline",
        "play",
        "finished",
    ]
    session.get_audio.assert_called_once_with("audio-id")
    assert not workflow.is_recording


@pytest.mark.asyncio
async def test_rejects_conflicting_device_operations():
    session = FakeSession()
    audio = FakeAudio()
    workflow = LocalDeviceWorkflow(session=session, audio=audio)

    await workflow.start_recording()

    with pytest.raises(QuestionInProgress):
        await workflow.start_recording()
    with pytest.raises(QuestionInProgress):
        await workflow.process_upload(make_wav())

    await workflow.reset()
    with pytest.raises(LocalAudioNotRecording):
        await workflow.stop_recording()


@pytest.mark.asyncio
async def test_short_recording_is_rejected_before_asr():
    session = FakeSession()
    audio = FakeAudio(captured=make_wav(frames=1000))
    workflow = LocalDeviceWorkflow(
        session=session,
        audio=audio,
        min_recording_seconds=0.5,
    )
    await workflow.start_recording()

    with pytest.raises(InvalidDeviceAudio, match="录音时间过短"):
        await workflow.stop_recording()

    session.process_recorded_wav.assert_not_awaited()
    session.fail_recording.assert_awaited_once_with("录音时间过短，请重新录音")


@pytest.mark.asyncio
async def test_degraded_turn_without_audio_returns_to_idle():
    session = FakeSession(
        DeviceTurnResult(
            transcript="问题",
            answer="文字答案",
            audio_id=None,
            warning="语音暂时不可用",
        )
    )
    audio = FakeAudio()
    workflow = LocalDeviceWorkflow(session=session, audio=audio)
    await workflow.start_recording()

    result = await workflow.stop_recording()

    assert result.warning == "语音暂时不可用"
    audio.play.assert_not_awaited()
    session.finish_playback.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_playback_failure_updates_device_state():
    session = FakeSession()
    audio = FakeAudio()
    audio.play.side_effect = LocalAudioError("output failed")
    workflow = LocalDeviceWorkflow(session=session, audio=audio)
    await workflow.start_recording()

    await workflow.stop_recording()
    await let_background_tasks_run()

    session.fail_playback.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_timeout_uses_the_same_stop_pipeline():
    processed = asyncio.Event()
    session = FakeSession(
        DeviceTurnResult(
            transcript="问题",
            answer="文字答案",
            audio_id=None,
        )
    )

    async def process_recorded(source):
        processed.set()
        return session.result

    session.process_recorded_wav.side_effect = process_recorded
    audio = FakeAudio()
    workflow = LocalDeviceWorkflow(
        session=session,
        audio=audio,
        max_recording_seconds=0.01,
    )

    await workflow.start_recording()
    await asyncio.wait_for(processed.wait(), timeout=0.2)

    audio.stop_recording.assert_awaited_once_with()
    session.finish_playback.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_reset_stops_recording_but_rejects_processing():
    session = FakeSession()
    audio = FakeAudio()
    workflow = LocalDeviceWorkflow(session=session, audio=audio)
    await workflow.start_recording()

    await workflow.reset()

    audio.abort_recording.assert_awaited_once_with()
    session.reset.assert_awaited_once_with()

    started = asyncio.Event()
    release = asyncio.Event()

    async def process_recorded(source):
        started.set()
        await release.wait()
        return session.result

    session.process_recorded_wav.side_effect = process_recorded
    await workflow.start_recording()
    running = asyncio.create_task(workflow.stop_recording())
    await started.wait()

    with pytest.raises(QuestionInProgress):
        await workflow.reset()

    release.set()
    await running
    await let_background_tasks_run()


@pytest.mark.asyncio
async def test_upload_uses_shared_guard_and_close_cleans_resources():
    session = FakeSession()
    audio = FakeAudio()
    workflow = LocalDeviceWorkflow(session=session, audio=audio)

    result = await workflow.process_upload(make_wav())
    await workflow.aclose()

    assert result is session.result
    session.process_wav.assert_awaited_once_with(make_wav())
    audio.aclose.assert_awaited_once_with()
    session.clear.assert_awaited_once_with()
