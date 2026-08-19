from pathlib import Path

import pytest
import yaml

from showroom_guide.faq_cache import (
    CacheConfigError,
    CachePriority,
    load_cache,
    normalize_question,
)


def make_entry(
    entry_id: str = "example",
    *,
    aliases: list[str] | None = None,
    enabled: bool = True,
    answer: str = "固定回答",
    priority: str = "high",
) -> dict[str, object]:
    return {
        "id": entry_id,
        "title": "示例条目",
        "enabled": enabled,
        "priority": priority,
        "version": 1,
        "aliases": aliases or ["示例问题"],
        "answer": answer,
        "audio_file": "prepared_audio/example.wav",
    }


def write_config(
    tmp_path: Path,
    entries: list[dict[str, object]],
    *,
    schema_version: object = 1,
    include_schema_version: bool = True,
) -> Path:
    config: dict[str, object] = {
        "source_document": "test.md",
        "entries": entries,
    }
    if include_schema_version:
        config["schema_version"] = schema_version

    path = tmp_path / "cache.yaml"
    path.write_text(
        yaml.safe_dump(
            config,
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def test_loads_a_valid_cache_yaml(tmp_path: Path):
    path = write_config(
        tmp_path,
        [
            make_entry(
                "center_overview",
                aliases=["介绍一下调推中心"],
                answer="调推中心整体介绍",
            ),
            make_entry(
                "core_concept",
                aliases=["核心理念是什么"],
                priority="medium",
            ),
        ],
    )
    cache = load_cache(path)

    assert len(cache.entries) == 2
    assert cache.entries[0].id == "center_overview"
    assert cache.entries[0].priority == CachePriority.HIGH
    assert cache.match("介绍一下调推中心") is cache.entries[0]


def test_normalize_question_removes_chinese_punctuation_and_whitespace():
    assert normalize_question("  介绍 一下调推中心？！\n") == "介绍一下调推中心"


def test_normalize_question_handles_case_and_full_width_text():
    assert normalize_question(" ＡＩ　Why？ ") == "aiwhy"


def test_match_requires_a_normalized_complete_alias(tmp_path: Path):
    cache = load_cache(
        write_config(tmp_path, [make_entry(aliases=["介绍一下调推中心"])])
    )

    assert cache.match(" 介绍一下调推中心！ ") is cache.entries[0]
    assert cache.match("介绍一下调推") is None
    assert cache.match("整个中心介绍一下吧") is None


def test_disabled_entry_is_not_indexed_or_matched(tmp_path: Path):
    path = write_config(
        tmp_path,
        [make_entry(aliases=["停用问题"], enabled=False)],
    )

    cache = load_cache(path)

    assert cache.entries[0].enabled is False
    assert cache.match("停用问题") is None
    assert "停用问题" not in cache.alias_index


def test_dynamic_realtime_question_does_not_match_static_cache(tmp_path: Path):
    path = write_config(
        tmp_path,
        [make_entry(aliases=["介绍一下运维大屏"])],
    )

    cache = load_cache(path)

    assert cache.match("现在算力使用率是多少") is None


def test_duplicate_id_is_rejected(tmp_path: Path):
    path = write_config(tmp_path, [make_entry("same"), make_entry("same")])

    with pytest.raises(CacheConfigError, match="id 重复.*same"):
        load_cache(path)


def test_duplicate_alias_within_one_entry_is_rejected(tmp_path: Path):
    path = write_config(tmp_path, [make_entry(aliases=["你好？", "你好"])])

    with pytest.raises(CacheConfigError, match="标准化 alias 重复.*你好"):
        load_cache(path)


def test_alias_conflict_across_entries_is_rejected(tmp_path: Path):
    path = write_config(
        tmp_path,
        [make_entry("first", aliases=["你好？"]), make_entry("second", aliases=["你好"])],
    )

    with pytest.raises(CacheConfigError, match="不同条目的标准化 alias 冲突.*你好"):
        load_cache(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", ""),
        ("answer", "  "),
        ("aliases", []),
    ],
)
def test_required_fields_cannot_be_empty(
    tmp_path: Path, field: str, value: object
):
    entry = make_entry()
    entry[field] = value
    path = write_config(tmp_path, [entry])

    with pytest.raises(CacheConfigError, match=rf"entries\.0\.{field}"):
        load_cache(path)


def test_alias_that_becomes_empty_after_normalization_is_rejected(tmp_path: Path):
    path = write_config(tmp_path, [make_entry(aliases=["？！"])] )

    with pytest.raises(CacheConfigError, match="标准化后为空"):
        load_cache(path)


def test_priority_must_use_a_configured_legal_value(tmp_path: Path):
    path = write_config(tmp_path, [make_entry(priority="low")])

    with pytest.raises(CacheConfigError, match=r"entries\.0\.priority"):
        load_cache(path)


def test_schema_version_is_required(tmp_path: Path):
    path = write_config(tmp_path, [make_entry()], include_schema_version=False)

    with pytest.raises(CacheConfigError, match=r"schema_version.*required"):
        load_cache(path)


def test_unsupported_schema_version_is_rejected(tmp_path: Path):
    path = write_config(tmp_path, [make_entry()], schema_version=2)

    with pytest.raises(CacheConfigError, match=r"schema_version.*1"):
        load_cache(path)


def test_missing_configuration_file_is_rejected(tmp_path: Path):
    path = tmp_path / "missing.yaml"

    with pytest.raises(CacheConfigError, match="配置文件不存在"):
        load_cache(path)


def test_invalid_yaml_is_rejected(tmp_path: Path):
    path = tmp_path / "invalid.yaml"
    path.write_text("entries: [", encoding="utf-8")

    with pytest.raises(CacheConfigError, match="YAML 格式错误"):
        load_cache(path)


@pytest.mark.parametrize("content", ["", "schema_version: 1\nentries: []\n"])
def test_empty_configuration_is_rejected(tmp_path: Path, content: str):
    path = tmp_path / "empty.yaml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(CacheConfigError, match="(为空|entries)"):
        load_cache(path)


def test_alias_index_is_read_only(tmp_path: Path):
    path = write_config(tmp_path, [make_entry(aliases=["只读索引"])])
    cache = load_cache(path)

    with pytest.raises(TypeError):
        cache.alias_index["另一个问题"] = cache.entries[0]  # type: ignore[index]
