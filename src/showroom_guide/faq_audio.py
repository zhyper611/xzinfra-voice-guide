"""Batch generation and validation of FAQ cache WAV files."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import io
import json
import os
import re
import stat
import sys
import tempfile
import wave
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any, Literal

from pydantic import ValidationError

from showroom_guide.clients.speech import SpeechClient
from showroom_guide.config import Settings
from showroom_guide.faq_cache import CacheConfigError, CacheEntry, FaqCache, load_cache


class AudioConfigError(ValueError):
    """Raised when FAQ audio output paths or manifest data are invalid."""

    def __init__(self, message: object) -> None:
        # Sanitize at the exception boundary so non-CLI callers cannot
        # accidentally log raw command-line values or configuration secrets.
        super().__init__(_redact_sensitive_text(str(message)))


class WavValidationError(ValueError):
    """Raised when an audio file is not a usable uncompressed WAV."""


class AudioTransactionError(RuntimeError):
    """A commit failed and contains structured recovery information."""

    def __init__(
        self,
        *,
        original_error: BaseException,
        wav_recovery_error: BaseException | None = None,
        manifest_recovery_error: BaseException | None = None,
        cleanup_errors: tuple[tuple[str, BaseException], ...] = (),
        recovery_backups: tuple[Path, ...] = (),
        retained_paths: tuple[Path, ...] = (),
    ) -> None:
        self.original_error = original_error
        self.wav_recovery_error = wav_recovery_error
        self.manifest_recovery_error = manifest_recovery_error
        self.cleanup_errors = cleanup_errors
        self.recovery_backups = recovery_backups
        self.retained_paths = retained_paths
        details = [
            f"原始提交错误: {_safe_error_text(original_error)}",
        ]
        if manifest_recovery_error is not None:
            details.append(
                "manifest 恢复错误: "
                f"{_safe_error_text(manifest_recovery_error)}"
            )
        if wav_recovery_error is not None:
            details.append(
                f"WAV 恢复错误: {_safe_error_text(wav_recovery_error)}"
            )
        for resource, error in cleanup_errors:
            details.append(
                f"{resource} cleanup 错误: {_safe_error_text(error)}"
            )
        if recovery_backups:
            details.append(
                "请保留并检查 recovery backup: "
                + ", ".join(str(path) for path in recovery_backups)
            )
        other_retained = tuple(
            path for path in retained_paths if path not in recovery_backups
        )
        if other_retained:
            details.append(
                "尚存待清理文件: "
                + ", ".join(str(path) for path in other_retained)
            )
        super().__init__("音频事务提交失败；" + "；".join(details))


class AudioRecoveryError(RuntimeError):
    """A resource recovery attempted all required steps but had failures."""

    def __init__(self, resource: str, errors: tuple[BaseException, ...]) -> None:
        self.resource = resource
        self.errors = errors
        super().__init__(
            f"{resource} 恢复包含 {len(errors)} 个错误: "
            + "; ".join(_safe_error_text(error) for error in errors)
        )


def _safe_error_text(error: BaseException) -> str:
    """Keep transaction diagnostics free of settings and authorization data."""

    details = _redact_sensitive_text(str(error))
    notes = getattr(error, "__notes__", ())
    if notes:
        details += "; " + "; ".join(
            _redact_sensitive_text(str(note)) for note in notes
        )
    return f"{type(error).__name__}: {details}"


_SENSITIVE_KEY = (
    r"authorization|api[-_ ]?key|access[-_ ]?token|"
    r"refresh[-_ ]?token|token|password|secret"
)
_JSON_SENSITIVE_VALUE = re.compile(
    rf"(?i)(?P<key_quote>[\"'])(?P<key>(?:{_SENSITIVE_KEY}))"
    r"(?P=key_quote)(?P<separator>\s*:\s*)"
    r"(?P<value_quote>[\"'])(?P<value>.*?)(?P=value_quote)"
)
_QUOTED_SENSITIVE_VALUE = re.compile(
    rf"(?i)(?P<key>\b(?:{_SENSITIVE_KEY})\b)"
    r"(?P<separator>\s*[:=]\s*)(?P<quote>[\"'])(?P<value>.*?)"
    r"(?P=quote)"
)
_WRAPPED_SENSITIVE_ASSIGNMENT = re.compile(
    rf"(?i)(?P<wrap>[\"'])(?P<key>(?:{_SENSITIVE_KEY}))"
    r"(?P<separator>\s*[:=]\s*)(?P<value>.*?)(?P=wrap)"
)
_UNQUOTED_SENSITIVE_VALUE = re.compile(
    rf"(?i)(?P<key>\b(?:{_SENSITIVE_KEY})\b)"
    r"(?P<separator>\s*[:=]\s*)(?P<value>[^&;,\)\r\n\"']*?)"
    r"(?=(?:&|[;,\)\r\n]|$|\s+\w[\w-]*\s*[:=]))"
)
_BEARER_VALUE = re.compile(
    r"(?i)\bBearer\s+[^&;,\)\r\n\"']*?"
    r"(?=(?:&|[;,\)\r\n]|$|\s+\w[\w-]*\s*[:=]))"
)


def _redact_sensitive_text(text: str) -> str:
    """Redact complete credential values while preserving safe diagnostics."""

    def replace_quoted(match: re.Match[str]) -> str:
        return (
            f"{match.group('key')}{match.group('separator')}"
            f"{match.group('quote')}<redacted>{match.group('quote')}"
        )

    def replace_json(match: re.Match[str]) -> str:
        return (
            f"{match.group('key_quote')}{match.group('key')}"
            f"{match.group('key_quote')}{match.group('separator')}"
            f"{match.group('value_quote')}<redacted>"
            f"{match.group('value_quote')}"
        )

    def replace_unquoted(match: re.Match[str]) -> str:
        return f"{match.group('key')}{match.group('separator')}<redacted>"

    def replace_wrapped(match: re.Match[str]) -> str:
        return (
            f"{match.group('wrap')}{match.group('key')}"
            f"{match.group('separator')}<redacted>{match.group('wrap')}"
        )

    redacted = _JSON_SENSITIVE_VALUE.sub(replace_json, text)
    redacted = _WRAPPED_SENSITIVE_ASSIGNMENT.sub(replace_wrapped, redacted)
    redacted = _QUOTED_SENSITIVE_VALUE.sub(replace_quoted, redacted)
    redacted = _UNQUOTED_SENSITIVE_VALUE.sub(replace_unquoted, redacted)
    return _BEARER_VALUE.sub("Bearer <redacted>", redacted)


def _safe_validation_error_text(error: ValidationError) -> str:
    """Format Pydantic errors without exposing their input values."""

    details: list[str] = []
    for item in error.errors(include_url=False):
        location = ".".join(str(part) for part in item.get("loc", ())) or "<model>"
        message = _redact_sensitive_text(str(item.get("msg", "Validation error")))
        details.append(f"{_redact_sensitive_text(location)}: {message}")
    return "; ".join(details) or "configuration validation failed"


@dataclass(frozen=True)
class WavMetadata:
    """The WAV properties recorded in the generation manifest."""

    sample_rate: int
    channels: int
    sample_width: int
    frames: int
    duration_seconds: float

    def as_dict(self) -> dict[str, int | float]:
        return {
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "sample_width": self.sample_width,
            "frames": self.frames,
            "duration_seconds": self.duration_seconds,
        }


@dataclass(frozen=True)
class TtsProfile:
    """Non-secret TTS settings used to identify generated audio."""

    model: str
    voice: str
    speed: float


@dataclass(frozen=True)
class EntryAssessment:
    entry_id: str
    state: Literal["valid", "missing", "invalid", "stale"]
    reason: str
    metadata: WavMetadata | None = None


@dataclass(frozen=True)
class EntryFailure:
    entry_id: str
    error: str


@dataclass(frozen=True)
class TransactionCommitResult:
    metadata: WavMetadata
    wav_sha256: str
    warnings: tuple[str, ...] = ()


@dataclass
class _ResourceState:
    name: str
    path: Path
    backup: Path | None = None
    new_installed: bool = False


@dataclass
class GenerationResult:
    generated: int = 0
    skipped: int = 0
    planned: int = 0
    verified: int = 0
    stale: int = 0
    failed: int = 0
    failures: list[EntryFailure] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    assessments: list[EntryAssessment] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 1 if self.failed or self.stale else 0


Synthesizer = Any
Reporter = Callable[[str], None]
_WAV_READ_FRAMES = 4096


def validate_wav(
    source: Path | bytes | bytearray | memoryview | io.BufferedIOBase,
) -> WavMetadata:
    """Validate a WAV and return its actual audio metadata.

    The validator intentionally accepts any valid uncompressed PCM sample rate,
    channel count, and sample width.  The current TTS output is not forced to a
    particular hardware playback format at this stage.
    """

    close_source = False
    if isinstance(source, Path):
        wav_source: str | io.BufferedIOBase = str(source)
    elif isinstance(source, (bytes, bytearray, memoryview)):
        wav_source = io.BytesIO(bytes(source))
        close_source = True
    else:
        wav_source = source

    try:
        with wave.open(wav_source, "rb") as wav:
            channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            sample_rate = wav.getframerate()
            frames = wav.getnframes()
            comptype = wav.getcomptype()
            if channels <= 0:
                raise WavValidationError("WAV 声道数必须大于 0")
            if sample_width <= 0:
                raise WavValidationError("WAV 采样宽度必须大于 0")
            if sample_rate <= 0:
                raise WavValidationError("WAV 采样率必须大于 0")
            if frames <= 0:
                raise WavValidationError("WAV 必须包含至少一帧音频")
            if comptype != "NONE":
                raise WavValidationError(
                    f"WAV 必须是未压缩 PCM，实际压缩类型为 {comptype!r}"
                )
            frame_width = channels * sample_width
            expected_bytes = frames * frame_width
            actual_bytes = 0
            remaining_frames = frames
            while remaining_frames:
                requested_frames = min(_WAV_READ_FRAMES, remaining_frames)
                pcm = wav.readframes(requested_frames)
                expected_chunk_bytes = requested_frames * frame_width
                actual_bytes += len(pcm)
                if len(pcm) != expected_chunk_bytes:
                    raise WavValidationError(
                        "WAV PCM 数据不完整: "
                        f"声明 {expected_bytes} 字节，实际读取到 {actual_bytes} 字节"
                    )
                remaining_frames -= requested_frames
            if actual_bytes != expected_bytes:
                raise WavValidationError(
                    "WAV PCM 数据长度不一致: "
                    f"声明 {expected_bytes} 字节，实际读取到 {actual_bytes} 字节"
                )
            return WavMetadata(
                sample_rate=sample_rate,
                channels=channels,
                sample_width=sample_width,
                frames=frames,
                duration_seconds=frames / sample_rate,
            )
    except WavValidationError:
        raise
    except (OSError, EOFError, TypeError, wave.Error) as error:
        raise WavValidationError(f"WAV 无法打开或格式无效: {error}") from error
    finally:
        if close_source:
            wav_source.close()  # type: ignore[union-attr]


def _resolve_managed_path(
    config_dir: Path,
    configured_path: str | Path,
    *,
    label: str,
    allow_absolute: bool,
) -> Path:
    """Resolve an output path while rejecting symlink and special-file hazards."""

    configured_text = os.fspath(configured_path)
    if "\\" in configured_text and not (
        isinstance(configured_path, Path) and os.name == "nt"
    ):
        raise AudioConfigError(
            f"{label} 必须使用正斜杠 /，不允许反斜杠: {configured_text!r}"
        )
    configured = Path(configured_text)
    windows_configured = PureWindowsPath(configured_text)
    has_absolute_form = (
        configured.is_absolute()
        or bool(configured.drive)
        or bool(configured.root)
        or windows_configured.is_absolute()
        or bool(windows_configured.drive)
        or bool(windows_configured.root)
    )
    if has_absolute_form and not allow_absolute:
        raise AudioConfigError(
            f"{label} 不允许使用绝对路径: {configured_text!r}"
        )
    if (
        (bool(windows_configured.drive) or bool(windows_configured.root))
        and not configured.is_absolute()
    ):
        raise AudioConfigError(
            f"{label} 不是当前平台可安全解析的路径: {configured_text!r}"
        )

    candidate = configured if configured.is_absolute() else config_dir / configured
    lexical = Path(os.path.normpath(os.fspath(candidate)))
    resolved = candidate.resolve(strict=False)
    try:
        relative = lexical.relative_to(config_dir)
        resolved.relative_to(config_dir)
    except ValueError as error:
        raise AudioConfigError(
            f"{label} 不能逃出 YAML 配置目录: {configured_text!r}"
        ) from error

    current = config_dir
    relative_parts = relative.parts
    for index, part in enumerate(relative_parts):
        current /= part
        if current.is_symlink():
            raise AudioConfigError(
                f"{label} 不能经过符号链接组件: {current}"
            )
        if index < len(relative_parts) - 1 and current.exists() and not current.is_dir():
            raise AudioConfigError(
                f"{label} 的父路径不是普通目录: {current}"
            )

    if lexical.exists() or lexical.is_symlink():
        try:
            mode = lexical.stat().st_mode
        except OSError as error:
            raise AudioConfigError(
                f"{label} 无法检查目标文件: {lexical}: {error}"
            ) from error
        if not stat.S_ISREG(mode):
            raise AudioConfigError(
                f"{label} 必须是普通文件或尚不存在，实际目标为非普通文件: {lexical}"
            )
    return resolved


def resolve_audio_paths(
    config_path: str | Path,
    entries: Iterable[CacheEntry],
) -> dict[str, Path]:
    """Resolve and validate every configured audio path before TTS starts."""

    config_dir = Path(config_path).resolve().parent
    paths: dict[str, Path] = {}
    owners: dict[Path, tuple[str, str]] = {}
    for entry in entries:
        if Path(entry.audio_file).suffix.lower() != ".wav":
            raise AudioConfigError(
                f"条目 {entry.id!r} 的 audio_file 必须使用 .wav 扩展名: "
                f"{entry.audio_file!r}"
            )
        target = _resolve_managed_path(
            config_dir,
            entry.audio_file,
            label=f"条目 {entry.id!r} 的 audio_file",
            allow_absolute=False,
        )
        previous = owners.get(target)
        if previous is not None:
            previous_id, previous_file = previous
            raise AudioConfigError(
                f"audio_file 路径重复: 条目 {previous_id!r} 的 {previous_file!r} "
                f"与条目 {entry.id!r} 的 {entry.audio_file!r} 指向 {target}"
            )
        owners[target] = (entry.id, entry.audio_file)
        paths[entry.id] = target
    return paths


def select_entries(
    entries: Iterable[CacheEntry],
    *,
    priority: str = "high",
    entry_ids: Iterable[str] = (),
) -> tuple[CacheEntry, ...]:
    """Select enabled entries in configuration order.

    Explicit ``entry_ids`` restrict selection and intentionally take precedence
    over the priority filter, which makes ``--entry`` useful for a medium item
    without requiring a second command-line flag.
    """

    all_entries = tuple(entries)
    if priority not in {"high", "medium", "all"}:
        raise AudioConfigError(f"不支持的 priority: {priority!r}")
    by_id = {entry.id: entry for entry in all_entries}
    requested = tuple(dict.fromkeys(entry_ids))
    missing = [entry_id for entry_id in requested if entry_id not in by_id]
    if missing:
        raise AudioConfigError(f"不存在的 entry id: {', '.join(repr(item) for item in missing)}")

    if requested:
        requested_set = set(requested)
        return tuple(
            entry
            for entry in all_entries
            if entry.id in requested_set and entry.enabled
        )

    return tuple(
        entry
        for entry in all_entries
        if entry.enabled
        and (priority == "all" or entry.priority.value == priority)
    )


def manifest_path_for_config(config_path: str | Path) -> Path:
    config_dir = Path(config_path).resolve().parent
    return _resolve_managed_path(
        config_dir,
        "prepared_audio/manifest.json",
        label="manifest 路径",
        allow_absolute=False,
    )


def resolve_manifest_path(
    config_path: str | Path,
    manifest_path: str | Path | None = None,
) -> Path:
    config_dir = Path(config_path).resolve().parent
    configured = manifest_path or "prepared_audio/manifest.json"
    return _resolve_managed_path(
        config_dir,
        configured,
        label="manifest 路径",
        allow_absolute=True,
    )


def load_manifest(path: str | Path) -> dict[str, dict[str, Any]]:
    """Load a manifest, treating missing or malformed data as stale."""

    manifest_path = Path(path)
    if not manifest_path.is_file():
        return {}
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}

    if isinstance(raw, dict):
        records = raw.get("entries", [])
    else:
        records = raw
    if not isinstance(records, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        if isinstance(record, dict) and isinstance(record.get("entry_id"), str):
            result[record["entry_id"]] = record
    return result


def _expected_manifest_record(
    entry: CacheEntry,
    profile: TtsProfile,
    metadata: WavMetadata,
    wav_sha256: str,
) -> dict[str, Any]:
    return {
        "entry_id": entry.id,
        "entry_version": entry.version,
        "answer_sha256": hashlib.sha256(entry.answer.encode("utf-8")).hexdigest(),
        "audio_file": entry.audio_file,
        "tts_model": profile.model,
        "tts_voice": profile.voice,
        "tts_speed": profile.speed,
        "wav_sha256": wav_sha256,
        **metadata.as_dict(),
    }


def _manifest_matches(record: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    return all(record.get(key) == value for key, value in expected.items())


def _assessment_for_metadata(
    entry: CacheEntry,
    manifest: Mapping[str, Mapping[str, Any]],
    profile: TtsProfile,
    metadata: WavMetadata,
    wav_sha256: str,
) -> EntryAssessment:
    record = manifest.get(entry.id)
    if record is None:
        return EntryAssessment(entry.id, "stale", "manifest 中没有对应条目", metadata)
    expected = _expected_manifest_record(entry, profile, metadata, wav_sha256)
    if not _manifest_matches(record, expected):
        return EntryAssessment(
            entry.id,
            "stale",
            "YAML、TTS 配置或 WAV 元数据已变化",
            metadata,
        )
    return EntryAssessment(entry.id, "valid", "WAV 和 manifest 均有效", metadata)


def assess_entry_content(
    entry: CacheEntry,
    content: bytes,
    manifest: Mapping[str, Mapping[str, Any]],
    profile: TtsProfile,
) -> EntryAssessment:
    """Validate one already-read WAV against the existing manifest rules."""

    try:
        metadata = validate_wav(content)
    except WavValidationError as error:
        return EntryAssessment(
            entry.id,
            "invalid",
            _redact_sensitive_text(str(error)),
        )
    wav_sha256 = hashlib.sha256(content).hexdigest()
    return _assessment_for_metadata(
        entry,
        manifest,
        profile,
        metadata,
        wav_sha256,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise WavValidationError(f"无法读取 WAV 内容用于校验: {error}") from error
    return digest.hexdigest()


def assess_entry(
    entry: CacheEntry,
    target: Path,
    manifest: Mapping[str, Mapping[str, Any]],
    profile: TtsProfile,
) -> EntryAssessment:
    """Return whether an entry's WAV and manifest are current."""

    if not target.is_file():
        return EntryAssessment(entry.id, "missing", "WAV 文件不存在")
    try:
        content = target.read_bytes()
    except OSError as error:
        return EntryAssessment(
            entry.id,
            "invalid",
            _redact_sensitive_text(f"无法读取 WAV 内容: {error}"),
        )
    return assess_entry_content(entry, content, manifest, profile)


