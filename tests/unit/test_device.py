import asyncio
import io
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
    validate_wav,
)
from showroom_guide.models import GuidePhase
from showroom_guide.state import GuideStateStore


def make_wav(
    *,
    channels: int = 1,
    sample_width: int = 2,
    sample_rate: int = 16000,
) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(sample_width)
        audio.setframerate(sample_rate)
        audio.writeframes(b"\x00" * sample_width * channels * 160)
    return output.getvalue()


class TrackingXzkb:
    def __init__(self) -> None:
        self.messages = []

    async def stream_chat(self, messages):
        self.messages.append([dict(item) for item in messages])
        question = messages[-1]["content"].removesuffix("\n/no_think")
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


def make_session(speech=None):
    state = GuideStateStore()
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
        (make_wav(channels=2), "单声道"),
        (make_wav(sample_width=1), "16-bit"),
        (make_wav(sample_rate=8000), "16 kHz"),
    ],
)
def test_validate_wav_rejects_invalid_audio(audio, message):
    with pytest.raises(InvalidDeviceAudio, match=message):
        validate_wav(audio)


def test_validate_wav_accepts_16khz_16bit_mono_pcm():
    validate_wav(make_wav())


def test_validate_wav_rejects_compressed_format():
    audio = bytearray(make_wav())
    audio[20:22] = (3).to_bytes(2, "little")

    with pytest.raises(InvalidDeviceAudio, match="PCM"):
        validate_wav(bytes(audio))


@pytest.mark.asyncio
async def test_process_wav_runs_full_pipeline_and_stores_audio():
    session, state, _, speech = make_session()
    queue = state.subscribe()
    source = make_wav()

    result = await session.process_wav(source)

    events = []
    while not queue.empty():
        events.append((await queue.get()).phase)
    ordered_phases = []
    for phase in events:
        if not ordered_phases or ordered_phases[-1] is not phase:
            ordered_phases.append(phase)
    assert ordered_phases == [
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
async def test_process_wav_preserves_follow_up_context():
    speech = TrackingSpeech(["介绍展项甲", "它有什么特点？"])
    session, _, xzkb, _ = make_session(speech)

    await session.process_wav(make_wav())
    await session.finish_playback()
    await session.process_wav(make_wav())

    assert xzkb.messages[1][1:] == [
        {"role": "user", "content": "介绍展项甲"},
        {"role": "assistant", "content": "回答：介绍展项甲"},
        {"role": "user", "content": "它有什么特点？\n/no_think"},
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
    assert xzkb.messages[1][1:] == [
        {"role": "user", "content": "介绍展项乙\n/no_think"}
    ]
