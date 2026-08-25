"""Standalone three-scenario latency benchmark for the device voice chain.

This module deliberately lives outside the production request path.  It starts
the configured application in a child process, injects scenario flags through
that child's environment, and records only timing/routing metadata.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import io
import json
import os
import platform
import re
import socket
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType
from typing import Protocol, Self

import httpx
from pydantic import SecretStr, ValidationError

from showroom_guide.config import Settings
from showroom_guide.device import validate_wav as validate_device_wav
from showroom_guide.faq_audio import (
    WavMetadata,
    speech_client_from_settings,
    validate_wav as validate_generic_wav,
)
from showroom_guide.faq_cache import load_cache
from showroom_guide.latency import METRIC_NAMES as SERVER_METRIC_NAMES
from showroom_guide.latency import nearest_rank


QUESTION = "请介绍一下八大车间。"
TARGET_CACHE_ENTRY_ID = "eight_workshops_overview"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_AUDIO = Path("benchmark-fixtures/eight-workshops-question.wav")
DEFAULT_OUTPUT_DIR = Path("benchmark-results")
STANDARD_AUDIO_QUESTIONS: tuple[tuple[Path, str], ...] = (
    (
        Path("benchmark-fixtures/eight-workshops/01-introduce.wav"),
        "请介绍一下八大车间。",
    ),
    (
        Path("benchmark-fixtures/eight-workshops/02-eight-workshops.wav"),
        "给我讲讲八个车间。",
    ),
    (
        Path("benchmark-fixtures/eight-workshops/03-ai-factory.wav"),
        "AI 工厂都有哪些车间？",
    ),
    (
        Path("benchmark-fixtures/eight-workshops/04-roles.wav"),
        "说说八大车间分别做什么。",
    ),
    (
        Path("benchmark-fixtures/eight-workshops/05-factory-workshops.wav"),
        "请讲一讲 AI 工厂的车间。",
    ),
)
SCENARIO_NAMES = (
    "xzkb_online_tts",
    "faq_online_tts",
    "prepared_audio",
)
BENCHMARK_METRIC_NAMES = ("client_roundtrip_ms", *SERVER_METRIC_NAMES)
VALID_OUTCOMES = frozenset({"success", "degraded"})
BENCHMARK_MODES = ("latency", "reliability")
EXECUTION_ORDERS = ("sequential", "interleaved")
RECOVERABLE_HTTP_ERRORS = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.RemoteProtocolError,
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[^\s,;)]*")


class BenchmarkError(RuntimeError):
    """A safe, user-facing benchmark failure."""


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Remove credentials from diagnostics without retaining response bodies."""

    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "<redacted>")
    redacted = _BEARER_RE.sub("Bearer <redacted>", redacted)
    return redacted


def safe_exception_type(error: BaseException) -> str:
    """Return an exception class name, never its potentially sensitive text."""

    return type(error).__name__


@dataclass(frozen=True)
class Scenario:
    name: str
    faq_cache_enabled: bool
    prepared_audio_enabled: bool
    expected_served_from: str

    @property
    def env_overrides(self) -> dict[str, str]:
        return {
            "GUIDE_FAQ_CACHE_ENABLED": str(self.faq_cache_enabled).lower(),
            "GUIDE_FAQ_PREPARED_AUDIO_ENABLED": str(
                self.prepared_audio_enabled
            ).lower(),
        }

    @property
    def expected_cache_hit(self) -> bool:
        return self.faq_cache_enabled

    @property
    def expected_cache_entry_id(self) -> str | None:
        return TARGET_CACHE_ENTRY_ID if self.faq_cache_enabled else None


SCENARIOS: dict[str, Scenario] = {
    "xzkb_online_tts": Scenario("xzkb_online_tts", False, False, "xzkb_online_tts"),
    "faq_online_tts": Scenario("faq_online_tts", True, False, "faq_online_tts"),
    "prepared_audio": Scenario("prepared_audio", True, True, "prepared_audio"),
}


@dataclass(frozen=True)
class AudioInfo:
    file_name: str
    sha256: str
    metadata: WavMetadata


@dataclass(frozen=True)
class BenchmarkAudio:
    content: bytes
    info: AudioInfo | None = None
    transcript: str | None = None


@dataclass
class BenchmarkSample:
    scenario: str
    run_number: int
    warmup: bool
    audio_file_name: str | None = None
    audio_sha256: str | None = None
    status_code: int | None = None
    metrics_status_code: int | None = None
    playback_status_code: int | None = None
    turn_request_sent: bool = False
    turn_response_received: bool = False
    # Backward-compatible alias for the HTTP response state.
    request_completed: bool = False
    previous_turn_id: str | None = None
    turn_id: str | None = None
    outcome: str | None = None
    cache_hit: bool | None = None
    cache_entry_id: str | None = None
    served_from: str | None = None
    valid: bool = False
    stale_metrics: bool = False
    restart_required: bool = False
    invalid_reason: str | None = None
    failure_stage: str | None = None
    error_type: str | None = None
    client_roundtrip_ms: float | None = None
    asr_ms: float | None = None
    xzkb_queue_ms: float | None = None
    xzkb_headers_ms: float | None = None
    xzkb_first_sse_ms: float | None = None
    xzkb_first_content_ms: float | None = None
    xzkb_ttft_ms: float | None = None
    xzkb_generation_ms: float | None = None
    xzkb_total_ms: float | None = None
    tts_queue_ms: float | None = None
    tts_synthesis_ms: float | None = None
    server_pipeline_total_ms: float | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "scenario": self.scenario,
            "run_number": self.run_number,
            "warmup": self.warmup,
            "audio_file_name": self.audio_file_name,
            "audio_sha256": self.audio_sha256,
            "status_code": self.status_code,
            "metrics_status_code": self.metrics_status_code,
            "playback_status_code": self.playback_status_code,
            "turn_request_sent": self.turn_request_sent,
            "turn_response_received": self.turn_response_received,
            "request_completed": self.request_completed,
            "previous_turn_id": self.previous_turn_id,
            "turn_id": self.turn_id,
            "outcome": self.outcome,
            "cache_hit": self.cache_hit,
            "cache_entry_id": self.cache_entry_id,
            "served_from": self.served_from,
            "valid": self.valid,
            "stale_metrics": self.stale_metrics,
            "restart_required": self.restart_required,
            "invalid_reason": self.invalid_reason,
            "failure_stage": self.failure_stage,
            "error_type": self.error_type,
            **{name: getattr(self, name) for name in BENCHMARK_METRIC_NAMES},
        }


