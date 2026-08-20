import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import WebSocket
from fastapi.testclient import TestClient
from pydantic import SecretStr

from showroom_guide.controller import (
    GuideServiceUnavailable,
    QuestionInProgress,
    TextQuestionResult,
)
from showroom_guide.main import create_app
from showroom_guide.sessions import SessionManager


class FakeController:
    def __init__(self, state):
        self.state = state
        self.ask_text = AsyncMock()
        self.finish_playback = AsyncMock()
        self.reset = AsyncMock(side_effect=state.reset)
        self.is_busy = False


class FakeRuntime:
    def __init__(self):
        self.controllers = []

        def controller_factory(state):
            controller = FakeController(state)
            self.controllers.append(controller)
            return controller

        self.sessions = SessionManager(
            controller_factory=controller_factory,
            max_sessions=100,
            idle_seconds=1800,
            audio_ttl_seconds=600,
            audio_items_per_session=3,
        )
        self.cleanup_seconds = 60.0
        self.device = MagicMock()
        self.device_api_key = SecretStr("device-test-key")
        self.device_max_upload_bytes = 10 * 1024 * 1024
        self.aclose = AsyncMock()


def establish_session(client, runtime):
    client.get("/")
    session_id = client.cookies.get("showroom_session")
    session = runtime.sessions.get(session_id)
    assert session is not None
    return session


def test_index_serves_mobile_question_interface():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "展厅 AI 讲解" in response.text
    assert 'name="question"' in response.text
    runtime.aclose.assert_awaited_once()


def test_device_test_page_is_served_without_creating_visitor_session():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        response = client.get("/device-test")

    assert response.status_code == 200
    assert "树莓派语音链路测试" in response.text
    assert 'id="device-key"' in response.text
    assert 'id="local-record"' in response.text
    assert 'id="local-record-label"' in response.text
    assert 'id="replay-recording"' in response.text
    assert 'id="replay-recording-label"' in response.text
    assert 'data-mode="microphone"' in response.text
    assert 'data-mode="wav"' in response.text
    assert 'id="wav-file"' in response.text
    assert 'id="device-audio"' in response.text
    assert 'id="latency-current-tab"' in response.text
    assert 'id="latency-stats-tab"' in response.text
    assert 'id="latency-refresh"' in response.text
    assert 'id="latency-stages"' in response.text
    assert 'id="latency-stats-body"' in response.text
    assert '<span>03</span>' in response.text
    assert '<p>LATENCY TIMING</p><h2 id="latency-title">链路耗时</h2>' in response.text
    assert 'src="/static/device-test.js?v=' in response.text
    assert 'href="/static/device-test.css?v=' in response.text
    assert 'rel="icon" href="/static/xzinfra-logo.svg"' in response.text
    assert "showroom_session=" not in response.headers.get("set-cookie", "")


def test_device_test_styles_are_branded_responsive_and_accessible():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        response = client.get("/static/device-test.css")

    assert response.status_code == 200
    css = response.text.replace("\r\n", "\n")
    assert "--brand: #1c6af6" in css
    assert "--navy: #19213d" in css
    assert ".latency-panel" in css
    assert ".latency-table-wrap" in css
    assert "@media (max-width: 760px)" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert ":focus-visible" in css
    assert "button:disabled {\n  cursor: not-allowed;" in css


def test_device_test_script_uses_protected_device_contract_without_persisting_key():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        response = client.get("/static/device-test.js")

    assert response.status_code == 200
    assert 'headers.set("X-Device-Key", deviceKey.value.trim())' in response.text
    assert 'request("/api/device/turn"' in response.text
    assert 'request("/api/device/recording/start"' in response.text
    assert 'request("/api/device/recording/stop"' in response.text
    assert 'request("/api/device/recording/replay"' in response.text
    assert 'request("/api/device/state"' in response.text
    assert 'request("/api/device/metrics")' in response.text
    assert 'request(payload.audio_url' in response.text
    assert "URL.createObjectURL(audioBlob)" in response.text
    assert 'request("/api/device/playback-finished"' in response.text
    assert 'request("/api/device/reset"' in response.text
    assert 'transcript.textContent = snapshot.transcript || "尚未识别"' in response.text
    assert 'answer.textContent = snapshot.answer || "回答会显示在这里"' in response.text
    assert "if (snapshot.transcript)" not in response.text
    assert "if (snapshot.answer)" not in response.text
    assert "textContent" in response.text
    assert "function formatDuration" in response.text
    assert "function refreshMetrics" in response.text
    assert "await refreshMetrics();" in response.text
    assert 'window.setInterval(refreshState, 2000)' in response.text
    assert "setInterval(refreshMetrics" not in response.text
    assert 'latency-current-tab' in response.text
    assert 'latency-stats-tab' in response.text
    assert "localStorage" not in response.text
    assert "sessionStorage" not in response.text
    assert "document.cookie" not in response.text
    assert "innerHTML" not in response.text
    assert "if (!operationPending) clearError()" not in response.text
    assert 'if (error.message === "设备凭证无效") stopPolling()' in response.text
    assert 'currentPhase === "recording"' in response.text
    assert 'localRecordLabel.textContent = "结束并提交"' in response.text
    assert "snapshot.has_last_recording" in response.text
    assert 'replayRecordingLabel.textContent = "正在播放录音"' in response.text
    assert "URL.createObjectURL(recording" not in response.text
    assert 'inputMode === "microphone" && currentPhase === "speaking"' in response.text
    assert 'audioHint.textContent = "正在由树莓派扬声器播放"' in response.text
    assert (
        'const NO_SPEECH_MESSAGE = "没有听清您的声音，请靠近麦克风后再试一次。"'
        in response.text
    )
    assert 'phase.textContent = "未检测到语音"' in response.text
    assert '422: "没有听清您的声音，请靠近麦克风后再试一次。"' in response.text


