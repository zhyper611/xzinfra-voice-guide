import io
import wave
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from showroom_guide.controller import GuideServiceUnavailable, QuestionInProgress
from showroom_guide.device import (
    DeviceTranscriptionUnavailable,
    DeviceTurnResult,
    InvalidDeviceAudio,
    NO_SPEECH_MESSAGE,
    NoSpeechDetected,
)
from showroom_guide.main import create_app
from showroom_guide.knowledge_mode import KnowledgeModeState, KnowledgeProcessingStage
from showroom_guide.knowledge_web import (
    KnowledgeControlState,
    KnowledgeEntrySnapshot,
    KnowledgeSyncState,
    KnowledgeWebError,
    KnowledgeWebState,
)
from showroom_guide.local_audio import LocalAudioError
from showroom_guide.local_device import LastRecordingNotFound
from showroom_guide.models import GuidePhase, GuideSnapshot
from showroom_guide.sessions import SessionManager


DEVICE_KEY = "device-test-key"


def make_wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 160)
    return output.getvalue()


class FakeDevice:
    def __init__(self) -> None:
        self.snapshot = GuideSnapshot()
        self.process_wav = AsyncMock()
        self.get_audio = MagicMock()
        self.finish_playback = AsyncMock()
        self.reset = AsyncMock()
        self.metrics_snapshot = MagicMock(
            return_value={
                "window_size": 0,
                "counts": {"success": 0, "degraded": 0, "error": 0},
                "latest": None,
                "metrics": {},
            }
        )


class FakeRuntime:
    def __init__(self) -> None:
        self.sessions = SessionManager(
            controller_factory=lambda _state: MagicMock(),
            max_sessions=100,
            idle_seconds=1800,
            audio_ttl_seconds=600,
            audio_items_per_session=3,
        )
        self.device = FakeDevice()
        self.local_device = MagicMock()
        self.local_device.start_recording = AsyncMock(
            return_value=GuideSnapshot(
                phase=GuidePhase.RECORDING,
                message="正在录音，再次点击后提交",
            )
        )
        self.local_device.stop_recording = AsyncMock()
        self.local_device.process_upload = AsyncMock()
        self.local_device.reset = AsyncMock()
        self.local_device.replay_last_recording = AsyncMock()
        self.local_device.has_last_recording = False
        self.device_api_key = SecretStr(DEVICE_KEY)
        self.device_max_upload_bytes = 1024
        self.cleanup_seconds = 60.0
        self.knowledge_web = MagicMock()
        self.knowledge_web.acquire = AsyncMock()
        self.knowledge_web.state = AsyncMock()
        self.knowledge_web.short_press = AsyncMock()
        self.knowledge_web.long_press = AsyncMock()
        self.knowledge_web.release = AsyncMock()
        self.knowledge_web.entry = AsyncMock()
        self.aclose = AsyncMock()


def authorized_headers() -> dict[str, str]:
    return {"X-Device-Key": DEVICE_KEY}


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("get", "/api/device/state", {}),
        ("get", "/api/device/metrics", {}),
        ("post", "/api/device/turn", {"files": {"file": ("q.wav", make_wav())}}),
        ("post", "/api/device/recording/start", {}),
        ("post", "/api/device/recording/stop", {}),
        ("post", "/api/device/recording/replay", {}),
        ("get", "/api/device/audio/audio-id", {}),
        ("post", "/api/device/playback-finished", {}),
        ("post", "/api/device/reset", {}),
        ("post", "/api/device/knowledge/acquire", {}),
        ("get", "/api/device/knowledge/state", {}),
        ("post", "/api/device/knowledge/short-press", {}),
        ("post", "/api/device/knowledge/long-press", {}),
        ("post", "/api/device/knowledge/release", {}),
        ("get", "/api/device/knowledge/entries/entry-id", {}),
    ],
)
@pytest.mark.parametrize("provided_key", [None, "wrong-device-key"])
def test_device_routes_require_the_same_valid_key(method, path, kwargs, provided_key):
    runtime = FakeRuntime()
    headers = {} if provided_key is None else {"X-Device-Key": provided_key}
    with TestClient(create_app(runtime)) as client:
        response = getattr(client, method)(path, headers=headers, **kwargs)

    assert response.status_code == 401
    assert response.json() == {"detail": "设备凭证无效"}


