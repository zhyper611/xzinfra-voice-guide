import io
import wave
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import yaml

from showroom_guide.config import Settings
from showroom_guide.controller import GuideController
from showroom_guide.device import DeviceVoiceSession
from showroom_guide.faq_cache import CacheConfigError, load_cache
from showroom_guide.main import create_runtime
from showroom_guide.state import GuidePhase, GuideStateStore
from showroom_guide.audio_store import AudioStore
from showroom_guide.clients.xzkb import ChatStreamEvent
from showroom_guide.latency import DeviceLatencyRecorder


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FORMAL_CACHE = PROJECT_ROOT / "config" / "faq_cache.yaml"


def cache_entry(
    entry_id: str = "fixed",
    *,
    alias: str = "固定问题",
    answer: str = "固定缓存回答",
) -> dict[str, object]:
    return {
        "id": entry_id,
        "title": "测试缓存",
        "enabled": True,
        "priority": "high",
        "version": 1,
        "aliases": [alias],
        "answer": answer,
        "audio_file": "prepared_audio/unused.wav",
    }


def write_cache(tmp_path: Path, entries: list[dict[str, object]]) -> Path:
    path = tmp_path / "faq_cache.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "source_document": "test.md",
                "entries": entries,
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def make_settings(*, faq_cache_enabled: bool = True, faq_cache_file=None) -> Settings:
    return Settings(
        _env_file=None,
        xzkb_base_url="http://xzkb.test",
        xzkb_api_key="xzkb-test-key",
        xzkb_empty_search_response="知识库中无相关内容",
        asr_base_url="http://asr.test",
        asr_api_key="asr-test-key",
        asr_model="company-asr",
        tts_base_url="http://tts.test",
        tts_api_key="tts-test-key",
        tts_model="company-tts",
        device_api_key="device-test-key",
        faq_cache_enabled=faq_cache_enabled,
        faq_cache_file=faq_cache_file or FORMAL_CACHE,
    )


def make_wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 160)
    return output.getvalue()


async def events(*texts: str):
    for text in texts:
        yield ChatStreamEvent(text=text)


class CountingGate:
    def __init__(self) -> None:
        self.calls = 0

    @asynccontextmanager
    async def slot(self, on_wait=None):
        self.calls += 1
        yield


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class TimedSpeech:
    def __init__(self, clock: Clock, *, fail_tts: bool = False) -> None:
        self.clock = clock
        self.fail_tts = fail_tts
        self.synthesized: list[str] = []

    async def transcribe(self, _audio) -> str:
        self.clock.advance(0.01)
        return "固定问题"

    async def synthesize(self, text: str) -> bytes:
        self.clock.advance(0.04)
        self.synthesized.append(text)
        if self.fail_tts:
            raise httpx.ReadTimeout("timeout")
        return make_wav()


def make_cached_controller(
    cache_path: Path,
    *,
    xzkb=None,
    speech=None,
    xzkb_gate=None,
    tts_gate=None,
):
    state = GuideStateStore()
    cache = load_cache(cache_path)
    xzkb = xzkb or MagicMock()
    speech = speech or AsyncMock()
    controller = GuideController(
        state,
        xzkb,
        speech,
        xzkb_gate=xzkb_gate,
        tts_gate=tts_gate,
        faq_cache=cache,
    )
    return controller, state, xzkb, speech


def test_formal_repository_cache_loads_and_matches_a_representative_question():
    cache = load_cache(FORMAL_CACHE)

    entry = cache.match("介绍一下调推中心！")

    assert entry is not None
    assert entry.id == "center_overview"
    assert entry.answer


@pytest.mark.asyncio
async def test_cache_disabled_does_not_read_the_config_file(tmp_path: Path):
    runtime = create_runtime(
        make_settings(
            faq_cache_enabled=False,
            faq_cache_file=tmp_path / "does-not-exist.yaml",
        )
    )

    assert runtime.faq_cache is None
    await runtime.aclose()


def test_enabled_cache_missing_file_fails_startup(tmp_path: Path):
    with pytest.raises(CacheConfigError, match="配置文件不存在"):
        create_runtime(make_settings(faq_cache_file=tmp_path / "missing.yaml"))


def test_enabled_cache_invalid_file_fails_startup(tmp_path: Path):
    path = tmp_path / "invalid.yaml"
    path.write_text("schema_version: 2\nentries: []\n", encoding="utf-8")

    with pytest.raises(CacheConfigError, match="schema_version"):
        create_runtime(make_settings(faq_cache_file=path))


@pytest.mark.asyncio
async def test_cache_hit_uses_fixed_answer_skips_xzkb_and_calls_tts(tmp_path: Path):
    path = write_cache(tmp_path, [cache_entry(answer="这是完整的固定回答")])
    xzkb_gate = CountingGate()
    xzkb = MagicMock()
    speech = AsyncMock()
    wav = make_wav()
    speech.synthesize.return_value = wav
    controller, state, xzkb, speech = make_cached_controller(
        path,
        xzkb=xzkb,
        speech=speech,
        xzkb_gate=xzkb_gate,
    )

    result = await controller.ask_text(" 固定问题！ ")

    assert result.answer == "这是完整的固定回答"
    assert result.audio == wav
    assert state.snapshot.answer == "这是完整的固定回答"
    assert state.snapshot.message != "缓存命中"
    xzkb.stream_chat.assert_not_called()
    assert xzkb_gate.calls == 0
    speech.synthesize.assert_awaited_once_with("这是完整的固定回答")


