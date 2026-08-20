import hashlib
import io
import json
import os
import wave
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from showroom_guide.config import Settings
from showroom_guide.faq_cache import load_cache
from showroom_guide.faq_admin import (
    MAX_ADMIN_AUDIO_BYTES,
    FaqAdminConfigError,
    FaqCacheReadService,
)
from showroom_guide.faq_audio import TtsProfile, validate_wav
from showroom_guide.main import create_app, create_runtime


PROFILE = TtsProfile(model="model-a", voice="voice-a", speed=1.0)
ADMIN_KEY = "faq-admin-test-key"


def make_wav(sample: bytes = b"\x00\x00", frames: int = 8) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(sample * frames)
    return output.getvalue()


def cache_entry(
    entry_id: str,
    *,
    audio_file: str | None = None,
    enabled: bool = True,
    priority: str = "high",
    answer: str | None = None,
) -> dict[str, object]:
    return {
        "id": entry_id,
        "title": f"Title {entry_id}",
        "enabled": enabled,
        "priority": priority,
        "version": 1,
        "aliases": [f"Question {entry_id}"],
        "answer": answer or f"Answer {entry_id}",
        "audio_file": audio_file or f"prepared_audio/{entry_id}.wav",
    }


def write_cache(
    tmp_path: Path,
    entries: list[dict[str, object]],
    *,
    source_document: str = "source.docx",
) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    path = config_dir / "faq_cache.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "source_document": source_document,
                "rule_excludes": ["当前", "实时"],
                "entries": entries,
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def manifest_record(entry: dict[str, object], content: bytes) -> dict[str, object]:
    metadata = validate_wav(content)
    return {
        "entry_id": entry["id"],
        "entry_version": entry["version"],
        "answer_sha256": hashlib.sha256(
            str(entry["answer"]).encode("utf-8")
        ).hexdigest(),
        "audio_file": entry["audio_file"],
        "tts_model": PROFILE.model,
        "tts_voice": PROFILE.voice,
        "tts_speed": PROFILE.speed,
        "wav_sha256": hashlib.sha256(content).hexdigest(),
        **metadata.as_dict(),
    }


def write_manifest(config_path: Path, records: list[dict[str, object]]) -> Path:
    path = config_path.parent / "prepared_audio" / "manifest.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        json.dumps({"entries": records}),
        encoding="utf-8",
    )
    return path


def base_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "xzkb_base_url": "http://xzkb.test",
        "xzkb_api_key": "xzkb-test-key",
        "xzkb_empty_search_response": "No result",
        "asr_base_url": "http://asr.test",
        "asr_api_key": "asr-test-key",
        "asr_model": "asr-model",
        "tts_base_url": "http://tts.test",
        "tts_api_key": "tts-test-key",
        "tts_model": PROFILE.model,
        "tts_voice": PROFILE.voice,
        "tts_speed": PROFILE.speed,
        "device_api_key": "device-test-key",
        "faq_cache_enabled": False,
        "faq_admin_enabled": True,
        "faq_admin_api_key": ADMIN_KEY,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.asyncio
async def test_snapshot_reports_all_audio_states_and_safe_fields(tmp_path: Path):
    valid = cache_entry("valid")
    valid["match_rules"] = {
        "subjects": ["主题"],
        "intents": ["介绍"],
        "excludes": ["案例"],
    }
    missing = cache_entry("missing")
    stale = cache_entry("stale")
    invalid = cache_entry("invalid")
    disabled = cache_entry("disabled", enabled=False, priority="medium")
    entries = [valid, missing, stale, invalid, disabled]
    config_path = write_cache(
        tmp_path,
        entries,
        source_document=str(tmp_path / "private-source.docx"),
    )
    valid_content = make_wav()
    (config_path.parent / valid["audio_file"]).parent.mkdir(exist_ok=True)
    (config_path.parent / valid["audio_file"]).write_bytes(valid_content)
    (config_path.parent / stale["audio_file"]).write_bytes(valid_content)
    (config_path.parent / invalid["audio_file"]).write_bytes(b"not a wav")
    write_manifest(config_path, [manifest_record(valid, valid_content)])

    snapshot = FaqCacheReadService(config_path, PROFILE).snapshot()
    payload = snapshot.model_dump(mode="json", exclude_none=True)
    states = {entry["id"]: entry["audio"] for entry in payload["entries"]}

    assert payload["schema_version"] == 1
    assert payload["source_document"] == "<hidden>"
    assert payload["rule_excludes"] == ["当前", "实时"]
    assert payload["entries"][0]["match_rules"] == {
        "subjects": ["主题"],
        "intents": ["介绍"],
        "excludes": ["案例"],
    }
    assert payload["tts_profile"] == {
        "model": "model-a",
        "voice": "voice-a",
        "speed": 1.0,
    }
    assert payload["summary"] == {
        "total": 5,
        "enabled": 4,
        "high": 4,
        "medium": 1,
        "audio_valid": 1,
        "audio_missing": 1,
        "audio_stale": 1,
        "audio_invalid": 1,
            "audio_disabled": 1,
            "draft_ready": 0,
        }
    assert states["valid"]["status"] == "valid"
    assert states["valid"]["size_bytes"] == len(valid_content)
    assert states["missing"] == {"status": "missing"}
    assert states["stale"] == {"status": "stale"}
    assert states["invalid"] == {"status": "invalid"}
    assert states["disabled"] == {"status": "disabled"}
    assert [entry["id"] for entry in payload["entries"]] == [
        "valid",
        "missing",
        "stale",
        "invalid",
        "disabled",
    ]
    assert str(tmp_path) not in json.dumps(payload, ensure_ascii=False)


