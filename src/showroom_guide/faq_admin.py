"""Protected FAQ cache inspection, editing, and audio approval services."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import stat
import tempfile
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool

from showroom_guide.faq_audio import (
    AudioConfigError,
    FaqAudioGenerator,
    TtsProfile,
    assess_entry_content,
    load_manifest,
    resolve_audio_paths,
    resolve_manifest_path,
    validate_wav,
)
from showroom_guide.faq_cache import CacheConfigError, CacheEntry, FaqCache, load_cache


logger = logging.getLogger(__name__)

AudioStatus = Literal["valid", "missing", "stale", "invalid", "disabled"]
DraftStatus = Literal["none", "ready", "stale", "invalid"]
MAX_ADMIN_AUDIO_BYTES = 10 * 1024 * 1024
MAX_DRAFT_METADATA_BYTES = 64 * 1024


class FaqAdminConfigError(RuntimeError):
    """Raised when the FAQ document or a global audio setting is invalid."""


class FaqAdminUnavailable(RuntimeError):
    """Raised when the configured FAQ storage cannot be read temporarily."""


class FaqAdminEntryNotFound(RuntimeError):
    """Raised when an operation targets an unknown FAQ entry."""


class FaqAdminOperationConflict(RuntimeError):
    """Raised when an entry cannot be generated or approved in its current state."""


class FaqAdminSynthesisError(RuntimeError):
    """Raised when TTS cannot produce a valid review draft."""


class FaqAdminTtsProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    voice: str
    speed: float


class FaqAdminMatchRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subjects: tuple[str, ...]
    intents: tuple[str, ...]
    excludes: tuple[str, ...] = ()


class FaqAdminAudio(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: AudioStatus
    size_bytes: int | None = None
    duration_seconds: float | None = None
    sample_rate: int | None = None
    channels: int | None = None
    sample_width: int | None = None


class FaqAdminDraftAudio(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: DraftStatus = "none"
    generated_at: str | None = None
    size_bytes: int | None = None
    duration_seconds: float | None = None
    sample_rate: int | None = None
    channels: int | None = None
    sample_width: int | None = None


class FaqAdminEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    enabled: bool
    priority: str
    version: int
    edit_token: str
    aliases: tuple[str, ...]
    match_rules: FaqAdminMatchRules | None
    answer: str
    audio_file: str
    audio: FaqAdminAudio
    draft_audio: FaqAdminDraftAudio


class FaqAdminSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int
    enabled: int
    high: int
    medium: int
    audio_valid: int
    audio_missing: int
    audio_stale: int
    audio_invalid: int
    audio_disabled: int
    draft_ready: int


class FaqCacheSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    source_document: str
    rule_excludes: tuple[str, ...]
    tts_profile: FaqAdminTtsProfile
    summary: FaqAdminSummary
    entries: tuple[FaqAdminEntry, ...]
    reload_policy: Literal["restart_required"] = "restart_required"


class FaqAdminAudioAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_id: str
    status: Literal["draft_ready", "approved"]
    message: str


class FaqAdminEntryFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    enabled: bool
    priority: Literal["high", "medium"]
    aliases: tuple[str, ...] = Field(min_length=1)
    match_rules: FaqAdminMatchRules | None = None
    answer: str = Field(min_length=1)

    @field_validator("title", "answer")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("不能为空")
        return stripped


class FaqAdminEntryCreate(FaqAdminEntryFields):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")


class FaqAdminEntryUpdate(FaqAdminEntryFields):
    expected_edit_token: str = Field(min_length=64, max_length=64)


class FaqAdminDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_edit_token: str = Field(min_length=64, max_length=64)


class FaqAdminEntryAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_id: str
    status: Literal["created", "updated", "deleted"]
    version: int | None = None
    message: str


class FaqCacheReadService:
    """Reload and inspect one fixed FAQ configuration on every request."""

    def __init__(
        self,
        config_path: str | Path,
        profile: TtsProfile,
        synthesizer: Any | None = None,
    ) -> None:
        self._config_path = Path(config_path)
        self._profile = profile
        self._synthesizer = synthesizer
        self._operation_lock = asyncio.Lock()
        self._file_lock = threading.Lock()

    def snapshot(self) -> FaqCacheSnapshot:
        cache = self._load_cache()
        targets = self._resolve_audio_targets(cache.entries)
        manifest = self._load_manifest()
        entries: list[FaqAdminEntry] = []
        counts = {
            "valid": 0,
            "missing": 0,
            "stale": 0,
            "invalid": 0,
            "disabled": 0,
        }
        draft_ready = 0

        for entry in cache.entries:
            audio = self._inspect_entry(entry, targets.get(entry.id), manifest)
            draft = self._inspect_draft(entry)
            counts[audio.status] += 1
            draft_ready += draft.status == "ready"
            entries.append(self._entry_snapshot(entry, audio, draft))

        summary = FaqAdminSummary(
            total=len(cache.entries),
            enabled=sum(entry.enabled for entry in cache.entries),
            high=sum(entry.priority.value == "high" for entry in cache.entries),
            medium=sum(entry.priority.value == "medium" for entry in cache.entries),
            audio_valid=counts["valid"],
            audio_missing=counts["missing"],
            audio_stale=counts["stale"],
            audio_invalid=counts["invalid"],
            audio_disabled=counts["disabled"],
            draft_ready=draft_ready,
        )
        return FaqCacheSnapshot(
            schema_version=cache.schema_version,
            source_document=_safe_display_text(cache.source_document),
            rule_excludes=cache.rule_excludes,
            tts_profile=FaqAdminTtsProfile(
                model=self._profile.model,
                voice=self._profile.voice,
                speed=self._profile.speed,
            ),
            summary=summary,
            entries=tuple(entries),
        )

    def _load_cache(self) -> FaqCache:
        try:
            return load_cache(self._config_path)
        except CacheConfigError as error:
            cause = error.__cause__
            if isinstance(cause, (OSError, UnicodeError)) or not _is_file(
                self._config_path
            ):
                raise FaqAdminUnavailable from error
            raise FaqAdminConfigError from error

    def _resolve_audio_targets(
        self,
        entries: tuple[CacheEntry, ...],
    ) -> dict[str, Path | None]:
        lexical_owners: dict[PurePosixPath, str] = {}
        for entry in entries:
            lexical_target = PurePosixPath(entry.audio_file)
            if lexical_target in lexical_owners:
                raise FaqAdminConfigError
            lexical_owners[lexical_target] = entry.id

        targets: dict[str, Path | None] = {}
        owners: dict[Path, str] = {}
        duplicate_found = False

        for entry in entries:
            try:
                target = resolve_audio_paths(self._config_path, (entry,))[entry.id]
            except Exception:
                targets[entry.id] = None
                continue

            previous_id = owners.get(target)
            if previous_id is not None:
                duplicate_found = True
            else:
                owners[target] = entry.id
            targets[entry.id] = target

        if duplicate_found:
            raise FaqAdminConfigError
        return targets

    def _load_manifest(self) -> dict[str, dict[str, object]]:
        try:
            manifest_path = resolve_manifest_path(self._config_path)
            return load_manifest(manifest_path, strict=True)
        except (AudioConfigError, OSError, UnicodeError) as error:
            raise FaqAdminUnavailable from error

    def _inspect_entry(
        self,
        entry: CacheEntry,
        target: Path | None,
        manifest: dict[str, dict[str, object]],
    ) -> FaqAdminAudio:
        if not entry.enabled:
            return FaqAdminAudio(status="disabled")

        if target is None:
            self._warn(entry, "invalid")
            return FaqAdminAudio(status="invalid")

        read_status, content = _read_audio_content(target)
        if read_status is not None:
            self._warn(entry, read_status)
            return FaqAdminAudio(status=read_status)
        assert content is not None

        try:
            assessment = assess_entry_content(
                entry,
                content,
                manifest,
                self._profile,
            )
        except Exception:
            self._warn(entry, "invalid")
            return FaqAdminAudio(status="invalid")

        if assessment.state != "valid" or assessment.metadata is None:
            self._warn(entry, assessment.state)
            return FaqAdminAudio(status=assessment.state)

        metadata = assessment.metadata
        return FaqAdminAudio(
            status="valid",
            size_bytes=len(content),
            duration_seconds=metadata.duration_seconds,
            sample_rate=metadata.sample_rate,
            channels=metadata.channels,
            sample_width=metadata.sample_width,
        )

    @staticmethod
    def _entry_snapshot(
        entry: CacheEntry,
        audio: FaqAdminAudio,
        draft_audio: FaqAdminDraftAudio,
    ) -> FaqAdminEntry:
        rules = entry.match_rules
        return FaqAdminEntry(
            id=entry.id,
            title=entry.title,
            enabled=entry.enabled,
            priority=entry.priority.value,
            version=entry.version,
            edit_token=_entry_edit_token(entry),
            aliases=entry.aliases,
            match_rules=(
                None
                if rules is None
                else FaqAdminMatchRules(
                    subjects=rules.subjects,
                    intents=rules.intents,
                    excludes=rules.excludes,
                )
            ),
            answer=entry.answer,
            audio_file=_safe_display_text(entry.audio_file),
            audio=audio,
            draft_audio=draft_audio,
        )

    async def generate_draft(self, entry_id: str) -> FaqAdminAudioAction:
        if self._synthesizer is None:
            raise FaqAdminSynthesisError
        async with self._operation_lock:
            entry = self._find_entry(entry_id)
            if not entry.enabled:
                raise FaqAdminOperationConflict("disabled")
            try:
                content = await self._synthesizer.synthesize(entry.answer)
                if not isinstance(content, (bytes, bytearray, memoryview)):
                    raise ValueError("invalid audio result")
                audio = bytes(content)
                if len(audio) > MAX_ADMIN_AUDIO_BYTES:
                    raise ValueError("audio exceeds limit")
                validate_wav(audio)
            except Exception as error:
                logger.warning(
                    "FAQ admin TTS generation failed: entry_id=%s error_type=%s",
                    entry.id,
                    type(error).__name__,
                )
                raise FaqAdminSynthesisError from error

            current = self._find_entry(entry_id)
            if current.version != entry.version or current.answer != entry.answer:
                raise FaqAdminOperationConflict("entry_changed")
            try:
                await run_in_threadpool(self._store_draft, current, audio)
            except AudioConfigError as error:
                raise FaqAdminConfigError from error
            except OSError as error:
                raise FaqAdminUnavailable from error
            return FaqAdminAudioAction(
                entry_id=entry.id,
                status="draft_ready",
                message="语音草稿已生成，等待试听审批",
            )

    async def approve_draft(self, entry_id: str) -> FaqAdminAudioAction:
        async with self._operation_lock:
            return await run_in_threadpool(self._approve_draft, entry_id)

    async def create_entry(
        self,
        payload: FaqAdminEntryCreate,
    ) -> FaqAdminEntryAction:
        async with self._operation_lock:
            return await run_in_threadpool(self._create_entry, payload)

    async def update_entry(
        self,
        entry_id: str,
        payload: FaqAdminEntryUpdate,
    ) -> FaqAdminEntryAction:
        async with self._operation_lock:
            return await run_in_threadpool(self._update_entry, entry_id, payload)

    async def delete_entry(
        self,
        entry_id: str,
        expected_edit_token: str,
    ) -> FaqAdminEntryAction:
        async with self._operation_lock:
            return await run_in_threadpool(
                self._delete_entry,
                entry_id,
                expected_edit_token,
            )

    def get_audio(self, entry_id: str, source: Literal["draft", "active"]) -> bytes:
        entry = self._find_entry(entry_id)
        if source == "draft":
            draft, content = self._read_draft(entry)
            if draft.status != "ready" or content is None:
                raise FaqAdminOperationConflict("draft_unavailable")
            return content

        cache = self._load_cache()
        targets = self._resolve_audio_targets(cache.entries)
        target = targets.get(entry.id)
        manifest = self._load_manifest()
        if target is None:
            raise FaqAdminOperationConflict("active_unavailable")
        read_status, content = _read_audio_content(target)
        if read_status is not None or content is None:
            raise FaqAdminOperationConflict("active_unavailable")
        assessment = assess_entry_content(entry, content, manifest, self._profile)
        if assessment.state != "valid":
            raise FaqAdminOperationConflict("active_unavailable")
        return content

    def _find_entry(self, entry_id: str) -> CacheEntry:
        cache = self._load_cache()
        entry = next((item for item in cache.entries if item.id == entry_id), None)
        if entry is None:
            raise FaqAdminEntryNotFound
        return entry

    def _create_entry(self, payload: FaqAdminEntryCreate) -> FaqAdminEntryAction:
        with self._file_lock:
            cache = self._load_cache()
            if any(entry.id == payload.id for entry in cache.entries):
                raise FaqAdminOperationConflict("duplicate_id")
            try:
                created = CacheEntry(
                    **payload.model_dump(),
                    version=1,
                    audio_file=f"prepared_audio/{payload.id}.wav",
                )
            except Exception as error:
                raise FaqAdminConfigError from error
            self._write_cache(cache, (*cache.entries, created))
            return FaqAdminEntryAction(
                entry_id=created.id,
                status="created",
                version=created.version,
                message="高频问答条目已创建，重启服务后进入运行时缓存",
            )

    def _update_entry(
        self,
        entry_id: str,
        payload: FaqAdminEntryUpdate,
    ) -> FaqAdminEntryAction:
        with self._file_lock:
            cache = self._load_cache()
            current = next((entry for entry in cache.entries if entry.id == entry_id), None)
            if current is None:
                raise FaqAdminEntryNotFound
            if _entry_edit_token(current) != payload.expected_edit_token:
                raise FaqAdminOperationConflict("version_conflict")
            next_version = current.version + (payload.answer != current.answer)
            try:
                updated = CacheEntry(
                    id=current.id,
                    **payload.model_dump(exclude={"expected_edit_token"}),
                    version=next_version,
                    audio_file=current.audio_file,
                )
            except Exception as error:
                raise FaqAdminConfigError from error
            entries = tuple(
                updated if entry.id == entry_id else entry
                for entry in cache.entries
            )
            self._write_cache(cache, entries)
            return FaqAdminEntryAction(
                entry_id=updated.id,
                status="updated",
                version=updated.version,
                message="条目已保存，重启服务后进入运行时缓存",
            )

    def _delete_entry(
        self,
        entry_id: str,
        expected_edit_token: str,
    ) -> FaqAdminEntryAction:
        with self._file_lock:
            cache = self._load_cache()
            current = next((entry for entry in cache.entries if entry.id == entry_id), None)
            if current is None:
                raise FaqAdminEntryNotFound
            if _entry_edit_token(current) != expected_edit_token:
                raise FaqAdminOperationConflict("version_conflict")
            entries = tuple(entry for entry in cache.entries if entry.id != entry_id)
            if not entries:
                raise FaqAdminOperationConflict("last_entry")
            self._write_cache(cache, entries)
            return FaqAdminEntryAction(
                entry_id=current.id,
                status="deleted",
                message="条目配置已删除，关联音频资产已保留",
            )

    def _write_cache(
        self,
        cache: FaqCache,
        entries: tuple[CacheEntry, ...],
    ) -> None:
        document = {
            "schema_version": cache.schema_version,
            "source_document": cache.source_document,
            "rule_excludes": list(cache.rule_excludes),
            "entries": [
                entry.model_dump(mode="json", exclude_none=True)
                for entry in entries
            ],
        }
        try:
            _atomic_write_cache_yaml(self._config_path, document)
        except CacheConfigError as error:
            raise FaqAdminConfigError from error
        except (OSError, UnicodeError, yaml.YAMLError) as error:
            raise FaqAdminUnavailable from error

    def _pending_path(self, filename: str) -> Path:
        return resolve_manifest_path(
            self._config_path,
            f"prepared_audio/.pending/{filename}",
        )

    def _draft_metadata_path(self, entry: CacheEntry) -> Path:
        return self._pending_path(f"{entry.id}.json")

    def _store_draft(self, entry: CacheEntry, content: bytes) -> None:
        with self._file_lock:
            metadata = validate_wav(content)
            wav_hash = hashlib.sha256(content).hexdigest()
            token = uuid.uuid4().hex
            wav_name = f"{entry.id}-{token}.wav"
            wav_path = self._pending_path(wav_name)
            metadata_path = self._draft_metadata_path(entry)
            previous = self._load_draft_metadata(entry)
            payload = {
                "entry_id": entry.id,
                "entry_version": entry.version,
                "answer_sha256": hashlib.sha256(entry.answer.encode("utf-8")).hexdigest(),
                "tts_model": self._profile.model,
                "tts_voice": self._profile.voice,
                "tts_speed": self._profile.speed,
                "wav_sha256": wav_hash,
                "wav_file": wav_name,
                "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                **metadata.as_dict(),
            }
            _atomic_write_bytes(wav_path, content)
            try:
                _atomic_write_json(metadata_path, payload)
            except BaseException:
                wav_path.unlink(missing_ok=True)
                raise
            old_name = previous.get("wav_file") if isinstance(previous, dict) else None
            if isinstance(old_name, str) and old_name != wav_name:
                try:
                    self._pending_path(old_name).unlink(missing_ok=True)
                except (AudioConfigError, OSError):
                    logger.warning("FAQ admin old draft cleanup failed: entry_id=%s", entry.id)

    def _load_draft_metadata(self, entry: CacheEntry) -> dict[str, Any]:
        path = self._draft_metadata_path(entry)
        status, content = _read_regular_content(path, MAX_DRAFT_METADATA_BYTES)
        if status is not None or content is None:
            return {}
        try:
            payload = json.loads(content.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _read_draft(
        self,
        entry: CacheEntry,
    ) -> tuple[FaqAdminDraftAudio, bytes | None]:
        payload = self._load_draft_metadata(entry)
        if not payload:
            return FaqAdminDraftAudio(status="none"), None
        wav_name = payload.get("wav_file")
        if not isinstance(wav_name, str) or wav_name != Path(wav_name).name:
            return FaqAdminDraftAudio(status="invalid"), None
        try:
            wav_path = self._pending_path(wav_name)
        except AudioConfigError:
            return FaqAdminDraftAudio(status="invalid"), None
        status, content = _read_audio_content(wav_path)
        if status is not None or content is None:
            return FaqAdminDraftAudio(status="invalid"), None
        try:
            metadata = validate_wav(content)
        except Exception:
            return FaqAdminDraftAudio(status="invalid"), None
        expected = {
            "entry_id": entry.id,
            "entry_version": entry.version,
            "answer_sha256": hashlib.sha256(entry.answer.encode("utf-8")).hexdigest(),
            "tts_model": self._profile.model,
            "tts_voice": self._profile.voice,
            "tts_speed": self._profile.speed,
            "wav_sha256": hashlib.sha256(content).hexdigest(),
            **metadata.as_dict(),
        }
        current = all(payload.get(key) == value for key, value in expected.items())
        draft_status: DraftStatus = "ready" if current and entry.enabled else "stale"
        return (
            FaqAdminDraftAudio(
                status=draft_status,
                generated_at=(payload.get("generated_at") if isinstance(payload.get("generated_at"), str) else None),
                size_bytes=len(content),
                duration_seconds=metadata.duration_seconds,
                sample_rate=metadata.sample_rate,
                channels=metadata.channels,
                sample_width=metadata.sample_width,
            ),
            content,
        )

    def _inspect_draft(self, entry: CacheEntry) -> FaqAdminDraftAudio:
        draft, _ = self._read_draft(entry)
        return draft

    def _approve_draft(self, entry_id: str) -> FaqAdminAudioAction:
        with self._file_lock:
            cache = self._load_cache()
            entry = next((item for item in cache.entries if item.id == entry_id), None)
            if entry is None:
                raise FaqAdminEntryNotFound
            draft, content = self._read_draft(entry)
            if draft.status != "ready" or content is None:
                raise FaqAdminOperationConflict("draft_unavailable")

            class DraftSynthesizer:
                def synthesize(self, _text: str) -> bytes:
                    return content  # type: ignore[return-value]

            try:
                generator = FaqAudioGenerator(cache, self._config_path, self._profile)
            except AudioConfigError as error:
                raise FaqAdminConfigError from error
            result = asyncio.run(
                generator.run(
                    DraftSynthesizer(),
                    entry_ids=(entry.id,),
                    force=True,
                )
            )
            if result.failed or result.generated != 1:
                raise FaqAdminUnavailable
            payload = self._load_draft_metadata(entry)
            wav_name = payload.get("wav_file")
            cleanup_paths = [self._draft_metadata_path(entry)]
            if isinstance(wav_name, str) and wav_name == Path(wav_name).name:
                cleanup_paths.append(self._pending_path(wav_name))
            for cleanup_path in cleanup_paths:
                try:
                    cleanup_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning(
                        "FAQ admin approved draft cleanup failed: entry_id=%s",
                        entry.id,
                    )
            return FaqAdminAudioAction(
                entry_id=entry.id,
                status="approved",
                message="语音已审批并安装，重启服务后进入运行时缓存",
            )

    @staticmethod
    def _warn(entry: CacheEntry, status: str) -> None:
        logger.warning(
            "FAQ admin audio unavailable: entry_id=%s status=%s",
            entry.id,
            status,
        )


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _read_audio_content(
    target: Path,
) -> tuple[Literal["missing", "invalid"] | None, bytes | None]:
    """Read one regular WAV target with a bounded, single-handle read."""

    return _read_regular_content(target, MAX_ADMIN_AUDIO_BYTES)


def _read_regular_content(
    target: Path,
    limit: int,
) -> tuple[Literal["missing", "invalid"] | None, bytes | None]:
    """Read one regular file through a bounded, single handle."""

    descriptor: int | None = None
    try:
        descriptor = os.open(
            os.fspath(target),
            os.O_RDONLY | getattr(os, "O_NONBLOCK", 0),
        )
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            return "invalid", None
        if file_stat.st_size > limit:
            return "invalid", None

        with os.fdopen(descriptor, "rb") as source:
            descriptor = None
            content = source.read(limit + 1)
        if len(content) > limit:
            return "invalid", None
        if len(content) != file_stat.st_size:
            return "invalid", None
        return None, content
    except FileNotFoundError:
        return "missing", None
    except (OSError, ValueError):
        return "invalid", None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    content = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _atomic_write_bytes(path, content)


def _atomic_write_cache_yaml(path: Path, document: dict[str, Any]) -> None:
    if path.is_symlink() or not path.is_file():
        raise OSError("FAQ 配置必须是普通文件")
    content = yaml.safe_dump(
        document,
        allow_unicode=True,
        sort_keys=False,
        width=1000,
    )
    descriptor, raw_temp = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".yaml",
        dir=path.parent,
    )
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        load_cache(temp_path)
        os.chmod(temp_path, stat.S_IMODE(path.stat().st_mode))
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _safe_display_text(value: str) -> str:
    """Hide absolute filesystem paths while retaining configured relative names."""

    text = os.fspath(value)
    windows = PureWindowsPath(text)
    if Path(text).is_absolute() or windows.is_absolute() or windows.drive:
        return "<hidden>"
    return text


def _entry_edit_token(entry: CacheEntry) -> str:
    payload = json.dumps(
        entry.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "FaqAdminAudioAction",
    "FaqAdminConfigError",
    "FaqAdminDeleteRequest",
    "FaqAdminEntry",
    "FaqAdminEntryAction",
    "FaqAdminEntryCreate",
    "FaqAdminEntryNotFound",
    "FaqAdminEntryUpdate",
    "FaqAdminOperationConflict",
    "FaqAdminSynthesisError",
    "FaqAdminUnavailable",
    "FaqCacheReadService",
    "FaqCacheSnapshot",
    "MAX_ADMIN_AUDIO_BYTES",
]
