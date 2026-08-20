import io
import wave
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from showroom_guide.config import Settings
from showroom_guide.faq_audio import FaqAudioGenerator, TtsProfile
from showroom_guide.faq_cache import load_cache
from showroom_guide.main import create_app, create_runtime


def make_wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 80)
    return output.getvalue()


def write_cache(tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    path = config_dir / "faq_cache.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "entries": [
                    {
                        "id": "fixed",
                        "title": "固定问题",
                        "enabled": True,
                        "priority": "high",
                        "version": 1,
                        "aliases": ["固定问题"],
                        "answer": "固定回答正文",
                        "audio_file": "prepared_audio/fixed.wav",
                    }
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


class FakeSynthesizer:
    def __init__(self, audio: bytes) -> None:
        self.audio = audio

    async def synthesize(self, _text: str) -> bytes:
        return self.audio


def make_settings(config_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        xzkb_base_url="http://xzkb.test",
        xzkb_api_key="xzkb-test-key",
        xzkb_empty_search_response="无相关内容",
        asr_base_url="http://asr.test",
        asr_api_key="asr-test-key",
        asr_model="company-asr",
        tts_base_url="http://tts.test",
        tts_api_key="tts-test-key",
        tts_model="model-a",
        tts_voice="voice-a",
        tts_speed=1.0,
        device_api_key="device-test-key",
        faq_cache_file=config_path,
        faq_prepared_audio_enabled=True,
    )


@pytest.mark.asyncio
async def test_web_and_device_audio_use_existing_protected_audio_store(
    tmp_path: Path,
):
    config_path = write_cache(tmp_path)
    profile = TtsProfile(model="model-a", voice="voice-a", speed=1.0)
    expected_audio = make_wav()
    generator = FaqAudioGenerator(
        load_cache(config_path),
        config_path,
        profile,
    )
    result = await generator.run(
        FakeSynthesizer(expected_audio),
        entry_ids=["fixed"],
    )
    assert result.generated == 1

    runtime = create_runtime(make_settings(config_path))
    runtime.speech.transcribe = AsyncMock(return_value="固定问题")
    runtime.speech.synthesize = AsyncMock()
    runtime.xzkb.stream_chat = MagicMock()
    app = create_app(runtime)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            home = await client.get("/")
            assert home.status_code == 200

            web_response = await client.post(
                "/api/questions",
                json={"question": "固定问题"},
            )
            assert web_response.status_code == 200
            web_payload = web_response.json()
            assert web_payload["answer"] == "固定回答正文"
            web_audio = await client.get(web_payload["audio_url"])
            assert web_audio.status_code == 200
            assert web_audio.content == expected_audio

            device_response = await client.post(
                "/api/device/turn",
                headers={"X-Device-Key": "device-test-key"},
                files={"file": ("question.wav", make_wav(), "audio/wav")},
            )
            assert device_response.status_code == 200
            device_payload = device_response.json()
            device_audio = await client.get(
                device_payload["audio_url"],
                headers={"X-Device-Key": "device-test-key"},
            )
            assert device_audio.status_code == 200
            assert device_audio.content == expected_audio

            metrics = await client.get(
                "/api/device/metrics",
                headers={"X-Device-Key": "device-test-key"},
            )
            latest = metrics.json()["latest"]
            assert latest["cache_hit"] is True
            assert latest["cache_entry_id"] == "fixed"
            assert latest["served_from"] == "prepared_audio"
            assert str(tmp_path) not in str(latest)
            assert "固定回答正文" not in str(latest)
            assert "device-test-key" not in str(latest)
    finally:
        await runtime.device.reset()
        assert runtime.prepared_audio is not None
        assert runtime.prepared_audio.get("fixed") == expected_audio
        await runtime.aclose()