class DeviceApiClient:
    """Small API client whose HTTP transport can be replaced in tests."""

    def __init__(
        self,
        base_url: str,
        device_api_key: str,
        *,
        client: httpx.AsyncClient | None = None,
        turn_timeout: float = 240.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"X-Device-Key": device_api_key}
        self._client = client or httpx.AsyncClient(timeout=turn_timeout)
        self._owns_client = client is None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def run_turn(
        self,
        audio: bytes,
        scenario: Scenario,
        *,
        run_number: int,
        warmup: bool,
    ) -> BenchmarkSample:
        sample = BenchmarkSample(
            scenario=scenario.name,
            run_number=run_number,
            warmup=warmup,
        )
        try:
            previous_status, previous_latest = await self._read_latest()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            sample.error_type = safe_exception_type(error)
            sample.invalid_reason = "previous_metrics_failed"
            sample.restart_required = True
            return sample
        if previous_status != 200:
            sample.invalid_reason = "previous_metrics_failed"
            sample.restart_required = True
            return sample
        sample.previous_turn_id = self._turn_id(previous_latest)

        started = time.perf_counter()
        try:
            sample.turn_request_sent = True
            response = await self._client.post(
                f"{self._base_url}/api/device/turn",
                headers=self._headers,
                files={"file": ("question.wav", audio, "audio/wav")},
            )
            sample.client_roundtrip_ms = round(
                (time.perf_counter() - started) * 1000,
                2,
            )
            sample.status_code = response.status_code
            sample.turn_response_received = True
            sample.request_completed = True
        except asyncio.CancelledError:
            raise
        except RECOVERABLE_HTTP_ERRORS as error:
            sample.client_roundtrip_ms = round(
                (time.perf_counter() - started) * 1000,
                2,
            )
            sample.error_type = safe_exception_type(error)
            sample.invalid_reason = "turn_request_failed"
            sample.restart_required = True
            return sample
        except Exception as error:
            sample.error_type = safe_exception_type(error)
            sample.invalid_reason = "turn_request_failed"
            sample.restart_required = True
            return sample

        if sample.status_code == 409:
            sample.error_type = "HTTP409Busy"
            sample.failure_stage = "device_busy"
            sample.invalid_reason = "turn_busy_409"
            sample.restart_required = True
            return sample
        if sample.status_code != 200:
            self._apply_safe_http_failure(sample, response)
            await self._capture_failed_turn_metrics(sample)
            sample.invalid_reason = "turn_http_status"
            return sample

        try:
            metrics_status, latest = await self._read_latest()
            sample.metrics_status_code = metrics_status
        except asyncio.CancelledError:
            raise
        except RECOVERABLE_HTTP_ERRORS as error:
            sample.error_type = safe_exception_type(error)
            sample.invalid_reason = "metrics_request_failed"
            sample.restart_required = True
            return sample
        except Exception as error:
            sample.error_type = safe_exception_type(error)
            sample.invalid_reason = "metrics_request_failed"
            sample.restart_required = True
            return sample
        if metrics_status != 200 or latest is None:
            sample.invalid_reason = "metrics_request_failed"
            sample.restart_required = True
            return sample

        self._apply_latest(sample, latest)
        sample.turn_id = self._turn_id(latest)
        if sample.turn_id is None or sample.turn_id == sample.previous_turn_id:
            sample.stale_metrics = True
            sample.invalid_reason = "stale_metrics"

        try:
            playback_response = await self._client.post(
                f"{self._base_url}/api/device/playback-finished",
                headers=self._headers,
            )
            sample.playback_status_code = playback_response.status_code
        except asyncio.CancelledError:
            raise
        except RECOVERABLE_HTTP_ERRORS as error:
            sample.error_type = safe_exception_type(error)
            sample.invalid_reason = "playback_request_failed"
            sample.restart_required = True
            return sample
        except Exception as error:
            sample.error_type = safe_exception_type(error)
            sample.invalid_reason = "playback_request_failed"
            sample.restart_required = True
            return sample
        if sample.playback_status_code != 204:
            sample.invalid_reason = "playback_request_failed"
            sample.restart_required = True
            return sample

        self._classify(sample, scenario)
        return sample

    async def _read_latest(self) -> tuple[int, dict[str, object] | None]:
        response = await self._client.get(
            f"{self._base_url}/api/device/metrics",
            headers=self._headers,
        )
        if response.status_code != 200:
            return response.status_code, None
        payload = response.json()
        candidate = payload.get("latest") if isinstance(payload, dict) else None
        return response.status_code, candidate if isinstance(candidate, dict) else None

    async def _capture_failed_turn_metrics(self, sample: BenchmarkSample) -> None:
        """Best-effort correlation for completed HTTP failures only."""

        try:
            metrics_status, latest = await self._read_latest()
        except asyncio.CancelledError:
            raise
        except Exception:
            return
        sample.metrics_status_code = metrics_status
        if metrics_status != 200 or latest is None:
            return
        latest_turn_id = self._turn_id(latest)
        if latest_turn_id is None or latest_turn_id == sample.previous_turn_id:
            sample.stale_metrics = True
            return
        sample.turn_id = latest_turn_id
        self._apply_latest(sample, latest)

    @staticmethod
    def _apply_safe_http_failure(
        sample: BenchmarkSample,
        response: httpx.Response,
    ) -> None:
        sample.error_type = f"HTTP{response.status_code}"
        sample.failure_stage = "unknown"
        try:
            payload = response.json()
        except (ValueError, TypeError):
            return
        detail = payload.get("detail") if isinstance(payload, dict) else None
        known_failures = {
            "语音识别暂时不可用，请稍后重试": (
                "asr",
                "DeviceTranscriptionUnavailable",
            ),
            "当前使用人数较多，请稍后重试": (
                "capacity",
                "GuideServiceUnavailable",
            ),
            "知识库暂时不可用，请稍后重试": (
                "xzkb",
                "GuideServiceUnavailable",
            ),
        }
        if isinstance(detail, str) and detail in known_failures:
            sample.failure_stage, sample.error_type = known_failures[detail]

    @staticmethod
    def _turn_id(latest: dict[str, object] | None) -> str | None:
        if latest is None:
            return None
        value = latest.get("turn_id")
        return value if isinstance(value, str) else None

    @staticmethod
    def _apply_latest(
        sample: BenchmarkSample,
        latest: dict[str, object] | None,
    ) -> None:
        if latest is None:
            return
        outcome = latest.get("outcome")
        sample.outcome = outcome if isinstance(outcome, str) else None
        cache_hit = latest.get("cache_hit")
        sample.cache_hit = cache_hit if isinstance(cache_hit, bool) else None
        cache_entry_id = latest.get("cache_entry_id")
        sample.cache_entry_id = (
            cache_entry_id if isinstance(cache_entry_id, str) else None
        )
        served_from = latest.get("served_from")
        sample.served_from = served_from if isinstance(served_from, str) else None
        failure_stage = latest.get("failure_stage")
        if isinstance(failure_stage, str):
            sample.failure_stage = failure_stage
        error_type = latest.get("error_type")
        if isinstance(error_type, str):
            sample.error_type = error_type
        for name in SERVER_METRIC_NAMES:
            value = latest.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                setattr(sample, name, float(value))

    @staticmethod
    def _classify(
        sample: BenchmarkSample,
        scenario: Scenario,
    ) -> None:
        if sample.status_code != 200:
            sample.valid = False
            sample.invalid_reason = "turn_http_status"
            return
        if sample.metrics_status_code != 200:
            sample.valid = False
            sample.invalid_reason = "metrics_request_failed"
            return
        if sample.playback_status_code != 204:
            sample.valid = False
            sample.invalid_reason = "playback_request_failed"
            return
        if sample.stale_metrics:
            sample.valid = False
            sample.invalid_reason = "stale_metrics"
            return
        if sample.served_from != scenario.expected_served_from:
            sample.valid = False
            sample.invalid_reason = "served_from_mismatch"
            return
        if sample.cache_hit != scenario.expected_cache_hit:
            sample.valid = False
            sample.invalid_reason = "cache_hit_mismatch"
            return
        if sample.cache_entry_id != scenario.expected_cache_entry_id:
            sample.valid = False
            sample.invalid_reason = "cache_entry_id_mismatch"
            return
        if sample.outcome not in VALID_OUTCOMES | {"error"}:
            sample.valid = False
            sample.invalid_reason = "outcome_missing_or_unknown"
            return
        if sample.outcome == "error":
            sample.valid = False
            sample.invalid_reason = "server_outcome_error"
            return
        sample.valid = True


class ProcessLike(Protocol):
    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


