import asyncio
import io
import wave

from pydantic import SecretStr

from showroom_guide.audio_store import AudioStore
from showroom_guide.clients.xzkb import ChatStreamEvent
from showroom_guide.concurrency import AsyncGate
from showroom_guide.controller import GuideController
from showroom_guide.device import DeviceVoiceSession
from showroom_guide.main import create_app
from showroom_guide.sessions import SessionManager
from showroom_guide.state import GuideStateStore


def silent_wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 4000)
    return output.getvalue()


class DemoXzkb:
    async def stream_chat(self, _messages):
        for text in ("这是一个界面演示。", "正式运行时，内容将来自公司知识库。"):
            await asyncio.sleep(0.25)
            yield ChatStreamEvent(text=text)


class DemoSpeech:
    async def transcribe(self, _audio) -> str:
        return "请介绍这个展项"

    async def synthesize(self, _text: str) -> bytes:
        return silent_wav()


class DemoRuntime:
    def __init__(self) -> None:
        xzkb = DemoXzkb()
        speech = DemoSpeech()
        xzkb_gate = AsyncGate(limit=4, timeout_seconds=120)
        tts_gate = AsyncGate(limit=2, timeout_seconds=120)

        def controller_factory(state):
            return GuideController(
                state,
                xzkb,
                speech,
                xzkb_gate=xzkb_gate,
                tts_gate=tts_gate,
            )

        self.sessions = SessionManager(
            controller_factory=controller_factory,
            max_sessions=100,
            idle_seconds=1800,
            audio_ttl_seconds=600,
            audio_items_per_session=3,
        )
        device_state = GuideStateStore()
        self.device = DeviceVoiceSession(
            state=device_state,
            controller=controller_factory(device_state),
            speech=speech,
            audio=AudioStore(),
        )
        self.device_api_key = SecretStr("demo-device-key")
        self.device_max_upload_bytes = 10 * 1024 * 1024
        self.cleanup_seconds = 60.0

    async def aclose(self) -> None:
        await self.sessions.clear()
        await self.device.clear()


app = create_app(DemoRuntime())