def _write_wav_temp(target: Path, content: bytes) -> tuple[Path, WavMetadata]:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        metadata = validate_wav(temp_path)
        return temp_path, metadata
    except BaseException as original_error:
        try:
            temp_path.unlink(missing_ok=True)
        except BaseException as cleanup_error:
            original_error.add_note(
                "WAV 临时文件清理失败: "
                f"{temp_path}: {_safe_error_text(cleanup_error)}"
            )
        raise


def _copy_backup(source: Path, backup: Path) -> None:
    """Durably copy a file while leaving the live source untouched."""

    with source.open("rb") as input_file, backup.open("wb") as output_file:
        while chunk := input_file.read(1024 * 1024):
            output_file.write(chunk)
        output_file.flush()
        os.fsync(output_file.fileno())


def _backup_existing(path: Path) -> Path | None:
    if not path.exists() and not path.is_symlink():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_backup = tempfile.mkstemp(
        prefix=f".{path.name}.backup.", suffix=".tmp", dir=path.parent
    )
    backup_path = Path(raw_backup)
    try:
        os.close(descriptor)
        _copy_backup(path, backup_path)
    except BaseException as original_error:
        try:
            backup_path.unlink(missing_ok=True)
        except BaseException as cleanup_error:
            original_error.add_note(
                "WAV/manifest backup 临时文件清理失败（原文件保持不变）: "
                f"{backup_path}: {_safe_error_text(cleanup_error)}"
            )
        raise
    return backup_path