def test_index_uses_local_xzinfra_brand_assets():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert 'src="/static/xzinfra-logo.svg"' in response.text
    assert "让每一次参观" in response.text
    assert "都有 <em>AI</em> 讲解" in response.text
    assert "https://" not in response.text
    assert "http://" not in response.text


def test_brand_logo_is_served_as_svg():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        response = client.get("/static/xzinfra-logo.svg")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert response.text.lstrip().startswith("<svg")


def test_index_exposes_accessible_live_regions_and_audio_controls():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        response = client.get("/")

    assert 'id="connection-text"' in response.text
    assert 'aria-live="polite"' in response.text
    assert 'id="form-error"' in response.text
    assert 'role="alert"' in response.text
    assert 'id="audio"' in response.text
    assert "controls" in response.text


def test_frontend_script_keeps_existing_api_contract():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        response = client.get("/static/app.js")

    assert response.status_code == 200
    assert 'new WebSocket(`${protocol}//${window.location.host}/ws`)' in response.text
    assert 'fetch("/api/questions"' in response.text
    assert 'fetch("/api/playback-finished"' in response.text
    assert "form.requestSubmit()" in response.text


def test_styles_include_brand_tokens_and_responsive_accessibility_rules():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        response = client.get("/static/styles.css")

    assert response.status_code == 200
    assert "--brand: #1c6af6" in response.text
    assert "--navy: #19213d" in response.text
    assert "@media (max-width: 600px)" in response.text
    assert "@media (prefers-reduced-motion: reduce)" in response.text
    assert "min-height: 50px" in response.text


def test_frontend_script_exposes_status_and_audio_fallback_copy():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        response = client.get("/static/app.js")

    assert response.status_code == 200
    assert "interactionCard.dataset.phase = snapshot.phase" in response.text
    assert 'submitLabel.textContent = "正在处理"' in response.text
    assert "浏览器阻止了自动播放，请点击播放讲解。" in response.text
    assert 'audioHint.textContent = "语音讲解已准备好。"' in response.text


def test_frontend_exposes_new_conversation_control():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        page = client.get("/")
        script = client.get("/static/app.js")

    assert 'id="new-conversation"' in page.text
    assert 'fetch("/api/session/reset"' in script.text
    assert "确定开始新对话吗？" in script.text
    assert "已开始新的讲解会话" in script.text


def test_index_creates_httponly_session_cookie():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        response = client.get("/")

    cookie = response.headers["set-cookie"]
    assert "showroom_session=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/" in cookie


def test_refresh_reuses_session_cookie():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        client.get("/")
        first_cookie = client.cookies.get("showroom_session")
        client.get("/")
        second_cookie = client.cookies.get("showroom_session")

    assert first_cookie == second_cookie
    assert len(runtime.controllers) == 1


def test_api_requires_session_cookie():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        response = client.post("/api/questions", json={"question": "问题"})

    assert response.status_code == 401
    assert response.json()["detail"] == "会话已失效，请刷新页面"


def test_different_cookies_use_different_controllers():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        first = client.get("/").cookies["showroom_session"]
        client.cookies.clear()
        second = client.get("/").cookies["showroom_session"]
        runtime.controllers[0].ask_text.return_value = TextQuestionResult(
            answer="访客甲", audio=None
        )
        runtime.controllers[1].ask_text.return_value = TextQuestionResult(
            answer="访客乙", audio=None
        )
        first_response = client.post(
            "/api/questions",
            headers={"Cookie": f"showroom_session={first}"},
            json={"question": "甲的问题"},
        )
        second_response = client.post(
            "/api/questions",
            headers={"Cookie": f"showroom_session={second}"},
            json={"question": "乙的问题"},
        )

    assert first_response.json()["answer"] == "访客甲"
    assert second_response.json()["answer"] == "访客乙"


