import io
import wave
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from showroom_guide.audio_store import AudioStore
from showroom_guide.clients.xzkb import ChatStreamEvent
from showroom_guide.controller import GuideController
from showroom_guide.device import DeviceVoiceSession
from showroom_guide.faq_cache import CacheConfigError, load_cache
from showroom_guide.latency import DeviceLatencyRecorder
from showroom_guide.state import GuideStateStore


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FORMAL_CACHE = PROJECT_ROOT / "config" / "faq_cache.yaml"


def make_entry(
    entry_id: str = "example",
    *,
    aliases: list[str] | None = None,
    enabled: bool = True,
    match_rules: dict[str, object] | None = None,
    include_match_rules: bool = False,
) -> dict[str, object]:
    entry: dict[str, object] = {
        "id": entry_id,
        "title": "测试条目",
        "enabled": enabled,
        "priority": "high",
        "version": 1,
        "aliases": aliases or ["固定问题"],
        "answer": "固定回答",
        "audio_file": "prepared_audio/example.wav",
    }
    if include_match_rules:
        entry["match_rules"] = match_rules
    return entry


def write_config(
    tmp_path: Path,
    entries: list[dict[str, object]],
    *,
    rule_excludes: list[str] | None = None,
    include_rule_excludes: bool = False,
) -> Path:
    config: dict[str, object] = {
        "schema_version": 1,
        "source_document": "test.md",
        "entries": entries,
    }
    if include_rule_excludes:
        config["rule_excludes"] = rule_excludes
    path = tmp_path / "faq_cache.yaml"
    path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def rules(
    subjects: list[str] | None = None,
    intents: list[str] | None = None,
    excludes: list[str] | None = None,
) -> dict[str, object]:
    match_rules: dict[str, object] = {
        "subjects": subjects if subjects is not None else ["八大车间"],
        "intents": intents if intents is not None else ["介绍"],
    }
    if excludes is not None:
        match_rules["excludes"] = excludes
    return match_rules


def test_old_config_without_rule_fields_still_loads(tmp_path: Path):
    cache = load_cache(write_config(tmp_path, [make_entry()]))

    assert cache.match("固定问题") is not None
    assert cache.match("八大车间介绍") is None


def test_exact_alias_has_priority_over_rule_excludes(tmp_path: Path):
    path = write_config(
        tmp_path,
        [
            make_entry(
                aliases=["现在介绍八大车间"],
                match_rules=rules(),
                include_match_rules=True,
            )
        ],
        rule_excludes=["现在"],
        include_rule_excludes=True,
    )

    cache = load_cache(path)

    assert cache.match("现在介绍八大车间").id == "example"


def test_exact_alias_has_priority_over_entry_rule_excludes(tmp_path: Path):
    path = write_config(
        tmp_path,
        [
            make_entry(
                aliases=["八大车间有哪些案例"],
                match_rules=rules(excludes=["案例"]),
                include_match_rules=True,
            )
        ],
    )

    cache = load_cache(path)

    assert cache.match("八大车间有哪些案例").id == "example"


def test_subject_and_intent_must_both_match(tmp_path: Path):
    cache = load_cache(
        write_config(
            tmp_path,
            [
                make_entry(
                    match_rules=rules(),
                    include_match_rules=True,
                )
            ],
        )
    )

    assert cache.match("请介绍八大车间") is not None
    assert cache.match("八大车间") is None
    assert cache.match("请介绍一下") is None


def test_rule_exclude_blocks_rule_match(tmp_path: Path):
    cache = load_cache(
        write_config(
            tmp_path,
            [make_entry(match_rules=rules(), include_match_rules=True)],
            rule_excludes=["实时", "今天", "现在"],
            include_rule_excludes=True,
        )
    )

    assert cache.match("八大车间今天哪个使用率最高") is None
    assert cache.match("现在介绍八大车间") is None


def test_entry_rule_excludes_block_only_that_entry(tmp_path: Path):
    cache = load_cache(
        write_config(
            tmp_path,
            [
                make_entry(
                    match_rules=rules(excludes=["案例"]),
                    include_match_rules=True,
                )
            ],
        )
    )

    assert cache.match("请介绍八大车间的案例") is None


def test_entry_rule_excludes_are_normalized(tmp_path: Path):
    cache = load_cache(
        write_config(
            tmp_path,
            [
                make_entry(
                    match_rules=rules(excludes=["案 例"]),
                    include_match_rules=True,
                )
            ],
        )
    )

    assert cache.match("请介绍八大车间案！例") is None


def test_disabled_entry_does_not_participate_in_rule_matching(tmp_path: Path):
    cache = load_cache(
        write_config(
            tmp_path,
            [
                make_entry(
                    enabled=False,
                    match_rules=rules(),
                    include_match_rules=True,
                )
            ],
        )
    )

    assert cache.match("请介绍八大车间") is None


def test_multiple_rule_matches_fall_back_to_knowledge_base(tmp_path: Path):
    cache = load_cache(
        write_config(
            tmp_path,
            [
                make_entry(
                    "first",
                    match_rules=rules(),
                    include_match_rules=True,
                ),
                make_entry(
                    "second",
                    aliases=["另一个固定问题"],
                    match_rules=rules(subjects=["八大车间", "AI 工厂"]),
                    include_match_rules=True,
                ),
            ],
        )
    )

    assert cache.match("请介绍八大车间和 AI 工厂") is None