def _remove_path(path: Path | None) -> None:
    if path is not None:
        path.unlink(missing_ok=True)


def _attempt_cleanup(
    path: Path,
    *,
    resource: str,
    cleanup_errors: list[tuple[str, BaseException]],
    retained_paths: list[Path],
) -> None:
    if not path.exists() and not path.is_symlink():
        return
    try:
        _remove_path(path)
    except BaseException as error:
        cleanup_errors.append((resource, error))
        retained_paths.append(path)


def _restore_resource(
    state: _ResourceState,
) -> tuple[AudioRecoveryError | None, tuple[Path, ...]]:
    errors: list[BaseException] = []
    retained_paths: list[Path] = []
    if state.backup is not None:
        try:
            _remove_path(state.path)
        except BaseException as error:
            errors.append(error)
        try:
            os.replace(state.backup, state.path)
        except BaseException as error:
            errors.append(error)
            retained_paths.append(state.backup)
        else:
            state.backup = None
    elif state.new_installed:
        try:
            _remove_path(state.path)
        except BaseException as error:
            errors.append(error)
            retained_paths.append(state.path)
    recovery_error = (
        AudioRecoveryError(state.name, tuple(errors)) if errors else None
    )
    return recovery_error, tuple(retained_paths)


def _commit_audio_and_manifest(
    *,
    entry: CacheEntry,
    profile: TtsProfile,
    target: Path,
    content: bytes,
    manifest_path: Path,
    manifest_records: Mapping[str, Mapping[str, Any]],
) -> TransactionCommitResult:
    """Commit one WAV and manifest record with recoverable rollback."""

    wav_temp, metadata = _write_wav_temp(target, content)
    wav_state = _ResourceState("WAV", target)
    manifest_state = _ResourceState("manifest", manifest_path)
    try:
        wav_state.backup = _backup_existing(target)
        # Rollback owns the target before replace: replace may commit and then
        # raise or be cancelled before control returns here.
        wav_state.new_installed = True
        os.replace(wav_temp, target)
        wav_sha256 = _sha256_file(target)

        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_state.backup = _backup_existing(manifest_path)
        # The atomic writer can commit and then raise before returning.
        manifest_state.new_installed = True
        candidate_manifest = dict(manifest_records)
        candidate_manifest[entry.id] = _expected_manifest_record(
            entry, profile, metadata, wav_sha256
        )
        _atomic_write_manifest(manifest_path, candidate_manifest)
    except BaseException as original_error:
        cleanup_errors: list[tuple[str, BaseException]] = []
        retained_paths: list[Path] = []
        manifest_recovery_error, manifest_retained = _restore_resource(manifest_state)
        wav_recovery_error, wav_retained = _restore_resource(wav_state)
        retained_paths.extend(manifest_retained)
        retained_paths.extend(wav_retained)
        _attempt_cleanup(
            wav_temp,
            resource="WAV 临时文件",
            cleanup_errors=cleanup_errors,
            retained_paths=retained_paths,
        )
        recovery_backups = tuple(
            path
            for state in (manifest_state, wav_state)
            if state.backup is not None
            for path in (state.backup,)
        )
        transaction_error = AudioTransactionError(
            original_error=original_error,
            wav_recovery_error=wav_recovery_error,
            manifest_recovery_error=manifest_recovery_error,
            cleanup_errors=tuple(cleanup_errors),
            recovery_backups=recovery_backups,
            retained_paths=tuple(dict.fromkeys(retained_paths)),
        )
        if isinstance(original_error, asyncio.CancelledError):
            original_error.add_note(str(transaction_error))
            raise
        if isinstance(original_error, (KeyboardInterrupt, SystemExit)):
            original_error.add_note(str(transaction_error))
            raise
        raise transaction_error from original_error

    warnings: list[str] = []
    for state in (wav_state, manifest_state):
        if state.backup is None:
            continue
        try:
            _remove_path(state.backup)
        except BaseException as error:
            warnings.append(
                f"事务已提交，但 {state.name} backup 清理失败，已保留 {state.backup}: "
                f"{_safe_error_text(error)}"
            )
        else:
            state.backup = None
    return TransactionCommitResult(metadata, wav_sha256, tuple(warnings))


