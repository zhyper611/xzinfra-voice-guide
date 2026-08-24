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
    assert 'id="unified-action"' in response.text
    assert 'id="interaction-mode"' in response.text
    assert 'id="interaction-stage"' in response.text
    assert 'id="gesture-hint"' in response.text
    assert 'id="replay-recording"' in response.text
    assert 'id="replay-recording-label"' in response.text
    assert 'id="advanced-wav"' in response.text
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


def test_device_page_uses_unified_button_and_advanced_wav():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        html = client.get("/device-test").text

    required_ids = [
        "unified-action",
        "interaction-mode",
        "interaction-stage",
        "gesture-hint",
        "advanced-wav",
        "wav-purpose-dialogue",
        "wav-purpose-knowledge",
        "wav-knowledge-review",
        "wav-review-hint",
        "wav-knowledge-actions",
        "wav-knowledge-discard",
        "wav-knowledge-retry",
        "wav-knowledge-save",
    ]
    for element_id in required_ids:
        assert f'id="{element_id}"' in html
    assert 'id="microphone-tab"' not in html
    assert 'id="knowledge-tab"' not in html


def test_device_page_loads_press_gesture_before_page_script():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        html = client.get("/device-test").text

    assert html.index("press-gesture.js") < html.index("device-interaction.js")
    assert html.index("device-interaction.js") < html.index("device-test.js")


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


def test_faq_cache_page_is_a_separate_no_store_admin_workspace():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        response = client.get("/faq-cache")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "高频问答缓存" in response.text
    assert 'id="admin-key"' in response.text
    assert 'id="entry-list"' in response.text
    assert 'id="entry-detail"' in response.text
    assert 'id="generate-audio"' in response.text
    assert 'id="play-draft"' in response.text
    assert 'id="approve-audio"' in response.text
    assert 'id="review-audio"' in response.text
    assert 'id="new-entry-button"' in response.text
    assert 'id="edit-entry-button"' in response.text
    assert 'id="delete-entry-button"' in response.text
    assert 'id="entry-editor"' in response.text
    assert 'id="entry-editor-form"' in response.text
    assert '<span>01</span>' in response.text
    assert '<span>02</span>' in response.text
    assert '<span>03</span>' in response.text
    assert 'src="/static/faq-cache.js?v=' in response.text
    assert 'href="/static/faq-cache.css?v=' in response.text
    assert "device-test" not in response.text
    assert "showroom_session=" not in response.headers.get("set-cookie", "")


def test_faq_cache_assets_are_responsive_and_do_not_persist_admin_key():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        css = client.get("/static/faq-cache.css").text.replace("\r\n", "\n")
        script = client.get("/static/faq-cache.js").text

    assert "--brand: #1c6af6" in css
    assert ".workspace" in css
    assert "@media (max-width: 760px)" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert ":focus-visible" in css
    assert 'fetch("/api/faq-cache"' in script
    assert '"X-FAQ-Admin-Key": key' in script
    assert 'cache: "no-store"' in script
    assert "/audio/generate" in script
    assert "?source=${source}" in script
    assert "/audio/approve" in script
    assert "reviewedDraftId !== entry.id" in script
    assert 'reviewAudio.addEventListener("ended"' in script
    assert 'method = "PUT"' in script
    assert 'method: "DELETE"' in script
    assert 'expected_edit_token: entry.edit_token' in script
    assert 'payload.expected_edit_token = editorEntry.edit_token' in script
    assert "entryEditor.showModal()" in script
    assert "localStorage" not in script
    assert "sessionStorage" not in script
    assert ".innerHTML" not in script