def test_device_state_returns_snapshot():
    runtime = FakeRuntime()
    runtime.local_device.has_last_recording = True
    runtime.device.snapshot = GuideSnapshot(
        phase=GuidePhase.TRANSCRIBING,
        transcript="问题",
        message="正在识别您的问题",
    )
    with TestClient(create_app(runtime)) as client:
        response = client.get("/api/device/state", headers=authorized_headers())

    assert response.status_code == 200
    assert response.json()["phase"] == "transcribing"
    assert response.json()["transcript"] == "问题"
    assert response.json()["has_last_recording"] is True


def test_device_recording_is_rejected_during_knowledge_mode():
    runtime = FakeRuntime()
    runtime.button_workflow = MagicMock()
    runtime.button_workflow.run_dialogue = AsyncMock(
        side_effect=RuntimeError("设备当前处于知识补充模式")
    )

    with TestClient(create_app(runtime)) as client:
        response = client.post(
            "/api/device/recording/start",
            headers=authorized_headers(),
        )

    assert response.status_code == 409
    assert response.json() == {"detail": "设备当前处于知识补充模式"}
    runtime.local_device.start_recording.assert_not_awaited()


def test_device_metrics_returns_protected_read_only_snapshot():
    runtime = FakeRuntime()
    runtime.device.metrics_snapshot.return_value = {
        "window_size": 1,
        "counts": {"success": 1, "degraded": 2, "error": 3},
        "latest": {
            "recorded_at": "2026-08-18T01:02:03.456Z",
            "turn_id": "latest-turn",
            "outcome": "degraded",
        },
        "metrics": {
            "asr_ms": {"samples": 1, "p50": 12.34, "p95": 12.34},
        },
    }
    with TestClient(create_app(runtime)) as client:
        response = client.get("/api/device/metrics", headers=authorized_headers())

    assert response.status_code == 200
    assert response.json() == runtime.device.metrics_snapshot.return_value
    runtime.device.metrics_snapshot.assert_called_once_with()


def test_device_turn_returns_transcript_answer_and_audio_url():
    runtime = FakeRuntime()
    runtime.local_device.process_upload.return_value = DeviceTurnResult(
        transcript="介绍展项甲",
        answer="讲解内容",
        audio_id="unguessable-audio-id",
    )
    source = make_wav()
    with TestClient(create_app(runtime)) as client:
        response = client.post(
            "/api/device/turn",
            headers=authorized_headers(),
            files={"file": ("question.wav", source, "audio/wav")},
        )

    assert response.status_code == 200
    assert response.json() == {
        "transcript": "介绍展项甲",
        "answer": "讲解内容",
        "audio_url": "/api/device/audio/unguessable-audio-id",
        "warning": None,
    }
    runtime.local_device.process_upload.assert_awaited_once_with(source)


def test_device_turn_keeps_text_when_tts_is_degraded():
    runtime = FakeRuntime()
    runtime.local_device.process_upload.return_value = DeviceTurnResult(
        transcript="问题",
        answer="文字答案",
        audio_id=None,
        warning="语音暂时不可用，您仍可阅读文字答案",
    )
    with TestClient(create_app(runtime)) as client:
        response = client.post(
            "/api/device/turn",
            headers=authorized_headers(),
            files={"file": ("question.wav", make_wav(), "audio/wav")},
        )

    assert response.status_code == 200
    assert response.json()["audio_url"] is None
    assert response.json()["warning"] == "语音暂时不可用，您仍可阅读文字答案"


def test_device_turn_rejects_oversized_upload_before_processing():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        response = client.post(
            "/api/device/turn",
            headers=authorized_headers(),
            files={"file": ("question.wav", b"x" * 1025, "audio/wav")},
        )

    assert response.status_code == 413
    runtime.local_device.process_upload.assert_not_awaited()


@pytest.mark.parametrize(
    ("error", "status", "detail"),
    [
        (InvalidDeviceAudio("WAV 参数错误"), 415, "WAV 参数错误"),
        (QuestionInProgress(), 409, "已有设备问题正在处理中"),
        (NoSpeechDetected(NO_SPEECH_MESSAGE), 422, NO_SPEECH_MESSAGE),
        (DeviceTranscriptionUnavailable(), 503, "语音识别暂时不可用，请稍后重试"),
        (GuideServiceUnavailable("xzkb"), 503, "知识库暂时不可用，请稍后重试"),
        (GuideServiceUnavailable("capacity"), 503, "当前使用人数较多，请稍后重试"),
    ],
)
def test_device_turn_maps_domain_errors(error, status, detail):
    runtime = FakeRuntime()
    runtime.local_device.process_upload.side_effect = error
    with TestClient(create_app(runtime)) as client:
        response = client.post(
            "/api/device/turn",
            headers=authorized_headers(),
            files={"file": ("question.wav", make_wav(), "audio/wav")},
        )

    assert response.status_code == status
    assert response.json()["detail"] == detail


