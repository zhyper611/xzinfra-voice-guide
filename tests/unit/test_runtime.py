import pytest

from showroom_guide.config import Settings
from showroom_guide.main import create_runtime


def make_settings() -> Settings:
    return Settings(
        _env_file=None,
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

    await runtime.aclose()

    assert runtime.xzkb._client.is_closed
    assert runtime.speech._client.is_closed