def test_device_test_script_uses_protected_device_contract_without_persisting_key():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        response = client.get("/static/device-test.js")

    assert response.status_code == 200
    assert 'headers.set("X-Device-Key", key)' in response.text
    assert 'request("/api/device/turn"' in response.text
    assert 'request("/api/device/recording/start"' in response.text
    assert 'request("/api/device/recording/stop"' in response.text
    assert 'request("/api/device/recording/replay"' in response.text
    assert 'request("/api/device/state"' in response.text
    assert 'request("/api/device/metrics"' in response.text
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
    assert 'const KNOWLEDGE_LEASE_KEY = "showroom-knowledge-lease"' in response.text
    assert 'const KNOWLEDGE_DRAFT_SOURCE_KEY = "showroom-knowledge-draft-source"' in response.text
    assert "document.cookie" not in response.text
    assert "innerHTML" not in response.text
    assert "if (!operationPending) clearError()" not in response.text
    assert 'if (error.message === "设备凭证无效") stopPolling()' in response.text
    assert 'currentPhase === "recording"' in response.text
    assert "async function runDialogueShortPress()" in response.text
    assert 'label: "短按结束录音"' in response.text
    assert "snapshot.has_last_recording" in response.text
    assert 'replayRecordingLabel.textContent = "正在播放录音"' in response.text
    assert "URL.createObjectURL(recording" not in response.text
    assert 'dialogueSource === "microphone" && currentPhase === "speaking"' in response.text
    assert 'audioHint.textContent = "正在由树莓派扬声器播放"' in response.text
    assert (
        'const NO_SPEECH_MESSAGE = "没有听清您的声音，请靠近麦克风后再试一次。"'
        in response.text
    )
    assert 'phase.textContent = "未检测到语音"' in response.text
    assert '422: "没有听清您的声音，请靠近麦克风后再试一次。"' in response.text
    assert "new AbortController()" in response.text
    assert "REQUEST_TIMEOUT_MS" in response.text
    assert "请求超时，请检查网络后重试" in response.text


def test_device_test_page_exposes_knowledge_capture_controls():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        response = client.get("/device-test")

    assert response.status_code == 200
    assert 'id="unified-action"' in response.text
    assert 'id="interaction-mode"' in response.text
    assert 'id="interaction-stage"' in response.text
    assert 'id="gesture-hint"' in response.text
    assert 'id="knowledge-control-state"' in response.text
    assert 'id="knowledge-mode-state"' in response.text
    assert 'id="knowledge-processing-stage"' in response.text
    assert 'id="knowledge-draft"' in response.text
    assert '<output id="knowledge-draft"' in response.text
    assert 'id="knowledge-sync"' in response.text
    assert 'id="knowledge-sync-state"' in response.text
    assert 'id="wav-knowledge-review"' in response.text
    assert 'aria-describedby="wav-review-hint"' in response.text
    assert 'id="wav-knowledge-discard"' in response.text
    assert 'id="wav-knowledge-retry"' in response.text
    assert 'id="wav-knowledge-save"' in response.text
    assert 'id="microphone-tab"' not in response.text
    assert 'id="wav-tab"' not in response.text
    assert 'id="knowledge-tab"' not in response.text


def test_device_test_styles_keep_knowledge_controls_stable_and_responsive():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        response = client.get("/static/device-test.css")

    assert response.status_code == 200
    css = response.text.replace("\r\n", "\n")
    assert ".knowledge-status-grid" in css
    assert ".unified-action" in css
    assert "inline-size: clamp(9rem, 28vw, 11rem);" in css
    assert "aspect-ratio: 1;" in css
    assert ".hold-progress circle" in css
    assert ".unified-action[data-holding=\"true\"]" in css
    assert "animation: hold-progress var(--hold-duration, 1500ms) linear forwards;" in css
    assert ".advanced-wav" in css
    assert ".wav-knowledge-actions" in css
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in css
    assert "#knowledge-draft" in css
    assert '@media (max-width: 599px)' in css
    assert ".wav-knowledge-actions {\n    grid-template-columns: 1fr;" in css
    assert "overflow-wrap: anywhere" in css


