import string
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class CacheConfigError(ValueError):
    """Raised when the high-frequency question cache cannot be loaded."""


class CachePriority(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"


# NFKC converts common full-width punctuation to its ASCII equivalent. These
# characters remain here because they are not all converted by NFKC.
_PUNCTUATION = frozenset(string.punctuation + "，。！？；：、（）【】［］「」『』《》〈〉“”‘’—…·")


def normalize_question(text: str) -> str:
    """Return the comparison form used for both questions and aliases."""
    normalized = unicodedata.normalize("NFKC", text).lower()
    return "".join(
        character
        for character in normalized
        if not character.isspace() and character not in _PUNCTUATION
    )


class CacheEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    title: str
    enabled: bool
    priority: CachePriority
    version: int
    aliases: tuple[str, ...] = Field(min_length=1)
    answer: str
    audio_file: str

    @field_validator("id", "answer", mode="before")
    @classmethod
    def validate_required_text(cls, value: object) -> object:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("不能为空")
        return value

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, aliases: tuple[str, ...]) -> tuple[str, ...]:
        seen: dict[str, str] = {}
        for alias in aliases:
            if not alias.strip():
                raise ValueError("alias 不能为空")
            normalized = normalize_question(alias)
            if not normalized:
                raise ValueError(f"alias {alias!r} 标准化后为空")
            previous = seen.get(normalized)
            if previous is not None:
                raise ValueError(
                    f"标准化 alias 重复: {previous!r} 与 {alias!r} -> {normalized!r}"
                )
            seen[normalized] = alias
        return aliases


class CacheDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1]
    source_document: str = ""
    entries: tuple[CacheEntry, ...] = Field(min_length=1)


@dataclass(frozen=True)
class FaqCache:
    """An immutable cache configuration and its exact-match index."""

    entries: tuple[CacheEntry, ...]
    _alias_index: Mapping[str, CacheEntry] = field(repr=False)

    @property
    def alias_index(self) -> Mapping[str, CacheEntry]:
        return self._alias_index

    def match(self, question: str) -> CacheEntry | None:
        """Match a question using only its normalized complete alias."""
        normalized = normalize_question(question)
        if not normalized:
            return None
        return self._alias_index.get(normalized)


def load_cache(path: str | Path) -> FaqCache:
    """Load, validate, and index a high-frequency question cache YAML file."""
    cache_path = Path(path)
    if not cache_path.is_file():
        raise CacheConfigError(f"缓存配置文件不存在: {cache_path}")

    try:
        with cache_path.open("r", encoding="utf-8") as stream:
            raw_config = yaml.safe_load(stream)
    except yaml.YAMLError as error:
        raise CacheConfigError(f"YAML 格式错误 ({cache_path}): {error}") from error
    except (OSError, UnicodeError) as error:
        raise CacheConfigError(f"无法读取缓存配置文件 {cache_path}: {error}") from error

    if raw_config is None:
        raise CacheConfigError(f"缓存配置为空: {cache_path}")

    try:
        document = CacheDocument.model_validate(raw_config)
    except ValidationError as error:
        details = "; ".join(
            f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
            for issue in error.errors()
        )
        raise CacheConfigError(f"缓存配置校验失败 ({cache_path}): {details}") from error

    alias_index: dict[str, CacheEntry] = {}
    ids: dict[str, CacheEntry] = {}
    alias_owners: dict[str, tuple[str, str]] = {}
    for entry in document.entries:
        previous_entry = ids.get(entry.id)
        if previous_entry is not None:
            raise CacheConfigError(
                f"缓存条目 id 重复: {entry.id!r} "
                f"(条目 {previous_entry.title!r} 与 {entry.title!r})"
            )
        ids[entry.id] = entry

        for alias in entry.aliases:
            normalized_alias = normalize_question(alias)
            previous_owner = alias_owners.get(normalized_alias)
            if previous_owner is not None:
                previous_id, previous_alias = previous_owner
                raise CacheConfigError(
                    f"不同条目的标准化 alias 冲突: {normalized_alias!r} "
                    f"(条目 {previous_id!r} 的 {previous_alias!r} 与 "
                    f"条目 {entry.id!r} 的 {alias!r})"
                )
            alias_owners[normalized_alias] = (entry.id, alias)
            if entry.enabled:
                alias_index[normalized_alias] = entry

    return FaqCache(document.entries, MappingProxyType(alias_index))
