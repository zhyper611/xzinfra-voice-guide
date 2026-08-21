import asyncio
from contextlib import suppress
from types import SimpleNamespace

import pytest

from showroom_guide.config import Settings
from showroom_guide.knowledge_web import KnowledgeWebError
from showroom_guide.main import Runtime, create_runtime


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


def make_close_probe(
    name,
    events,
    *,
    method="aclose",
    error=None,
    started=None,
    release=None,
):
    async def close():
        events.append(f"{name}:start" if started is not None else name)
        if started is not None:
            started.set()
            await release.wait()
            events.append(f"{name}:done")
        if error is not None:
            raise error

    return SimpleNamespace(**{method: close})


def make_close_test_runtime(events, **probes) -> Runtime:
    return Runtime(
        sessions=probes.get(
            "sessions",
            make_close_probe("sessions", events, method="clear"),
        ),
        device=object(),
        local_device=probes.get(
            "local_device",
            make_close_probe("local_device", events),
        ),
        device_api_key=object(),
        device_max_upload_bytes=1,
        xzkb=probes.get("xzkb", make_close_probe("xzkb", events)),
        speech=probes.get("speech", make_close_probe("speech", events)),
        cleanup_seconds=1,
        knowledge_outbox=None,
        knowledge_web=probes.get(
            "knowledge_web",
            make_close_probe("knowledge_web", events),
        ),
        knowledge_mode=probes.get(
            "knowledge_mode",
            make_close_probe("knowledge_mode", events),
        ),
        knowledge_sync=probes.get(
            "knowledge_sync",
            make_close_probe("knowledge_sync", events),
        ),
        gpio_button=probes.get(
            "gpio_button",
            make_close_probe("gpio_button", events),
        ),
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
    assert runtime.knowledge_outbox is None
    assert runtime.knowledge_sync is None
    assert (await runtime.knowledge_web.state()).enabled is False
    with pytest.raises(KnowledgeWebError) as caught:
        await runtime.knowledge_web.acquire()
    assert caught.value.code == "knowledge_capture_disabled"

    await runtime.aclose()

    assert runtime.xzkb._client.is_closed
    assert runtime.speech._client.is_closed


@pytest.mark.asyncio
async def test_disabled_knowledge_web_does_not_receive_gpio_workflow():
    runtime = create_runtime(make_settings(gpio_button_enabled=True))

    assert runtime.button_workflow is not None
    assert runtime.knowledge_web._buttons is None
    assert runtime.knowledge_web._knowledge is None
    assert runtime.knowledge_web._outbox is None

    await runtime.aclose()


@pytest.mark.asyncio
async def test_runtime_builds_optional_knowledge_pipeline(tmp_path):
    runtime = create_runtime(
        make_settings(
            knowledge_capture_enabled=True,
            xzkb_write_token="write-user-token",
            xzkb_knowledge_base_id="11111111-1111-1111-1111-111111111111",
            knowledge_outbox_path=tmp_path / "knowledge.sqlite3",
            knowledge_web_lease_seconds=45,
        )
    )

    assert runtime.knowledge_sync is not None
    assert runtime.button_workflow is not None
    assert runtime.gpio_button is None
    assert runtime.knowledge_outbox is not None
    assert runtime.knowledge_web._outbox is runtime.knowledge_outbox
    assert runtime.knowledge_web._knowledge is runtime.knowledge_mode
    assert runtime.knowledge_web._buttons is runtime.button_workflow
    assert runtime.knowledge_web._lease_seconds == 45

    await runtime.aclose()
    assert runtime.knowledge_sync._client._client.is_closed


@pytest.mark.asyncio
async def test_runtime_closes_knowledge_web_before_knowledge_resources(tmp_path):
    runtime = create_runtime(
        make_settings(
            knowledge_capture_enabled=True,
            xzkb_write_token="write-user-token",
            xzkb_knowledge_base_id="11111111-1111-1111-1111-111111111111",
            knowledge_outbox_path=tmp_path / "knowledge.sqlite3",
        )
    )
    events = []
    original_web_close = runtime.knowledge_web.aclose
    original_mode_close = runtime.knowledge_mode.aclose
    original_sync_close = runtime.knowledge_sync.aclose

    async def close_web():
        events.append("knowledge_web")
        await original_web_close()

    async def close_mode():
        events.append("knowledge_mode")
        await original_mode_close()

    async def close_sync():
        events.append("knowledge_sync")
        await original_sync_close()

    runtime.knowledge_web.aclose = close_web
    runtime.knowledge_mode.aclose = close_mode
    runtime.knowledge_sync.aclose = close_sync

    await runtime.aclose()

    assert events == ["knowledge_web", "knowledge_mode", "knowledge_sync"]


@pytest.mark.asyncio
async def test_runtime_close_attempts_all_resources_before_raising_first_error():
    events = []
    runtime = make_close_test_runtime(
        events,
        knowledge_web=make_close_probe(
            "knowledge_web",
            events,
            error=RuntimeError("web close failed"),
        ),
    )

    with pytest.raises(RuntimeError, match="web close failed"):
        await runtime.aclose()

    assert events == [
        "gpio_button",
        "knowledge_web",
        "knowledge_mode",
        "sessions",
        "local_device",
        "knowledge_sync",
        "xzkb",
        "speech",
    ]


@pytest.mark.asyncio
async def test_runtime_close_defers_repeated_caller_cancellation_until_cleanup():
    events = []
    web_started = asyncio.Event()
    web_release = asyncio.Event()
    mode_started = asyncio.Event()
    mode_release = asyncio.Event()
    runtime = make_close_test_runtime(
        events,
        knowledge_web=make_close_probe(
            "knowledge_web",
            events,
            started=web_started,
            release=web_release,
        ),
        knowledge_mode=make_close_probe(
            "knowledge_mode",
            events,
            started=mode_started,
            release=mode_release,
        ),
    )
    closing = asyncio.create_task(runtime.aclose())
    await web_started.wait()

    closing.cancel()
    web_release.set()
    try:
        await asyncio.wait_for(mode_started.wait(), timeout=1)
        closing.cancel()
        mode_release.set()

        with pytest.raises(asyncio.CancelledError):
            await closing
    finally:
        web_release.set()
        mode_release.set()
        if not closing.done():
            closing.cancel()
            with suppress(asyncio.CancelledError):
                await closing

    assert events == [
        "gpio_button",
        "knowledge_web:start",
        "knowledge_web:done",
        "knowledge_mode:start",
        "knowledge_mode:done",
        "sessions",
        "local_device",
        "knowledge_sync",
        "xzkb",
        "speech",
    ]


@pytest.mark.asyncio
async def test_runtime_close_preserves_error_that_precedes_caller_cancellation():
    events = []
    mode_started = asyncio.Event()
    mode_release = asyncio.Event()
    runtime = make_close_test_runtime(
        events,
        knowledge_web=make_close_probe(
            "knowledge_web",
            events,
            error=RuntimeError("web close failed"),
        ),
        knowledge_mode=make_close_probe(
            "knowledge_mode",
            events,
            started=mode_started,
            release=mode_release,
        ),
    )
    closing = asyncio.create_task(runtime.aclose())
    await mode_started.wait()

    closing.cancel()
    mode_release.set()

    with pytest.raises(RuntimeError, match="web close failed"):
        await closing
    assert events[-1] == "speech"