def test_device_test_script_implements_knowledge_lease_and_resync_contract():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        response = client.get("/static/device-test.js")

    assert response.status_code == 200
    script = response.text
    assert 'const KNOWLEDGE_LEASE_KEY = "showroom-knowledge-lease"' in script
    assert "sessionStorage.getItem(KNOWLEDGE_LEASE_KEY)" in script
    assert "sessionStorage.setItem(KNOWLEDGE_LEASE_KEY, knowledgeLeaseToken)" in script
    assert "sessionStorage.removeItem(KNOWLEDGE_LEASE_KEY)" in script
    assert "createKnowledgeDraftSourceStore" in script
    assert 'persistKnowledgeDraftSource("wav")' in script
    assert "resolveKnowledgeDraftTransition" in script
    assert "persistKnowledgeDraftSource(draftTransition.source)" in script
    assert "if (draftTransition.invalidateReview) clearKnowledgeReviewAudio()" in script
    assert "renderAuthoritativeKnowledgeState(snapshot, { captureEntry, shortPress })" in script
    assert "ShowroomDeviceInteraction.createRequestEpoch()" in script
    assert "requestEpoch = knowledgeStateEpoch.capture()" in script
    assert script.count("if (!knowledgeStateEpoch.isCurrent(requestEpoch)) return false;") == 2
    assert "!allowDuringOperation && (knowledgeOperationPending || operationPending)" in script
    assert "knowledgeOperationPending || operationPending || document.hidden" in script
    assert script.count("beginKnowledgeMutation();") >= 4
    assert "function renderAuthoritativeKnowledgeState(snapshot, options = {})" in script
    assert 'headers.set("X-Knowledge-Lease", knowledgeLeaseToken)' in script
    assert 'knowledgeRequest("/api/device/knowledge/acquire"' in script
    assert 'knowledgeRequest("/api/device/knowledge/state"' in script
    assert "/api/device/knowledge/short-press" in script
    assert "/api/device/knowledge/long-press" in script
    assert 'knowledgeRequest("/api/device/knowledge/release"' in script
    assert "async function releaseKnowledgeControl()" in script
    assert "function getUnifiedAction()" in script
    assert "function updateUnifiedAction()" in script
    assert 'label: "短按开始对话"' in script
    assert 'label: "短按录入知识"' in script
    assert 'label: "短按停止并复述"' in script
    assert 'label: "长按保存并返回"' in script
    assert 'errorState.control_state !== "owned"' in script
    assert '`/api/device/knowledge/entries/${encodeURIComponent(knowledgeEntryId)}`' in script
    assert "knowledgePollTimer = window.setInterval(refreshKnowledgeState, 2000)" in script
    assert "document.hidden" in script
    assert 'document.addEventListener("visibilitychange"' in script
    assert "stopAllPolling()" in script
    assert "refreshKnowledgeState({ showFailure: true });" in script
    assert "error.code = payload.code" in script
    assert "error.payload = payload" in script
    assert 'error.code === "knowledge_lease_expired"' in script
    assert "await resyncKnowledgeState({ showFailure: false })" in script
    assert "async function resyncKnowledgeState" in script
    assert "if (knowledgeOperationPending || operationPending) return;" in script
    assert "ShowroomDeviceInteraction.captureKnowledgeEntry" in script
    assert "if (entryCapture.capturedEntryId)" in script
    assert 'entry.sync_state === "synced"' in script
    assert 'entry.sync_state === "retrying"' in script
    assert 'owned && mode === "recording"' in script
    assert 'mode === "processing"' in script
    assert 'const knowledgeModeActive = knowledgeMode !== "inactive"' in script
    assert "runTest.disabled = controlsPending || busy || !keyReady || !wavAllowed" in script
    assert "resetDevice.disabled = controlsPending || knowledgeModeActive" in script
    assert "replayRecording.disabled" in script and "knowledgeModeActive" in script
    assert "knowledgeOperationPending || operationPending || document.hidden" in script
    assert "ShowroomPressGesture.createPressGesture" in script
    assert "ShowroomDeviceInteraction.bindUnifiedPress" in script
    assert "thresholdMs: HOLD_THRESHOLD_MS" in script
    assert "releaseTarget: window" in script
    assert 'window.addEventListener("pagehide"' in script
    assert "keepalive: true" in script
    assert "unifiedInput.cancel()" in script
    assert 'payload = await knowledgeRequest(' in script
    assert '"/api/device/knowledge/upload"' in script
    upload_handler = script.split("async function submitKnowledgeWav(file)", 1)[1].split(
        "async function loadKnowledgeReviewAudio", 1
    )[0]
    assert "renderAuthoritativeKnowledgeState(payload.knowledge_state);" in upload_handler
    assert upload_handler.index("beginKnowledgeMutation();") < upload_handler.index(
        '"/api/device/knowledge/upload"'
    )
    assert "captureEntry" not in upload_handler
    assert "clearKnowledgeLease" not in upload_handler
    assert "async function knowledgeRawRequest(" in script
    assert "reviewBlob.size === 0" in script
    assert "URL.createObjectURL(reviewBlob)" in script
    assert "wavReviewGate.markLoaded(objectUrl)" in script
    assert 'wavKnowledgeReview.addEventListener("ended"' in script
    assert 'wavKnowledgeReview.addEventListener("error"' in script
    assert "wavReviewGate.markReady(playback.token)" in script
    assert "wavReviewGate.markFailed(playback.token)" in script
    assert "deviceKey.disabled = controlsPending" in script
    assert "toggleKey.disabled = controlsPending" in script
    assert "wavReviewGate.markReady(objectUrl)" not in script
    assert 'knowledgeDraftSource !== "microphone" && !wavReviewGate.canSave' in script
    assert "wavKnowledgeSave.disabled" in script and "!wavReviewGate.canSave" in script
    assert "retryKnowledgeReviewAudio" in script
    assert 'wavKnowledgeRetry.addEventListener("click", retryKnowledgeReviewAudio)' in script
    assert "浏览器未能自动播放复述" in script
    assert "localRecord" not in script
    assert "knowledgeShortPress" not in script
    assert "knowledgeLongPress" not in script

    clear_review_handler = script.split("function clearKnowledgeReviewAudio()", 1)[1].split(
        "function mountKnowledgeReviewAudio", 1
    )[0]
    assert "wavReviewGate.clear()" in clear_review_handler
    assert "wavReview.hidden = true" in clear_review_handler
    assert "wavKnowledgeReview.hidden = true" in script

    review_handler = script.split("async function loadKnowledgeReviewAudio()", 1)[1].split(
        "async function retryKnowledgeReviewAudio", 1
    )[0]
    assert "knowledgeRawRequest(" in review_handler
    assert "knowledgeReviewAudioPath" in review_handler

    retry_handler = script.split("async function retryKnowledgeReviewAudio()", 1)[1].split(
        "async function submitDialogueWav", 1
    )[0]
    assert "await loadKnowledgeReviewAudio()" in retry_handler
    assert "/api/device/knowledge/upload" not in retry_handler

    input_handler = script.split('deviceKey.addEventListener("input"', 1)[1].split(
        'wavPurposeDialogue.addEventListener', 1
    )[0]
    assert "startKnowledgePolling()" not in input_handler
    assert "if (!deviceKey.value.trim())" in input_handler

    visibility_handler = script.split(
        'document.addEventListener("visibilitychange"', 1
    )[1].split('window.addEventListener("pagehide"', 1)[0]
    assert visibility_handler.count("startAllPolling();") == 1
    assert "await refreshState" not in visibility_handler
    assert "await refreshKnowledgeState" not in visibility_handler


