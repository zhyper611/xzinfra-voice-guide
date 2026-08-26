import asyncio
import io
import math
import wave
from unittest.mock import AsyncMock

import httpx
import pytest

from showroom_guide.audio_store import AudioNotFound, AudioStore
from showroom_guide.clients.xzkb import ChatStreamEvent
from showroom_guide.controller import GuideController, QuestionInProgress
from showroom_guide.device import (
    DeviceTranscriptionUnavailable,
    DeviceVoiceSession,
    InvalidDeviceAudio,
    InvalidWavFormat,
    NO_SPEECH_MESSAGE,
    NoSpeechDetected,
    inspect_wav,
    validate_wav,
)
from showroom_guide.models import GuidePhase
from showroom_guide.state import GuideStateStore


def make_wav(
    *,
    channels: int = 1,
    sample_width: int = 2,
    sample_rate: int = 16000,
    frames: int = 160,
    sample: int = 0,
) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(sample_width)
        audio.setframerate(sample_rate)
        frame = int(sample).to_bytes(sample_width, "little", signed=True)
        audio.writeframes(frame * channels * frames)
    return output.getvalue()


class TrackingXzkb:
    def __init__(self) -> None:
        self.messages = []

    async def stream_chat(self, messages, observer=None):
        self.messages.append([dict(item) for item in messages])
        question = messages[-1]["content"]
        yield ChatStreamEvent(text=f"回答：{question}")


class TrackingSpeech:
    def __init__(self, transcripts=None) -> None:
        self.transcripts = list(transcripts or ["介绍展项甲"])
        self.transcribed_audio = []
        self.synthesized_text = []

    async def transcribe(self, audio) -> str:
        self.transcribed_audio.append(audio.read())
        return self.transcripts.pop(0)

    async def synthesize(self, text: str) -> bytes:
        self.synthesized_text.append(text)
        return make_wav()


class TrackingStateStore(GuideStateStore):
    def __init__(self) -> None:
        super().__init__()
        self.phases: list[GuidePhase] = []

    async def start_recording(self):
        snapshot = await super().start_recording()
        self.phases.append(snapshot.phase)
        return snapshot

    async def start_text_question(self, transcript):
        snapshot = await super().start_text_question(transcript)
        self.phases.append(snapshot.phase)
        return snapshot

    async def transition(self, phase):
        snapshot = await super().transition(phase)
        self.phases.append(snapshot.phase)
        return snapshot


def make_session(speech=None, state=None):
    state = state or GuideStateStore()
    xzkb = TrackingXzkb()
    speech = speech or TrackingSpeech()
    controller = GuideController(state, xzkb, speech)
    session = DeviceVoiceSession(
        state=state,
        controller=controller,
        speech=speech,
        audio=AudioStore(),
    )
    return session, state, xzkb, speech


@pytest.mark.parametrize(
    ("audio", "message"),
    [
        (b"not-a-wave", "WAV"),
        (b"RIFF\x04\x00\x00\x00WAVE", "损坏"),
        (make_wav(channels=2), "单声道"),
        (make_wav(sample_width=1), "16-bit"),
        (make_wav(sample_rate=8000), "16 kHz"),
    ],
)
def test_validate_wav_rejects_invalid_audio(audio, message):
    with pytest.raises(InvalidWavFormat, match=message) as caught:
        validate_wav(audio)

    assert isinstance(caught.value, InvalidDeviceAudio)


def test_validate_wav_accepts_16khz_16bit_mono_pcm():
    validate_wav(make_wav())


def test_inspect_wav_returns_duration_and_dbfs():
    metrics = inspect_wav(make_wav(frames=16000, sample=8192))

    assert metrics.duration_seconds == pytest.approx(1.0)
    assert metrics.dbfs == pytest.approx(-12.04, abs=0.1)


def test_inspect_wav_returns_negative_infinity_for_silence():
    metrics = inspect_wav(make_wav(sample=0))

    assert metrics.dbfs == -math.inf


def test_validate_wav_rejects_compressed_format():
    audio = bytearray(make_wav())
    audio[20:22] = (3).to_bytes(2, "little")

    with pytest.raises(InvalidWavFormat, match="PCM") as caught:
        validate_wav(bytes(audio))

    assert isinstance(caught.value, InvalidDeviceAudio)


