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


def _validate_rule_terms(
    terms: tuple[str, ...],
    field_name: str,
) -> tuple[str, ...]:
    seen: dict[str, str] = {}
    for term in terms:
        normalized = normalize_question(term)
        if not normalized:
            raise ValueError(f"{field_name} 词条 {term!r} 标准化后为空")
        previous = seen.get(normalized)
        if previous is not None:
            raise ValueError(
                f"{field_name} 标准化后重复: {previous!r} 与 {term!r} -> {normalized!r}"
            )
        seen[normalized] = term
    return terms


class CacheMatchRules(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    subjects: tuple[str, ...] = Field(min_length=1)
    intents: tuple[str, ...] = Field(min_length=1)
    excludes: tuple[str, ...] = ()

    @field_validator("subjects", "intents", "excludes")
    @classmethod
    def validate_terms(cls, terms: tuple[str, ...], info) -> tuple[str, ...]:
        return _validate_rule_terms(terms, info.field_name)


class CacheEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    title: str
    enabled: bool
    priority: CachePriority
    version: int
    aliases: tuple[str, ...] = Field(min_length=1)
    match_rules: CacheMatchRules | None = None
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
    rule_excludes: tuple[str, ...] = ()
    entries: tuple[CacheEntry, ...] = Field(min_length=1)

    @field_validator("rule_excludes")
    @classmethod
    def validate_rule_excludes(cls, terms: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_rule_terms(terms, "rule_excludes")


@dataclass(frozen=True)
class CompiledMatchRule:
    entry: CacheEntry
    subjects: tuple[str, ...]
    intents: tuple[str, ...]
    excludes: tuple[str, ...]


@dataclass(frozen=True)
class FaqCache:
    """An immutable cache configuration with alias and rule indexes."""

    entries: tuple[CacheEntry, ...]
    _alias_index: Mapping[str, CacheEntry] = field(repr=False)
    _rule_excludes: tuple[str, ...] = field(default=(), repr=False)
    _rule_index: tuple[CompiledMatchRule, ...] = field(default=(), repr=False)

    @property
    def alias_index(self) -> Mapping[str, CacheEntry]:
        return self._alias_index

    def match(self, question: str) -> CacheEntry | None:
        """Match by exact alias first, then by an unambiguous subject/intent rule."""
        normalized = normalize_question(question)
        if not normalized:
            return None
        exact_match = self._alias_index.get(normalized)
        if exact_match is not None:
            return exact_match
        if any(exclude in normalized for exclude in self._rule_excludes):
            return None

        matches = [
            rule.entry
            for rule in self._rule_index
            if any(subject in normalized for subject in rule.subjects)
            and any(intent in normalized for intent in rule.intents)
            and not any(exclude in normalized for exclude in rule.excludes)
        ]
        return matches[0] if len(matches) == 1 else None


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
    rule_index: list[CompiledMatchRule] = []
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

        if entry.enabled and entry.match_rules is not None:
            rule_index.append(
                CompiledMatchRule(
                    entry=entry,
                    subjects=tuple(
                        normalize_question(subject)
                        for subject in entry.match_rules.subjects
                    ),
                    intents=tuple(
                        normalize_question(intent)
                        for intent in entry.match_rules.intents
                    ),
                    excludes=tuple(
                        normalize_question(exclude)
                        for exclude in entry.match_rules.excludes
                    ),
                )
            )

    return FaqCache(
        document.entries,
        MappingProxyType(alias_index),
        tuple(normalize_question(term) for term in document.rule_excludes),
        tuple(rule_index),
    )
