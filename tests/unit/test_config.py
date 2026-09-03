import pytest
from pathlib import Path
from pydantic import ValidationError

from showroom_guide.config import Settings


def make_settings(**overrides):
    values = {
        "xzkb_base_url": "http://xzkb.test",
        "xzkb_api_key": "test-key",
        "xzkb_empty_search_response": "请询问展厅相关内容。",
        "asr_base_url": "http://asr.test",
        "asr_api_key": "asr-test-key",
        "asr_model": "company-asr",
        "tts_base_url": "http://tts.test",
        "tts_api_key": "tts-test-key",
        "tts_model": "company-tts",
        "device_api_key": "device-test-key",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_servo_defaults_to_disabled():
    settings = make_settings()

    assert settings.servo_enabled is False
    assert settings.servo_pin == 18
    assert settings.servo_min_angle == 10.0
    assert settings.servo_max_angle == 140.0
    assert settings.servo_yes_angle == 20.0
    assert settings.servo_neutral_angle == 75.0
    assert settings.servo_no_angle == 130.0
    assert settings.servo_min_pulse_width_seconds == 0.0005
    assert settings.servo_max_pulse_width_seconds == 0.0025


@pytest.mark.parametrize(
    "overrides",
    [
        {"servo_min_angle": 140, "servo_max_angle": 10},
        {
            "servo_min_pulse_width_seconds": 0.0025,
            "servo_max_pulse_width_seconds": 0.0005,
        },
        {
            "servo_enabled": True,
            "gpio_button_enabled": True,
            "servo_pin": 17,
            "ptt_pin": 17,
        },
    ],
)
def test_invalid_servo_configuration_is_rejected(overrides):
    with pytest.raises(ValidationError):
        make_settings(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"servo_yes_angle": 75, "servo_neutral_angle": 75},
        {"servo_yes_angle": 80, "servo_neutral_angle": 60, "servo_no_angle": 70},
        {"servo_yes_angle": 5},
        {"servo_no_angle": 150},
    ],
)
def test_invalid_servo_verdict_positions_are_rejected(overrides):
    with pytest.raises(ValidationError):
        make_settings(**overrides)


def test_verdict_defaults_to_disabled():
    settings = make_settings()

    assert settings.verdict_enabled is False
    assert settings.verdict_base_url is None
    assert settings.verdict_api_key is None
    assert settings.verdict_timeout_seconds == 15.0


@pytest.mark.parametrize(
    "overrides",
    [
        {"verdict_enabled": True},
        {
            "verdict_enabled": True,
            "verdict_base_url": "https://kb.test",
        },
        {"verdict_enabled": True, "verdict_api_key": "verdict-secret"},
    ],
)
def test_verdict_enabled_requires_complete_application_credentials(overrides):
    with pytest.raises(ValidationError, match="判断应用"):
        make_settings(**overrides)


def test_verdict_application_does_not_require_model_name():
    settings = make_settings(
        verdict_enabled=True,
        verdict_base_url="https://kb.test/",
        verdict_api_key="verdict-secret",
    )

    assert settings.verdict_base_url == "https://kb.test"


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
    assert settings.asr_timeout_seconds == 8.0
    assert settings.tts_timeout_seconds == 12.0
    assert settings.playback_timeout_seconds == 300.0
    assert settings.knowledge_outbox_path == (
        Path.home()
        / ".local"
        / "share"
        / "showroom-guide"
        / "knowledge-outbox.sqlite3"
    )
    assert settings.session_idle_seconds == 1800.0
    assert settings.session_cleanup_seconds == 60.0
    assert settings.max_active_sessions == 100
    assert settings.xzkb_concurrency == 4
    assert settings.tts_concurrency == 2
    assert settings.queue_timeout_seconds == 120.0
    assert settings.xzkb_total_timeout_seconds == 120.0
    assert settings.audio_ttl_seconds == 600.0
    assert settings.audio_items_per_session == 3
    assert settings.audio_max_item_bytes == 8 * 1024 * 1024
    assert settings.audio_total_bytes == 256 * 1024 * 1024
    assert settings.device_api_key.get_secret_value() == "device-test-key"
    assert settings.device_max_upload_bytes == 10 * 1024 * 1024
    assert settings.faq_cache_enabled is True
    assert settings.faq_cache_file == Path("config/faq_cache.yaml")
    assert settings.faq_prepared_audio_enabled is True
    assert settings.faq_pending_audio_dir == (
        Path.home()
        / ".local"
        / "share"
        / "showroom-guide"
        / "prepared-audio"
        / "pending"
    )
    assert settings.local_recording_max_seconds == 60.0
    assert settings.local_recording_min_seconds == 0.5
    assert settings.local_recording_min_dbfs == -45.0
    assert settings.local_recording_max_bytes == 4 * 1024 * 1024
    assert settings.answer_max_chars == 220
    assert settings.knowledge_web_lease_seconds == 120.0


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
    monkeypatch.setenv("GUIDE_FAQ_CACHE_ENABLED", "false")
    monkeypatch.setenv("GUIDE_FAQ_CACHE_FILE", "custom/faq-cache.yaml")
    monkeypatch.setenv("GUIDE_FAQ_PREPARED_AUDIO_ENABLED", "false")
    monkeypatch.setenv("GUIDE_LOCAL_RECORDING_MIN_DBFS", "-50")
    monkeypatch.setenv("GUIDE_KNOWLEDGE_WEB_LEASE_SECONDS", "45.5")
    monkeypatch.setenv("GUIDE_ASR_TIMEOUT_SECONDS", "6.5")
    monkeypatch.setenv("GUIDE_TTS_TIMEOUT_SECONDS", "10.5")

    settings = Settings(_env_file=None)

    assert settings.max_active_sessions == 20
    assert settings.xzkb_concurrency == 3
    assert settings.tts_concurrency == 1
    assert settings.queue_timeout_seconds == 45.0
    assert settings.faq_cache_enabled is False
    assert settings.faq_cache_file == Path("custom/faq-cache.yaml")
    assert settings.faq_prepared_audio_enabled is False
    assert settings.local_recording_min_dbfs == -50.0
    assert settings.knowledge_web_lease_seconds == 45.5
    assert settings.asr_timeout_seconds == 6.5
    assert settings.tts_timeout_seconds == 10.5


