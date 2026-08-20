import asyncio
import io
import wave
from unittest.mock import AsyncMock, MagicMock

import pytest

from showroom_guide.controller import GuideServiceUnavailable, QuestionInProgress
from showroom_guide.device import (
    DeviceTranscriptionUnavailable,
    DeviceTurnResult,
    InvalidDeviceAudio,
    NoSpeechDetected,
)
from showroom_guide.local_audio import LocalAudioError, LocalAudioNotRecording
from showroom_guide.local_device import (
    LastRecordingNotFound,
    LocalDeviceMode,
    LocalDeviceWorkflow,
)
from showroom_guide.models import GuidePhase, GuideSnapshot


def make_wav(*, frames: int = 16000, sample: int = 4000) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(sample.to_bytes(2, "little", signed=True) * frames)
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
        self.play_start_cue = AsyncMock()
        self.play_stop_cue = AsyncMock()
        self.play_no_speech_prompt = AsyncMock()
        self.play_prompt = AsyncMock()
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
    audio.play_start_cue.side_effect = lambda: events.append("start-cue")
    audio.play_stop_cue.side_effect = lambda: events.append("stop-cue")
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
    assert events.index("start-cue") < events.index("audio-start")
    assert events.index("audio-start") < events.index("state-recording")
    assert events.index("audio-stop") < events.index("stop-cue")
    assert events.index("audio-stop") < events.index("pipeline")
    assert events.index("stop-cue") < events.index("play")
    assert events.index("pipeline") < events.index("play")
    assert events.index("play") < events.index("finished")
    session.get_audio.assert_called_once_with("audio-id")
    assert not workflow.is_recording


def test_idle_property_tracks_device_ownership():
    workflow = LocalDeviceWorkflow(session=FakeSession(), audio=FakeAudio())

    assert workflow.is_idle is True
    workflow._mode = LocalDeviceMode.RECORDING
    assert workflow.is_idle is False


@pytest.mark.asyncio
async def test_stop_cue_runs_with_pipeline_and_finishes_before_answer_playback():
    pipeline_started = asyncio.Event()
    release_pipeline = asyncio.Event()
    cue_started = asyncio.Event()
    release_cue = asyncio.Event()
    session = FakeSession()
    audio = FakeAudio()

    async def process_recorded(source):
        pipeline_started.set()
        await release_pipeline.wait()
        return session.result

    async def play_stop_cue():
        cue_started.set()
        await release_cue.wait()

    session.process_recorded_wav.side_effect = process_recorded
    audio.play_stop_cue.side_effect = play_stop_cue
    workflow = LocalDeviceWorkflow(session=session, audio=audio)

    await workflow.start_recording()
    stopping = asyncio.create_task(workflow.stop_recording())
    await asyncio.wait_for(pipeline_started.wait(), timeout=0.2)
    await asyncio.wait_for(cue_started.wait(), timeout=0.2)

    release_pipeline.set()
    await asyncio.sleep(0)
    audio.play.assert_not_awaited()

    release_cue.set()
    await stopping
    await let_background_tasks_run()

    audio.play.assert_awaited_once_with(make_wav())


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_cue", ["play_start_cue", "play_stop_cue"])
async def test_cue_failure_does_not_block_the_device_pipeline(failing_cue):
    session = FakeSession(
        DeviceTurnResult(
            transcript="问题",
            answer="文字答案",
            audio_id=None,
        )
    )
    audio = FakeAudio()
    getattr(audio, failing_cue).side_effect = LocalAudioError("cue failed")
    workflow = LocalDeviceWorkflow(session=session, audio=audio)

    await workflow.start_recording()
    result = await workflow.stop_recording()

    assert result is session.result
    audio.play_start_cue.assert_awaited_once_with()
    audio.play_stop_cue.assert_awaited_once_with()
    session.process_recorded_wav.assert_awaited_once_with(make_wav())


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

    with pytest.raises(
        InvalidDeviceAudio,
        match="录音时间太短，请听到开始提示音后再说话",
    ):
        await workflow.stop_recording()

    session.process_recorded_wav.assert_not_awaited()
    audio.play_no_speech_prompt.assert_not_awaited()
    session.fail_recording.assert_awaited_once_with(
        "录音时间太短，请听到开始提示音后再说话。"
    )