def test_local_recording_start_and_stop_return_device_state_and_turn():
    runtime = FakeRuntime()
    runtime.local_device.stop_recording.return_value = DeviceTurnResult(
        transcript="介绍展项甲",
        answer="讲解内容",
        audio_id="audio-id",
    )
    with TestClient(create_app(runtime)) as client:
        started = client.post(
            "/api/device/recording/start",
            headers=authorized_headers(),
        )
        stopped = client.post(
            "/api/device/recording/stop",
            headers=authorized_headers(),
        )

    assert started.status_code == 200
    assert started.json()["phase"] == "recording"
    assert stopped.status_code == 200
    assert stopped.json() == {
        "transcript": "介绍展项甲",
        "answer": "讲解内容",
        "audio_url": "/api/device/audio/audio-id",
        "warning": None,
    }
    runtime.local_device.start_recording.assert_awaited_once_with()
    runtime.local_device.stop_recording.assert_awaited_once_with()


@pytest.mark.parametrize(
    ("path", "error", "status", "detail"),
    [
        (
            "/api/device/recording/start",
            LocalAudioError("无法启动录音设备，请检查默认输入"),
            503,
            "无法启动录音设备，请检查默认输入",
        ),
        (
            "/api/device/recording/stop",
            QuestionInProgress(),
            409,
            "设备正在处理上一轮录音",
        ),
        (
            "/api/device/recording/stop",
            InvalidDeviceAudio("录音时间过短，请重新录音"),
            415,
            "录音时间过短，请重新录音",
        ),
        (
            "/api/device/recording/stop",
            NoSpeechDetected(NO_SPEECH_MESSAGE),
            422,
            NO_SPEECH_MESSAGE,
        ),
    ],
)
def test_local_recording_maps_domain_errors(path, error, status, detail):
    runtime = FakeRuntime()
    method = (
        runtime.local_device.start_recording
        if path.endswith("/start")
        else runtime.local_device.stop_recording
    )
    method.side_effect = error
    with TestClient(create_app(runtime)) as client:
        response = client.post(path, headers=authorized_headers())

    assert response.status_code == status
    assert response.json()["detail"] == detail


def test_replay_last_recording_returns_no_content():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        response = client.post(
            "/api/device/recording/replay",
            headers=authorized_headers(),
        )

    assert response.status_code == 204
    runtime.local_device.replay_last_recording.assert_awaited_once_with()


@pytest.mark.parametrize(
    ("error", "status", "detail"),
    [
        (LastRecordingNotFound("没有可播放的录音"), 404, "没有可播放的录音"),
        (QuestionInProgress(), 409, "设备正在使用，暂时不能播放录音"),
        (LocalAudioError("扬声器播放失败"), 503, "扬声器播放失败"),
    ],
)
def test_replay_last_recording_maps_domain_errors(error, status, detail):
    runtime = FakeRuntime()
    runtime.local_device.replay_last_recording.side_effect = error
    with TestClient(create_app(runtime)) as client:
        response = client.post(
            "/api/device/recording/replay",
            headers=authorized_headers(),
        )

    assert response.status_code == status
    assert response.json() == {"detail": detail}


def test_device_audio_download_is_protected_and_not_cached():
    runtime = FakeRuntime()
    runtime.device.get_audio.return_value = make_wav()
    with TestClient(create_app(runtime)) as client:
        response = client.get(
            "/api/device/audio/audio-id",
            headers=authorized_headers(),
        )

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.headers["cache-control"] == "no-store"
    runtime.device.get_audio.assert_called_once_with("audio-id")


def test_device_audio_missing_returns_not_found():
    from showroom_guide.audio_store import AudioNotFound

    runtime = FakeRuntime()
    runtime.device.get_audio.side_effect = AudioNotFound("expired")
    with TestClient(create_app(runtime)) as client:
        response = client.get(
            "/api/device/audio/expired",
            headers=authorized_headers(),
        )

    assert response.status_code == 404
    runtime.device.get_audio.assert_called_once_with("expired")


def test_device_playback_finished_and_reset_return_no_content():
    runtime = FakeRuntime()
    with TestClient(create_app(runtime)) as client:
        playback = client.post(
            "/api/device/playback-finished",
            headers=authorized_headers(),
        )
        reset = client.post("/api/device/reset", headers=authorized_headers())

    assert playback.status_code == 204
    assert reset.status_code == 204
    runtime.device.finish_playback.assert_awaited_once()
    runtime.local_device.reset.assert_awaited_once()