def test_single_invalid_path_does_not_block_other_entries(tmp_path: Path):
    valid = cache_entry("valid")
    invalid = cache_entry("invalid", audio_file="../outside.wav")
    config_path = write_cache(tmp_path, [valid, invalid])
    content = make_wav()
    valid_path = config_path.parent / valid["audio_file"]
    valid_path.parent.mkdir(exist_ok=True)
    valid_path.write_bytes(content)
    write_manifest(config_path, [manifest_record(valid, content)])

    snapshot = FaqCacheReadService(config_path, PROFILE).snapshot()
    states = {entry.id: entry.audio.status for entry in snapshot.entries}

    assert states == {"valid": "valid", "invalid": "invalid"}


@pytest.mark.parametrize(
    "enabled_values",
    [
        (True, True),
        (False, True),
        (False, False),
    ],
)
def test_duplicate_audio_target_fails_closed(
    tmp_path: Path,
    enabled_values: tuple[bool, bool],
):
    first = cache_entry(
        "first",
        audio_file="prepared_audio/same.wav",
        enabled=enabled_values[0],
    )
    second = cache_entry(
        "second",
        audio_file="prepared_audio/same.wav",
        enabled=enabled_values[1],
    )
    config_path = write_cache(tmp_path, [first, second])

    with pytest.raises(FaqAdminConfigError):
        FaqCacheReadService(config_path, PROFILE).snapshot()


def test_duplicate_invalid_audio_target_fails_closed(tmp_path: Path):
    first = cache_entry("one", audio_file="../same.wav")
    second = cache_entry("two", audio_file="../same.wav", enabled=False)
    config_path = write_cache(tmp_path, [first, second])

    with pytest.raises(FaqAdminConfigError):
        FaqCacheReadService(config_path, PROFILE).snapshot()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "enabled_values",
    [
        (True, True),
        (False, True),
        (False, False),
    ],
)
async def test_duplicate_audio_target_returns_422(
    tmp_path: Path,
    enabled_values: tuple[bool, bool],
):
    entries = [
        cache_entry(
            "first",
            audio_file="prepared_audio/same.wav",
            enabled=enabled_values[0],
        ),
        cache_entry(
            "second",
            audio_file="prepared_audio/same.wav",
            enabled=enabled_values[1],
        ),
    ]
    config_path = write_cache(tmp_path, entries)
    runtime = create_runtime(
        base_settings(
            faq_cache_file=config_path,
            faq_cache_enabled=False,
        )
    )
    app = create_app(runtime)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.get(
                "/api/faq-cache",
                headers={"X-FAQ-Admin-Key": ADMIN_KEY},
            )
        assert response.status_code == 422
        assert response.json() == {"detail": "高频问答配置校验失败"}
        assert str(tmp_path) not in response.text
    finally:
        await runtime.aclose()


def test_disabled_entry_does_not_read_wav(tmp_path: Path, monkeypatch):
    entry = cache_entry("disabled", enabled=False)
    config_path = write_cache(tmp_path, [entry])
    reads: list[Path] = []

    def fail_read(path: Path):
        reads.append(path)
        raise AssertionError("disabled audio must not be read")

    monkeypatch.setattr("showroom_guide.faq_admin._read_audio_content", fail_read)

    snapshot = FaqCacheReadService(config_path, PROFILE).snapshot()

    assert snapshot.entries[0].audio.status == "disabled"
    assert reads == []


