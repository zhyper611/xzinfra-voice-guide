from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GUIDE_",
        env_file="/etc/showroom-guide/showroom-guide.env",
        extra="ignore",
    )

    xzkb_base_url: str = Field(min_length=1)
    xzkb_api_key: SecretStr = Field(min_length=8)
    xzkb_empty_search_response: str = Field(min_length=1)
    faq_cache_enabled: bool = True
    faq_cache_file: Path = Path("config/faq_cache.yaml")
    asr_base_url: str = Field(min_length=1)
    asr_api_key: SecretStr = Field(min_length=8)
    asr_model: str = Field(min_length=1)
    tts_base_url: str = Field(min_length=1)
    tts_api_key: SecretStr = Field(min_length=8)
    tts_model: str = Field(min_length=1)
    device_api_key: SecretStr = Field(min_length=8)
    device_max_upload_bytes: int = Field(default=10 * 1024 * 1024, gt=0)
    tts_voice: str = "alloy"
    tts_speed: float = Field(default=1.0, ge=0.25, le=4.0)
    request_timeout_seconds: float = Field(default=30.0, gt=0)
    first_audio_timeout_seconds: float = Field(default=5.0, gt=0)
    session_idle_seconds: float = Field(default=1800.0, gt=0)
    session_cleanup_seconds: float = Field(default=60.0, gt=0)
    max_active_sessions: int = Field(default=100, gt=0)
    xzkb_concurrency: int = Field(default=4, gt=0)
    tts_concurrency: int = Field(default=2, gt=0)
    queue_timeout_seconds: float = Field(default=120.0, gt=0)
    audio_ttl_seconds: float = Field(default=600.0, gt=0)
    audio_items_per_session: int = Field(default=3, gt=0)
    capture_device: str = "default"
    playback_device: str = "default"
    sample_rate: int = Field(default=16000, gt=0)
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
