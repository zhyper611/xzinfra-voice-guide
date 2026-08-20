import io
import wave
from pathlib import Path

import pytest
import yaml

from showroom_guide.faq_audio import AudioConfigError, FaqAudioGenerator, TtsProfile
from showroom_guide.faq_cache import load_cache
from showroom_guide.prepared_audio import PreparedAudioStore


PROFILE = TtsProfile(model="model-a", voice="voice-a", speed=1.0)


def make_wav(*, frames: int = 80, sample: bytes = b"\x00\x00") -> bytes:
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
    enabled: bool = True,
    version: int = 1,
    answer: str | None = None,
) -> dict[str, object]:
    return {
        "id": entry_id,
        "title": entry_id,
        "enabled": enabled,
        "priority": "high",
        "version": version,
        "aliases": [f"问题 {entry_id}"],
        "answer": answer or f"回答 {entry_id}",
        "audio_file": f"prepared_audio/{entry_id}.wav",
    }


def write_config(tmp_path: Path, entries: list[dict[str, object]]) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    path = config_dir / "faq_cache.yaml"
    path.write_text(
        yaml.safe_dump(
            {"schema_version": 1, "entries": entries},
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


class FakeSynthesizer:
    def __init__(self, audio: bytes) -> None:
        self.audio = audio

    async def synthesize(self, _text: str) -> bytes:
        return self.audio


async def generate_one(
    config_path: Path,
    entry_id: str,
    audio: bytes | None = None,
) -> tuple[Path, bytes]:
    cache = load_cache(config_path)
    generator = FaqAudioGenerator(cache, config_path, PROFILE)
    generated = audio or make_wav()
    result = await generator.run(
        FakeSynthesizer(generated),
        entry_ids=[entry_id],
    )
    assert result.generated == 1
    return generator.audio_paths[entry_id], generated


def make_store(config_path: Path, profile: TtsProfile = PROFILE) -> PreparedAudioStore:
    return PreparedAudioStore(load_cache(config_path), config_path, profile)


@pytest.mark.asyncio
async def test_valid_manifest_and_wav_load_into_read_only_store(tmp_path: Path):
    config_path = write_config(tmp_path, [cache_entry("one")])
    wav_path, expected = await generate_one(config_path, "one")

    store = make_store(config_path)

    assert store.available_entry_ids == ("one",)
    assert store.get("one") == expected == wav_path.read_bytes()
    assert store.get("missing") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["manifest", "missing", "corrupt", "hash"])
async def test_invalid_prepared_audio_is_skipped_with_warning(
    caplog,
    tmp_path: Path,
    failure: str,
):
    config_path = write_config(tmp_path, [cache_entry("one")])
    wav_path, _ = await generate_one(config_path, "one")
    manifest_path = config_path.parent / "prepared_audio" / "manifest.json"
    if failure == "manifest":
        manifest_path.unlink()
    elif failure == "missing":
        wav_path.unlink()
    elif failure == "corrupt":
        wav_path.write_bytes(b"not a wav")
    else:
        wav_path.write_bytes(make_wav(sample=b"\x01\x00"))

    with caplog.at_level("WARNING", logger="showroom_guide.prepared_audio"):
        store = make_store(config_path)

    assert store.available_entry_ids == ()
    assert store.get("one") is None
    assert "entry_id=one" in caplog.text
    assert str(tmp_path) not in caplog.text
    assert "回答 one" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "profile"),
    [
        ("answer", PROFILE),
        ("version", PROFILE),
        ("profile", TtsProfile(model="model-b", voice="voice-a", speed=1.0)),
    ],
)
async def test_answer_version_and_profile_changes_make_audio_stale(
    tmp_path: Path,
    change: str,
    profile: TtsProfile,
):
    original = cache_entry("one")
    config_path = write_config(tmp_path, [original])
    await generate_one(config_path, "one")

    changed = dict(original)
    if change == "answer":
        changed["answer"] = "回答已经变化"
    elif change == "version":
        changed["version"] = 2
    config_path.write_text(
        yaml.safe_dump(
            {"schema_version": 1, "entries": [changed]},
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    store = make_store(config_path, profile)

    assert store.available_entry_ids == ()


@pytest.mark.asyncio
async def test_one_invalid_entry_does_not_block_another_valid_entry(tmp_path: Path):
    config_path = write_config(
        tmp_path,
        [cache_entry("valid"), cache_entry("missing")],
    )
    await generate_one(config_path, "valid")

    store = make_store(config_path)

    assert store.available_entry_ids == ("valid",)
    assert store.get("valid") is not None
    assert store.get("missing") is None


@pytest.mark.asyncio
async def test_invalid_audio_path_does_not_block_valid_entry(
    caplog,
    tmp_path: Path,
):
    config_path = write_config(tmp_path, [cache_entry("valid")])
    await generate_one(config_path, "valid")
    config_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "entries": [
                    cache_entry("valid"),
                    {
                        **cache_entry("invalid"),
                        "audio_file": "../outside.wav",
                    },
                ],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with caplog.at_level("WARNING", logger="showroom_guide.prepared_audio"):
        store = make_store(config_path)

    assert store.available_entry_ids == ("valid",)
    assert store.get("valid") is not None
    assert store.get("invalid") is None
    assert "entry_id=invalid" in caplog.text
    assert "outside.wav" not in caplog.text
    assert str(tmp_path) not in caplog.text


def test_duplicate_audio_paths_fail_closed(tmp_path: Path):
    first = cache_entry("first")
    second = {
        **cache_entry("second"),
        "audio_file": first["audio_file"],
    }
    config_path = write_config(tmp_path, [first, second])

    with pytest.raises(AudioConfigError, match="audio_file 路径重复.*first.*second"):
        make_store(config_path)


@pytest.mark.asyncio
async def test_store_keeps_the_single_read_bytes_when_file_changes_after_read(
    monkeypatch,
    tmp_path: Path,
):
    config_path = write_config(tmp_path, [cache_entry("one")])
    wav_path, original = await generate_one(config_path, "one")
    replacement = make_wav(sample=b"\x01\x00")
    original_read_bytes = Path.read_bytes
    reads = 0

    def read_once_then_replace(path: Path) -> bytes:
        nonlocal reads
        content = original_read_bytes(path)
        if path == wav_path:
            reads += 1
            path.write_bytes(replacement)
        return content

    monkeypatch.setattr(Path, "read_bytes", read_once_then_replace)

    store = make_store(config_path)

    assert reads == 1
    assert store.get("one") == original
    assert store.get("one") != replacement


@pytest.mark.asyncio
async def test_disabled_entry_is_not_loaded(tmp_path: Path):
    config_path = write_config(
        tmp_path,
        [cache_entry("valid"), cache_entry("disabled", enabled=False)],
    )
    await generate_one(config_path, "valid")

    store = make_store(config_path)

    assert store.available_entry_ids == ("valid",)
    assert store.get("disabled") is None