@pytest.mark.asyncio
async def test_process_wav_runs_full_pipeline_and_stores_audio():
    state = TrackingStateStore()
    session, _, _, speech = make_session(state=state)
    source = make_wav()

    result = await session.process_wav(source)

    assert state.phases == [
        GuidePhase.RECORDING,
        GuidePhase.TRANSCRIBING,
        GuidePhase.THINKING,
        GuidePhase.SPEAKING,
    ]
    assert speech.transcribed_audio == [source]
    assert result.transcript == "介绍展项甲"
    assert result.answer == "回答：介绍展项甲"
    assert session.get_audio(result.audio_id) == make_wav()


@pytest.mark.asyncio
async def test_process_recorded_wav_does_not_repeat_recording_phase():
    state = TrackingStateStore()
    session, _, _, _ = make_session(state=state)
    await session.begin_recording()
    state.phases.clear()

    await session.process_recorded_wav(make_wav())

    assert state.phases[0] is GuidePhase.TRANSCRIBING
    assert GuidePhase.RECORDING not in state.phases


@pytest.mark.asyncio
async def test_process_wav_preserves_follow_up_context():
    speech = TrackingSpeech(["介绍展项甲", "它有什么特点？"])
    session, _, xzkb, _ = make_session(speech)

    await session.process_wav(make_wav())
    await session.finish_playback()
    await session.process_wav(make_wav())

    assert xzkb.messages[1] == [
        {"role": "user", "content": "介绍展项甲"},
        {"role": "assistant", "content": "回答：介绍展项甲"},
        {"role": "user", "content": "它有什么特点？"},
    ]


@pytest.mark.asyncio
async def test_asr_failure_enters_error_and_allows_next_turn():
    speech = TrackingSpeech(["介绍展项甲"])
    speech.transcribe = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))
    session, state, _, _ = make_session(speech)

    with pytest.raises(DeviceTranscriptionUnavailable):
        await session.process_wav(make_wav())

    assert state.snapshot.phase is GuidePhase.ERROR
    assert state.snapshot.message == "语音识别暂时不可用，请稍后重试"


@pytest.mark.asyncio
async def test_malformed_asr_response_enters_recoverable_error_state():
    speech = TrackingSpeech(["介绍展项甲"])
    speech.transcribe = AsyncMock(side_effect=ValueError("invalid ASR response"))
    session, state, _, _ = make_session(speech)

    with pytest.raises(DeviceTranscriptionUnavailable):
        await session.process_wav(make_wav())

    assert state.snapshot.phase is GuidePhase.ERROR
    assert state.snapshot.message == "语音识别暂时不可用，请稍后重试"


@pytest.mark.asyncio
async def test_empty_transcript_has_actionable_message():
    speech = TrackingSpeech(["   "])
    session, state, xzkb, speech = make_session(speech)

    with pytest.raises(NoSpeechDetected, match=NO_SPEECH_MESSAGE):
        await session.process_wav(make_wav())

    assert state.snapshot.message == NO_SPEECH_MESSAGE
    assert xzkb.messages == []
    assert speech.synthesized_text == []


@pytest.mark.asyncio
async def test_tts_failure_returns_text_without_audio():
    session, state, _, speech = make_session()
    speech.synthesize = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))

    result = await session.process_wav(make_wav())

    assert result.answer == "回答：介绍展项甲"
    assert result.audio_id is None
    assert result.warning == "语音暂时不可用，您仍可阅读文字答案"
    assert state.snapshot.phase is GuidePhase.DEGRADED


@pytest.mark.asyncio
async def test_concurrent_turn_and_reset_are_rejected():
    started = asyncio.Event()
    release = asyncio.Event()
    speech = TrackingSpeech()

    async def blocking_transcribe(audio):
        audio.read()
        started.set()
        await release.wait()
        return "介绍展项甲"

    speech.transcribe = blocking_transcribe
    session, _, _, _ = make_session(speech)
    running = asyncio.create_task(session.process_wav(make_wav()))
    await started.wait()

    with pytest.raises(QuestionInProgress):
        await session.process_wav(make_wav())
    with pytest.raises(QuestionInProgress):
        await session.reset()

    release.set()
    await running


@pytest.mark.asyncio
async def test_finish_playback_and_reset_clear_device_state_and_audio():
    speech = TrackingSpeech(["介绍展项甲", "介绍展项乙"])
    session, state, xzkb, _ = make_session(speech)
    first = await session.process_wav(make_wav())

    await session.finish_playback()

    assert state.snapshot.phase is GuidePhase.IDLE
    await session.reset()
    assert state.snapshot.transcript == ""
    with pytest.raises(AudioNotFound):
        session.get_audio(first.audio_id)

    await session.process_wav(make_wav())
    assert xzkb.messages[1] == [
        {"role": "user", "content": "介绍展项乙"}
    ]