def test_audio_cannot_be_read_from_another_session():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        first = client.get("/").cookies["showroom_session"]
        client.cookies.clear()
        second = client.get("/").cookies["showroom_session"]
        first_session = runtime.sessions.get(first)
        assert first_session is not None
        audio_id = first_session.audio.put(b"RIFF-audio")

        owner = client.get(
            f"/api/audio/{audio_id}",
            headers={"Cookie": f"showroom_session={first}"},
        )
        stranger = client.get(
            f"/api/audio/{audio_id}",
            headers={"Cookie": f"showroom_session={second}"},
        )

    assert owner.status_code == 200
    assert stranger.status_code == 404


def test_reset_affects_only_current_session():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        first = client.get("/").cookies["showroom_session"]
        client.cookies.clear()
        client.get("/")
        response = client.post(
            "/api/session/reset",
            headers={"Cookie": f"showroom_session={first}"},
        )

    assert response.status_code == 204
    runtime.controllers[0].reset.assert_awaited_once()
    runtime.controllers[1].reset.assert_not_awaited()


def test_websockets_receive_only_their_session_state():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        first = client.get("/").cookies["showroom_session"]
        client.cookies.clear()
        second = client.get("/").cookies["showroom_session"]
        first_session = runtime.sessions.get(first)
        second_session = runtime.sessions.get(second)
        assert first_session is not None and second_session is not None
        asyncio.run(first_session.state.set_message("只属于访客甲"))
        asyncio.run(second_session.state.set_message("只属于访客乙"))

        with client.websocket_connect(
            "/ws", headers={"Cookie": f"showroom_session={first}"}
        ) as first_socket:
            assert first_socket.receive_json()["message"] == "只属于访客甲"
        with client.websocket_connect(
            "/ws", headers={"Cookie": f"showroom_session={second}"}
        ) as second_socket:
            assert second_socket.receive_json()["message"] == "只属于访客乙"


def test_websocket_receives_initial_state_snapshot():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        establish_session(client, runtime)
        with client.websocket_connect("/ws") as socket:
            payload = socket.receive_json()

    assert payload["phase"] == "idle"
    assert payload["message"] == "输入问题开始讲解"


@pytest.mark.asyncio
async def test_expired_websocket_session_accepts_then_closes_with_policy_code():
    runtime = FakeRuntime()
    app = create_app(runtime)
    route = next(item for item in app.routes if item.path == "/ws")
    sent = []

    async def receive():
        return {"type": "websocket.connect"}

    async def send(message):
        sent.append(message)

    websocket = WebSocket(
        {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "scheme": "ws",
            "path": "/ws",
            "raw_path": b"/ws",
            "query_string": b"",
            "headers": [(b"cookie", b"showroom_session=expired")],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "subprotocols": [],
            "state": {},
            "app": app,
        },
        receive,
        send,
    )

    await route.endpoint(websocket)

    assert [message["type"] for message in sent] == [
        "websocket.accept",
        "websocket.close",
    ]
    assert sent[1]["code"] == 1008


def test_question_returns_temporary_audio_url():
    runtime = FakeRuntime()
    wav = b"RIFF\x04\x00\x00\x00WAVE"
    with TestClient(create_app(runtime)) as client:
        session = establish_session(client, runtime)
        session.controller.ask_text.return_value = TextQuestionResult(
            answer="讲解内容。", audio=wav
        )
        response = client.post("/api/questions", json={"question": "介绍这个展项"})
        audio_response = client.get(response.json()["audio_url"])

    assert response.status_code == 200
    assert response.json()["answer"] == "讲解内容。"
    assert audio_response.content == wav
    assert audio_response.headers["content-type"] == "audio/wav"
    assert audio_response.headers["cache-control"] == "no-store"


def test_tts_warning_returns_answer_without_audio_url():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        session = establish_session(client, runtime)
        session.controller.ask_text.return_value = TextQuestionResult(
            answer="仍然可以阅读。",
            audio=None,
            warning="语音暂时不可用，您仍可阅读文字答案",
        )
        response = client.post("/api/questions", json={"question": "问题"})

    assert response.status_code == 200
    assert response.json()["audio_url"] is None
    assert response.json()["warning"] == "语音暂时不可用，您仍可阅读文字答案"


def test_busy_question_maps_to_conflict():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        session = establish_session(client, runtime)
        session.controller.ask_text.side_effect = QuestionInProgress()
        response = client.post("/api/questions", json={"question": "问题"})

    assert response.status_code == 409
    assert response.json()["detail"] == "已有问题正在处理中"


def test_xzkb_failure_maps_to_service_unavailable():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        session = establish_session(client, runtime)
        session.controller.ask_text.side_effect = GuideServiceUnavailable("xzkb")
        response = client.post("/api/questions", json={"question": "问题"})

    assert response.status_code == 503
    assert response.json()["detail"] == "知识库暂时不可用，请稍后重试"


def test_playback_finished_notifies_controller():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        session = establish_session(client, runtime)
        response = client.post("/api/playback-finished")

    assert response.status_code == 204
    session.controller.finish_playback.assert_awaited_once()