def test_device_reset_rejects_busy_session():
    runtime = FakeRuntime()
    runtime.local_device.reset.side_effect = QuestionInProgress()
    with TestClient(create_app(runtime)) as client:
        response = client.post("/api/device/reset", headers=authorized_headers())

    assert response.status_code == 409
    assert response.json()["detail"] == "设备问题正在处理中，暂时不能重置"


def knowledge_state(
    *,
    enabled: bool = True,
    mode_state: KnowledgeModeState = KnowledgeModeState.READY,
    processing_stage: KnowledgeProcessingStage | None = None,
    control_state: KnowledgeControlState = KnowledgeControlState.OWNED,
    lease_expires_at: float | None = 1234.5,
    draft_text: str | None = None,
    last_entry_id: str | None = None,
) -> KnowledgeWebState:
    return KnowledgeWebState(
        enabled=enabled,
        mode_state=mode_state,
        processing_stage=processing_stage,
        control_state=control_state,
        lease_expires_at=lease_expires_at,
        draft_text=draft_text,
        last_entry_id=last_entry_id,
    )


def knowledge_headers(lease_token: str | None = None) -> dict[str, str]:
    headers = authorized_headers()
    if lease_token is not None:
        headers["X-Knowledge-Lease"] = lease_token
    return headers


def test_knowledge_acquire_returns_lease_and_serialized_state_without_device_key():
    runtime = FakeRuntime()
    runtime.knowledge_web.acquire.return_value = (
        "lease-token",
        knowledge_state(draft_text="待确认知识"),
    )

    with TestClient(create_app(runtime)) as client:
        response = client.post(
            "/api/device/knowledge/acquire",
            headers=authorized_headers(),
        )

    assert response.status_code == 200
    assert response.json() == {
        "lease_token": "lease-token",
        "knowledge_state": {
            "enabled": True,
            "mode_state": "ready",
            "processing_stage": None,
            "control_state": "owned",
            "lease_expires_at": 1234.5,
            "draft_text": "待确认知识",
            "last_entry_id": None,
        },
    }
    assert DEVICE_KEY not in response.text
    runtime.knowledge_web.acquire.assert_awaited_once_with()


def test_second_knowledge_acquire_returns_busy_observer_state_with_redacted_draft():
    runtime = FakeRuntime()
    observer = knowledge_state(
        mode_state=KnowledgeModeState.CONFIRMING,
        processing_stage=KnowledgeProcessingStage.SYNTHESIZING,
        control_state=KnowledgeControlState.OBSERVED,
        lease_expires_at=None,
        draft_text=None,
    )
    runtime.knowledge_web.acquire.side_effect = KnowledgeWebError(
        "knowledge_control_busy",
        "知识补充功能正由其他页面控制",
        409,
        observer,
    )

    with TestClient(create_app(runtime)) as client:
        response = client.post(
            "/api/device/knowledge/acquire",
            headers=authorized_headers(),
        )

    assert response.status_code == 409
    assert response.json() == {
        "code": "knowledge_control_busy",
        "detail": "知识补充功能正由其他页面控制",
        "knowledge_state": {
            "enabled": True,
            "mode_state": "confirming",
            "processing_stage": "synthesizing",
            "control_state": "observed",
            "lease_expires_at": None,
            "draft_text": None,
            "last_entry_id": None,
        },
    }


def test_knowledge_acquire_returns_disabled_error_code():
    runtime = FakeRuntime()
    disabled = knowledge_state(
        enabled=False,
        mode_state=KnowledgeModeState.INACTIVE,
        control_state=KnowledgeControlState.AVAILABLE,
        lease_expires_at=None,
    )
    runtime.knowledge_web.acquire.side_effect = KnowledgeWebError(
        "knowledge_capture_disabled",
        "知识补充功能未启用",
        503,
        disabled,
    )

    with TestClient(create_app(runtime)) as client:
        response = client.post(
            "/api/device/knowledge/acquire",
            headers=authorized_headers(),
        )

    assert response.status_code == 503
    assert response.json()["code"] == "knowledge_capture_disabled"
    assert response.json()["knowledge_state"]["enabled"] is False
    assert isinstance(response.json()["detail"], str)