def test_device_test_polling_discards_stale_device_key_requests():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        response = client.get("/static/device-test.js")

    assert response.status_code == 200
    script = response.text
    assert "let deviceKeyGeneration = 0" in script
    assert "deviceKeyGeneration += 1" in script
    assert "function isCurrentDeviceKeyRequest(generation, key)" in script
    assert script.count("const requestGeneration = deviceKeyGeneration") >= 4
    assert script.count("const requestKey = deviceKey.value.trim()") >= 4
    assert script.count(
        "if (!isCurrentDeviceKeyRequest(requestGeneration, requestKey)) return;"
    ) >= 4
    assert "const requestId = ++stateRequestId" in script
    assert "const requestId = ++knowledgeStateRequestId" in script
    assert "const requestId = ++knowledgeEntryRequestId" in script
    assert "const requestId = ++metricsRequestId" in script
    assert "if (requestId === stateRequestId)" in script
    assert "if (requestId === knowledgeStateRequestId)" in script
    assert "if (requestId === knowledgeEntryRequestId)" in script
    assert "if (requestId === metricsRequestId)" in script


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
    assert 'request("/api/questions"' in response.text
    assert 'request("/api/playback-finished"' in response.text
    assert "form.requestSubmit()" in response.text
    assert "new AbortController()" in response.text
    assert "REQUEST_TIMEOUT_MS" in response.text
    assert "请求超时，请检查网络后重试" in response.text


def test_health_endpoint_reports_process_liveness_without_session():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


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
    assert 'audio.addEventListener("error"' in response.text
    assert "notifyPlaybackFinished" in response.text


def test_device_test_reports_audio_load_failure_to_backend():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        response = client.get("/static/device-test.js")

    assert response.status_code == 200
    assert 'audio.addEventListener("error"' in response.text
    assert 'request("/api/device/playback-finished"' in response.text


def test_frontend_exposes_new_conversation_control():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        page = client.get("/")
        script = client.get("/static/app.js")

    assert 'id="new-conversation"' in page.text
    assert 'request("/api/session/reset"' in script.text
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