def _atomic_write_manifest(path: Path, records: Mapping[str, Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "entries": [records[entry_id] for entry_id in sorted(records)],
    }
    descriptor, raw_temp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, path)
    except BaseException as original_error:
        try:
            temp_path.unlink(missing_ok=True)
        except BaseException as cleanup_error:
            original_error.add_note(
                "manifest 临时文件清理失败: "
                f"{temp_path}: {_safe_error_text(cleanup_error)}"
            )
        raise
    else:
        try:
            temp_path.unlink(missing_ok=True)
        except BaseException:
            # os.replace already committed the manifest; do not turn a
            # successful commit into a rollback because the old temp name is
            # unexpectedly unavailable to unlink.
            pass


class FaqAudioGenerator:
    """Generate FAQ WAVs using an injected asynchronous or synchronous fake."""

    def __init__(
        self,
        cache: FaqCache,
        config_path: str | Path,
        profile: TtsProfile,
        *,
        manifest_path: str | Path | None = None,
    ) -> None:
        self._cache = cache
        self._config_path = Path(config_path).resolve()
        self._profile = profile
        self._audio_paths = resolve_audio_paths(self._config_path, cache.entries)
        self._manifest_path = resolve_manifest_path(self._config_path, manifest_path)

    @property
    def manifest_path(self) -> Path:
        return self._manifest_path

    @property
    def audio_paths(self) -> Mapping[str, Path]:
        return self._audio_paths

    async def run(
        self,
        synthesizer: Synthesizer | None = None,
        *,
        priority: str = "high",
        entry_ids: Iterable[str] = (),
        dry_run: bool = False,
        verify_only: bool = False,
        force: bool = False,
        report: Reporter | None = None,
    ) -> GenerationResult:
        if dry_run and verify_only:
            raise AudioConfigError("--dry-run 与 --verify-only 不能同时使用")
        if verify_only and force:
            raise AudioConfigError("--verify-only 与 --force 不能同时使用")
        if not dry_run and not verify_only and synthesizer is None:
            raise AudioConfigError("生成 WAV 时必须提供 synthesizer")

        selected = select_entries(
            self._cache.entries,
            priority=priority,
            entry_ids=entry_ids,
        )
        manifest = load_manifest(self._manifest_path)
        result = GenerationResult()
        for entry in selected:
            assessment = assess_entry(
                entry,
                self._audio_paths[entry.id],
                manifest,
                self._profile,
            )
            result.assessments.append(assessment)

            if verify_only:
                if assessment.state == "valid":
                    result.verified += 1
                else:
                    result.stale += 1
                self._report(report, entry, assessment.state, assessment.reason)
                continue

            if not force and assessment.state == "valid":
                result.skipped += 1
                self._report(report, entry, "skip", assessment.reason)
                continue

            if dry_run:
                result.planned += 1
                self._report(report, entry, "generate", assessment.reason)
                continue

            try:
                synthesized = synthesizer.synthesize(entry.answer)  # type: ignore[union-attr]
                audio = await synthesized if inspect.isawaitable(synthesized) else synthesized
                if not isinstance(audio, (bytes, bytearray, memoryview)):
                    raise WavValidationError("synthesizer 必须返回 WAV bytes")
                target = self._audio_paths[entry.id]
                commit = _commit_audio_and_manifest(
                    entry=entry,
                    profile=self._profile,
                    target=target,
                    content=bytes(audio),
                    manifest_path=self._manifest_path,
                    manifest_records=manifest,
                )
                manifest[entry.id] = _expected_manifest_record(
                    entry,
                    self._profile,
                    commit.metadata,
                    commit.wav_sha256,
                )
                result.generated += 1
                result.warnings.extend(commit.warnings)
                self._report(report, entry, "generated", "已写入并校验 WAV")
                for warning in commit.warnings:
                    self._report(report, entry, "warning", warning)
            except Exception as error:
                result.failed += 1
                failure = EntryFailure(
                    _redact_sensitive_text(entry.id),
                    _safe_error_text(error),
                )
                result.failures.append(failure)
                self._report(report, entry, "failed", failure.error)

        return result

    @staticmethod
    def _report(
        report: Reporter | None,
        entry: CacheEntry,
        state: str,
        reason: str,
    ) -> None:
        if report is not None:
            report(
                f"{_redact_sensitive_text(entry.id)}: {state} "
                f"({_redact_sensitive_text(reason)})"
            )