def test_knowledge_state_allows_observer_without_lease_and_redacts_owner_fields():
    runtime = FakeRuntime()
    runtime.knowledge_web.state.return_value = knowledge_state(
        mode_state=KnowledgeModeState.CONFIRMING,
        processing_stage=KnowledgeProcessingStage.PLAYING_REVIEW,
        control_state=KnowledgeControlState.OBSERVED,
        lease_expires_at=None,
        draft_text=None,
    )

    with TestClient(create_app(runtime)) as client:
        response = client.get(
            "/api/device/knowledge/state",
            headers=authorized_headers(),
        )

    assert response.status_code == 200
    assert response.json()["control_state"] == "observed"
    assert response.json()["processing_stage"] == "playing_review"
    assert response.json()["lease_expires_at"] is None
    assert response.json()["draft_text"] is None
    runtime.knowledge_web.state.assert_awaited_once_with(None)


@pytest.mark.parametrize(
    ("path", "method_name"),
    [
        ("/api/device/knowledge/short-press", "short_press"),
        ("/api/device/knowledge/long-press", "long_press"),
        ("/api/device/knowledge/release", "release"),
    ],
)
@pytest.mark.parametrize("lease_token", [None, "wrong-lease"])
def test_knowledge_control_rejects_missing_or_wrong_lease_with_domain_error(
    path,
    method_name,
    lease_token,
):
    runtime = FakeRuntime()
    method = getattr(runtime.knowledge_web, method_name)
    error_state = knowledge_state(
        control_state=KnowledgeControlState.OBSERVED,
        lease_expires_at=None,
    )
    runtime.knowledge_web.state.return_value = error_state
    method.side_effect = KnowledgeWebError(
        "knowledge_lease_expired",
        "知识补充控制权已过期",
        409,
        error_state,
    )

    with TestClient(create_app(runtime)) as client:
        response = client.post(path, headers=knowledge_headers(lease_token))

    assert response.status_code == 409
    assert response.json()["code"] == "knowledge_lease_expired"
    assert response.json()["detail"] == "知识补充控制权已过期"
    assert response.json()["knowledge_state"]["control_state"] == "observed"
    if lease_token is None:
        runtime.knowledge_web.state.assert_awaited_once_with()
        method.assert_not_awaited()
    else:
        method.assert_awaited_once_with(lease_token)


def test_knowledge_long_press_returns_saved_entry_id():
    runtime = FakeRuntime()
    runtime.knowledge_web.long_press.return_value = knowledge_state(
        mode_state=KnowledgeModeState.INACTIVE,
        control_state=KnowledgeControlState.AVAILABLE,
        lease_expires_at=None,
        last_entry_id="saved-entry",
    )

    with TestClient(create_app(runtime)) as client:
        response = client.post(
            "/api/device/knowledge/long-press",
            headers=knowledge_headers("lease-token"),
        )

    assert response.status_code == 200
    assert response.json()["last_entry_id"] == "saved-entry"
    assert response.json()["control_state"] == "available"
    runtime.knowledge_web.long_press.assert_awaited_once_with("lease-token")


def test_knowledge_entry_returns_sync_snapshot_without_requiring_lease():
    runtime = FakeRuntime()
    runtime.knowledge_web.entry.return_value = KnowledgeEntrySnapshot(
        entry_id="saved-entry",
        sync_state=KnowledgeSyncState.SYNCED,
        attempts=2,
        last_error=None,
        next_attempt_at=1200.0,
        updated_at=1300.0,
    )

    with TestClient(create_app(runtime)) as client:
        response = client.get(
            "/api/device/knowledge/entries/saved-entry",
            headers=authorized_headers(),
        )

    assert response.status_code == 200
    assert response.json() == {
        "entry_id": "saved-entry",
        "sync_state": "synced",
        "attempts": 2,
        "last_error": None,
        "next_attempt_at": 1200.0,
        "updated_at": 1300.0,
    }
    runtime.knowledge_web.entry.assert_awaited_once_with("saved-entry")


def test_knowledge_controller_error_uses_top_level_structured_payload():
    runtime = FakeRuntime()
    runtime.knowledge_web.short_press.side_effect = KnowledgeWebError(
        "asr_unavailable",
        "语音识别暂时不可用",
        503,
        knowledge_state(draft_text="不会丢失的草稿"),
    )

    with TestClient(create_app(runtime)) as client:
        response = client.post(
            "/api/device/knowledge/short-press",
            headers=knowledge_headers("lease-token"),
        )

    assert response.status_code == 503
    assert response.json()["code"] == "asr_unavailable"
    assert response.json()["detail"] == "语音识别暂时不可用"
    assert response.json()["knowledge_state"]["draft_text"] == "不会丢失的草稿"
    assert isinstance(response.json()["detail"], str)