@pytest.mark.parametrize("value", ["0", "-1"])
def test_settings_rejects_non_positive_knowledge_web_lease(monkeypatch, value):
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
    monkeypatch.setenv("GUIDE_KNOWLEDGE_WEB_LEASE_SECONDS", value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


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


def test_settings_rejects_recording_minimum_not_below_maximum():
    with pytest.raises(ValidationError, match="最短录音时长必须小于最长录音时长"):
        Settings(
            _env_file=None,
            xzkb_base_url="http://xzkb.test",
            xzkb_api_key="test-key",
            xzkb_empty_search_response="请询问展厅相关内容。",
            asr_base_url="http://asr.test",
            asr_api_key="asr-test-key",
            asr_model="company-asr",
            tts_base_url="http://tts.test",
            tts_api_key="tts-test-key",
            tts_model="company-tts",
            device_api_key="device-test-key",
            local_recording_min_seconds=60,
            local_recording_max_seconds=60,
        )


def test_knowledge_capture_requires_write_credentials():
    with pytest.raises(ValidationError, match="专用账号"):
        Settings(
            _env_file=None,
            xzkb_base_url="http://xzkb.test",
            xzkb_api_key="test-key",
            xzkb_empty_search_response="请询问展厅相关内容。",
            asr_base_url="http://asr.test",
            asr_api_key="asr-test-key",
            asr_model="company-asr",
            tts_base_url="http://tts.test",
            tts_api_key="tts-test-key",
            tts_model="company-tts",
            device_api_key="device-test-key",
            knowledge_capture_enabled=True,
        )


@pytest.mark.parametrize(
    "credentials",
    [
        {"xzkb_username": "showroom-writer"},
        {"xzkb_password": "dedicated-account-password"},
    ],
)
def test_knowledge_capture_requires_complete_dedicated_account(credentials):
    with pytest.raises(ValidationError, match="专用账号"):
        Settings(
            _env_file=None,
            xzkb_base_url="http://xzkb.test",
            xzkb_api_key="test-key",
            xzkb_empty_search_response="请询问展厅相关内容。",
            asr_base_url="http://asr.test",
            asr_api_key="asr-test-key",
            asr_model="company-asr",
            tts_base_url="http://tts.test",
            tts_api_key="tts-test-key",
            tts_model="company-tts",
            device_api_key="device-test-key",
            knowledge_capture_enabled=True,
            xzkb_knowledge_base_id="11111111-1111-1111-1111-111111111111",
            **credentials,
        )


def test_knowledge_capture_validation_does_not_expose_password():
    password = "LEAK-ME-NOW"

    with pytest.raises(ValidationError, match="专用账号") as exc:
        Settings(
            _env_file=None,
            xzkb_base_url="http://xzkb.test",
            xzkb_api_key="test-key",
            xzkb_empty_search_response="请询问展厅相关内容。",
            asr_base_url="http://asr.test",
            asr_api_key="asr-test-key",
            asr_model="company-asr",
            tts_base_url="http://tts.test",
            tts_api_key="tts-test-key",
            tts_model="company-tts",
            device_api_key="device-test-key",
            knowledge_capture_enabled=True,
            xzkb_username="showroom-writer",
            xzkb_password=password,
        )

    assert password not in str(exc.value)
    assert password not in repr(exc.value)
    assert password not in repr(exc.value.errors())
    assert password not in exc.value.json()


@pytest.mark.parametrize("username", [123, ["showroom-writer"]])
def test_settings_rejects_non_string_xzkb_username(username):
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            xzkb_base_url="http://xzkb.test",
            xzkb_api_key="test-key",
            xzkb_empty_search_response="请询问展厅相关内容。",
            asr_base_url="http://asr.test",
            asr_api_key="asr-test-key",
            asr_model="company-asr",
            tts_base_url="http://tts.test",
            tts_api_key="tts-test-key",
            tts_model="company-tts",
            device_api_key="device-test-key",
            xzkb_username=username,
        )