def tts_profile_from_settings(settings: Settings) -> TtsProfile:
    return TtsProfile(
        model=settings.tts_model,
        voice=settings.tts_voice,
        speed=settings.tts_speed,
    )


def speech_client_from_settings(settings: Settings) -> SpeechClient:
    return SpeechClient(
        settings.asr_base_url,
        settings.asr_api_key.get_secret_value(),
        settings.asr_model,
        settings.tts_base_url,
        settings.tts_api_key.get_secret_value(),
        settings.tts_model,
        settings.tts_voice,
        settings.tts_speed,
        settings.request_timeout_seconds,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="批量生成高频问答缓存 WAV")
    parser.add_argument("--config", default="config/faq_cache.yaml", help="缓存 YAML 路径")
    parser.add_argument("--env-file", default=".env", help="Settings 使用的 env 文件")
    parser.add_argument(
        "--priority",
        choices=("high", "medium", "all"),
        default="high",
        help="选择生成优先级，默认只生成 high",
    )
    parser.add_argument("--entry", action="append", default=[], help="只处理指定 entry id，可重复")
    parser.add_argument("--dry-run", action="store_true", help="只显示计划，不调用 TTS 或写文件")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--verify-only", action="store_true", help="只校验 WAV 和 manifest")
    mode.add_argument("--force", action="store_true", help="即使未过期也重新生成")
    return parser