class SubprocessServer(AbstractAsyncContextManager[str]):
    """Start one isolated Uvicorn child and always reap it."""

    def __init__(
        self,
        *,
        project_root: Path,
        child_env: dict[str, str],
        startup_timeout: float,
        process_factory: Callable[..., ProcessLike] = subprocess.Popen,
        health_check: Callable[[str, float], Awaitable[None]] | None = None,
        log_dir: Path | None = None,
    ) -> None:
        self._project_root = project_root
        self._child_env = child_env
        self._startup_timeout = startup_timeout
        self._process_factory = process_factory
        self._health_check = health_check
        self._log_dir = log_dir
        self._process: ProcessLike | None = None
        self._url: str | None = None

    async def __aenter__(self) -> str:
        port = find_free_port()
        self._url = f"http://127.0.0.1:{port}"
        command = [
            sys.executable,
            "-m",
            "uvicorn",
            "showroom_guide.main:create_configured_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--app-dir",
            str(self._project_root / "src"),
        ]
        try:
            self._clean_stale_logs()
            self._process = self._process_factory(
                command,
                cwd=str(self._project_root),
                env=self._child_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            health_check = self._health_check or self._wait_until_healthy
            await health_check(self._url, self._startup_timeout)
            return self._url
        except BaseException:
            await self.stop()
            raise

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        cleanup = asyncio.create_task(self.stop())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            # A cancelled benchmark still gets a best-effort synchronous reap.
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                pass
            raise

    async def stop(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, 5.0)
            except (subprocess.TimeoutExpired, TimeoutError):
                process.kill()
        if process is not None and process.poll() is None:
            try:
                await asyncio.to_thread(process.wait, 5.0)
            except (subprocess.TimeoutExpired, TimeoutError):
                # There is no safe way to continue using this server.  Do
                # not retain the process object or leak it into the report.
                return

    def _clean_stale_logs(self) -> None:
        if self._log_dir is None:
            return
        self._log_dir.mkdir(parents=True, exist_ok=True)
        try:
            stale_logs = tuple(self._log_dir.glob("*.tmp.log"))
        except OSError:
            raise BenchmarkError("无法清理临时服务日志") from None
        for path in stale_logs:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                raise BenchmarkError("无法清理临时服务日志") from None

    async def _wait_until_healthy(self, url: str, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        async with httpx.AsyncClient(timeout=1.0) as client:
            while time.monotonic() < deadline:
                if self._process is not None and self._process.poll() is not None:
                    raise BenchmarkError("临时服务在启动期间退出")
                try:
                    response = await client.get(f"{url}/healthz")
                    if response.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.1)
        raise BenchmarkError("临时服务启动超时")


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def settings_to_child_env(settings: Settings, scenario: Scenario) -> dict[str, str]:
    """Materialize validated Settings into child env vars without logging keys."""

    child_env = os.environ.copy()
    for key in tuple(child_env):
        if key.startswith("GUIDE_"):
            child_env.pop(key, None)

    for field_name in Settings.model_fields:
        value = getattr(settings, field_name)
        if value is None:
            continue
        if isinstance(value, SecretStr):
            text_value = value.get_secret_value()
        elif isinstance(value, bool):
            text_value = str(value).lower()
        else:
            text_value = str(value)
        child_env[f"GUIDE_{field_name.upper()}"] = text_value

    child_env.update(scenario.env_overrides)
    # These features are not part of the benchmark and must not write data.
    child_env.update(
        {
            "GUIDE_FAQ_ADMIN_ENABLED": "false",
            "GUIDE_KNOWLEDGE_CAPTURE_ENABLED": "false",
            "GUIDE_GPIO_BUTTON_ENABLED": "false",
        }
    )
    source_path = str(PROJECT_ROOT / "src")
    current_pythonpath = child_env.get("PYTHONPATH")
    child_env["PYTHONPATH"] = (
        source_path
        if not current_pythonpath
        else source_path + os.pathsep + current_pythonpath
    )
    return child_env


def load_validated_settings(env_file: Path) -> Settings:
    try:
        return Settings(_env_file=str(env_file))
    except ValidationError:
        # Do not include pydantic's input_value in CLI diagnostics.
        raise BenchmarkError("无法安全加载 Settings/.env 配置") from None
    except Exception:
        raise BenchmarkError("无法读取 Settings/.env 配置") from None


def _safe_fixture_path(path: Path, project_root: Path) -> Path:
    resolved = path if path.is_absolute() else project_root / path
    resolved = resolved.resolve(strict=False)
    prepared_root = (project_root / "config" / "prepared_audio").resolve()
    if resolved == prepared_root or prepared_root in resolved.parents:
        raise BenchmarkError("测试音频不得写入正式 prepared_audio 目录")
    if resolved.name.lower() == "manifest.json":
        raise BenchmarkError("测试工具不得写入 manifest.json")
    return resolved


def _display_file_name(path: Path, project_root: Path) -> str:
    try:
        return path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return path.name


def _safe_output_dir(path: Path, project_root: Path) -> Path:
    resolved = path if path.is_absolute() else project_root / path
    resolved = resolved.resolve(strict=False)
    prepared_root = (project_root / "config" / "prepared_audio").resolve()
    if resolved == prepared_root or prepared_root in resolved.parents:
        raise BenchmarkError("测试结果不得写入正式 prepared_audio 目录")
    return resolved


def load_and_validate_audio(
    path: Path, project_root: Path = PROJECT_ROOT
) -> tuple[bytes, AudioInfo]:
    safe_path = _safe_fixture_path(path, project_root)
    try:
        content = safe_path.read_bytes()
    except OSError:
        raise BenchmarkError("无法读取测试 WAV") from None
    if not content:
        raise BenchmarkError("测试 WAV 为空")
    try:
        metadata = validate_generic_wav(content)
        validate_device_wav(content)
    except Exception:
        raise BenchmarkError(
            "测试 WAV 必须是非空、合法的单声道 16 kHz 16-bit PCM WAV"
        ) from None
    info = AudioInfo(
        file_name=_display_file_name(safe_path, project_root),
        sha256=hashlib.sha256(content).hexdigest(),
        metadata=metadata,
    )
    return content, info


async def ensure_question_audio(
    path: Path,
    settings: Settings,
    *,
    confirm_live_requests: bool,
    question: str = QUESTION,
    project_root: Path = PROJECT_ROOT,
) -> tuple[bytes, AudioInfo, bool]:
    safe_path = _safe_fixture_path(path, project_root)
    if safe_path.is_file():
        content, info = load_and_validate_audio(safe_path, project_root)
        return content, info, False
    if not confirm_live_requests:
        raise BenchmarkError(
            "测试音频不存在；生成音频需要显式参数 --confirm-live-requests"
        )

    speech = speech_client_from_settings(settings)
    try:
        try:
            content = await speech.synthesize(question)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise BenchmarkError("测试问题音频生成请求失败") from None
    finally:
        await speech.aclose()
    try:
        metadata = validate_generic_wav(content)
        validate_device_wav(content)
    except Exception:
        raise BenchmarkError("TTS 生成的测试音频未通过现有 WAV 校验") from None
    safe_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with safe_path.open("xb") as output:
            output.write(content)
    except FileExistsError:
        # Another invocation won the race.  Re-read it and verify it instead
        # of replacing an existing fixture.
        content, info = load_and_validate_audio(safe_path, project_root)
        return content, info, False
    except OSError:
        raise BenchmarkError("无法保存 benchmark-fixtures 测试音频") from None
    info = AudioInfo(
        file_name=_display_file_name(safe_path, project_root),
        sha256=hashlib.sha256(content).hexdigest(),
        metadata=metadata,
    )
    return content, info, True


async def verify_asr_and_cache(
    audio: bytes,
    settings: Settings,
) -> str:
    speech = speech_client_from_settings(settings)
    try:
        try:
            transcript = (await speech.transcribe(io.BytesIO(audio))).strip()
        except asyncio.CancelledError:
            raise
        except Exception:
            raise BenchmarkError("ASR 冒烟请求失败") from None
    finally:
        await speech.aclose()
    if not transcript:
        raise BenchmarkError("ASR 冒烟未识别出文本")
    try:
        matched = load_cache(settings.faq_cache_file).match(transcript)
    except Exception:
        raise BenchmarkError("无法校验 ASR 文本对应的 FAQ 缓存") from None
    if matched is None or matched.id != TARGET_CACHE_ENTRY_ID:
        raise BenchmarkError(f"ASR 未命中目标缓存；识别文本：{redact_text(transcript)}")
    return transcript


class ScenarioServerFactory(Protocol):
    def __call__(self, scenario: Scenario) -> AbstractAsyncContextManager[str]: ...


@dataclass
class ScenarioRunStats:
    scenario: str
    planned_warmup_requests: int = 0
    planned_formal_requests: int = 0
    timeout_count: int = 0
    restart_count: int = 0
    circuit_breaker_triggered: bool = False
    stale_metrics_count: int = 0
    sample_attempts: int = 0
    turn_requests_sent: int = 0
    turn_responses_received: int = 0
    successful_voice_turns: int = 0
    warmup_attempts: int = 0
    formal_attempts: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "planned_warmup_requests": self.planned_warmup_requests,
            "planned_formal_requests": self.planned_formal_requests,
            "planned_requests": (
                self.planned_warmup_requests + self.planned_formal_requests
            ),
            "timeout_count": self.timeout_count,
            "restart_count": self.restart_count,
            "circuit_breaker_triggered": self.circuit_breaker_triggered,
            "stale_metrics_count": self.stale_metrics_count,
            "sample_attempts": self.sample_attempts,
            "turn_requests_sent": self.turn_requests_sent,
            "turn_responses_received": self.turn_responses_received,
            "successful_voice_turns": self.successful_voice_turns,
            "warmup_attempts": self.warmup_attempts,
            "formal_attempts": self.formal_attempts,
        }


@dataclass
class _ScenarioSession:
    server_context: AbstractAsyncContextManager[str]
    client_context: DeviceApiClient
    client: DeviceApiClient
    closed: bool = False

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True

        async def cleanup() -> None:
            try:
                await self.client_context.__aexit__(None, None, None)
            finally:
                await self.server_context.__aexit__(None, None, None)

        task = asyncio.create_task(cleanup())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                pass
            raise