def test_each_wav_is_opened_and_read_once(tmp_path: Path, monkeypatch):
    entry = cache_entry("one")
    config_path = write_cache(tmp_path, [entry])
    content = make_wav()
    target = config_path.parent / entry["audio_file"]
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(content)
    write_manifest(config_path, [manifest_record(entry, content)])
    reads: list[Path] = []
    original_open = os.open

    def open_file(path, flags, mode=0o777, *, dir_fd=None):
        if isinstance(path, (str, bytes, os.PathLike)) and Path(path) == target:
            reads.append(Path(path))
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", open_file)

    snapshot = FaqCacheReadService(config_path, PROFILE).snapshot()

    assert snapshot.entries[0].audio.status == "valid"
    assert reads == [target]


def test_audio_over_limit_is_invalid_without_affecting_valid_entry(tmp_path: Path):
    oversized = cache_entry("oversized")
    valid = cache_entry("valid")
    config_path = write_cache(tmp_path, [oversized, valid])
    oversized_path = config_path.parent / oversized["audio_file"]
    valid_path = config_path.parent / valid["audio_file"]
    oversized_path.parent.mkdir(exist_ok=True)
    valid_path.parent.mkdir(exist_ok=True)
    with oversized_path.open("wb") as output:
        output.truncate(MAX_ADMIN_AUDIO_BYTES + 1)
    valid_content = make_wav()
    valid_path.write_bytes(valid_content)
    write_manifest(config_path, [manifest_record(valid, valid_content)])

    snapshot = FaqCacheReadService(config_path, PROFILE).snapshot()
    states = {entry.id: entry.audio.status for entry in snapshot.entries}

    assert states == {"oversized": "invalid", "valid": "valid"}


def test_special_file_is_invalid_without_blocking(tmp_path: Path):
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO is not available on this platform")
    entry = cache_entry("special", audio_file="prepared_audio/special.wav")
    config_path = write_cache(tmp_path, [entry])
    special_path = config_path.parent / entry["audio_file"]
    special_path.parent.mkdir(exist_ok=True)
    os.mkfifo(special_path)

    snapshot = FaqCacheReadService(config_path, PROFILE).snapshot()

    assert snapshot.entries[0].audio.status == "invalid"


def test_second_snapshot_reloads_yaml(tmp_path: Path):
    entry = cache_entry("one")
    config_path = write_cache(tmp_path, [entry], source_document="first.docx")
    first = FaqCacheReadService(config_path, PROFILE).snapshot()
    changed = dict(entry)
    changed["title"] = "Changed title"
    write_cache(tmp_path, [changed], source_document="second.docx")

    second = FaqCacheReadService(config_path, PROFILE).snapshot()

    assert first.source_document == "first.docx"
    assert second.source_document == "second.docx"
    assert second.entries[0].title == "Changed title"


def test_settings_default_admin_is_disabled():
    settings = base_settings(faq_admin_enabled=False, faq_admin_api_key=None)

    assert settings.faq_admin_enabled is False
    assert settings.faq_admin_api_key is None


def test_settings_rejects_enabled_admin_without_long_key():
    with pytest.raises(ValidationError, match="faq_admin_api_key"):
        base_settings(faq_admin_api_key="too-short")


