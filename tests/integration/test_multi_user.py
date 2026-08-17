import asyncio
import io
import wave

import pytest

from showroom_guide.audio_store import AudioStore
from showroom_guide.clients.xzkb import ChatStreamEvent
from showroom_guide.concurrency import AsyncGate
from showroom_guide.controller import GuideController
from showroom_guide.device import DeviceVoiceSession
from showroom_guide.state import GuideStateStore


def silent_wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 160)
    return output.getvalue()


class TrackingXzkb:
    def __init__(self) -> None:
        self.active = 0
        self.maximum = 0
        self.received_messages = []

    async def stream_chat(self, messages):
        self.received_messages.append([dict(item) for item in messages])
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        try:
            await asyncio.sleep(0.02)
            question = messages[-1]["content"].removesuffix("\n/no_think")
            yield ChatStreamEvent(text=f"回答：{question}")
        finally:
            self.active -= 1


class TrackingSpeech:
    def __init__(self, transcripts=None) -> None:
        self.active = 0
        self.maximum = 0
        self.transcripts = list(transcripts or [])

    async def transcribe(self, _audio):
        return self.transcripts.pop(0)

    async def synthesize(self, _text):
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        try:
            await asyncio.sleep(0.02)
            return b"RIFF\x04\x00\x00\x00WAVE"
        finally:
            self.active -= 1


@pytest.mark.asyncio
async def test_twenty_sessions_are_isolated_and_respect_backend_limits():
    xzkb = TrackingXzkb()
    speech = TrackingSpeech()
    xzkb_gate = AsyncGate(limit=4, timeout_seconds=2)
    tts_gate = AsyncGate(limit=2, timeout_seconds=2)
    states = [GuideStateStore() for _ in range(20)]
    controllers = [
        GuideController(
            state,
            xzkb,
            speech,
            xzkb_gate=xzkb_gate,
            tts_gate=tts_gate,
        )
        for state in states
    ]

    results = await asyncio.gather(
        *[
            controller.ask_text(f"展项-{index}")
            for index, controller in enumerate(controllers)
        ]
    )

    assert xzkb.maximum == 4
    assert speech.maximum == 2
    for index, (state, result) in enumerate(zip(states, results)):
        assert result.answer == f"回答：展项-{index}"
        assert state.snapshot.transcript == f"展项-{index}"


@pytest.mark.asyncio
async def test_follow_up_context_stays_inside_each_controller():
    xzkb = TrackingXzkb()
    speech = TrackingSpeech()
    first = GuideController(GuideStateStore(), xzkb, speech)
    second = GuideController(GuideStateStore(), xzkb, speech)

    await first.ask_text("展项甲")
    await second.ask_text("展项乙")
    await first.finish_playback()
    await first.ask_text("它有什么特点？")

    second_request = xzkb.received_messages[1]
    first_follow_up = xzkb.received_messages[2]
    assert any(item["content"] == "展项甲" for item in first_follow_up)
    assert all(item["content"] != "展项甲" for item in second_request)


@pytest.mark.asyncio
async def test_device_context_is_isolated_from_web_context():
    xzkb = TrackingXzkb()
    speech = TrackingSpeech(["介绍设备展项", "它有什么特点？"])
    device_state = GuideStateStore()
    device = DeviceVoiceSession(
        state=device_state,
        controller=GuideController(device_state, xzkb, speech),
        speech=speech,
        audio=AudioStore(),
    )
    web = GuideController(GuideStateStore(), xzkb, speech)

    await device.process_wav(silent_wav())
    await web.ask_text("介绍网页展项")
    await device.process_wav(silent_wav())

    web_request = xzkb.received_messages[1]
    device_follow_up = xzkb.received_messages[2]
    assert all(item["content"] != "介绍设备展项" for item in web_request)
    assert any(item["content"] == "介绍设备展项" for item in device_follow_up)
    assert all(item["content"] != "介绍网页展项" for item in device_follow_up)


@pytest.mark.asyncio
async def test_device_and_web_share_backend_concurrency_gates():
    xzkb = TrackingXzkb()
    speech = TrackingSpeech(["设备问题"])
    xzkb_gate = AsyncGate(limit=1, timeout_seconds=2)
    tts_gate = AsyncGate(limit=1, timeout_seconds=2)
    device_state = GuideStateStore()
    device = DeviceVoiceSession(
        state=device_state,
        controller=GuideController(
            device_state,
            xzkb,
            speech,
            xzkb_gate=xzkb_gate,
            tts_gate=tts_gate,
        ),
        speech=speech,
        audio=AudioStore(),
    )
    web = GuideController(
        GuideStateStore(),
        xzkb,
        speech,
        xzkb_gate=xzkb_gate,
        tts_gate=tts_gate,
    )

    await asyncio.gather(
        device.process_wav(silent_wav()),
        web.ask_text("网页问题"),
    )

    assert xzkb.maximum == 1
    assert speech.maximum == 1