@pytest.mark.parametrize(
    "credentials",
    [
        {
            "xzkb_username": "",
            "xzkb_password": "dedicated-account-password",
        },
        {"xzkb_username": "showroom-writer", "xzkb_password": ""},
        {
            "xzkb_username": " \t ",
            "xzkb_password": "dedicated-account-password",
        },
        {"xzkb_username": "showroom-writer", "xzkb_password": " \t "},
    ],
)
def test_knowledge_capture_rejects_empty_dedicated_account(credentials):
    with pytest.raises(ValidationError, match="专用账号"):
        Settings(
            _env_file=None,
            xzkb_base_url="http://xzkb.test",
            xzkb_api_key="test-key",
            xzkb_empty_search_response="请询问展厅相关内容。",
            asr_base_url="http://asr.test",
            asr_api_key="asr-test-key",
            asr_model="company-asr",
            tts_base_url="http://tts.test",
            tts_api_key="tts-test-key",
            tts_model="company-tts",
            device_api_key="device-test-key",
            knowledge_capture_enabled=True,
            xzkb_knowledge_base_id="11111111-1111-1111-1111-111111111111",
            **credentials,
        )


def test_knowledge_capture_accepts_complete_configuration(tmp_path):
    settings = Settings(
        _env_file=None,
        xzkb_base_url="http://xzkb.test",
        xzkb_api_key="test-key",
        xzkb_empty_search_response="请询问展厅相关内容。",
        asr_base_url="http://asr.test",
        asr_api_key="asr-test-key",
        asr_model="company-asr",
        tts_base_url="http://tts.test",
        tts_api_key="tts-test-key",
        tts_model="company-tts",
        device_api_key="device-test-key",
        knowledge_capture_enabled=True,
        xzkb_username=" showroom-writer ",
        xzkb_password=" dedicated-account-password ",
        xzkb_knowledge_base_id="11111111-1111-1111-1111-111111111111",
        knowledge_outbox_path=tmp_path / "knowledge.sqlite3",
        gpio_button_enabled=True,
    )

    assert settings.knowledge_capture_enabled is True
    assert settings.gpio_button_enabled is True
    assert settings.button_hold_seconds == 1.5
    assert settings.xzkb_username == "showroom-writer"
    assert (
        settings.xzkb_password.get_secret_value()
        == " dedicated-account-password "
    )
    assert "dedicated-account-password" not in repr(settings)


def test_knowledge_outbox_path_expands_user_home():
    settings = Settings(
        _env_file=None,
        xzkb_base_url="http://xzkb.test",
        xzkb_api_key="test-key",
        xzkb_empty_search_response="请询问展厅相关内容。",
        asr_base_url="http://asr.test",
        asr_api_key="asr-test-key",
        asr_model="company-asr",
        tts_base_url="http://tts.test",
        tts_api_key="tts-test-key",
        tts_model="company-tts",
        device_api_key="device-test-key",
        knowledge_outbox_path="~/.local/share/custom.sqlite3",
    )

    assert settings.knowledge_outbox_path == (
        Path.home() / ".local" / "share" / "custom.sqlite3"
    )


def test_example_environment_is_valid_with_optional_features_disabled():
    root = Path(__file__).parents[2]

    settings = Settings(_env_file=root / ".env.example")

    assert settings.knowledge_capture_enabled is False
    assert settings.gpio_button_enabled is False
    assert settings.knowledge_web_lease_seconds == 120.0


@pytest.mark.parametrize(
    ("env_value", "requires_account"),
    [("true", True), ("false", False)],
)
def test_knowledge_capture_parses_boolean_environment(
    monkeypatch, env_value, requires_account
):
    monkeypatch.setenv("GUIDE_KNOWLEDGE_CAPTURE_ENABLED", env_value)
    settings_kwargs = {
        "_env_file": None,
        "xzkb_base_url": "http://xzkb.test",
        "xzkb_api_key": "test-key",
        "xzkb_empty_search_response": "请询问展厅相关内容。",
        "asr_base_url": "http://asr.test",
        "asr_api_key": "asr-test-key",
        "asr_model": "company-asr",
        "tts_base_url": "http://tts.test",
        "tts_api_key": "tts-test-key",
        "tts_model": "company-tts",
        "device_api_key": "device-test-key",
    }

    if requires_account:
        with pytest.raises(ValidationError, match="专用账号"):
            Settings(**settings_kwargs)
    else:
        settings = Settings(**settings_kwargs)
        assert settings.knowledge_capture_enabled is False