@pytest.mark.parametrize(
    ("match_rules", "location"),
    [
        (rules(subjects=[]), "entries.0.match_rules.subjects"),
        (rules(intents=[]), "entries.0.match_rules.intents"),
        (rules(subjects=["？！"]), "entries.0.match_rules.subjects"),
        (
            rules(subjects=["八大车间", "八大车间！"]),
            "entries.0.match_rules.subjects",
        ),
        (rules(intents=["介绍", "介绍！"]), "entries.0.match_rules.intents"),
        (rules(excludes=["？！"]), "entries.0.match_rules.excludes"),
        (
            rules(excludes=["案例", "案例！"]),
            "entries.0.match_rules.excludes",
        ),
    ],
)
def test_invalid_match_rules_are_rejected(
    tmp_path: Path,
    match_rules: dict[str, object],
    location: str,
):
    path = write_config(
        tmp_path,
        [
            make_entry(
                match_rules=match_rules,
                include_match_rules=True,
            )
        ],
    )

    with pytest.raises(CacheConfigError, match=location.replace(".", r"\.")):
        load_cache(path)


@pytest.mark.parametrize(
    "rule_excludes",
    [
        ["？！"],
        ["当前", "当前！"],
    ],
)
def test_invalid_rule_excludes_are_rejected(
    tmp_path: Path,
    rule_excludes: list[str],
):
    path = write_config(
        tmp_path,
        [make_entry()],
        rule_excludes=rule_excludes,
        include_rule_excludes=True,
    )

    with pytest.raises(CacheConfigError, match="rule_excludes"):
        load_cache(path)


def test_rule_matching_normalizes_punctuation_spaces_case_and_width(tmp_path: Path):
    cache = load_cache(
        write_config(
            tmp_path,
            [
                make_entry(
                    match_rules=rules(subjects=["AI 工厂车间"], intents=["介绍"]),
                    include_match_rules=True,
                )
            ],
        )
    )

    assert cache.match("请 介 绍 ＡＩ　工厂车间！") is not None


@pytest.mark.parametrize(
    "question",
    [
        "介绍八大车间",
        "请介绍八大车间",
        "给我讲讲八个车间",
        "请讲一讲 AI 工厂的车间",
        "我想了解八大车间",
        "AI 工厂都有哪些车间",
        "说说八个车间分别做什么",
        "AI 工厂的车间是怎么组成的",
    ],
)
def test_formal_eight_workshops_rule_positive_questions_match(question: str):
    cache = load_cache(FORMAL_CACHE)

    entry = cache.match(question)

    assert entry is not None
    assert entry.id == "eight_workshops_overview"


@pytest.mark.parametrize(
    "question",
    [
        "介绍 AI 工厂",
        "我想了解 AI 工厂",
        "介绍 AI 工厂的能力",
        "八大车间",
        "介绍一下",
        "八大车间今天哪个使用率最高",
        "现在介绍八大车间",
        "八大车间和行业案例有什么区别",
        "把八大车间总结成一句话",
        "用英文介绍八大车间",
        "重新介绍八大车间并加入最新数据",
        "这个车间是做什么的",
        "八大车间有哪些案例",
        "八大车间的介绍视频在哪里",
        "给我看看八大车间的图片",
        "不要介绍八大车间",
        "不用讲八大车间",
    ],
)
def test_formal_eight_workshops_rule_negative_questions_fall_back(question: str):
    cache = load_cache(FORMAL_CACHE)

    assert cache.match(question) is None


class CountingGate:
    def __init__(self) -> None:
        self.calls = 0

    @asynccontextmanager
    async def slot(self, on_wait=None):
        self.calls += 1
        yield


@pytest.mark.asyncio
async def test_rule_hit_reuses_controller_cache_path_without_xzkb(tmp_path: Path):
    path = write_config(
        tmp_path,
        [make_entry(match_rules=rules(), include_match_rules=True)],
    )
    xzkb = MagicMock()
    xzkb_gate = CountingGate()
    speech = AsyncMock()
    speech.synthesize.return_value = b"wav"
    controller = GuideController(
        GuideStateStore(),
        xzkb,
        speech,
        xzkb_gate=xzkb_gate,
        faq_cache=load_cache(path),
    )

    result = await controller.ask_text("请介绍八大车间")

    assert result.answer == "固定回答"
    xzkb.stream_chat.assert_not_called()
    assert xzkb_gate.calls == 0
    speech.synthesize.assert_awaited_once_with("固定回答")


def make_wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * 160)
    return output.getvalue()


@pytest.mark.asyncio
async def test_rule_hit_device_timing_leaves_xzkb_fields_null(tmp_path: Path):
    path = write_config(
        tmp_path,
        [make_entry(match_rules=rules(), include_match_rules=True)],
    )
    state = GuideStateStore()
    xzkb_gate = CountingGate()
    speech = AsyncMock()
    speech.transcribe.return_value = "请介绍八大车间"
    speech.synthesize.return_value = make_wav()
    controller = GuideController(
        state,
        MagicMock(),
        speech,
        xzkb_gate=xzkb_gate,
        faq_cache=load_cache(path),
    )
    recorder = DeviceLatencyRecorder()
    session = DeviceVoiceSession(
        state=state,
        controller=controller,
        speech=speech,
        audio=AudioStore(),
        metrics=recorder,
    )

    await session.process_wav(make_wav())
    latest = recorder.snapshot()["latest"]

    assert xzkb_gate.calls == 0
    for name in (
        "xzkb_queue_ms",
        "xzkb_headers_ms",
        "xzkb_first_sse_ms",
        "xzkb_first_content_ms",
        "xzkb_ttft_ms",
        "xzkb_generation_ms",
        "xzkb_total_ms",
    ):
        assert latest[name] is None
    assert latest["tts_queue_ms"] is not None
    assert latest["tts_synthesis_ms"] is not None
    assert latest["server_pipeline_total_ms"] is not None
