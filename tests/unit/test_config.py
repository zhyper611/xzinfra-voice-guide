import pytest
from pydantic import ValidationError

from showroom_guide.config import Settings


def test_settings_requires_service_credentials(monkeypatch):
    for name in (
        "GUIDE_XZKB_BASE_URL",
        "GUIDE_XZKB_API_KEY",
        "GUIDE_XZKB_EMPTY_SEARCH_RESPONSE",
        "GUIDE_ASR_BASE_URL",
        "GUIDE_ASR_API_KEY",
        "GUIDE_ASR_MODEL",
        "GUIDE_TTS_BASE_URL",
        "GUIDE_TTS_API_KEY",
        "GUIDE_TTS_MODEL",
        "GUIDE_DEVICE_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_normalizes_base_urls(monkeypatch):
    monkeypatch.setenv("GUIDE_XZKB_BASE_URL", "http://xzkb.test/")
    monkeypatch.setenv("GUIDE_XZKB_API_KEY", "test-key")
    monkeypatch.setenv("GUIDE_XZKB_EMPTY_SEARCH_RESPONSE", "请询问展厅相关内容。")
    monkeypatch.setenv("GUIDE_ASR_BASE_URL", "http://asr.test/")
    monkeypatch.setenv("GUIDE_ASR_API_KEY", "asr-test-key")
    monkeypatch.setenv("GUIDE_ASR_MODEL", "company-asr")
    monkeypatch.setenv("GUIDE_TTS_BASE_URL", "http://tts.test/")
    monkeypatch.setenv("GUIDE_TTS_API_KEY", "tts-test-key")
    monkeypatch.setenv("GUIDE_TTS_MODEL", "company-tts")
    monkeypatch.setenv("GUIDE_DEVICE_API_KEY", "device-test-key")

    settings = Settings(_env_file=None)

    assert settings.xzkb_base_url == "http://xzkb.test"
    assert settings.xzkb_empty_search_response == "请询问展厅相关内容。"
    assert settings.asr_base_url == "http://asr.test"
    assert settings.tts_base_url == "http://tts.test"
    assert settings.ptt_pin == 17
    assert settings.first_audio_timeout_seconds == 5.0
    assert settings.session_idle_seconds == 1800.0
    assert settings.session_cleanup_seconds == 60.0
    assert settings.max_active_sessions == 100
    assert settings.xzkb_concurrency == 4
    assert settings.tts_concurrency == 2
    assert settings.queue_timeout_seconds == 120.0
    assert settings.audio_ttl_seconds == 600.0
    assert settings.audio_items_per_session == 3
    assert settings.device_api_key.get_secret_value() == "device-test-key"
    assert settings.device_max_upload_bytes == 10 * 1024 * 1024


def test_settings_reads_multi_user_overrides(monkeypatch):
    monkeypatch.setenv("GUIDE_XZKB_BASE_URL", "http://xzkb.test")
    monkeypatch.setenv("GUIDE_XZKB_API_KEY", "test-key")
    monkeypatch.setenv("GUIDE_XZKB_EMPTY_SEARCH_RESPONSE", "请询问展厅相关内容。")
    monkeypatch.setenv("GUIDE_ASR_BASE_URL", "http://asr.test")
    monkeypatch.setenv("GUIDE_ASR_API_KEY", "asr-test-key")
    monkeypatch.setenv("GUIDE_ASR_MODEL", "company-asr")
    monkeypatch.setenv("GUIDE_TTS_BASE_URL", "http://tts.test")
    monkeypatch.setenv("GUIDE_TTS_API_KEY", "tts-test-key")
    monkeypatch.setenv("GUIDE_TTS_MODEL", "company-tts")
    monkeypatch.setenv("GUIDE_DEVICE_API_KEY", "device-test-key")
    monkeypatch.setenv("GUIDE_MAX_ACTIVE_SESSIONS", "20")
    monkeypatch.setenv("GUIDE_XZKB_CONCURRENCY", "3")
    monkeypatch.setenv("GUIDE_TTS_CONCURRENCY", "1")
    monkeypatch.setenv("GUIDE_QUEUE_TIMEOUT_SECONDS", "45")

    settings = Settings(_env_file=None)

    assert settings.max_active_sessions == 20
    assert settings.xzkb_concurrency == 3
    assert settings.tts_concurrency == 1
    assert settings.queue_timeout_seconds == 45.0


def test_settings_rejects_non_positive_device_upload_limit(monkeypatch):
    monkeypatch.setenv("GUIDE_XZKB_BASE_URL", "http://xzkb.test")
    monkeypatch.setenv("GUIDE_XZKB_API_KEY", "test-key")
    monkeypatch.setenv("GUIDE_XZKB_EMPTY_SEARCH_RESPONSE", "请询问展厅相关内容。")
    monkeypatch.setenv("GUIDE_ASR_BASE_URL", "http://asr.test")
    monkeypatch.setenv("GUIDE_ASR_API_KEY", "asr-test-key")
    monkeypatch.setenv("GUIDE_ASR_MODEL", "company-asr")
    monkeypatch.setenv("GUIDE_TTS_BASE_URL", "http://tts.test")
    monkeypatch.setenv("GUIDE_TTS_API_KEY", "tts-test-key")
    monkeypatch.setenv("GUIDE_TTS_MODEL", "company-tts")
    monkeypatch.setenv("GUIDE_DEVICE_API_KEY", "device-test-key")
    monkeypatch.setenv("GUIDE_DEVICE_MAX_UPLOAD_BYTES", "0")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_requires_device_api_key(monkeypatch):
    monkeypatch.setenv("GUIDE_XZKB_BASE_URL", "http://xzkb.test")
    monkeypatch.setenv("GUIDE_XZKB_API_KEY", "test-key")
    monkeypatch.setenv("GUIDE_XZKB_EMPTY_SEARCH_RESPONSE", "请询问展厅相关内容。")
    monkeypatch.setenv("GUIDE_ASR_BASE_URL", "http://asr.test")
    monkeypatch.setenv("GUIDE_ASR_API_KEY", "asr-test-key")
    monkeypatch.setenv("GUIDE_ASR_MODEL", "company-asr")
    monkeypatch.setenv("GUIDE_TTS_BASE_URL", "http://tts.test")
    monkeypatch.setenv("GUIDE_TTS_API_KEY", "tts-test-key")
    monkeypatch.setenv("GUIDE_TTS_MODEL", "company-tts")
    monkeypatch.delenv("GUIDE_DEVICE_API_KEY", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_requires_xzkb_empty_search_response(monkeypatch):
    monkeypatch.setenv("GUIDE_XZKB_BASE_URL", "http://xzkb.test")
    monkeypatch.setenv("GUIDE_XZKB_API_KEY", "test-key")
    monkeypatch.delenv("GUIDE_XZKB_EMPTY_SEARCH_RESPONSE", raising=False)
    monkeypatch.setenv("GUIDE_ASR_BASE_URL", "http://asr.test")
    monkeypatch.setenv("GUIDE_ASR_API_KEY", "asr-test-key")
    monkeypatch.setenv("GUIDE_ASR_MODEL", "company-asr")
    monkeypatch.setenv("GUIDE_TTS_BASE_URL", "http://tts.test")
    monkeypatch.setenv("GUIDE_TTS_API_KEY", "tts-test-key")
    monkeypatch.setenv("GUIDE_TTS_MODEL", "company-tts")
    monkeypatch.setenv("GUIDE_DEVICE_API_KEY", "device-test-key")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)
