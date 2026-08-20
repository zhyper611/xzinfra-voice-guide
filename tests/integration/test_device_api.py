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
