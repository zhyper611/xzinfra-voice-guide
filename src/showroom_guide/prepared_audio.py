"""Read-only startup storage for validated FAQ prepared audio."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from showroom_guide.faq_audio import (
    AudioConfigError,
    TtsProfile,
    assess_entry_content,
    load_manifest,
    resolve_audio_paths,
    resolve_manifest_path,
)
from showroom_guide.faq_cache import CacheEntry, FaqCache


logger = logging.getLogger(__name__)


class PreparedAudioStore:
    """Immutable in-memory audio for entries valid at application startup."""

    def __init__(
        self,
        cache: FaqCache,
        config_path: str | Path,
        profile: TtsProfile,
        *,
        manifest_path: str | Path | None = None,
    ) -> None:
        enabled_entries = tuple(entry for entry in cache.entries if entry.enabled)
        resolved_manifest = resolve_manifest_path(config_path, manifest_path)
        manifest = load_manifest(resolved_manifest)
        loaded: dict[str, bytes] = {}
        owners: dict[Path, str] = {}

        for entry in enabled_entries:
            try:
                # Resolve one entry at a time so one malformed path falls back
                # to online TTS without hiding other valid prepared audio.
                target = resolve_audio_paths(config_path, (entry,))[entry.id]
            except Exception:
                self._warn(entry, "invalid")
                continue

            previous_id = owners.get(target)
            if previous_id is not None:
                raise AudioConfigError(
                    f"audio_file 路径重复: 条目 {previous_id!r} 与条目 {entry.id!r}"
                )
            owners[target] = entry.id

            try:
                content = target.read_bytes()
                assessment = assess_entry_content(
                    entry,
                    content,
                    manifest,
                    profile,
                )
            except Exception:
                self._warn(entry, "invalid")
                continue
            if assessment.state != "valid":
                self._warn(entry, assessment.state)
                continue
            loaded[entry.id] = content

        self._audio: Mapping[str, bytes] = MappingProxyType(loaded)
        self._available_entry_ids = tuple(loaded)

    @property
    def available_entry_ids(self) -> tuple[str, ...]:
        return self._available_entry_ids

    def get(self, entry_id: str) -> bytes | None:
        return self._audio.get(entry_id)

    @staticmethod
    def _warn(entry: CacheEntry, state: str) -> None:
        # Keep startup diagnostics free of paths, answer text and settings.
        logger.warning(
            "prepared FAQ audio unavailable: entry_id=%s state=%s",
            entry.id,
            state,
        )


__all__ = ["PreparedAudioStore"]
