import asyncio
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from pydantic import SecretStr

from showroom_guide import main as main_module
from showroom_guide.async_outbox import AsyncKnowledgeOutbox
from showroom_guide.config import Settings
from showroom_guide.knowledge_web import KnowledgeWebError
from showroom_guide.main import Runtime, create_app, create_runtime
from showroom_guide.main import cleanup_sessions
from showroom_guide.verdict_motion import WebSimulationOutput


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
        knowledge_outbox=probes.get("knowledge_outbox"),
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
        servo_motion=probes.get("servo_motion"),
    )


@pytest.mark.asyncio
async def test_disabled_servo_does_not_construct_gpio(monkeypatch):
    constructor = MagicMock()
    monkeypatch.setattr(main_module, "GpioZeroServoDriver", constructor, raising=False)

    runtime = create_runtime(make_settings(servo_enabled=False))

    assert runtime.servo_motion is None
    constructor.assert_not_called()
    await runtime.aclose()


@pytest.mark.asyncio
async def test_verdict_runtime_without_servo_uses_simulation_output():
    runtime = create_runtime(
        make_settings(
            verdict_enabled=True,
            verdict_base_url="http://verdict.test",
            verdict_api_key="verdict-test-key",
        )
    )

    assert runtime.verdict_workflow is runtime.device._verdict_workflow
    assert isinstance(runtime.verdict_workflow.motion, WebSimulationOutput)
    assert runtime.verdict_workflow.motion._state is runtime.device._state

    await runtime.aclose()


@pytest.mark.asyncio
async def test_web_verdict_sessions_are_isolated_from_device_and_each_other():
    runtime = create_runtime(
        make_settings(
            verdict_enabled=True,
            verdict_base_url="http://verdict.test",
            verdict_api_key="verdict-test-key",
        )
    )

    first, _ = await runtime.sessions.get_or_create(None)
    second, _ = await runtime.sessions.get_or_create(None)

    assert first.verdict_workflow is not None
    assert second.verdict_workflow is not None
    assert isinstance(first.verdict_workflow.motion, WebSimulationOutput)
    assert isinstance(second.verdict_workflow.motion, WebSimulationOutput)
    assert first.verdict_workflow.motion._state is first.state
    assert second.verdict_workflow.motion._state is second.state
    assert first.verdict_workflow is not second.verdict_workflow
    assert first.verdict_workflow is not runtime.verdict_workflow

    await runtime.aclose()


@pytest.mark.asyncio
async def test_enabled_servo_is_injected_into_device_workflows(monkeypatch):
    driver = MagicMock()
    constructor = MagicMock(return_value=driver)
    monkeypatch.setattr(main_module, "GpioZeroServoDriver", constructor, raising=False)

    runtime = create_runtime(
        make_settings(servo_enabled=True, gpio_button_enabled=True)
    )

    constructor.assert_called_once_with(
        pin=18,
        min_angle=10.0,
        max_angle=140.0,
        min_pulse_width=0.0005,
        max_pulse_width=0.0025,
    )
    assert runtime.servo_motion is not None
    assert runtime.local_device._before_recording.__self__ is runtime.servo_motion
    await runtime.aclose()


@pytest.mark.asyncio
async def test_servo_initialization_failure_does_not_abort_runtime(monkeypatch):
    monkeypatch.setattr(
        main_module,
        "GpioZeroServoDriver",
        MagicMock(side_effect=OSError("GPIO unavailable")),
        raising=False,
    )

    runtime = create_runtime(make_settings(servo_enabled=True))

    assert runtime.servo_motion is None
    await runtime.aclose()