@pytest.mark.asyncio
async def test_cache_miss_keeps_xzkb_and_tts_flow(tmp_path: Path):
    path = write_cache(tmp_path, [cache_entry()])
    xzkb = MagicMock()
    xzkb.stream_chat.return_value = events("实时回答")
    speech = AsyncMock()
    speech.synthesize.return_value = make_wav()
    controller, _, xzkb, speech = make_cached_controller(
        path,
        xzkb=xzkb,
        speech=speech,
    )

    result = await controller.ask_text("一个实时问题")

    assert result.answer == "实时回答"
    xzkb.stream_chat.assert_called_once()
    speech.synthesize.assert_awaited_once_with("实时回答")


@pytest.mark.asyncio
async def test_cache_hit_is_added_to_follow_up_xzkb_context(tmp_path: Path):
    path = write_cache(tmp_path, [cache_entry(answer="第一轮固定回答")])
    xzkb = MagicMock()
    xzkb.stream_chat.return_value = events("第二轮实时回答")
    speech = AsyncMock()
    speech.synthesize.return_value = make_wav()
    controller, _, xzkb, _ = make_cached_controller(path, xzkb=xzkb, speech=speech)

    await controller.ask_text("固定问题")
    await controller.finish_playback()
    await controller.ask_text("请结合刚才内容继续回答")

    assert xzkb.stream_chat.call_args.args[0] == [
        {"role": "user", "content": "固定问题"},
        {"role": "assistant", "content": "第一轮固定回答"},
        {"role": "user", "content": "请结合刚才内容继续回答"},
    ]


@pytest.mark.asyncio
async def test_reset_removes_cached_answer_from_follow_up_context(tmp_path: Path):
    path = write_cache(tmp_path, [cache_entry(answer="第一轮固定回答")])
    xzkb = MagicMock()
    xzkb.stream_chat.return_value = events("重置后的实时回答")
    speech = AsyncMock()
    speech.synthesize.return_value = make_wav()
    controller, _, xzkb, _ = make_cached_controller(path, xzkb=xzkb, speech=speech)

    await controller.ask_text("固定问题")
    await controller.finish_playback()
    await controller.reset()
    await controller.ask_text("重置后的实时问题")

    assert xzkb.stream_chat.call_args.args[0] == [
        {"role": "user", "content": "重置后的实时问题"},
    ]


@pytest.mark.asyncio
async def test_web_and_device_controllers_share_one_cache_instance(tmp_path: Path):
    path = write_cache(tmp_path, [cache_entry(answer="共享固定回答")])
    runtime = create_runtime(make_settings(faq_cache_file=path))
    speech_synthesize = AsyncMock(return_value=make_wav())
    speech_transcribe = AsyncMock(return_value="固定问题")
    runtime.speech.synthesize = speech_synthesize
    runtime.speech.transcribe = speech_transcribe
    runtime.xzkb.stream_chat = MagicMock()

    web_session, _ = await runtime.sessions.get_or_create(None)
    web_result = await web_session.controller.ask_text("固定问题")
    await web_session.controller.finish_playback()
    device_result = await runtime.device.process_wav(make_wav())

    assert web_session.controller._faq_cache is runtime.faq_cache
    assert runtime.device._controller._faq_cache is runtime.faq_cache
    assert web_result.answer == "共享固定回答"
    assert device_result.answer == "共享固定回答"
    runtime.xzkb.stream_chat.assert_not_called()

    await runtime.aclose()


@pytest.mark.asyncio
async def test_cached_device_turn_leaves_xzkb_timings_null(tmp_path: Path):
    path = write_cache(tmp_path, [cache_entry(answer="设备固定回答")])
    clock = Clock()
    speech = TimedSpeech(clock)
    recorder = DeviceLatencyRecorder()
    state = GuideStateStore()
    xzkb_gate = CountingGate()
    controller = GuideController(
        state,
        MagicMock(),
        speech,
        xzkb_gate=xzkb_gate,
        faq_cache=load_cache(path),
    )
    session = DeviceVoiceSession(
        state=state,
        controller=controller,
        speech=speech,
        audio=AudioStore(),
        metrics=recorder,
        clock=clock,
    )

    result = await session.process_wav(make_wav())
    latest = recorder.snapshot()["latest"]

    assert result.audio_id is not None
    assert xzkb_gate.calls == 0
    for name in (
        "xzkb_queue_ms",
        "xzkb_headers_ms",
        "xzkb_first_sse_ms",
        "xzkb_first_content_ms",
        "xzkb_ttft_ms",
        "xzkb_generation_ms",
        "xzkb_total_ms",
    ):
        assert latest[name] is None
    assert latest["tts_queue_ms"] is not None
    assert latest["tts_synthesis_ms"] is not None
    assert latest["server_pipeline_total_ms"] is not None


@pytest.mark.asyncio
async def test_cached_answer_tts_failure_keeps_text_and_degrades(tmp_path: Path):
    path = write_cache(tmp_path, [cache_entry(answer="仍然返回的固定回答")])
    xzkb = MagicMock()
    speech = AsyncMock()
    speech.synthesize.side_effect = httpx.ReadTimeout("timeout")
    controller, state, xzkb, speech = make_cached_controller(
        path,
        xzkb=xzkb,
        speech=speech,
    )

    result = await controller.ask_text("固定问题")

    assert result.answer == "仍然返回的固定回答"
    assert result.audio is None
    assert result.warning is not None
    assert state.snapshot.phase is GuidePhase.DEGRADED
    xzkb.stream_chat.assert_not_called()
    speech.synthesize.assert_awaited_once_with("仍然返回的固定回答")