class BenchmarkRunner:
    def __init__(
        self,
        *,
        server_factory: ScenarioServerFactory,
        client_factory: Callable[[str], DeviceApiClient],
        max_consecutive_errors: int = 3,
        interval_seconds: float = 1.0,
        scenario_cooldown_seconds: float = 10.0,
        progress: Callable[[str], None] | None = None,
        benchmark_mode: str = "latency",
        execution_order: str = "sequential",
    ) -> None:
        if benchmark_mode not in BENCHMARK_MODES:
            raise ValueError("unknown benchmark mode")
        if execution_order not in EXECUTION_ORDERS:
            raise ValueError("unknown execution order")
        self._server_factory = server_factory
        self._client_factory = client_factory
        self._max_consecutive_errors = max_consecutive_errors
        self._interval_seconds = interval_seconds
        self._scenario_cooldown_seconds = scenario_cooldown_seconds
        self._progress = progress or (lambda _message: None)
        self._benchmark_mode = benchmark_mode
        self._execution_order = execution_order
        self.run_stats: dict[str, ScenarioRunStats] = {}

    async def run(
        self,
        audio: bytes | Sequence[BenchmarkAudio],
        scenarios: Sequence[Scenario],
        *,
        runs: int,
        warmup: int,
    ) -> list[BenchmarkSample]:
        fixtures = (
            (BenchmarkAudio(audio),) if isinstance(audio, bytes) else tuple(audio)
        )
        if not fixtures:
            raise ValueError("at least one benchmark audio is required")
        records: list[BenchmarkSample] = []
        self.run_stats = {}
        if self._execution_order == "interleaved":
            return await self._run_interleaved(
                fixtures,
                scenarios,
                runs=runs,
                warmup=warmup,
            )
        for scenario_index, scenario in enumerate(scenarios):
            records.extend(
                await self._run_scenario(
                    fixtures,
                    scenario,
                    runs=runs,
                    warmup=warmup,
                )
            )
            if scenario_index + 1 < len(scenarios):
                await asyncio.sleep(self._scenario_cooldown_seconds)
        return records

    async def _run_scenario(
        self,
        fixtures: Sequence[BenchmarkAudio],
        scenario: Scenario,
        *,
        runs: int,
        warmup: int,
    ) -> list[BenchmarkSample]:
        stats = ScenarioRunStats(
            scenario.name,
            planned_warmup_requests=warmup,
            planned_formal_requests=runs,
        )
        self.run_stats[scenario.name] = stats
        records: list[BenchmarkSample] = []
        session: _ScenarioSession | None = None
        started_once = False
        consecutive_errors = 0
        self._progress(
            f"[{scenario.name}] 场景开始：warmup {warmup} 次，正式 {runs} 次"
        )

        async def close_on_cancel() -> None:
            nonlocal session
            if session is None:
                return
            current = session
            session = None
            cleanup = asyncio.create_task(current.close())
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    pass
                raise

        async def close_for_restart() -> None:
            nonlocal session
            if session is None:
                return
            await session.close()
            session = None

        async def start_session() -> _ScenarioSession:
            nonlocal started_once
            server_context = self._server_factory(scenario)
            if started_once:
                stats.restart_count += 1
            started_once = True
            base_url = await server_context.__aenter__()
            try:
                client_context = self._client_factory(base_url)
                client = await client_context.__aenter__()
            except BaseException:
                await server_context.__aexit__(None, None, None)
                raise
            return _ScenarioSession(server_context, client_context, client)

        async def attempt(
            run_number: int,
            is_warmup: bool,
        ) -> BenchmarkSample:
            nonlocal session
            fixture = fixtures[(run_number - 1) % len(fixtures)]
            try:
                if session is None:
                    session = await start_session()
                sample = await session.client.run_turn(
                    fixture.content,
                    scenario,
                    run_number=run_number,
                    warmup=is_warmup,
                )
                self._apply_audio_info(sample, fixture)
                return sample
            except asyncio.CancelledError:
                await close_on_cancel()
                raise
            except Exception as error:
                sample = BenchmarkSample(
                    scenario=scenario.name,
                    run_number=run_number,
                    warmup=is_warmup,
                    error_type=safe_exception_type(error),
                    invalid_reason="server_startup_failed",
                    restart_required=True,
                )
                self._apply_audio_info(sample, fixture)
                return sample

        async def timed_attempt(
            run_number: int,
            is_warmup: bool,
            total: int,
        ) -> BenchmarkSample:
            phase = "warmup" if is_warmup else "正式"
            self._progress(f"[{scenario.name}] {phase} {run_number}/{total} 开始")
            started_at = time.perf_counter()
            sample = await attempt(run_number, is_warmup)
            elapsed_seconds = time.perf_counter() - started_at
            result = (
                sample.outcome
                if sample.valid
                else (sample.error_type or sample.invalid_reason or "invalid")
            )
            source = f"，served_from={sample.served_from}" if sample.served_from else ""
            self._progress(
                f"[{scenario.name}] {phase} {run_number}/{total} 完成："
                f"{result}，{elapsed_seconds:.2f} 秒{source}"
            )
            return sample

        def record(sample: BenchmarkSample, is_warmup: bool) -> None:
            stats.sample_attempts += 1
            if sample.turn_request_sent:
                stats.turn_requests_sent += 1
            if sample.turn_response_received:
                stats.turn_responses_received += 1
            if sample.valid and sample.outcome == "success":
                stats.successful_voice_turns += 1
            if sample.stale_metrics:
                stats.stale_metrics_count += 1
            if sample.error_type in {
                "TimeoutException",
                "ReadTimeout",
                "ConnectTimeout",
                "WriteTimeout",
                "PoolTimeout",
            }:
                stats.timeout_count += 1
            if is_warmup:
                stats.warmup_attempts += 1
            else:
                stats.formal_attempts += 1

        async def handle_sample(sample: BenchmarkSample) -> None:
            nonlocal session, consecutive_errors
            if sample.valid:
                consecutive_errors = 0
                return
            consecutive_errors += 1
            await close_for_restart()

        warmup_index = 1
        while warmup_index <= warmup:
            sample = await timed_attempt(warmup_index, True, warmup)
            records.append(sample)
            record(sample, True)
            if sample.valid:
                warmup_index += 1
                consecutive_errors = 0
            else:
                await handle_sample(sample)
                if self._benchmark_mode == "reliability":
                    warmup_index += 1
                elif consecutive_errors >= self._max_consecutive_errors:
                    stats.circuit_breaker_triggered = True
                    break
            try:
                await asyncio.sleep(self._interval_seconds)
            except asyncio.CancelledError:
                await close_on_cancel()
                raise

        if not stats.circuit_breaker_triggered:
            consecutive_errors = 0
            for run_number in range(1, runs + 1):
                sample = await timed_attempt(run_number, False, runs)
                records.append(sample)
                record(sample, False)
                await handle_sample(sample)
                if (
                    self._benchmark_mode == "latency"
                    and consecutive_errors >= self._max_consecutive_errors
                ):
                    stats.circuit_breaker_triggered = True
                    break
                try:
                    await asyncio.sleep(self._interval_seconds)
                except asyncio.CancelledError:
                    await close_on_cancel()
                    raise

        if session is not None:
            await session.close()
        formal_successes = sum(
            1
            for sample in records
            if not sample.warmup and sample.valid and sample.outcome == "success"
        )
        self._progress(
            f"[{scenario.name}] 场景完成：正式成功 "
            f"{formal_successes}/{stats.formal_attempts}，"
            f"重启 {stats.restart_count} 次，超时 {stats.timeout_count} 次"
        )
        return records

    async def _run_interleaved(
        self,
        fixtures: Sequence[BenchmarkAudio],
        scenarios: Sequence[Scenario],
        *,
        runs: int,
        warmup: int,
    ) -> list[BenchmarkSample]:
        """Run one request per scenario per round while keeping sessions alive."""

        @dataclass
        class State:
            scenario: Scenario
            stats: ScenarioRunStats
            records: list[BenchmarkSample]
            session: _ScenarioSession | None = None
            started_once: bool = False
            consecutive_errors: int = 0
            warmup_successes: int = 0

        states = [
            State(
                scenario=scenario,
                stats=ScenarioRunStats(
                    scenario.name,
                    planned_warmup_requests=warmup,
                    planned_formal_requests=runs,
                ),
                records=[],
            )
            for scenario in scenarios
        ]
        all_records: list[BenchmarkSample] = []
        self.run_stats = {state.scenario.name: state.stats for state in states}
        for state in states:
            self._progress(
                f"[{state.scenario.name}] 场景就绪：warmup {warmup} 次，"
                f"正式 {runs} 次（交错执行）"
            )

        async def close(state: State) -> None:
            if state.session is None:
                return
            current = state.session
            state.session = None
            await current.close()

        async def start_session(state: State) -> _ScenarioSession:
            server_context = self._server_factory(state.scenario)
            if state.started_once:
                state.stats.restart_count += 1
            state.started_once = True
            base_url = await server_context.__aenter__()
            try:
                client_context = self._client_factory(base_url)
                client = await client_context.__aenter__()
            except BaseException:
                await server_context.__aexit__(None, None, None)
                raise
            return _ScenarioSession(server_context, client_context, client)

        async def attempt(
            state: State,
            run_number: int,
            is_warmup: bool,
            total: int,
        ) -> BenchmarkSample:
            phase = "warmup" if is_warmup else "正式"
            fixture = fixtures[(run_number - 1) % len(fixtures)]
            self._progress(f"[{state.scenario.name}] {phase} {run_number}/{total} 开始")
            started_at = time.perf_counter()
            try:
                if state.session is None:
                    state.session = await start_session(state)
                sample = await state.session.client.run_turn(
                    fixture.content,
                    state.scenario,
                    run_number=run_number,
                    warmup=is_warmup,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                sample = BenchmarkSample(
                    scenario=state.scenario.name,
                    run_number=run_number,
                    warmup=is_warmup,
                    error_type=safe_exception_type(error),
                    invalid_reason="server_startup_failed",
                    restart_required=True,
                )
            self._apply_audio_info(sample, fixture)
            elapsed_seconds = time.perf_counter() - started_at
            result = (
                sample.outcome
                if sample.valid
                else (sample.error_type or sample.invalid_reason or "invalid")
            )
            source = f"，served_from={sample.served_from}" if sample.served_from else ""
            self._progress(
                f"[{state.scenario.name}] {phase} {run_number}/{total} 完成："
                f"{result}，{elapsed_seconds:.2f} 秒{source}"
            )
            return sample

        def record(state: State, sample: BenchmarkSample) -> None:
            stats = state.stats
            state.records.append(sample)
            all_records.append(sample)
            stats.sample_attempts += 1
            stats.warmup_attempts += int(sample.warmup)
            stats.formal_attempts += int(not sample.warmup)
            stats.turn_requests_sent += int(sample.turn_request_sent)
            stats.turn_responses_received += int(sample.turn_response_received)
            if sample.valid and sample.outcome == "success":
                stats.successful_voice_turns += 1
            stats.stale_metrics_count += int(sample.stale_metrics)
            if sample.error_type in {
                "TimeoutException",
                "ReadTimeout",
                "ConnectTimeout",
                "WriteTimeout",
                "PoolTimeout",
            }:
                stats.timeout_count += 1

        async def handle(state: State, sample: BenchmarkSample) -> None:
            if sample.valid:
                state.consecutive_errors = 0
                return
            state.consecutive_errors += 1
            await close(state)
            if (
                self._benchmark_mode == "latency"
                and state.consecutive_errors >= self._max_consecutive_errors
            ):
                state.stats.circuit_breaker_triggered = True

        async def pause() -> None:
            if self._interval_seconds:
                await asyncio.sleep(self._interval_seconds)

        try:
            if self._benchmark_mode == "reliability":
                for warmup_number in range(1, warmup + 1):
                    for state in states:
                        sample = await attempt(
                            state,
                            warmup_number,
                            True,
                            warmup,
                        )
                        record(state, sample)
                        await handle(state, sample)
                        await pause()
            else:
                while any(
                    state.warmup_successes < warmup
                    and not state.stats.circuit_breaker_triggered
                    for state in states
                ):
                    for state in states:
                        if (
                            state.warmup_successes >= warmup
                            or state.stats.circuit_breaker_triggered
                        ):
                            continue
                        sample = await attempt(
                            state,
                            state.warmup_successes + 1,
                            True,
                            warmup,
                        )
                        record(state, sample)
                        if sample.valid:
                            state.warmup_successes += 1
                        await handle(state, sample)
                        await pause()

            for run_number in range(1, runs + 1):
                for state in states:
                    if (
                        self._benchmark_mode == "latency"
                        and state.stats.circuit_breaker_triggered
                    ):
                        continue
                    sample = await attempt(state, run_number, False, runs)
                    record(state, sample)
                    await handle(state, sample)
                    await pause()
        finally:
            for state in states:
                await close(state)

        for state in states:
            formal_successes = sum(
                1
                for sample in state.records
                if not sample.warmup and sample.valid and sample.outcome == "success"
            )
            self._progress(
                f"[{state.scenario.name}] 场景完成：正式成功 "
                f"{formal_successes}/{state.stats.formal_attempts}，"
                f"重启 {state.stats.restart_count} 次，"
                f"超时 {state.stats.timeout_count} 次"
            )
        return all_records

    @staticmethod
    def _apply_audio_info(
        sample: BenchmarkSample,
        fixture: BenchmarkAudio,
    ) -> None:
        if fixture.info is None:
            return
        sample.audio_file_name = fixture.info.file_name
        sample.audio_sha256 = fixture.info.sha256


def formal_records(records: Iterable[BenchmarkSample]) -> list[BenchmarkSample]:
    return [record for record in records if not record.warmup]


def latency_values(records: Iterable[BenchmarkSample], metric: str) -> list[float]:
    return [
        float(value)
        for record in records
        if record.valid
        and record.outcome == "success"
        and (value := getattr(record, metric)) is not None
    ]


def percentile_pair(values: Sequence[float]) -> tuple[float | None, float | None]:
    return (
        nearest_rank(list(values), 50),
        nearest_rank(list(values), 95),
    )


def scenario_counts(records: Iterable[BenchmarkSample]) -> dict[str, int]:
    records = list(records)
    counts = Counter(record.outcome for record in records if record.valid)
    counts.update(
        {
            "invalid": sum(1 for record in records if not record.valid),
        }
    )
    return {
        "success": counts.get("success", 0),
        "degraded": counts.get("degraded", 0),
        "error": sum(
            1
            for record in records
            if not record.valid
            and record.invalid_reason != "served_from_mismatch"
            and record.invalid_reason != "cache_hit_mismatch"
            and record.invalid_reason != "cache_entry_id_mismatch"
            and record.invalid_reason != "outcome_missing_or_unknown"
            and record.invalid_reason != "stale_metrics"
        ),
        "invalid": sum(
            1
            for record in records
            if not record.valid
            and record.invalid_reason
            in {
                "served_from_mismatch",
                "cache_hit_mismatch",
                "cache_entry_id_mismatch",
                "outcome_missing_or_unknown",
                "stale_metrics",
            }
        ),
    }


def _fmt(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2f}"


def _ratio(baseline: float | None, candidate: float | None) -> str:
    if baseline is None or candidate is None or baseline <= 0:
        return "N/A"
    return f"{(baseline - candidate) / baseline * 100:.2f}%"


def _success_rate(successes: int, attempts: int) -> str:
    if attempts <= 0:
        return "N/A"
    return f"{successes / attempts * 100:.2f}%"


def generate_report(
    *,
    records: Sequence[BenchmarkSample],
    audio_info: AudioInfo,
    transcript: str,
    settings: Settings,
    scenarios: Sequence[Scenario],
    runs: int,
    warmup: int,
    startup_timeout: float,
    env_file_name: str = ".env",
    turn_timeout: float = 240.0,
    max_consecutive_errors: int = 3,
    interval_seconds: float = 1.0,
    scenario_cooldown_seconds: float = 10.0,
    run_stats: dict[str, ScenarioRunStats] | None = None,
    project_root: Path = PROJECT_ROOT,
    question_audio_generated: bool = False,
    audio_infos: Sequence[AudioInfo] | None = None,
    transcripts: Sequence[str] | None = None,
    question_audio_generated_count: int = 0,
    benchmark_mode: str = "latency",
    execution_order: str = "sequential",
) -> str:
    report_audio_infos = tuple(audio_infos or (audio_info,))
    report_transcripts = tuple(transcripts or (transcript,))
    if len(report_audio_infos) != len(report_transcripts):
        raise ValueError("audio info and transcript counts must match")
    generated_count = question_audio_generated_count or int(question_audio_generated)
    run_stats = run_stats or {}
    report_stats = {
        scenario.name: run_stats.get(
            scenario.name,
            ScenarioRunStats(
                scenario.name,
                planned_warmup_requests=warmup,
                planned_formal_requests=runs,
            ),
        )
        for scenario in scenarios
    }
    turn_requests_sent_by_scenario = {
        name: stats.turn_requests_sent for name, stats in report_stats.items()
    }
    formal_by_scenario = {
        scenario.name: [
            record
            for record in records
            if record.scenario == scenario.name and not record.warmup
        ]
        for scenario in scenarios
    }
    lines = [
        "# showroom-guide 三场景语音链路基准测试",
        "",
        f"生成时间（UTC）：{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
        "## 输入与 ASR 冒烟",
        "",
        f"- 输入音频数量：`{len(report_audio_infos)}`",
        f"- 目标缓存：`{TARGET_CACHE_ENTRY_ID}`（已命中）",
        (
            f"- 本次新生成测试音频：`{generated_count}`；"
            f"复用：`{len(report_audio_infos) - generated_count}`。"
        ),
        "- 测试音频准备与逐条 ASR 冒烟不计入任何链路耗时。",
        "",
        "| 序号 | 输入文件 | SHA-256 | WAV | ASR 冒烟识别文本 |",
        "| ---: | --- | --- | --- | --- |",
    ]
    for index, (info, recognized_text) in enumerate(
        zip(report_audio_infos, report_transcripts, strict=True),
        start=1,
    ):
        lines.append(
            f"| {index} | `{info.file_name}` | `{info.sha256}` | "
            f"{info.metadata.sample_rate} Hz / {info.metadata.channels} ch / "
            f"{info.metadata.sample_width * 8} bit / "
            f"{info.metadata.duration_seconds:.2f} s | {recognized_text} |"
        )
    lines.extend(
        [
            "",
            "测试采用同一组 AI 合成的标准化问题音频；每条音频按轮次进入三个场景进行配对比较，不代表真实环境下麦克风采集质量。",
            "",
            "## 样本与结果",
            "",
            "以下正式统计不包含 warmup；成功语音样本 P50/P95 只使用 routing metadata 校验通过且 outcome 为 success 的样本，degraded 仅单独计数。",
            "",
            "| 场景 | 正式请求 | 成功延迟样本 | 请求成功率 | 成功 | 降级 | 错误 | invalid |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for scenario in scenarios:
        formal = formal_by_scenario[scenario.name]
        counts = scenario_counts(formal)
        stats = report_stats[scenario.name]
        attempted_formal = (
            stats.formal_attempts if scenario.name in run_stats else len(formal)
        )
        success_latency = sum(
            1 for record in formal if record.valid and record.outcome == "success"
        )
        lines.append(
            f"| `{scenario.name}` | {attempted_formal} | {success_latency} | "
            f"{_success_rate(success_latency, attempted_formal)} | "
            f"{counts['success']} | {counts['degraded']} | "
            f"{counts['error']} | {counts['invalid']} |"
        )

    lines.extend(
        [
            "",
            "## 音频轮转覆盖",
            "",
            "| 输入文件 | 场景 | 正式尝试 | 成功 | 成功率 |",
            "| --- | --- | ---: | ---: | ---: |",
        ]
    )
    for info in report_audio_infos:
        for scenario in scenarios:
            matched_records = [
                record
                for record in formal_by_scenario[scenario.name]
                if record.audio_sha256 == info.sha256
            ]
            successes = sum(
                1
                for record in matched_records
                if record.valid and record.outcome == "success"
            )
            lines.append(
                f"| `{info.file_name}` | `{scenario.name}` | "
                f"{len(matched_records)} | {successes} | "
                f"{_success_rate(successes, len(matched_records))} |"
            )

    failure_counts = Counter(
        (
            record.scenario,
            "warmup" if record.warmup else "formal",
            record.status_code,
            record.failure_stage or "unknown",
            record.error_type or "unknown",
        )
        for record in records
        if not record.valid
    )
    lines.extend(
        [
            "",
            "## 失败分布",
            "",
            "失败信息仅包含状态码、阶段和错误类型，不保存响应正文。",
            "",
            "| 场景 | 阶段 | HTTP | failure_stage | error_type | 次数 |",
            "| --- | --- | ---: | --- | --- | ---: |",
        ]
    )
    if failure_counts:
        for key, count in sorted(
            failure_counts.items(),
            key=lambda item: tuple(str(value) for value in item[0]),
        ):
            scenario_name, phase, status_code, failure_stage, error_type = key
            lines.append(
                f"| `{scenario_name}` | {phase} | "
                f"{status_code if status_code is not None else 'N/A'} | "
                f"`{failure_stage}` | `{error_type}` | {count} |"
            )
    else:
        lines.append("| - | - | N/A | - | - | 0 |")

    lines.extend(
        [
            "",
            "## 恢复与完成情况",
            "",
            "| 场景 | timeout_count | restart_count | circuit_breaker_triggered | stale_metrics_count | turn_responses_received | successful_voice_turns |",
            "| --- | ---: | ---: | --- | ---: | ---: | ---: |",
        ]
    )
    for scenario in scenarios:
        stats = report_stats[scenario.name]
        lines.append(
            f"| `{scenario.name}` | {stats.timeout_count} | {stats.restart_count} | "
            f"{str(stats.circuit_breaker_triggered).lower()} | "
            f"{stats.stale_metrics_count} | {stats.turn_responses_received} | "
            f"{stats.successful_voice_turns} |"
        )

    lines.extend(
        [
            "",
            "## 请求计划与实际尝试",
            "",
            "| 场景 | 计划 warmup | 实际 warmup attempts | 计划正式 | 实际 formal attempts | sample_attempts | turn_requests_sent | turn_responses_received | successful_voice_turns | restart_count |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for scenario in scenarios:
        stats = report_stats[scenario.name]
        lines.append(
            f"| `{scenario.name}` | {stats.planned_warmup_requests} | "
            f"{stats.warmup_attempts} | {stats.planned_formal_requests} | "
            f"{stats.formal_attempts} | {stats.sample_attempts} | "
            f"{stats.turn_requests_sent} | {stats.turn_responses_received} | "
            f"{stats.successful_voice_turns} | {stats.restart_count} |"
        )

    lines.extend(
        [
            "",
            "## 成功语音样本 P50/P95（ms）",
            "",
            "| 场景 | 指标 | P50 | P95 |",
            "| --- | --- | ---: | ---: |",
        ]
    )
    for scenario in scenarios:
        formal = formal_by_scenario[scenario.name]
        for metric in BENCHMARK_METRIC_NAMES:
            p50, p95 = percentile_pair(latency_values(formal, metric))
            lines.append(
                f"| `{scenario.name}` | `{metric}` | {_fmt(p50)} | {_fmt(p95)} |"
            )

    by_name = {
        scenario.name: formal_by_scenario[scenario.name] for scenario in scenarios
    }
    pipeline_p50 = {
        name: percentile_pair(
            latency_values(records_for_name, "server_pipeline_total_ms")
        )[0]
        for name, records_for_name in by_name.items()
    }
    tts_p50 = {
        name: percentile_pair(latency_values(records_for_name, "tts_synthesis_ms"))[0]
        for name, records_for_name in by_name.items()
    }
    tts_saving = (
        "100.00%"
        if tts_p50.get("faq_online_tts") is not None
        else "N/A（文本缓存场景没有有效 TTS 样本）"
    )
    lines.extend(
        [
            "",
            "## 链路下降与 TTS 节省",
            "",
            f"- 完整在线链路（`server_pipeline_total_ms` P50）到文本缓存的下降比例：**{_ratio(pipeline_p50.get('xzkb_online_tts'), pipeline_p50.get('faq_online_tts'))}**。",
            f"- 完整在线链路（`server_pipeline_total_ms` P50）到预生成音频的下降比例：**{_ratio(pipeline_p50.get('xzkb_online_tts'), pipeline_p50.get('prepared_audio'))}**。",
            (
                f"- 文本缓存到预生成音频的 TTS 节省：**{tts_saving}**（预生成音频场景按设计不调用 TTS；"
                f"文本缓存场景 TTS P50 为 {_fmt(tts_p50.get('faq_online_tts'))} ms）。"
            ),
            "",
            "## 测试环境与配置",
            "",
            f"- Python：`{platform.python_version()}`；平台：`{platform.system()}`",
            f"- Settings 来源：`{Path(env_file_name).name}`（密钥未保存）",
            f"- runs：`{runs}`；warmup：`{warmup}`；startup-timeout：`{startup_timeout}` s",
            f"- benchmark 模式：`{benchmark_mode}`；执行顺序：`{execution_order}`。",
            (
                "- reliability 模式固定执行全部正式请求，不因连续错误提前熔断；延迟分布仍只统计成功样本。"
                if benchmark_mode == "reliability"
                else "- latency 模式保留连续错误熔断，避免上游故障时持续施压。"
            ),
            f"- 子服务上游请求超时：继承 Settings/.env 中的 `GUIDE_REQUEST_TIMEOUT_SECONDS`（benchmark 不覆盖，默认由 Settings 负责）；基准客户端 `/api/device/turn` 独立超时：`{turn_timeout}` s。",
            f"- 连续错误熔断阈值：`{max_consecutive_errors}`；样本间隔：`{interval_seconds}` s；场景冷却：`{scenario_cooldown_seconds}` s。",
            f"- TTS model：`{settings.tts_model}`；voice：`{settings.tts_voice}`；speed：`{settings.tts_speed}`",
            "- ASR、XZKB、TTS API key：已从 Settings 读取，未写入报告、原始结果、异常或日志。",
            "- 三个场景只通过子进程环境覆盖 `GUIDE_FAQ_CACHE_ENABLED` 与 `GUIDE_FAQ_PREPARED_AUDIO_ENABLED`；服务逐场景使用临时端口，不触碰现有 8765。",
            "- 测试未写入 `config/prepared_audio`、`config/prepared_audio/.pending`、`manifest.json` 或正式 FAQ 音频。",
            "",
            "## 调用量",
            "",
            f"- 计划 benchmark `/api/device/turn` 请求：`{sum(stats.planned_warmup_requests + stats.planned_formal_requests for stats in report_stats.values())}`；sample_attempts：`{sum(stats.sample_attempts for stats in report_stats.values())}`；turn_requests_sent：`{sum(turn_requests_sent_by_scenario.values())}`；turn_responses_received：`{sum(stats.turn_responses_received for stats in report_stats.values())}`。",
            f"- warmup_attempts：`{sum(stats.warmup_attempts for stats in report_stats.values())}`；formal_attempts：`{sum(stats.formal_attempts for stats in report_stats.values())}`；restart_count：`{sum(stats.restart_count for stats in report_stats.values())}`；successful_voice_turns：`{sum(stats.successful_voice_turns for stats in report_stats.values())}`。",
            f"- ASR 逻辑调用上界估算 ≤ `{sum(turn_requests_sent_by_scenario.values())}`；知识库逻辑调用上界估算 ≤ `{turn_requests_sent_by_scenario.get('xzkb_online_tts', 0)}`；在线 TTS 逻辑调用上界估算 ≤ `{turn_requests_sent_by_scenario.get('xzkb_online_tts', 0) + turn_requests_sent_by_scenario.get('faq_online_tts', 0)}`。以上只基于已开始的 `/turn`，不包含也无法获知服务内部重试次数，不是精确上游调用量。",
            (
                f"- 本次新生成测试问题音频 `{generated_count}` 条，复用 "
                f"`{len(report_audio_infos) - generated_count}` 条；另有 "
                f"`{len(report_audio_infos)}` 次 ASR 冒烟校验。它们均不计入"
                "上述链路阶段耗时。"
            ),
        ]
    )
    return "\n".join(lines) + "\n"


def _raw_metadata(
    *,
    audio_info: AudioInfo,
    transcript: str,
    scenarios: Sequence[Scenario],
    runs: int,
    warmup: int,
    run_stats: dict[str, ScenarioRunStats] | None = None,
    benchmark_mode: str = "latency",
    execution_order: str = "sequential",
    audio_infos: Sequence[AudioInfo] | None = None,
    transcripts: Sequence[str] | None = None,
) -> dict[str, object]:
    metadata_audio_infos = tuple(audio_infos or (audio_info,))
    metadata_transcripts = tuple(transcripts or (transcript,))
    run_stats = run_stats or {}
    return {
        "question": QUESTION,
        "asr_smoke_transcript": transcript,
        "audio_file_name": audio_info.file_name,
        "audio_sha256": audio_info.sha256,
        "audio_metadata": audio_info.metadata.as_dict(),
        "audio_fixtures": [
            {
                "file_name": info.file_name,
                "sha256": info.sha256,
                "metadata": info.metadata.as_dict(),
                "asr_smoke_transcript": recognized_text,
            }
            for info, recognized_text in zip(
                metadata_audio_infos,
                metadata_transcripts,
                strict=True,
            )
        ],
        "runs": runs,
        "warmup": warmup,
        "scenarios": [scenario.name for scenario in scenarios],
        "benchmark_mode": benchmark_mode,
        "execution_order": execution_order,
        "scenario_stats": {name: stats.as_dict() for name, stats in run_stats.items()},
        "answer_content_saved": False,
        "api_keys_saved": False,
    }


def write_outputs(
    output_dir: Path,
    *,
    records: Sequence[BenchmarkSample],
    audio_info: AudioInfo,
    transcript: str,
    settings: Settings,
    scenarios: Sequence[Scenario],
    runs: int,
    warmup: int,
    startup_timeout: float,
    env_file_name: str = ".env",
    turn_timeout: float = 240.0,
    max_consecutive_errors: int = 3,
    interval_seconds: float = 1.0,
    scenario_cooldown_seconds: float = 10.0,
    run_stats: dict[str, ScenarioRunStats] | None = None,
    project_root: Path = PROJECT_ROOT,
    question_audio_generated: bool = False,
    benchmark_mode: str = "latency",
    execution_order: str = "sequential",
    audio_infos: Sequence[AudioInfo] | None = None,
    transcripts: Sequence[str] | None = None,
    question_audio_generated_count: int = 0,
) -> tuple[Path, Path, Path]:
    safe_output = _safe_output_dir(output_dir, project_root)
    safe_output.mkdir(parents=True, exist_ok=True)
    json_path = safe_output / "raw.json"
    csv_path = safe_output / "raw.csv"
    report_path = safe_output / "report.md"
    metadata = _raw_metadata(
        audio_info=audio_info,
        transcript=transcript,
        scenarios=scenarios,
        runs=runs,
        warmup=warmup,
        run_stats=run_stats,
        benchmark_mode=benchmark_mode,
        execution_order=execution_order,
        audio_infos=audio_infos,
        transcripts=transcripts,
    )
    json_path.write_text(
        json.dumps(
            {"metadata": metadata, "records": [record.as_dict() for record in records]},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    fields = list(BenchmarkSample(scenario="", run_number=0, warmup=False).as_dict())
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(record.as_dict() for record in records)
    report_path.write_text(
        generate_report(
            records=records,
            audio_info=audio_info,
            transcript=transcript,
            settings=settings,
            scenarios=scenarios,
            runs=runs,
            warmup=warmup,
            startup_timeout=startup_timeout,
            env_file_name=env_file_name,
            turn_timeout=turn_timeout,
            max_consecutive_errors=max_consecutive_errors,
            interval_seconds=interval_seconds,
            scenario_cooldown_seconds=scenario_cooldown_seconds,
            run_stats=run_stats,
            project_root=project_root,
            question_audio_generated=question_audio_generated,
            benchmark_mode=benchmark_mode,
            execution_order=execution_order,
            audio_infos=audio_infos,
            transcripts=transcripts,
            question_audio_generated_count=question_audio_generated_count,
        ),
        encoding="utf-8",
    )
    return csv_path, json_path, report_path


def parse_scenarios(values: Sequence[str]) -> list[Scenario]:
    names = [
        name.strip() for value in values for name in value.split(",") if name.strip()
    ]
    if not names:
        raise argparse.ArgumentTypeError("至少指定一个场景")
    unknown = [name for name in names if name not in SCENARIOS]
    if unknown:
        raise argparse.ArgumentTypeError("存在未知 benchmark 场景")
    return [SCENARIOS[name] for name in names]


def question_for_audio_path(path: Path, project_root: Path = PROJECT_ROOT) -> str:
    resolved = _safe_fixture_path(path, project_root)
    for standard_path, question in STANDARD_AUDIO_QUESTIONS:
        if resolved == _safe_fixture_path(standard_path, project_root):
            return question
    return QUESTION


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="showroom-guide 三场景语音链路基准测试"
    )
    parser.add_argument(
        "--audio",
        type=Path,
        nargs="+",
        default=None,
        help="一条或多条 WAV；多条时按正式 run_number 轮转",
    )
    parser.add_argument(
        "--standard-fixtures",
        action="store_true",
        help="使用内置的 5 条八大车间标准问法音频",
    )
    parser.add_argument(
        "--prepare-audio-only",
        action="store_true",
        help="只准备并校验音频，不执行三场景链路请求",
    )
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument(
        "--mode",
        choices=BENCHMARK_MODES,
        default="latency",
        help="latency 保留熔断；reliability 固定完成全部请求",
    )
    parser.add_argument(
        "--execution-order",
        choices=EXECUTION_ORDERS,
        default="interleaved",
        help="interleaved 按轮次交错场景，降低固定顺序偏差",
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--scenarios",
        nargs="+",
        default=list(SCENARIO_NAMES),
        metavar="SCENARIO",
        help="场景名，可用空格或逗号分隔",
    )
    parser.add_argument("--startup-timeout", type=float, default=30.0)
    parser.add_argument("--turn-timeout", type=float, default=240.0)
    parser.add_argument("--max-consecutive-errors", type=int, default=3)
    parser.add_argument("--interval-seconds", type=float, default=1.0)
    parser.add_argument("--scenario-cooldown-seconds", type=float, default=10.0)
    parser.add_argument(
        "--confirm-live-requests",
        action="store_true",
        help="显式允许测试音频生成、ASR 冒烟和真实三场景请求",
    )
    return parser


def _validate_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.standard_fixtures and args.audio:
        parser.error("--standard-fixtures 不能与 --audio 同时使用")
    if args.runs <= 0:
        parser.error("--runs 必须大于 0")
    if args.warmup < 0:
        parser.error("--warmup 不能小于 0")
    if args.startup_timeout <= 0:
        parser.error("--startup-timeout 必须大于 0")

    if args.turn_timeout <= 0:
        parser.error("--turn-timeout must be greater than 0")
    if args.max_consecutive_errors <= 0:
        parser.error("--max-consecutive-errors must be greater than 0")
    if args.interval_seconds < 0:
        parser.error("--interval-seconds cannot be negative")
    if args.scenario_cooldown_seconds < 0:
        parser.error("--scenario-cooldown-seconds cannot be negative")


def benchmark_incomplete(
    records: Sequence[BenchmarkSample],
    scenarios: Sequence[Scenario],
    run_stats: dict[str, ScenarioRunStats],
    *,
    runs: int,
    benchmark_mode: str,
) -> bool:
    if benchmark_mode == "reliability":
        return any(
            run_stats.get(
                scenario.name,
                ScenarioRunStats(scenario.name),
            ).formal_attempts
            < runs
            for scenario in scenarios
        )
    return any(
        not any(
            record.scenario == scenario.name
            and not record.warmup
            and record.valid
            and record.outcome == "success"
            for record in records
        )
        or run_stats.get(
            scenario.name,
            ScenarioRunStats(scenario.name),
        ).circuit_breaker_triggered
        for scenario in scenarios
    )


async def run_cli(args: argparse.Namespace) -> int:
    _validate_args(args, build_parser())
    scenarios = parse_scenarios(args.scenarios)
    settings = load_validated_settings(
        args.env_file if args.env_file.is_absolute() else PROJECT_ROOT / args.env_file
    )
    audio_paths = (
        [path for path, _question in STANDARD_AUDIO_QUESTIONS]
        if args.standard_fixtures
        else (args.audio or [DEFAULT_AUDIO])
    )
    fixtures: list[BenchmarkAudio] = []
    audio_infos: list[AudioInfo] = []
    transcripts: list[str] = []
    generated_count = 0
    for index, audio_path in enumerate(audio_paths, start=1):
        print(
            f"正在准备测试问题音频 {index}/{len(audio_paths)}...",
            flush=True,
        )
        content, info, generated = await ensure_question_audio(
            audio_path,
            settings,
            confirm_live_requests=args.confirm_live_requests,
            question=question_for_audio_path(audio_path),
            project_root=PROJECT_ROOT,
        )
        generated_count += int(generated)
        if not args.confirm_live_requests:
            raise BenchmarkError(
                "默认不执行真实 API 请求；请在确认调用量后增加 --confirm-live-requests"
            )
        print(f"正在执行 ASR 冒烟校验 {index}/{len(audio_paths)}...", flush=True)
        recognized_text = await verify_asr_and_cache(content, settings)
        print(
            f"ASR 冒烟校验 {index}/{len(audio_paths)} 完成，目标高频问答已命中。",
            flush=True,
        )
        audio_infos.append(info)
        transcripts.append(recognized_text)
        fixtures.append(BenchmarkAudio(content, info, recognized_text))
    if args.prepare_audio_only:
        print(
            f"音频准备完成：共 {len(fixtures)} 条，新生成 {generated_count} 条，"
            f"复用 {len(fixtures) - generated_count} 条；全部通过 ASR 与 FAQ 命中校验。",
            flush=True,
        )
        return 0
    if not args.confirm_live_requests:
        raise BenchmarkError(
            "默认不执行真实 API 请求；请在确认调用量后增加 --confirm-live-requests"
        )

    expected_asr = len(scenarios) * (args.warmup + args.runs)
    expected_kb = (
        args.warmup + args.runs
        if any(scenario.name == "xzkb_online_tts" for scenario in scenarios)
        else 0
    )
    expected_tts = sum(
        args.warmup + args.runs
        for scenario in scenarios
        if scenario.name in {"xzkb_online_tts", "faq_online_tts"}
    )
    print(
        "即将执行真实请求（上界估算，不含服务内部重试）："
        f"ASR 逻辑调用上界 ≤ {expected_asr} 次，知识库逻辑调用上界 ≤ {expected_kb} 次，"
        f"在线 TTS 逻辑调用上界 ≤ {expected_tts} 次；"
        f"测试问题音频共 {len(fixtures)} 条，其中新生成 {generated_count} 条；"
        f"ASR 冒烟共 {len(fixtures)} 次并已完成。",
        flush=True,
    )
    print(
        f"测试模式：{args.mode}；执行顺序：{args.execution_order}。",
        flush=True,
    )

    safe_output = _safe_output_dir(args.output_dir, PROJECT_ROOT)
    safe_output.mkdir(parents=True, exist_ok=True)
    log_dir = safe_output / "logs"

    def server_factory(scenario: Scenario) -> SubprocessServer:
        return SubprocessServer(
            project_root=PROJECT_ROOT,
            child_env=settings_to_child_env(settings, scenario),
            startup_timeout=args.startup_timeout,
            log_dir=log_dir,
        )

    def client_factory(base_url: str) -> DeviceApiClient:
        return DeviceApiClient(
            base_url,
            settings.device_api_key.get_secret_value(),
            turn_timeout=args.turn_timeout,
        )

    runner = BenchmarkRunner(
        server_factory=server_factory,
        client_factory=client_factory,
        max_consecutive_errors=args.max_consecutive_errors,
        interval_seconds=args.interval_seconds,
        scenario_cooldown_seconds=args.scenario_cooldown_seconds,
        progress=lambda message: print(message, flush=True),
        benchmark_mode=args.mode,
        execution_order=args.execution_order,
    )
    records = await runner.run(
        fixtures,
        scenarios,
        runs=args.runs,
        warmup=args.warmup,
    )
    csv_path, json_path, report_path = write_outputs(
        args.output_dir,
        records=records,
        audio_info=audio_infos[0],
        transcript=transcripts[0],
        settings=settings,
        scenarios=scenarios,
        runs=args.runs,
        warmup=args.warmup,
        startup_timeout=args.startup_timeout,
        env_file_name=args.env_file.name,
        turn_timeout=args.turn_timeout,
        max_consecutive_errors=args.max_consecutive_errors,
        interval_seconds=args.interval_seconds,
        scenario_cooldown_seconds=args.scenario_cooldown_seconds,
        run_stats=runner.run_stats,
        project_root=PROJECT_ROOT,
        question_audio_generated=generated_count > 0,
        benchmark_mode=args.mode,
        execution_order=args.execution_order,
        audio_infos=audio_infos,
        transcripts=transcripts,
        question_audio_generated_count=generated_count,
    )
    print(
        f"已完成：{csv_path.name}, {json_path.name}, {report_path.name}"
        f"；测试音频新生成 {generated_count} 条、复用 "
        f"{len(fixtures) - generated_count} 条。"
    )
    incomplete = benchmark_incomplete(
        records,
        scenarios,
        runner.run_stats,
        runs=args.runs,
        benchmark_mode=args.mode,
    )
    if incomplete:
        print(
            "警告：至少一个场景没有成功语音样本或已触发连续错误熔断。",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(run_cli(args))
    except KeyboardInterrupt:
        print("已取消；临时服务会被清理。", file=sys.stderr)
        return 130
    except BenchmarkError as error:
        print(f"错误：{redact_text(str(error))}", file=sys.stderr)
        return 2
    except OSError:
        print("错误：无法写入 benchmark 输出文件。", file=sys.stderr)
        return 2
    except asyncio.CancelledError:
        print("已取消；临时服务会被清理。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AudioInfo",
    "BENCHMARK_METRIC_NAMES",
    "BenchmarkError",
    "BenchmarkRunner",
    "BenchmarkSample",
    "DeviceApiClient",
    "QUESTION",
    "SCENARIOS",
    "Scenario",
    "SubprocessServer",
    "build_parser",
    "formal_records",
    "generate_report",
    "latency_values",
    "load_and_validate_audio",
    "main",
    "nearest_rank",
    "parse_scenarios",
    "redact_text",
    "settings_to_child_env",
    "write_outputs",
]