def test_settings_reads_admin_environment(monkeypatch):
    values = {
        "GUIDE_XZKB_BASE_URL": "http://xzkb.test",
        "GUIDE_XZKB_API_KEY": "xzkb-test-key",
        "GUIDE_XZKB_EMPTY_SEARCH_RESPONSE": "No result",
        "GUIDE_ASR_BASE_URL": "http://asr.test",
        "GUIDE_ASR_API_KEY": "asr-test-key",
        "GUIDE_ASR_MODEL": "asr-model",
        "GUIDE_TTS_BASE_URL": "http://tts.test",
        "GUIDE_TTS_API_KEY": "tts-test-key",
        "GUIDE_TTS_MODEL": "model-a",
        "GUIDE_DEVICE_API_KEY": "device-test-key",
        "GUIDE_FAQ_ADMIN_ENABLED": "true",
        "GUIDE_FAQ_ADMIN_API_KEY": ADMIN_KEY,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    settings = Settings(_env_file=None)

    assert settings.faq_admin_enabled is True
    assert settings.faq_admin_api_key is not None
    assert settings.faq_admin_api_key.get_secret_value() == ADMIN_KEY


@pytest.mark.asyncio
async def test_admin_disabled_builds_no_service_and_returns_404(
    monkeypatch,
    tmp_path: Path,
):
    def fail_service(*_args, **_kwargs):
        raise AssertionError("admin service must not be built")

    monkeypatch.setattr("showroom_guide.main.FaqCacheReadService", fail_service)
    settings = base_settings(
        faq_admin_enabled=False,
        faq_admin_api_key=None,
        faq_cache_file=tmp_path / "missing.yaml",
    )
    runtime = create_runtime(settings)
    app = create_app(runtime)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.get(
                "/api/faq-cache",
                headers={"X-FAQ-Admin-Key": ADMIN_KEY},
            )
            page = await client.get("/faq-cache")
        assert runtime.faq_admin_service is None
        assert response.status_code == 404
        assert page.status_code == 200
        assert page.headers["cache-control"] == "no-store"
        assert "高频问答缓存" in page.text
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_admin_api_auth_snapshot_and_no_store(tmp_path: Path, monkeypatch):
    entry = cache_entry("one")
    config_path = write_cache(tmp_path, [entry])
    runtime = create_runtime(
        base_settings(
            faq_cache_file=config_path,
            faq_cache_enabled=False,
        )
    )
    app = create_app(runtime)
    threadpool_calls: list[object] = []

    async def fake_run_in_threadpool(func, *args, **kwargs):
        threadpool_calls.append(func)
        return func(*args, **kwargs)

    monkeypatch.setattr("showroom_guide.main.run_in_threadpool", fake_run_in_threadpool)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            missing = await client.get("/api/faq-cache")
            wrong = await client.get(
                "/api/faq-cache",
                headers={"X-FAQ-Admin-Key": "wrong-key"},
            )
            correct = await client.get(
                "/api/faq-cache",
                headers={"X-FAQ-Admin-Key": ADMIN_KEY},
            )

        assert missing.status_code == 401
        assert wrong.status_code == 401
        assert correct.status_code == 200
        assert len(threadpool_calls) == 1
        assert missing.headers["cache-control"] == "no-store"
        assert wrong.headers["cache-control"] == "no-store"
        assert correct.headers["cache-control"] == "no-store"
        payload = correct.json()
        assert payload["entries"][0]["match_rules"] is None
        assert payload["reload_policy"] == "restart_required"
        body = correct.text
        assert ADMIN_KEY not in body
        assert str(tmp_path) not in body
        assert "wav_sha256" not in body
        assert "WAVE" not in body
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("file_state", "expected_status", "expected_detail"),
    [
        ("missing", 503, "高频问答配置暂时无法读取"),
        ("invalid", 422, "高频问答配置校验失败"),
    ],
)
async def test_admin_api_safe_storage_errors(
    tmp_path: Path,
    file_state: str,
    expected_status: int,
    expected_detail: str,
):
    config_path = tmp_path / "config" / "faq_cache.yaml"
    config_path.parent.mkdir()
    if file_state == "invalid":
        config_path.write_text("schema_version: 2\nentries: []\n", encoding="utf-8")
    runtime = create_runtime(
        base_settings(
            faq_cache_file=config_path,
            faq_cache_enabled=False,
        )
    )
    app = create_app(runtime)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.get(
                "/api/faq-cache",
                headers={"X-FAQ-Admin-Key": ADMIN_KEY},
            )
        assert response.status_code == expected_status
        assert response.json() == {"detail": expected_detail}
        assert str(tmp_path) not in response.text
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_admin_is_independent_from_runtime_faq_matching(tmp_path: Path):
    config_path = write_cache(tmp_path, [cache_entry("one")])
    runtime = create_runtime(
        base_settings(
            faq_cache_file=config_path,
            faq_cache_enabled=False,
            faq_admin_enabled=True,
        )
    )

    try:
        assert runtime.faq_cache is None
        assert runtime.faq_admin_service is not None
        snapshot = runtime.faq_admin_service.snapshot()
        assert snapshot.entries[0].id == "one"
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_generate_preview_and_approve_audio_workflow(tmp_path: Path):
    entry = cache_entry("one")
    config_path = write_cache(tmp_path, [entry])
    runtime = create_runtime(
        base_settings(
            faq_cache_file=config_path,
            faq_cache_enabled=False,
        )
    )
    audio = make_wav(frames=320)
    runtime.speech.synthesize = AsyncMock(return_value=audio)
    app = create_app(runtime)
    headers = {"X-FAQ-Admin-Key": ADMIN_KEY}
    active_path = config_path.parent / entry["audio_file"]

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            unauthorized = await client.post(
                "/api/faq-cache/one/audio/generate"
            )
            generated = await client.post(
                "/api/faq-cache/one/audio/generate",
                headers=headers,
            )
            snapshot = await client.get("/api/faq-cache", headers=headers)
            draft = await client.get(
                "/api/faq-cache/one/audio?source=draft",
                headers=headers,
            )
            active_before = await client.get(
                "/api/faq-cache/one/audio?source=active",
                headers=headers,
            )
            active_exists_before = active_path.exists()
            approved = await client.post(
                "/api/faq-cache/one/audio/approve",
                headers=headers,
            )
            active_after = await client.get(
                "/api/faq-cache/one/audio?source=active",
                headers=headers,
            )
            draft_after = await client.get(
                "/api/faq-cache/one/audio?source=draft",
                headers=headers,
            )
            final_snapshot = await client.get("/api/faq-cache", headers=headers)

        assert unauthorized.status_code == 401
        assert generated.status_code == 200
        assert generated.json()["status"] == "draft_ready"
        assert active_exists_before is False
        runtime.speech.synthesize.assert_awaited_once_with(entry["answer"])
        entry_snapshot = snapshot.json()["entries"][0]
        assert entry_snapshot["draft_audio"]["status"] == "ready"
        assert snapshot.json()["summary"]["draft_ready"] == 1
        assert draft.status_code == 200
        assert draft.headers["content-type"] == "audio/wav"
        assert draft.content == audio
        assert active_before.status_code == 404
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"
        assert active_path.read_bytes() == audio
        assert active_after.content == audio
        assert draft_after.status_code == 404
        assert final_snapshot.json()["entries"][0]["audio"]["status"] == "valid"
        assert final_snapshot.json()["entries"][0]["draft_audio"]["status"] == "none"
        assert (config_path.parent / "prepared_audio" / "manifest.json").is_file()
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_disabled_entry_cannot_generate_audio(tmp_path: Path):
    config_path = write_cache(tmp_path, [cache_entry("disabled", enabled=False)])
    runtime = create_runtime(
        base_settings(faq_cache_file=config_path, faq_cache_enabled=False)
    )
    runtime.speech.synthesize = AsyncMock(return_value=make_wav())
    app = create_app(runtime)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/faq-cache/disabled/audio/generate",
                headers={"X-FAQ-Admin-Key": ADMIN_KEY},
            )
        assert response.status_code == 409
        runtime.speech.synthesize.assert_not_awaited()
    finally:
        await runtime.aclose()


