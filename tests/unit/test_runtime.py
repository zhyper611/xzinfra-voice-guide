import pytest

from showroom_guide.config import Settings
from showroom_guide.main import create_runtime


def make_settings(**overrides) -> Settings:
    values = dict(
        xzkb_base_url="http://xzkb.test",
        xzkb_api_key="xzkb-test-key",
        xzkb_empty_search_response="请询问展厅相关内容。",
        asr_base_url="http://asr.test",
        asr_api_key="asr-test-key",
        asr_model="company-asr",
        tts_base_url="http://tts.test",
        tts_api_key="tts-test-key",
        tts_model="company-tts",
        device_api_key="device-test-key",
    )
    values.update(overrides)
    return Settings(
        _env_file=None,
        **values,
    )


@pytest.mark.asyncio
async def test_runtime_builds_isolated_device_with_shared_clients_and_gates():
    runtime = create_runtime(make_settings())

    web_session, _ = await runtime.sessions.get_or_create(None)

    assert runtime.device.snapshot is not web_session.state.snapshot
    assert runtime.device._speech is runtime.speech
    assert runtime.device._controller._xzkb is runtime.xzkb
    assert (
        runtime.device._controller._xzkb_gate
        is web_session.controller._xzkb_gate
    )
    assert (
        runtime.device._controller._tts_gate
        is web_session.controller._tts_gate
    )
    assert runtime.device_api_key.get_secret_value() == "device-test-key"
    assert runtime.device_max_upload_bytes == 10 * 1024 * 1024
    assert runtime.xzkb._empty_search_response == "请询问展厅相关内容。"
    assert runtime.local_device._session is runtime.device
    assert runtime.local_device._audio._sample_rate == 16000
    assert runtime.local_device._max_recording_seconds == 60.0
    assert runtime.local_device._min_recording_seconds == 0.5
    assert runtime.local_device._min_recording_dbfs == -45.0
    assert runtime.device._controller._playback_timeout_seconds == 300.0
    assert runtime.local_device._audio._no_speech_prompt[:4] == b"RIFF"
    assert runtime.button_workflow is None
    assert runtime.knowledge_sync is None

    await runtime.aclose()

    assert runtime.xzkb._client.is_closed
    assert runtime.speech._client.is_closed


@pytest.mark.asyncio
async def test_runtime_builds_optional_knowledge_pipeline(tmp_path):
    runtime = create_runtime(
        make_settings(
            knowledge_capture_enabled=True,
            xzkb_write_token="write-user-token",
            xzkb_knowledge_base_id="11111111-1111-1111-1111-111111111111",
            knowledge_outbox_path=tmp_path / "knowledge.sqlite3",
        )
    )

    assert runtime.knowledge_sync is not None
    assert runtime.button_workflow is not None
    assert runtime.gpio_button is None

    await runtime.aclose()
    assert runtime.knowledge_sync._client._client.is_closed