@pytest.mark.asyncio
async def test_silent_recording_is_rejected_before_asr():
    session = FakeSession()
    audio = FakeAudio(captured=make_wav(sample=0))
    workflow = LocalDeviceWorkflow(
        session=session,
        audio=audio,
        min_recording_dbfs=-45.0,
    )
    await workflow.start_recording()

    with pytest.raises(NoSpeechDetected):
        await workflow.stop_recording()

    session.process_recorded_wav.assert_not_awaited()
    audio.play_no_speech_prompt.assert_awaited_once_with()
    session.fail_recording.assert_awaited_once_with(
        "没有听清您的声音，请靠近麦克风后再试一次。"
    )


@pytest.mark.asyncio
async def test_asr_empty_result_plays_no_speech_prompt():
    session = FakeSession()
    session.process_recorded_wav.side_effect = NoSpeechDetected(
        "没有听清您的声音，请靠近麦克风后再试一次。"
    )
    audio = FakeAudio()
    workflow = LocalDeviceWorkflow(session=session, audio=audio)
    await workflow.start_recording()

    with pytest.raises(NoSpeechDetected):
        await workflow.stop_recording()

    audio.play_no_speech_prompt.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_no_speech_prompt_failure_preserves_detection_error():
    session = FakeSession()
    audio = FakeAudio(captured=make_wav(sample=0))
    audio.play_no_speech_prompt.side_effect = LocalAudioError("output failed")
    workflow = LocalDeviceWorkflow(session=session, audio=audio)
    await workflow.start_recording()

    with pytest.raises(NoSpeechDetected):
        await workflow.stop_recording()

    session.process_recorded_wav.assert_not_awaited()


@pytest.mark.asyncio
async def test_uploaded_wav_no_speech_does_not_play_on_raspberry_pi():
    session = FakeSession()
    session.process_wav.side_effect = NoSpeechDetected(
        "没有听清您的声音，请靠近麦克风后再试一次。"
    )
    audio = FakeAudio()
    workflow = LocalDeviceWorkflow(session=session, audio=audio)

    with pytest.raises(NoSpeechDetected):
        await workflow.process_upload(make_wav())

    audio.play_no_speech_prompt.assert_not_awaited()


@pytest.mark.asyncio
async def test_asr_service_failure_plays_local_service_prompt():
    session = FakeSession()
    session.process_recorded_wav.side_effect = DeviceTranscriptionUnavailable()
    audio = FakeAudio()
    workflow = LocalDeviceWorkflow(session=session, audio=audio)
    await workflow.start_recording()

    with pytest.raises(DeviceTranscriptionUnavailable):
        await workflow.stop_recording()

    audio.play_no_speech_prompt.assert_not_awaited()
    audio.play_prompt.assert_awaited_once_with("asr-unavailable")


@pytest.mark.asyncio
async def test_xzkb_service_failure_plays_local_service_prompt():
    session = FakeSession()
    session.process_recorded_wav.side_effect = GuideServiceUnavailable("xzkb")
    audio = FakeAudio()
    workflow = LocalDeviceWorkflow(session=session, audio=audio)
    await workflow.start_recording()

    with pytest.raises(GuideServiceUnavailable):
        await workflow.stop_recording()

    audio.play_prompt.assert_awaited_once_with("guide-unavailable")


@pytest.mark.asyncio
async def test_completed_recording_can_be_replayed_locally():
    captured = make_wav(sample=3500)
    session = FakeSession(
        DeviceTurnResult(
            transcript="问题",
            answer="文字答案",
            audio_id=None,
        )
    )
    audio = FakeAudio(captured=captured)
    workflow = LocalDeviceWorkflow(session=session, audio=audio)

    await workflow.start_recording()
    await workflow.stop_recording()
    await workflow.replay_last_recording()

    assert workflow.has_last_recording is True
    audio.play.assert_awaited_once_with(captured)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pipeline_error",
    [
        NoSpeechDetected("没有听清您的声音，请靠近麦克风后再试一次。"),
        DeviceTranscriptionUnavailable(),
    ],
)
async def test_failed_pipeline_recording_can_be_replayed(pipeline_error):
    captured = make_wav(sample=3500)
    session = FakeSession()

    async def fail_pipeline(_source):
        session.snapshot = GuideSnapshot(
            phase=GuidePhase.ERROR,
            message="处理失败",
        )
        raise pipeline_error

    session.process_recorded_wav.side_effect = fail_pipeline
    audio = FakeAudio(captured=captured)
    workflow = LocalDeviceWorkflow(session=session, audio=audio)

    await workflow.start_recording()
    with pytest.raises(type(pipeline_error)):
        await workflow.stop_recording()
    await workflow.replay_last_recording()

    assert workflow.has_last_recording is True
    audio.play.assert_awaited_once_with(captured)