def admin_entry_payload(
    *,
    entry_id: str | None = None,
    title: str = "新条目",
    answer: str = "这是新的固定回答。",
    aliases: list[str] | None = None,
    expected_edit_token: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "title": title,
        "enabled": False,
        "priority": "medium",
        "aliases": aliases or ["介绍新条目"],
        "match_rules": {
            "subjects": ["新条目"],
            "intents": ["介绍"],
            "excludes": ["最新"],
        },
        "answer": answer,
    }
    if entry_id is not None:
        payload["id"] = entry_id
    if expected_edit_token is not None:
        payload["expected_edit_token"] = expected_edit_token
    return payload


@pytest.mark.asyncio
async def test_admin_entry_create_update_conflict_and_delete_workflow(tmp_path: Path):
    config_path = write_cache(
        tmp_path,
        [cache_entry("one"), cache_entry("two")],
        source_document="source.md",
    )
    runtime = create_runtime(
        base_settings(faq_cache_file=config_path, faq_cache_enabled=False)
    )
    app = create_app(runtime)
    headers = {"X-FAQ-Admin-Key": ADMIN_KEY}

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            created = await client.post(
                "/api/faq-cache/entries",
                headers=headers,
                json=admin_entry_payload(entry_id="new_entry"),
            )
            after_create = await client.get("/api/faq-cache", headers=headers)
            created_entry = next(
                entry
                for entry in after_create.json()["entries"]
                if entry["id"] == "new_entry"
            )
            first_token = created_entry["edit_token"]
            title_update = await client.put(
                "/api/faq-cache/new_entry",
                headers=headers,
                json=admin_entry_payload(
                    title="更新后的标题",
                    expected_edit_token=first_token,
                ),
            )
            after_title = await client.get("/api/faq-cache", headers=headers)
            title_entry = next(
                entry
                for entry in after_title.json()["entries"]
                if entry["id"] == "new_entry"
            )
            second_token = title_entry["edit_token"]
            answer_update = await client.put(
                "/api/faq-cache/new_entry",
                headers=headers,
                json=admin_entry_payload(
                    title="更新后的标题",
                    answer="修改后的固定回答。",
                    expected_edit_token=second_token,
                ),
            )
            stale_update = await client.put(
                "/api/faq-cache/new_entry",
                headers=headers,
                json=admin_entry_payload(
                    title="过期页面提交",
                    expected_edit_token=first_token,
                ),
            )
            after_answer = await client.get("/api/faq-cache", headers=headers)
            answer_entry = next(
                entry
                for entry in after_answer.json()["entries"]
                if entry["id"] == "new_entry"
            )
            active_path = config_path.parent / answer_entry["audio_file"]
            active_path.parent.mkdir(exist_ok=True)
            active_path.write_bytes(make_wav())
            deleted = await client.request(
                "DELETE",
                "/api/faq-cache/new_entry",
                headers=headers,
                json={"expected_edit_token": answer_entry["edit_token"]},
            )
            final_snapshot = await client.get("/api/faq-cache", headers=headers)

        assert created.status_code == 200
        assert created.json() == {
            "entry_id": "new_entry",
            "status": "created",
            "version": 1,
            "message": "高频问答条目已创建，重启服务后进入运行时缓存",
        }
        assert created_entry["audio_file"] == "prepared_audio/new_entry.wav"
        assert created_entry["enabled"] is False
        assert title_update.status_code == 200
        assert title_update.json()["version"] == 1
        assert first_token != second_token
        assert answer_update.status_code == 200
        assert answer_update.json()["version"] == 2
        assert stale_update.status_code == 409
        assert deleted.status_code == 200
        assert deleted.json()["status"] == "deleted"
        assert active_path.is_file()
        assert [entry["id"] for entry in final_snapshot.json()["entries"]] == [
            "one",
            "two",
        ]
        persisted = load_cache(config_path)
        assert persisted.source_document == "source.md"
        assert persisted.rule_excludes == ("当前", "实时")
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_admin_entry_rejects_alias_conflict_without_changing_yaml(tmp_path: Path):
    existing = cache_entry("one")
    config_path = write_cache(tmp_path, [existing])
    original = config_path.read_bytes()
    runtime = create_runtime(
        base_settings(faq_cache_file=config_path, faq_cache_enabled=False)
    )
    app = create_app(runtime)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/faq-cache/entries",
                headers={"X-FAQ-Admin-Key": ADMIN_KEY},
                json=admin_entry_payload(
                    entry_id="conflict",
                    aliases=[existing["aliases"][0]],
                ),
            )
        assert response.status_code == 422
        assert config_path.read_bytes() == original
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_admin_entry_atomic_replace_failure_preserves_yaml(
    tmp_path: Path,
    monkeypatch,
):
    config_path = write_cache(tmp_path, [cache_entry("one")])
    original = config_path.read_bytes()
    runtime = create_runtime(
        base_settings(faq_cache_file=config_path, faq_cache_enabled=False)
    )
    app = create_app(runtime)
    original_replace = os.replace

    def fail_config_replace(source, target):
        if Path(target) == config_path:
            raise OSError("replace failed")
        return original_replace(source, target)

    monkeypatch.setattr("showroom_guide.faq_admin.os.replace", fail_config_replace)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            snapshot = await client.get(
                "/api/faq-cache",
                headers={"X-FAQ-Admin-Key": ADMIN_KEY},
            )
            entry = snapshot.json()["entries"][0]
            response = await client.put(
                "/api/faq-cache/one",
                headers={"X-FAQ-Admin-Key": ADMIN_KEY},
                json=admin_entry_payload(
                    title="不会保存",
                    aliases=entry["aliases"],
                    answer=entry["answer"],
                    expected_edit_token=entry["edit_token"],
                ),
            )
        assert response.status_code == 503
        assert response.headers["cache-control"] == "no-store"
        assert config_path.read_bytes() == original
    finally:
        await runtime.aclose()