async def _run_cli(args: argparse.Namespace) -> int:
    cache = load_cache(args.config)
    settings = Settings(_env_file=args.env_file)
    generator = FaqAudioGenerator(
        cache,
        args.config,
        tts_profile_from_settings(settings),
    )

    speech: SpeechClient | None = None
    try:
        if not args.dry_run and not args.verify_only:
            speech = speech_client_from_settings(settings)
        result = await generator.run(
            speech,
            priority=args.priority,
            entry_ids=args.entry,
            dry_run=args.dry_run,
            verify_only=args.verify_only,
            force=args.force,
            report=print,
        )
    finally:
        if speech is not None:
            await speech.aclose()

    print(
        "汇总: "
        f"generated={result.generated}, skipped={result.skipped}, "
        f"planned={result.planned}, verified={result.verified}, "
        f"stale={result.stale}, failed={result.failed}"
    )
    return result.exit_code


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.dry_run and args.verify_only:
        parser.error("--dry-run 与 --verify-only 不能同时使用")
    try:
        return asyncio.run(_run_cli(args))
    except KeyboardInterrupt:
        print("已取消，临时文件已清理", file=sys.stderr)
        return 130
    except ValidationError as error:
        print(f"错误: {_safe_validation_error_text(error)}", file=sys.stderr)
        return 2
    except AudioConfigError as error:
        # Keep the established user-facing configuration wording unchanged.
        print(f"错误: {_redact_sensitive_text(str(error))}", file=sys.stderr)
        return 2
    except CacheConfigError as error:
        print(f"错误: {_redact_sensitive_text(str(error))}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"错误: {_safe_error_text(error)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AudioConfigError",
    "AudioRecoveryError",
    "AudioTransactionError",
    "EntryAssessment",
    "EntryFailure",
    "FaqAudioGenerator",
    "GenerationResult",
    "TransactionCommitResult",
    "TtsProfile",
    "WavMetadata",
    "WavValidationError",
    "assess_entry",
    "assess_entry_content",
    "build_parser",
    "load_manifest",
    "main",
    "manifest_path_for_config",
    "resolve_audio_paths",
    "resolve_manifest_path",
    "select_entries",
    "speech_client_from_settings",
    "tts_profile_from_settings",
    "validate_wav",
]
