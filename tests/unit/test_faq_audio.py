import asyncio
import io
import json
import os
import struct
import wave
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel, Field

from showroom_guide import faq_audio
from showroom_guide.faq_audio import (
    AudioConfigError,
    FaqAudioGenerator,
    TtsProfile,
    WavValidationError,
    resolve_audio_paths,
    select_entries,
    validate_wav,
)
from showroom_guide.faq_cache import CacheConfigError, load_cache


def make_wav(*, frames: int = 160, sample_rate: int = 16000) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(b"\x00\x00" * frames)
    return output.getvalue()


def make_variant_wav(*, frames: int = 160, sample: bytes = b"\x00\x00") -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(sample * frames)
    return output.getvalue()


def entry(
    entry_id: str,
    *,
    priority: str = "high",
    enabled: bool = True,
    version: int = 1,
    answer: str | None = None,
    audio_file: str | None = None,
) -> dict[str, object]:
    return {
        "id": entry_id,
        "title": entry_id,
        "enabled": enabled,
        "priority": priority,
        "version": version,
        "aliases": [f"问题 {entry_id}"],
        "answer": answer or f"回答 {entry_id}",
        "audio_file": audio_file or f"prepared_audio/{entry_id}.wav",
    }


def write_config(tmp_path: Path, entries: list[dict[str, object]]) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    path = config_dir / "faq_cache.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "entries": entries,
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def make_generator(
    tmp_path: Path,
    entries: list[dict[str, object]] | None = None,
    *,
    manifest_path: Path | None = None,
):
    config_path = write_config(tmp_path, entries or [entry("high")])
    cache = load_cache(config_path)
    return FaqAudioGenerator(
        cache,
        config_path,
        TtsProfile(model="model-a", voice="voice-a", speed=1.0),
        manifest_path=manifest_path,
    )


class FakeSynthesizer:
    def __init__(self, outputs: list[bytes | BaseException] | None = None) -> None:
        self.outputs = list(outputs or [make_wav()])
        self.calls: list[str] = []

    async def synthesize(self, text: str) -> bytes:
        self.calls.append(text)
        output = self.outputs.pop(0)
        if isinstance(output, BaseException):
            raise output
        return output


def make_symlink(link: Path, target: Path, *, target_is_directory: bool = False):
    if os.name == "nt":
        pytest.skip("Windows 测试环境无权限创建 symlink")
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except OSError as error:
        pytest.skip(f"当前环境无法创建 symlink: {error}")


def test_validate_wav_reports_actual_metadata():
    metadata = validate_wav(make_wav(frames=320, sample_rate=8000))

    assert metadata.sample_rate == 8000
    assert metadata.channels == 1
    assert metadata.sample_width == 2
    assert metadata.frames == 320
    assert metadata.duration_seconds == 320 / 8000


@pytest.mark.parametrize("cut_bytes", [1, 10, 20])
def test_validate_wav_rejects_truncated_pcm(cut_bytes: int):
    audio = make_wav(frames=160)

    with pytest.raises(WavValidationError, match="PCM 数据"):
        validate_wav(audio[:-cut_bytes])


def test_validate_wav_accepts_complete_wav_with_trailing_chunk():
    audio = make_wav(frames=160)
    trailing_chunk = b"JUNK" + struct.pack("<I", 4) + b"test"
    updated = bytearray(audio)
    struct.pack_into("<I", updated, 4, struct.unpack_from("<I", audio, 4)[0] + len(trailing_chunk))
    updated.extend(trailing_chunk)

    metadata = validate_wav(bytes(updated))

    assert metadata.frames == 160


@pytest.mark.parametrize("audio", [b"not wav", make_wav(frames=0)])
def test_validate_wav_rejects_corrupt_and_empty_audio(audio: bytes):
    with pytest.raises(WavValidationError):
        validate_wav(audio)


@pytest.mark.asyncio
async def test_dry_run_does_not_call_synthesizer_or_write_files(tmp_path: Path):
    generator = make_generator(tmp_path)
    synthesizer = FakeSynthesizer()

    result = await generator.run(synthesizer, dry_run=True)

    assert result.planned == 1
    assert result.generated == 0
    assert synthesizer.calls == []
    assert not generator.audio_paths["high"].exists()
    assert not generator.manifest_path.exists()


@pytest.mark.asyncio
async def test_run_rejects_conflicting_modes_directly(tmp_path: Path):
    generator = make_generator(tmp_path)

    with pytest.raises(AudioConfigError):
        await generator.run(None, dry_run=True, verify_only=True)
    with pytest.raises(AudioConfigError):
        await generator.run(None, verify_only=True, force=True)


def test_selection_priority_entry_and_disabled_behavior(tmp_path: Path):
    config_path = write_config(
        tmp_path,
        [
            entry("high", priority="high"),
            entry("medium", priority="medium"),
            entry("disabled", enabled=False),
        ],
    )
    entries = load_cache(config_path).entries

    assert [item.id for item in select_entries(entries)] == ["high"]
    assert [item.id for item in select_entries(entries, priority="medium")] == ["medium"]
    assert [item.id for item in select_entries(entries, priority="all")] == ["high", "medium"]
    assert [item.id for item in select_entries(entries, entry_ids=["medium"])] == ["medium"]
    assert select_entries(entries, entry_ids=["disabled"]) == ()