@pytest.mark.asyncio
async def test_locally_silent_recording_can_be_replayed():
    captured = make_wav(sample=0)
    session = FakeSession()
    audio = FakeAudio(captured=captured)
    workflow = LocalDeviceWorkflow(session=session, audio=audio)

    await workflow.start_recording()
    with pytest.raises(NoSpeechDetected):
        await workflow.stop_recording()
    await workflow.replay_last_recording()

    assert workflow.has_last_recording is True
    audio.play.assert_awaited_once_with(captured)


@pytest.mark.asyncio
async def test_new_recording_reset_and_close_clear_last_recording():
    session = FakeSession(
        DeviceTurnResult(transcript="问题", answer="答案", audio_id=None)
    )
    audio = FakeAudio()
    workflow = LocalDeviceWorkflow(session=session, audio=audio)

    await workflow.start_recording()
    await workflow.stop_recording()
    assert workflow.has_last_recording is True

    await workflow.start_recording()
    assert workflow.has_last_recording is False
    await workflow.reset()

    await workflow.start_recording()
    await workflow.stop_recording()
    await workflow.reset()
    assert workflow.has_last_recording is False

    await workflow.start_recording()
    await workflow.stop_recording()
    await workflow.aclose()
    assert workflow.has_last_recording is False


@pytest.mark.asyncio
async def test_failed_recording_start_preserves_last_recording():
    session = FakeSession(
        DeviceTurnResult(transcript="问题", answer="答案", audio_id=None)
    )
    audio = FakeAudio()
    workflow = LocalDeviceWorkflow(session=session, audio=audio)
    await workflow.start_recording()
    await workflow.stop_recording()

    audio.start_recording.side_effect = LocalAudioError("input failed")
    with pytest.raises(LocalAudioError):
        await workflow.start_recording()

    assert workflow.has_last_recording is True


@pytest.mark.asyncio
async def test_replay_requires_recording_and_idle_device():
    session = FakeSession()
    audio = FakeAudio()
    workflow = LocalDeviceWorkflow(session=session, audio=audio)

    with pytest.raises(LastRecordingNotFound, match="没有可播放的录音"):
        await workflow.replay_last_recording()

    await workflow.start_recording()
    with pytest.raises(QuestionInProgress):
        await workflow.replay_last_recording()


@pytest.mark.asyncio
async def test_invalid_capture_does_not_create_last_recording():
    session = FakeSession()
    audio = FakeAudio(captured=b"not-a-wav")
    workflow = LocalDeviceWorkflow(session=session, audio=audio)

    await workflow.start_recording()
    with pytest.raises(InvalidDeviceAudio):
        await workflow.stop_recording()

    assert workflow.has_last_recording is False


@pytest.mark.asyncio
async def test_replay_failure_retains_recording_and_releases_device():
    session = FakeSession(
        DeviceTurnResult(transcript="问题", answer="答案", audio_id=None)
    )
    audio = FakeAudio()
    workflow = LocalDeviceWorkflow(session=session, audio=audio)
    await workflow.start_recording()
    await workflow.stop_recording()

    audio.play.side_effect = LocalAudioError("output failed")
    with pytest.raises(LocalAudioError):
        await workflow.replay_last_recording()
    assert workflow.has_last_recording is True

    audio.play.side_effect = None
    await workflow.replay_last_recording()
    assert audio.play.await_count == 2


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
    audio.play_prompt.assert_awaited_once_with("tts-unavailable")
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
    finished = asyncio.Event()
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

    async def finish_playback():
        await session._finish_playback()
        finished.set()

    session.process_recorded_wav.side_effect = process_recorded
    session.finish_playback.side_effect = finish_playback
    audio = FakeAudio()
    workflow = LocalDeviceWorkflow(
        session=session,
        audio=audio,
        max_recording_seconds=0.01,
    )

    await workflow.start_recording()
    await asyncio.wait_for(processed.wait(), timeout=0.2)
    await asyncio.wait_for(finished.wait(), timeout=0.2)

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