@pytest.mark.asyncio
async def test_lifespan_starts_and_closes_servo_once():
    runtime = create_runtime(make_settings())
    servo = SimpleNamespace(enter_mode=AsyncMock(), aclose=AsyncMock())
    runtime.servo_motion = servo
    app = create_app(runtime)

    async with app.router.lifespan_context(app):
        servo.enter_mode.assert_awaited_once_with()

    servo.aclose.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_gpio_start_failure_does_not_abort_web_lifespan():
    runtime = create_runtime(make_settings())
    gpio = SimpleNamespace(
        start=MagicMock(side_effect=OSError("GPIO unavailable")),
        aclose=AsyncMock(),
    )
    runtime.gpio_button = gpio
    app = create_app(runtime)

    async with app.router.lifespan_context(app):
        gpio.start.assert_called_once_with()

    gpio.aclose.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_runtime_closes_servo_after_gpio_before_other_resources():
    events = []
    runtime = make_close_test_runtime(
        events,
        servo_motion=make_close_probe("servo_motion", events),
    )

    await runtime.aclose()

    assert events[:3] == ["gpio_button", "servo_motion", "knowledge_web"]


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
    assert runtime.speech._asr_timeout == 8.0
    assert runtime.speech._tts_timeout == 12.0
    assert runtime.local_device._session is runtime.device
    assert runtime.local_device._audio._sample_rate == 16000
    assert runtime.local_device._max_recording_seconds == 60.0
    assert runtime.local_device._min_recording_seconds == 0.5
    assert runtime.local_device._min_recording_dbfs == -45.0
    assert runtime.device._controller._playback_timeout_seconds == 300.0
    assert runtime.device._controller._xzkb_total_timeout_seconds == 120.0
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
async def test_runtime_passes_independent_speech_timeouts():
    runtime = create_runtime(
        make_settings(asr_timeout_seconds=6.5, tts_timeout_seconds=10.5)
    )

    assert runtime.speech._asr_timeout == 6.5
    assert runtime.speech._tts_timeout == 10.5

    await runtime.aclose()


@pytest.mark.asyncio
async def test_disabled_knowledge_web_does_not_receive_gpio_workflow():
    runtime = create_runtime(make_settings(gpio_button_enabled=True))

    assert runtime.button_workflow is not None
    assert runtime.knowledge_web._buttons is None
    assert runtime.knowledge_web._knowledge is None
    assert runtime.knowledge_web._outbox is None

    await runtime.aclose()


@pytest.mark.asyncio
async def test_runtime_builds_optional_knowledge_pipeline(tmp_path, monkeypatch):
    auth_arguments = []
    auth_closed = False

    class FakeAuth:
        async def aclose(self):
            nonlocal auth_closed
            auth_closed = True

    def build_auth(base_url, username, password, *, timeout):
        auth_arguments.append((base_url, username, password, timeout))
        return FakeAuth()

    monkeypatch.setattr(main_module, "XzkbLocalAccountAuth", build_auth)
    runtime = create_runtime(
        make_settings(
            knowledge_capture_enabled=True,
            xzkb_username="showroom-writer",
            xzkb_password="dedicated-account-password",
            xzkb_knowledge_base_id="11111111-1111-1111-1111-111111111111",
            knowledge_outbox_path=tmp_path / "knowledge.sqlite3",
            knowledge_web_lease_seconds=45,
        )
    )

    assert runtime.knowledge_sync is not None
    assert runtime.button_workflow is not None
    assert runtime.gpio_button is None
    assert runtime.knowledge_outbox is not None
    assert isinstance(runtime.knowledge_outbox, AsyncKnowledgeOutbox)
    assert runtime.knowledge_web._outbox is runtime.knowledge_outbox
    assert runtime.knowledge_web._knowledge is runtime.knowledge_mode
    assert runtime.knowledge_web._buttons is runtime.button_workflow
    assert runtime.knowledge_web._lease_seconds == 45
    knowledge_client = runtime.knowledge_sync._client
    assert auth_arguments == [
        (
            "http://xzkb.test",
            "showroom-writer",
            "dedicated-account-password",
            30.0,
        )
    ]

    await runtime.aclose()
    assert knowledge_client._client.is_closed
    assert auth_closed is True