def test_missing_entry_id_is_clear(tmp_path: Path):
    config_path = write_config(tmp_path, [entry("one")])

    with pytest.raises(AudioConfigError, match="不存在的 entry id"):
        select_entries(load_cache(config_path).entries, entry_ids=["missing"])


@pytest.mark.parametrize(
    "audio_file",
    [
        "C:/outside.wav",
        "../outside.wav",
        r"..\outside.wav",
        "prepared_audio/../../outside.wav",
        r"prepared_audio\..\..\outside.wav",
        "prepared_audio/mixed\\..\\..\\outside.wav",
        r"\\server\share\audio.wav",
        "prepared_audio/output.mp3",
    ],
)
def test_audio_path_safety_is_checked_before_generation(tmp_path: Path, audio_file: str):
    config_path = write_config(tmp_path, [entry("bad", audio_file=audio_file)])

    with pytest.raises(AudioConfigError):
        FaqAudioGenerator(
            load_cache(config_path),
            config_path,
            TtsProfile("model", "voice", 1.0),
        )


def test_duplicate_audio_paths_are_rejected(tmp_path: Path):
    config_path = write_config(
        tmp_path,
        [
            entry("one", audio_file="prepared_audio/same.wav"),
            entry("two", audio_file="prepared_audio/same.wav"),
        ],
    )

    with pytest.raises(AudioConfigError, match="路径重复"):
        resolve_audio_paths(config_path, load_cache(config_path).entries)


