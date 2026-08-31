from pathlib import Path
from typing import Any, Self
from uuid import UUID

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GUIDE_",
        env_file="/etc/showroom-guide/showroom-guide.env",
        extra="ignore",
        hide_input_in_errors=True,
    )

    xzkb_base_url: str = Field(min_length=1)
    xzkb_api_key: SecretStr = Field(min_length=8)
    xzkb_empty_search_response: str = Field(min_length=1)
    faq_cache_enabled: bool = True
    faq_cache_file: Path = Path("config/faq_cache.yaml")
    faq_prepared_audio_enabled: bool = True
    faq_admin_enabled: bool = False
    faq_admin_api_key: SecretStr | None = None
    faq_pending_audio_dir: Path = (
        Path.home()
        / ".local"
        / "share"
        / "showroom-guide"
        / "prepared-audio"
        / "pending"
    )
    asr_base_url: str = Field(min_length=1)
    asr_api_key: SecretStr = Field(min_length=8)
    asr_model: str = Field(min_length=1)
    tts_base_url: str = Field(min_length=1)
    tts_api_key: SecretStr = Field(min_length=8)
    tts_model: str = Field(min_length=1)
    device_api_key: SecretStr = Field(min_length=8)
    device_max_upload_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    local_recording_max_seconds: float = Field(default=60.0, gt=0)
    local_recording_max_bytes: int = Field(default=4 * 1024 * 1024, gt=0)
    local_recording_min_seconds: float = Field(default=0.5, gt=0)
    local_recording_min_dbfs: float = Field(default=-45.0, ge=-96.0, lt=0)
    tts_voice: str = "alloy"
    tts_speed: float = Field(default=1.0, ge=0.25, le=4.0)
    request_timeout_seconds: float = Field(default=30.0, gt=0)
    xzkb_total_timeout_seconds: float = Field(default=120.0, gt=0)
    asr_timeout_seconds: float = Field(default=8.0, gt=0)
    tts_timeout_seconds: float = Field(default=12.0, gt=0)
    first_audio_timeout_seconds: float = Field(default=5.0, gt=0)
    playback_timeout_seconds: float = Field(default=300.0, gt=0)
    session_idle_seconds: float = Field(default=1800.0, gt=0)
    session_cleanup_seconds: float = Field(default=60.0, gt=0)
    max_active_sessions: int = Field(default=100, gt=0)
    xzkb_concurrency: int = Field(default=4, gt=0)
    tts_concurrency: int = Field(default=2, gt=0)
    queue_timeout_seconds: float = Field(default=120.0, gt=0)
    answer_max_chars: int = Field(default=220, ge=40)
    audio_ttl_seconds: float = Field(default=600.0, gt=0)
    audio_items_per_session: int = Field(default=3, gt=0)
    audio_max_item_bytes: int = Field(default=8 * 1024 * 1024, gt=0)
    audio_total_bytes: int = Field(default=256 * 1024 * 1024, gt=0)
    capture_device: str = "default"
    playback_device: str = "default"
    sample_rate: int = Field(default=16000, gt=0)
    gpio_button_enabled: bool = False
    button_hold_seconds: float = Field(default=1.5, gt=0)
    knowledge_capture_enabled: bool = False
    xzkb_username: str | None = Field(default=None, min_length=1)
    xzkb_password: SecretStr | None = None
    xzkb_knowledge_base_id: UUID | None = None
    xzkb_knowledge_folder_id: UUID | None = None
    knowledge_outbox_path: Path = (
        Path.home()
        / ".local"
        / "share"
        / "showroom-guide"
        / "knowledge-outbox.sqlite3"
    )
    knowledge_sync_interval_seconds: float = Field(default=30.0, gt=0)
    knowledge_web_lease_seconds: float = Field(default=120.0, gt=0)
    ptt_pin: int = 17
    stop_pin: int = 27
    volume_up_pin: int = 22
    volume_down_pin: int = 23
    scripts_file: Path = Path("/var/lib/showroom-guide/scripts.yaml")
    audio_cache_dir: Path = Path("/var/lib/showroom-guide/audio")
    runtime_dir: Path = Path("/run/showroom-guide")

    @field_validator("xzkb_base_url", "asr_base_url", "tts_base_url")
    @classmethod
    def normalize_base_url(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("xzkb_empty_search_response")
    @classmethod
    def normalize_empty_search_response(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("XZKB 空回复不能为空")
        return normalized

    @field_validator("xzkb_username", mode="before")
    @classmethod
    def normalize_xzkb_username(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if not normalized:
            raise ValueError("XZKB 专用账号用户名不能为空")
        return normalized

    @field_validator(
        "knowledge_outbox_path",
        "faq_pending_audio_dir",
        mode="before",
    )
    @classmethod
    def expand_user_path(cls, value: str | Path) -> Path:
        return Path(value).expanduser()

    @model_validator(mode="before")
    @classmethod
    def protect_xzkb_password(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values

        password = values.get("xzkb_password")
        if isinstance(password, str):
            values["xzkb_password"] = SecretStr(password)
        return values

    @model_validator(mode="after")
    def validate_local_recording_limits(self) -> Self:
        if self.local_recording_min_seconds >= self.local_recording_max_seconds:
            raise ValueError("最短录音时长必须小于最长录音时长")
        return self

    @model_validator(mode="after")
    def validate_audio_cache_limits(self) -> Self:
        if self.audio_max_item_bytes > self.audio_total_bytes:
            raise ValueError("单条音频上限不能大于音频缓存总上限")
        return self

    @model_validator(mode="after")
    def validate_knowledge_capture_credentials(self) -> Self:
        password = self.xzkb_password
        if self.knowledge_capture_enabled and (
            self.xzkb_username is None
            or password is None
            or not password.get_secret_value().strip()
            or self.xzkb_knowledge_base_id is None
        ):
            raise ValueError(
                "启用知识补充时必须配置 XZKB 专用账号和知识库 ID"
            )
        return self

    @model_validator(mode="after")
    def validate_faq_admin_credentials(self) -> Self:
        if self.faq_admin_enabled:
            key = self.faq_admin_api_key
            if key is None or len(key.get_secret_value()) < 16:
                raise ValueError(
                    "faq_admin_api_key must contain at least 16 characters "
                    "when faq_admin_enabled is true"
                )
        return self