@pytest.mark.asyncio
async def test_runtime_creation_does_not_send_network_requests(
    tmp_path,
    monkeypatch,
):
    async def unexpected_request(*args, **kwargs):
        raise AssertionError("unexpected network")

    monkeypatch.setattr(httpx.AsyncClient, "request", unexpected_request)

    runtime = create_runtime(
        make_settings(
            knowledge_capture_enabled=True,
            xzkb_username="showroom-writer",
            xzkb_password="dedicated-account-password",
            xzkb_knowledge_base_id="11111111-1111-1111-1111-111111111111",
            knowledge_outbox_path=tmp_path / "knowledge.sqlite3",
        )
    )

    await runtime.aclose()


@pytest.mark.parametrize(
    ("attribute", "invalid_value"),
    [
        ("xzkb_username", None),
        ("xzkb_username", ""),
        ("xzkb_password", None),
        ("xzkb_password", SecretStr("")),
        ("xzkb_knowledge_base_id", None),
    ],
)
def test_runtime_rejects_invalid_knowledge_credentials(
    tmp_path,
    attribute,
    invalid_value,
):
    settings = make_settings(
        knowledge_capture_enabled=True,
        xzkb_username="showroom-writer",
        xzkb_password="dedicated-account-password",
        xzkb_knowledge_base_id="11111111-1111-1111-1111-111111111111",
        knowledge_outbox_path=tmp_path / "knowledge.sqlite3",
    )
    object.__setattr__(settings, attribute, invalid_value)

    with pytest.raises(
        ValueError,
        match="^启用知识补充时必须配置 XZKB 专用账号、密码和知识库 ID$",
    ):
        create_runtime(settings)


def test_runtime_does_not_create_auth_when_outbox_open_fails(
    tmp_path,
    monkeypatch,
):
    auth_arguments = []

    def fail_outbox(_path):
        raise OSError("outbox unavailable")

    def build_auth(*args, **kwargs):
        auth_arguments.append((args, kwargs))
        return SimpleNamespace()

    monkeypatch.setattr(main_module, "KnowledgeOutbox", fail_outbox)
    monkeypatch.setattr(main_module, "XzkbLocalAccountAuth", build_auth)

    with pytest.raises(OSError, match="outbox unavailable"):
        create_runtime(
            make_settings(
                knowledge_capture_enabled=True,
                xzkb_username="showroom-writer",
                xzkb_password="dedicated-account-password",
                xzkb_knowledge_base_id=(
                    "11111111-1111-1111-1111-111111111111"
                ),
                knowledge_outbox_path=tmp_path / "knowledge.sqlite3",
            )
        )

    assert auth_arguments == []


@pytest.mark.asyncio
async def test_runtime_closes_knowledge_web_before_knowledge_resources(tmp_path):
    runtime = create_runtime(
        make_settings(
            knowledge_capture_enabled=True,
            xzkb_username="showroom-writer",
            xzkb_password="dedicated-account-password",
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

    knowledge_client = runtime.knowledge_sync._client
    auth = knowledge_client._auth

    await runtime.aclose()

    assert events == ["knowledge_web", "knowledge_mode", "knowledge_sync"]
    assert knowledge_client._client.is_closed
    assert auth._client.is_closed


@pytest.mark.asyncio
async def test_runtime_closes_shared_outbox_after_sync_consumer():
    events = []
    runtime = make_close_test_runtime(
        events,
        knowledge_outbox=make_close_probe("knowledge_outbox", events),
    )

    await runtime.aclose()

    assert events.index("knowledge_sync") < events.index("knowledge_outbox")


@pytest.mark.asyncio
async def test_session_cleanup_continues_after_one_prune_failure():
    runtime = SimpleNamespace(
        cleanup_seconds=0.01,
        sessions=SimpleNamespace(
            prune=AsyncMock(side_effect=[OSError("temporary"), []])
        ),
    )
    task = asyncio.create_task(cleanup_sessions(runtime))
    try:
        for _ in range(50):
            if runtime.sessions.prune.await_count >= 2:
                break
            await asyncio.sleep(0.01)
        assert runtime.sessions.prune.await_count >= 2
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


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