def test_manifest_directory_symlink_outside_is_rejected(tmp_path: Path):
    config_path = write_config(
        tmp_path,
        [entry("high", audio_file="audio/high.wav")],
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    prepared_audio = config_path.parent / "prepared_audio"
    make_symlink(prepared_audio, outside, target_is_directory=True)

    with pytest.raises(AudioConfigError, match="manifest"):
        make_generator(tmp_path, [entry("high", audio_file="audio/high.wav")])


def test_manifest_symlink_outside_is_rejected(tmp_path: Path):
    config_path = write_config(tmp_path, [entry("high", audio_file="audio/high.wav")])
    prepared_audio = config_path.parent / "prepared_audio"
    prepared_audio.mkdir()
    outside_manifest = tmp_path / "outside-manifest.json"
    outside_manifest.write_text("{}", encoding="utf-8")
    make_symlink(prepared_audio / "manifest.json", outside_manifest)

    with pytest.raises(AudioConfigError, match="manifest"):
        make_generator(tmp_path, [entry("high", audio_file="audio/high.wav")])


def test_dangling_manifest_symlink_is_rejected(tmp_path: Path):
    config_path = write_config(tmp_path, [entry("high", audio_file="audio/high.wav")])
    prepared_audio = config_path.parent / "prepared_audio"
    prepared_audio.mkdir()
    make_symlink(
        prepared_audio / "manifest.json",
        tmp_path / "missing-manifest.json",
    )

    with pytest.raises(AudioConfigError, match="manifest"):
        make_generator(tmp_path, [entry("high", audio_file="audio/high.wav")])


def test_manifest_path_inside_config_directory_is_allowed(tmp_path: Path):
    config_path = write_config(tmp_path, [entry("high", audio_file="audio/high.wav")])
    injected = config_path.parent / "managed" / "manifest.json"

    generator = make_generator(
        tmp_path,
        [entry("high", audio_file="audio/high.wav")],
        manifest_path=injected,
    )

    assert generator.manifest_path == injected.resolve()


def test_audio_directory_target_is_rejected_and_preserved(tmp_path: Path):
    config_path = write_config(tmp_path, [entry("high")])
    target = config_path.parent / "prepared_audio" / "high.wav"
    target.mkdir(parents=True)
    marker = target / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(AudioConfigError, match="high.*high.wav"):
        make_generator(tmp_path)

    assert target.is_dir()
    assert marker.read_text(encoding="utf-8") == "keep"


def test_manifest_directory_target_is_rejected_and_preserved(tmp_path: Path):
    config_path = write_config(
        tmp_path,
        [entry("high", audio_file="audio/high.wav")],
    )
    manifest = config_path.parent / "prepared_audio" / "manifest.json"
    manifest.mkdir(parents=True)
    marker = manifest / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(AudioConfigError, match="manifest.*manifest.json"):
        make_generator(tmp_path, [entry("high", audio_file="audio/high.wav")])

    assert manifest.is_dir()
    assert marker.read_text(encoding="utf-8") == "keep"


@pytest.mark.asyncio
async def test_generation_writes_relative_wav_and_manifest_then_skips(tmp_path: Path):
    generator = make_generator(tmp_path)
    synthesizer = FakeSynthesizer([make_wav(frames=320)])

    first = await generator.run(synthesizer)
    second = await generator.run(synthesizer)

    target = generator.audio_paths["high"]
    assert first.generated == 1
    assert second.skipped == 1
    assert len(synthesizer.calls) == 1
    assert target == (tmp_path / "config" / "prepared_audio" / "high.wav").resolve()
    assert validate_wav(target).frames == 320
    manifest = json.loads(generator.manifest_path.read_text(encoding="utf-8"))
    record = manifest["entries"][0]
    assert record["entry_id"] == "high"
    assert record["entry_version"] == 1
    assert record["tts_model"] == "model-a"
    assert record["tts_voice"] == "voice-a"
    assert record["tts_speed"] == 1.0
    assert len(record["wav_sha256"]) == 64
    assert "api-key" not in json.dumps(manifest)


@pytest.mark.asyncio
async def test_force_regenerates_even_when_manifest_is_current(tmp_path: Path):
    generator = make_generator(tmp_path)
    synthesizer = FakeSynthesizer([make_wav(), make_wav(frames=320)])

    await generator.run(synthesizer)
    result = await generator.run(synthesizer, force=True)

    assert result.generated == 1
    assert len(synthesizer.calls) == 2
    assert validate_wav(generator.audio_paths["high"]).frames == 320


@pytest.mark.asyncio
async def test_invalid_new_wav_does_not_overwrite_existing_file(tmp_path: Path):
    generator = make_generator(tmp_path)
    await generator.run(FakeSynthesizer([make_wav()]))
    target = generator.audio_paths["high"]
    original = target.read_bytes()

    result = await generator.run(FakeSynthesizer([b"broken"]), force=True)

    assert result.failed == 1
    assert target.read_bytes() == original
    assert not list(target.parent.glob(f".{target.name}.*.tmp"))


def assert_no_transaction_artifacts(generator: FaqAudioGenerator) -> None:
    assert not list(generator.audio_paths[next(iter(generator.audio_paths))].parent.glob(".*.tmp"))


@pytest.mark.asyncio
async def test_manifest_failure_restores_old_wav_and_manifest(
    monkeypatch,
    tmp_path: Path,
):
    generator = make_generator(tmp_path)
    await generator.run(FakeSynthesizer([make_variant_wav(sample=b"\x00\x00")]))
    target = generator.audio_paths["high"]
    old_wav = target.read_bytes()
    old_manifest = generator.manifest_path.read_bytes()

    def fail_manifest(*_args, **_kwargs):
        raise OSError("manifest write failed")

    monkeypatch.setattr(faq_audio, "_atomic_write_manifest", fail_manifest)
    result = await generator.run(
        FakeSynthesizer([make_variant_wav(sample=b"\x01\x00")]),
        force=True,
    )

    assert result.failed == 1
    assert target.read_bytes() == old_wav
    assert generator.manifest_path.read_bytes() == old_manifest
    assert_no_transaction_artifacts(generator)


@pytest.mark.asyncio
async def test_first_manifest_failure_leaves_no_new_wav(
    monkeypatch,
    tmp_path: Path,
):
    generator = make_generator(tmp_path)

    monkeypatch.setattr(
        faq_audio,
        "_atomic_write_manifest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("manifest failed")),
    )
    result = await generator.run(FakeSynthesizer())

    assert result.failed == 1
    assert not generator.audio_paths["high"].exists()
    assert not generator.manifest_path.exists()
    assert_no_transaction_artifacts(generator)


@pytest.mark.asyncio
async def test_force_manifest_failure_preserves_old_files(
    monkeypatch,
    tmp_path: Path,
):
    generator = make_generator(tmp_path)
    await generator.run(FakeSynthesizer([make_wav()]))
    target = generator.audio_paths["high"]
    old_wav = target.read_bytes()
    old_manifest = generator.manifest_path.read_bytes()
    monkeypatch.setattr(
        faq_audio,
        "_atomic_write_manifest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("manifest failed")),
    )

    result = await generator.run(FakeSynthesizer([make_wav(frames=320)]), force=True)

    assert result.failed == 1
    assert target.read_bytes() == old_wav
    assert generator.manifest_path.read_bytes() == old_manifest
    assert_no_transaction_artifacts(generator)


@pytest.mark.asyncio
async def test_cancelled_commit_rolls_back_and_propagates(
    monkeypatch,
    tmp_path: Path,
):
    generator = make_generator(tmp_path)
    await generator.run(FakeSynthesizer([make_wav()]))
    target = generator.audio_paths["high"]
    old_wav = target.read_bytes()
    old_manifest = generator.manifest_path.read_bytes()

    def cancel_manifest(*_args, **_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(faq_audio, "_atomic_write_manifest", cancel_manifest)
    with pytest.raises(asyncio.CancelledError):
        await generator.run(FakeSynthesizer([make_wav(frames=320)]), force=True)

    assert target.read_bytes() == old_wav
    assert generator.manifest_path.read_bytes() == old_manifest
    assert_no_transaction_artifacts(generator)


def fail_backup_restore(monkeypatch, blocked_paths: set[Path]):
    original_replace = faq_audio.os.replace

    def replace(source, destination):
        source_path = Path(source)
        destination_path = Path(destination)
        if ".backup." in source_path.name and destination_path in blocked_paths:
            raise OSError(f"injected restore failure for {destination_path}")
        return original_replace(source, destination)

    monkeypatch.setattr(faq_audio.os, "replace", replace)


def fail_manifest_commit(monkeypatch, error: BaseException | None = None):
    failure = error or OSError("injected manifest commit failure")
    monkeypatch.setattr(
        faq_audio,
        "_atomic_write_manifest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
    )


def raise_after_real_replace(monkeypatch, destination: Path, error: BaseException):
    original_replace = faq_audio.os.replace
    raised = False

    def replace(source, target):
        nonlocal raised
        if not raised and Path(target) == destination:
            original_replace(source, target)
            raised = True
            raise error
        return original_replace(source, target)

    monkeypatch.setattr(faq_audio.os, "replace", replace)


def raise_after_real_backup_copy(
    monkeypatch,
    source: Path,
    error: BaseException,
):
    original_copy = faq_audio._copy_backup
    raised = False

    def copy_backup(copy_source, backup):
        nonlocal raised
        original_copy(copy_source, backup)
        if not raised and Path(copy_source) == source:
            raised = True
            raise error

    monkeypatch.setattr(faq_audio, "_copy_backup", copy_backup)


@pytest.mark.asyncio
async def test_first_wav_replace_then_cancel_cleans_real_target(
    monkeypatch,
    tmp_path: Path,
):
    generator = make_generator(tmp_path)
    target = generator.audio_paths["high"]
    raise_after_real_replace(monkeypatch, target, asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await generator.run(FakeSynthesizer([make_wav()]))

    assert not target.exists()
    assert not generator.manifest_path.exists()
    assert_no_transaction_artifacts(generator)


@pytest.mark.asyncio
async def test_first_manifest_replace_then_cancel_cleans_real_targets(
    monkeypatch,
    tmp_path: Path,
):
    generator = make_generator(tmp_path)
    raise_after_real_replace(
        monkeypatch,
        generator.manifest_path,
        asyncio.CancelledError(),
    )

    with pytest.raises(asyncio.CancelledError):
        await generator.run(FakeSynthesizer([make_wav()]))

    assert not generator.audio_paths["high"].exists()
    assert not generator.manifest_path.exists()
    assert_no_transaction_artifacts(generator)


@pytest.mark.asyncio
async def test_first_wav_replace_then_oserror_is_failed_and_cleans_targets(
    monkeypatch,
    tmp_path: Path,
):
    generator = make_generator(tmp_path)
    target = generator.audio_paths["high"]
    raise_after_real_replace(monkeypatch, target, OSError("replace failed"))

    result = await generator.run(FakeSynthesizer([make_wav()]))

    assert result.failed == 1
    assert result.generated == 0
    assert not target.exists()
    assert not generator.manifest_path.exists()
    assert_no_transaction_artifacts(generator)


@pytest.mark.asyncio
async def test_first_manifest_replace_then_oserror_is_failed_and_cleans_targets(
    monkeypatch,
    tmp_path: Path,
):
    generator = make_generator(tmp_path)
    raise_after_real_replace(
        monkeypatch,
        generator.manifest_path,
        OSError("manifest replace failed"),
    )

    result = await generator.run(FakeSynthesizer([make_wav()]))

    assert result.failed == 1
    assert result.generated == 0
    assert not generator.audio_paths["high"].exists()
    assert not generator.manifest_path.exists()
    assert_no_transaction_artifacts(generator)


@pytest.mark.parametrize(
    ("destination", "error_kind"),
    [
        ("wav", "cancelled"),
        ("wav", "oserror"),
        ("manifest", "cancelled"),
        ("manifest", "oserror"),
    ],
)
@pytest.mark.asyncio
async def test_replace_then_error_restores_existing_wav_and_manifest(
    monkeypatch,
    tmp_path: Path,
    destination: str,
    error_kind: str,
):
    generator = make_generator(tmp_path)
    await generator.run(FakeSynthesizer([make_variant_wav(sample=b"\x00\x00")]))
    target = generator.audio_paths["high"]
    old_wav = target.read_bytes()
    old_manifest = generator.manifest_path.read_bytes()
    error: BaseException = (
        asyncio.CancelledError()
        if error_kind == "cancelled"
        else OSError("replace completed then failed")
    )
    replace_target = target if destination == "wav" else generator.manifest_path
    raise_after_real_replace(monkeypatch, replace_target, error)

    if error_kind == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await generator.run(
                FakeSynthesizer([make_variant_wav(sample=b"\x01\x00")]),
                force=True,
            )
    else:
        result = await generator.run(
            FakeSynthesizer([make_variant_wav(sample=b"\x01\x00")]),
            force=True,
        )
        assert result.failed == 1
        assert result.generated == 0

    assert target.read_bytes() == old_wav
    assert generator.manifest_path.read_bytes() == old_manifest
    assert_no_transaction_artifacts(generator)


@pytest.mark.parametrize(
    ("resource", "error_kind"),
    [
        ("wav", "cancelled"),
        ("wav", "oserror"),
        ("manifest", "cancelled"),
        ("manifest", "oserror"),
    ],
)
@pytest.mark.asyncio
async def test_backup_copy_then_error_preserves_existing_pair(
    monkeypatch,
    tmp_path: Path,
    resource: str,
    error_kind: str,
):
    generator = make_generator(tmp_path)
    await generator.run(FakeSynthesizer([make_variant_wav(sample=b"\x00\x00")]))
    target = generator.audio_paths["high"]
    old_wav = target.read_bytes()
    old_manifest = generator.manifest_path.read_bytes()
    backup_source = target if resource == "wav" else generator.manifest_path
    error: BaseException = (
        asyncio.CancelledError()
        if error_kind == "cancelled"
        else OSError("backup copy completed then failed")
    )
    raise_after_real_backup_copy(monkeypatch, backup_source, error)

    if error_kind == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await generator.run(
                FakeSynthesizer([make_variant_wav(sample=b"\x01\x00")]),
                force=True,
            )
    else:
        result = await generator.run(
            FakeSynthesizer([make_variant_wav(sample=b"\x01\x00")]),
            force=True,
        )
        assert result.failed == 1
        assert result.generated == 0

    assert target.read_bytes() == old_wav
    assert generator.manifest_path.read_bytes() == old_manifest
    assert_no_transaction_artifacts(generator)


@pytest.mark.asyncio
async def test_manifest_delete_restore_failure_still_restores_wav(
    monkeypatch,
    tmp_path: Path,
):
    generator = make_generator(tmp_path)
    await generator.run(FakeSynthesizer())
    target = generator.audio_paths["high"]
    old_wav = target.read_bytes()
    old_manifest = generator.manifest_path.read_bytes()
    original_remove = faq_audio._remove_path

    def fail_manifest_remove(path):
        if Path(path) == generator.manifest_path:
            raise OSError("injected manifest delete failure")
        return original_remove(path)

    monkeypatch.setattr(faq_audio, "_remove_path", fail_manifest_remove)
    fail_manifest_commit(monkeypatch)
    result = await generator.run(FakeSynthesizer([make_wav(frames=320)]), force=True)

    assert result.failed == 1
    assert target.read_bytes() == old_wav
    assert generator.manifest_path.read_bytes() == old_manifest
    assert "manifest 恢复错误" in result.failures[0].error


@pytest.mark.asyncio
async def test_manifest_backup_replace_failure_keeps_backup_and_restores_wav(
    monkeypatch,
    tmp_path: Path,
):
    generator = make_generator(tmp_path)
    await generator.run(FakeSynthesizer())
    target = generator.audio_paths["high"]
    old_wav = target.read_bytes()
    fail_backup_restore(monkeypatch, {generator.manifest_path})
    fail_manifest_commit(monkeypatch)

    result = await generator.run(FakeSynthesizer([make_wav(frames=320)]), force=True)

    backups = list(generator.manifest_path.parent.glob(".manifest.json.backup.*.tmp"))
    assert result.failed == 1
    assert target.read_bytes() == old_wav
    assert backups
    assert str(backups[0]) in result.failures[0].error


@pytest.mark.asyncio
async def test_wav_delete_restore_failure_still_restores_manifest(
    monkeypatch,
    tmp_path: Path,
):
    generator = make_generator(tmp_path)
    await generator.run(FakeSynthesizer())
    old_manifest = generator.manifest_path.read_bytes()
    original_remove = faq_audio._remove_path

    def fail_wav_remove(path):
        if Path(path) == generator.audio_paths["high"]:
            raise OSError("injected WAV delete failure")
        return original_remove(path)

    monkeypatch.setattr(faq_audio, "_remove_path", fail_wav_remove)
    fail_manifest_commit(monkeypatch)
    result = await generator.run(FakeSynthesizer([make_wav(frames=320)]), force=True)

    assert result.failed == 1
    assert generator.manifest_path.read_bytes() == old_manifest
    assert "WAV 恢复错误" in result.failures[0].error


@pytest.mark.asyncio
async def test_wav_backup_replace_failure_keeps_backup_and_restores_manifest(
    monkeypatch,
    tmp_path: Path,
):
    generator = make_generator(tmp_path)
    await generator.run(FakeSynthesizer())
    old_manifest = generator.manifest_path.read_bytes()
    target = generator.audio_paths["high"]
    fail_backup_restore(monkeypatch, {target})
    fail_manifest_commit(monkeypatch)

    result = await generator.run(FakeSynthesizer([make_wav(frames=320)]), force=True)

    backups = list(target.parent.glob(".high.wav.backup.*.tmp"))
    assert result.failed == 1
    assert generator.manifest_path.read_bytes() == old_manifest
    assert backups
    assert str(backups[0]) in result.failures[0].error


@pytest.mark.asyncio
async def test_both_restore_failures_are_attempted_and_keep_both_backups(
    monkeypatch,
    tmp_path: Path,
):
    generator = make_generator(tmp_path)
    await generator.run(FakeSynthesizer())
    target = generator.audio_paths["high"]
    fail_backup_restore(monkeypatch, {target, generator.manifest_path})
    fail_manifest_commit(monkeypatch)

    result = await generator.run(FakeSynthesizer([make_wav(frames=320)]), force=True)

    wav_backups = list(target.parent.glob(".high.wav.backup.*.tmp"))
    manifest_backups = list(generator.manifest_path.parent.glob(".manifest.json.backup.*.tmp"))
    assert result.failed == 1
    assert wav_backups and manifest_backups
    assert "WAV 恢复错误" in result.failures[0].error
    assert "manifest 恢复错误" in result.failures[0].error


@pytest.mark.asyncio
async def test_recovery_cleanup_failure_does_not_hide_original_commit_error(
    monkeypatch,
    tmp_path: Path,
):
    generator = make_generator(tmp_path)
    await generator.run(FakeSynthesizer())
    original_remove = faq_audio._remove_path

    def fail_new_file_remove(path):
        if Path(path) in {
            generator.audio_paths["high"],
            generator.manifest_path,
        }:
            raise OSError("injected cleanup failure")
        return original_remove(path)

    monkeypatch.setattr(faq_audio, "_remove_path", fail_new_file_remove)
    monkeypatch.setattr(
        faq_audio,
        "_atomic_write_manifest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("original manifest failure")),
    )
    result = await generator.run(FakeSynthesizer([make_wav(frames=320)]), force=True)

    assert result.failed == 1
    assert "原始提交错误" in result.failures[0].error
    assert "original manifest failure" in result.failures[0].error


@pytest.mark.asyncio
async def test_successful_commit_keeps_in_memory_manifest_when_backup_cleanup_fails(
    monkeypatch,
    tmp_path: Path,
):
    entries = [entry("one"), entry("two")]
    generator = make_generator(tmp_path, entries)
    await generator.run(FakeSynthesizer([make_wav()]), entry_ids=["one"])
    original_remove = faq_audio._remove_path

    def fail_backup_cleanup(path):
        if ".backup." in Path(path).name:
            raise OSError("injected backup cleanup failure")
        return original_remove(path)

    monkeypatch.setattr(faq_audio, "_remove_path", fail_backup_cleanup)
    result = await generator.run(
        FakeSynthesizer([make_wav(frames=320), make_wav(frames=480)]),
        force=True,
    )

    manifest = json.loads(generator.manifest_path.read_text(encoding="utf-8"))
    entry_ids = {record["entry_id"] for record in manifest["entries"]}
    assert result.generated == 2
    assert result.warnings
    assert entry_ids == {"one", "two"}
    assert validate_wav(generator.audio_paths["one"]).frames == 320
    assert validate_wav(generator.audio_paths["two"]).frames == 480


@pytest.mark.asyncio
async def test_cancelled_commit_with_restore_failure_keeps_backup_and_propagates(
    monkeypatch,
    tmp_path: Path,
):
    generator = make_generator(tmp_path)
    await generator.run(FakeSynthesizer())
    target = generator.audio_paths["high"]
    fail_backup_restore(monkeypatch, {target, generator.manifest_path})
    monkeypatch.setattr(
        faq_audio,
        "_atomic_write_manifest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(asyncio.CancelledError()),
    )

    with pytest.raises(asyncio.CancelledError):
        await generator.run(FakeSynthesizer([make_wav(frames=320)]), force=True)

    assert list(target.parent.glob(".high.wav.backup.*.tmp"))
    assert list(generator.manifest_path.parent.glob(".manifest.json.backup.*.tmp"))


@pytest.mark.parametrize(
    ("field", "profile"),
    [
        ("version", TtsProfile("model-a", "voice-a", 1.0)),
        ("voice", TtsProfile("model-a", "voice-b", 1.0)),
        ("speed", TtsProfile("model-a", "voice-a", 1.25)),
    ],
)
@pytest.mark.asyncio
async def test_manifest_marks_version_or_tts_changes_stale(
    tmp_path: Path,
    field: str,
    profile: TtsProfile,
):
    config_path = write_config(tmp_path, [entry("high")])
    original = FaqAudioGenerator(
        load_cache(config_path),
        config_path,
        TtsProfile("model-a", "voice-a", 1.0),
    )
    await original.run(FakeSynthesizer())

    changed_entry = entry("high", version=2) if field == "version" else entry("high")
    config_path.write_text(
        yaml.safe_dump(
            {"schema_version": 1, "entries": [changed_entry]},
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    changed = FaqAudioGenerator(load_cache(config_path), config_path, profile)

    result = await changed.run(FakeSynthesizer(), verify_only=True)

    assert result.stale == 1


@pytest.mark.asyncio
async def test_manifest_marks_audio_file_change_stale(tmp_path: Path):
    config_path = write_config(tmp_path, [entry("high")])
    original = FaqAudioGenerator(
        load_cache(config_path),
        config_path,
        TtsProfile("model-a", "voice-a", 1.0),
    )
    await original.run(FakeSynthesizer())

    config_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "entries": [entry("high", audio_file="prepared_audio/renamed.wav")],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    changed = FaqAudioGenerator(
        load_cache(config_path),
        config_path,
        TtsProfile("model-a", "voice-a", 1.0),
    )

    result = await changed.run(FakeSynthesizer(), verify_only=True)

    assert result.stale == 1


@pytest.mark.asyncio
async def test_one_failure_does_not_stop_following_entries(tmp_path: Path):
    generator = make_generator(tmp_path, [entry("one"), entry("two")])
    synthesizer = FakeSynthesizer([RuntimeError("TTS failed"), make_wav()])

    result = await generator.run(synthesizer)

    assert result.failed == 1
    assert result.generated == 1
    assert result.failures[0].entry_id == "one"
    assert generator.audio_paths["two"].is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "secret"),
    [
        ("Authorization: Bearer topsecret", "topsecret"),
        ("Authorization=Bearer topsecret", "topsecret"),
        ("api_key=topsecret", "topsecret"),
        ("access_token=access-secret", "access-secret"),
        ("token=token-secret", "token-secret"),
        ("password: password-secret", "password-secret"),
        ('{"api_key": "json-secret"}', "json-secret"),
        ("https://example.test/?api_key=url-secret&x=1 HTTP 503", "url-secret"),
        ("api_key=top secret", "top secret"),
    ],
)
async def test_synthesizer_failure_and_report_are_redacted(
    tmp_path: Path,
    message: str,
    secret: str,
):
    generator = make_generator(tmp_path)
    reports: list[str] = []

    result = await generator.run(
        FakeSynthesizer([RuntimeError(message)]),
        report=reports.append,
    )

    output = "\n".join([result.failures[0].error, *reports])
    assert result.failed == 1
    assert "RuntimeError" in output
    assert secret not in output


@pytest.mark.asyncio
async def test_non_sensitive_synthesizer_error_keeps_useful_diagnostics(tmp_path: Path):
    generator = make_generator(tmp_path)

    result = await generator.run(
        FakeSynthesizer([RuntimeError("HTTP 503 upstream unavailable")])
    )

    assert result.failed == 1
    assert "RuntimeError" in result.failures[0].error
    assert "HTTP 503 upstream unavailable" in result.failures[0].error


def test_cache_rejects_unsafe_entry_id(tmp_path: Path):
    config_path = write_config(tmp_path, [entry("api_key=topsecret")])

    with pytest.raises(CacheConfigError, match="id"):
        load_cache(config_path)


def test_cli_unknown_sensitive_entry_id_is_redacted(monkeypatch, capsys, tmp_path: Path):
    config_path = write_config(tmp_path, [entry("high")])
    monkeypatch.setattr(
        faq_audio,
        "Settings",
        lambda _env_file: type(
            "FakeSettings",
            (),
            {"tts_model": "model", "tts_voice": "voice", "tts_speed": 1.0},
        )(),
    )

    assert faq_audio.main(
        ["--config", str(config_path), "--dry-run", "--entry", "api_key=topsecret"]
    ) == 2
    captured = capsys.readouterr()
    assert "topsecret" not in captured.err
    assert "api_key=<redacted>" in captured.err


def test_audio_config_error_is_redacted_for_library_callers(tmp_path: Path):
    config_path = write_config(tmp_path, [entry("high")])

    with pytest.raises(AudioConfigError) as caught:
        select_entries(
            load_cache(config_path).entries,
            entry_ids=["api_key=topsecret"],
        )

    assert "topsecret" not in str(caught.value)
    assert "api_key=<redacted>" in str(caught.value)


def test_reporter_defensively_redacts_entry_id(tmp_path: Path):
    config_path = write_config(tmp_path, [entry("high")])
    safe_entry = load_cache(config_path).entries[0]
    unsafe_entry = safe_entry.model_copy(update={"id": "api_key=topsecret"})
    reports: list[str] = []

    FaqAudioGenerator._report(reports.append, unsafe_entry, "failed", "TTS failed")

    assert "topsecret" not in reports[0]
    assert "api_key=<redacted>" in reports[0]


def test_cli_settings_validation_does_not_echo_input_value(
    monkeypatch,
    capsys,
    tmp_path: Path,
):
    config_path = write_config(tmp_path, [entry("high")])

    class ValidationProbe(BaseModel):
        tts_speed: float = Field(gt=0)

    with pytest.raises(ValueError):
        ValidationProbe.model_validate({"tts_speed": "topsecret"})
    try:
        ValidationProbe.model_validate({"tts_speed": "topsecret"})
    except Exception as error:
        validation_error = error
    else:  # pragma: no cover - the invalid probe must always fail
        raise AssertionError("expected a validation error")

    def raise_validation(_env_file):
        raise validation_error

    monkeypatch.setattr(faq_audio, "Settings", raise_validation)

    assert faq_audio.main(["--config", str(config_path), "--dry-run"]) == 2
    captured = capsys.readouterr()
    assert "tts_speed" in captured.err
    assert "topsecret" not in captured.err
    assert "input_value" not in captured.err


@pytest.mark.asyncio
async def test_stale_changes_and_verify_only_do_not_call_tts(tmp_path: Path):
    config_path = write_config(tmp_path, [entry("high")])
    generator = FaqAudioGenerator(
        load_cache(config_path),
        config_path,
        TtsProfile("model-a", "voice-a", 1.0),
    )
    await generator.run(FakeSynthesizer([make_wav()]))

    config_path.write_text(
        yaml.safe_dump(
            {"schema_version": 1, "entries": [entry("high", answer="新回答")]},
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    changed = FaqAudioGenerator(
        load_cache(config_path),
        config_path,
        TtsProfile("model-b", "voice-a", 1.0),
    )
    synthesizer = FakeSynthesizer()

    result = await changed.run(synthesizer, verify_only=True)

    assert result.verified == 0
    assert result.stale == 1
    assert synthesizer.calls == []


@pytest.mark.asyncio
async def test_same_metadata_different_pcm_is_stale(tmp_path: Path):
    generator = make_generator(tmp_path)
    await generator.run(FakeSynthesizer([make_variant_wav(sample=b"\x00\x00")]))
    generator.audio_paths["high"].write_bytes(make_variant_wav(sample=b"\x01\x00"))

    result = await generator.run(None, verify_only=True)

    assert result.stale == 1


@pytest.mark.asyncio
async def test_old_manifest_without_wav_hash_is_stale(tmp_path: Path):
    generator = make_generator(tmp_path)
    await generator.run(FakeSynthesizer())
    manifest = json.loads(generator.manifest_path.read_text(encoding="utf-8"))
    manifest["entries"][0].pop("wav_sha256")
    generator.manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )

    result = await generator.run(None, verify_only=True)

    assert result.stale == 1


@pytest.mark.asyncio
async def test_verify_only_reports_missing_and_corrupt_without_tts(tmp_path: Path):
    generator = make_generator(tmp_path)
    missing = await generator.run(None, verify_only=True)
    assert missing.stale == 1

    generator.audio_paths["high"].parent.mkdir(parents=True, exist_ok=True)
    generator.audio_paths["high"].write_bytes(b"bad")
    corrupt = await generator.run(FakeSynthesizer(), verify_only=True)
    assert corrupt.stale == 1


def test_cli_closes_speech_client(monkeypatch, tmp_path: Path):
    config_path = write_config(tmp_path, [entry("high")])

    class FakeSettings:
        tts_model = "model"
        tts_voice = "voice"
        tts_speed = 1.0

    class FakeSpeech(FakeSynthesizer):
        def __init__(self):
            super().__init__([make_wav()])
            self.closed = False

        async def aclose(self):
            self.closed = True

    speech = FakeSpeech()
    monkeypatch.setattr(faq_audio, "Settings", lambda _env_file: FakeSettings())
    monkeypatch.setattr(faq_audio, "speech_client_from_settings", lambda _settings: speech)

    exit_code = faq_audio.main(["--config", str(config_path)])

    assert exit_code == 0
    assert speech.closed is True


@pytest.mark.parametrize("output", [RuntimeError("TTS failed")])
def test_cli_closes_speech_client_after_tts_failure(
    monkeypatch,
    tmp_path: Path,
    output: Exception,
):
    config_path = write_config(tmp_path, [entry("high")])
    speech = FakeSynthesizer([output])
    speech.closed = False

    async def close():
        speech.closed = True

    speech.aclose = close
    monkeypatch.setattr(faq_audio, "Settings", lambda _env_file: type(
        "FakeSettings", (), {"tts_model": "model", "tts_voice": "voice", "tts_speed": 1.0}
    )())
    monkeypatch.setattr(faq_audio, "speech_client_from_settings", lambda _settings: speech)

    assert faq_audio.main(["--config", str(config_path)]) == 1
    assert speech.closed is True


def test_cli_closes_speech_client_after_manifest_failure(monkeypatch, tmp_path: Path):
    config_path = write_config(tmp_path, [entry("high")])
    speech = FakeSynthesizer([make_wav()])
    speech.closed = False

    async def close():
        speech.closed = True

    speech.aclose = close
    monkeypatch.setattr(faq_audio, "Settings", lambda _env_file: type(
        "FakeSettings", (), {"tts_model": "model", "tts_voice": "voice", "tts_speed": 1.0}
    )())
    monkeypatch.setattr(faq_audio, "speech_client_from_settings", lambda _settings: speech)
    monkeypatch.setattr(
        faq_audio,
        "_atomic_write_manifest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("manifest failed")),
    )

    assert faq_audio.main(["--config", str(config_path)]) == 1
    assert speech.closed is True


def test_cli_closes_speech_client_after_cancellation(monkeypatch, tmp_path: Path):
    config_path = write_config(tmp_path, [entry("high")])
    speech = FakeSynthesizer([asyncio.CancelledError()])
    speech.closed = False

    async def close():
        speech.closed = True

    speech.aclose = close
    monkeypatch.setattr(faq_audio, "Settings", lambda _env_file: type(
        "FakeSettings", (), {"tts_model": "model", "tts_voice": "voice", "tts_speed": 1.0}
    )())
    monkeypatch.setattr(faq_audio, "speech_client_from_settings", lambda _settings: speech)

    with pytest.raises(asyncio.CancelledError):
        faq_audio.main(["--config", str(config_path)])
    assert speech.closed is True


def test_cli_rejects_conflicting_modes(tmp_path: Path):
    config_path = write_config(tmp_path, [entry("high")])

    with pytest.raises(SystemExit) as verify_force:
        faq_audio.main(["--config", str(config_path), "--verify-only", "--force"])
    with pytest.raises(SystemExit) as dry_verify:
        faq_audio.main(["--config", str(config_path), "--dry-run", "--verify-only"])

    assert verify_force.value.code == 2
    assert dry_verify.value.code == 2


@pytest.mark.parametrize("mode", [["--dry-run"], ["--verify-only"]])
def test_dry_run_and_verify_only_do_not_create_speech_client(
    monkeypatch,
    tmp_path: Path,
    mode: list[str],
):
    config_path = write_config(tmp_path, [entry("high")])
    created = []
    monkeypatch.setattr(faq_audio, "Settings", lambda _env_file: type(
        "FakeSettings", (), {"tts_model": "model", "tts_voice": "voice", "tts_speed": 1.0}
    )())

    def fail_create(_settings):
        created.append(True)
        raise AssertionError("dry-run/verify-only must not create SpeechClient")

    monkeypatch.setattr(faq_audio, "speech_client_from_settings", fail_create)

    faq_audio.main(["--config", str(config_path), *mode])

    assert created == []
