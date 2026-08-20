import asyncio
import io
import os
import signal
import wave
from pathlib import Path

import pytest

from showroom_guide.local_audio import (
    LocalAudioBusy,
    LocalAudioController,
    LocalAudioError,
    LocalAudioNotRecording,
)


PROMPT_PATH = (
    Path(__file__).parents[2]
    / "src"
    / "showroom_guide"
    / "assets"
    / "no-speech-detected.wav"
)


def make_wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x01\x00" * 160)
    return output.getvalue()


def test_packaged_no_speech_prompt_is_valid_wav():
    with wave.open(str(PROMPT_PATH), "rb") as source:
        assert source.getnchannels() == 1
        assert source.getsampwidth() == 2
        assert source.getframerate() > 0
        assert source.getnframes() > source.getframerate()


class FakeProcess:
    def __init__(self, *, returncode=None, communicate_returncode=0) -> None:
        self.returncode = returncode
        self.communicate_returncode = communicate_returncode
        self.communicated_input = None
        self.signals = []
        self.terminated = False
        self.killed = False

    async def communicate(self, input=None):
        self.communicated_input = input
        self.returncode = self.communicate_returncode
        return b"", b""

    async def wait(self):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def send_signal(self, value):
        self.signals.append(value)

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


@pytest.mark.asyncio
async def test_records_pcm_wav_and_plays_from_stdin():
    calls = []

    async def process_factory(*args, **kwargs):
        process = FakeProcess()
        calls.append((args, kwargs, process))
        if args[0] == "pw-record":
            Path(args[-1]).write_bytes(make_wav())
        return process

    controller = LocalAudioController(
        sample_rate=16000,
        capture_device="default",
        playback_device="default",
        process_factory=process_factory,
    )

    await controller.start_recording()
    record_args, record_kwargs, record_process = calls[0]
    recording_path = Path(record_args[-1])

    assert record_args[:-1] == (
        "pw-record",
        "--rate",
        "16000",
        "--channels",
        "1",
        "--format",
        "s16",
    )
    assert "--target" not in record_args
    assert record_kwargs["stderr"] is asyncio.subprocess.PIPE
    if os.name == "posix":
        assert os.stat(recording_path).st_mode & 0o777 == 0o600

    captured = await controller.stop_recording()

    assert captured == make_wav()
    assert record_process.signals == [signal.SIGINT]
    assert not recording_path.exists()

    await controller.play(captured)
    play_args, play_kwargs, play_process = calls[1]

    assert play_args == ("pw-play", "-")
    assert play_kwargs["stdin"] is asyncio.subprocess.PIPE
    assert play_kwargs["stderr"] is asyncio.subprocess.PIPE
    assert play_process.communicated_input == captured


@pytest.mark.asyncio
async def test_accepts_pw_record_exit_one_after_intentional_sigint():
    class PipeWireRecordProcess(FakeProcess):
        async def wait(self):
            self.returncode = 1
            return self.returncode

    async def process_factory(*args, **kwargs):
        Path(args[-1]).write_bytes(make_wav())
        return PipeWireRecordProcess()

    controller = LocalAudioController(process_factory=process_factory)

    await controller.start_recording()
    captured = await controller.stop_recording()

    assert captured == make_wav()


@pytest.mark.asyncio
async def test_non_default_targets_are_passed_as_arguments():
    calls = []

    async def process_factory(*args, **kwargs):
        process = FakeProcess()
        calls.append(args)
        if args[0] == "pw-record":
            Path(args[-1]).write_bytes(make_wav())
        return process

    controller = LocalAudioController(
        sample_rate=16000,
        capture_device="bluez_input.test",
        playback_device="bluez_output.test",
        process_factory=process_factory,
    )

    await controller.start_recording()
    captured = await controller.stop_recording()
    await controller.play(captured)

    assert calls[0][1:3] == ("--target", "bluez_input.test")
    assert calls[1] == ("pw-play", "--target", "bluez_output.test", "-")


@pytest.mark.asyncio
async def test_recording_cues_are_valid_distinct_wav_chirps():
    processes = []

    async def process_factory(*args, **kwargs):
        process = FakeProcess()
        processes.append(process)
        return process

    controller = LocalAudioController(
        sample_rate=16000,
        process_factory=process_factory,
    )

    await controller.play_start_cue()
    await controller.play_stop_cue()

    start_cue = processes[0].communicated_input
    stop_cue = processes[1].communicated_input
    assert start_cue != stop_cue

    for cue in (start_cue, stop_cue):
        with wave.open(io.BytesIO(cue), "rb") as source:
            assert source.getnchannels() == 1
            assert source.getsampwidth() == 2
            assert source.getframerate() == 16000
            assert source.getnframes() == 1920


@pytest.mark.asyncio
async def test_no_speech_prompt_uses_the_shared_playback_path():
    processes = []
    prompt = make_wav()

    async def process_factory(*args, **kwargs):
        process = FakeProcess()
        processes.append(process)
        return process

    controller = LocalAudioController(
        no_speech_prompt=prompt,
        process_factory=process_factory,
    )

    await controller.play_no_speech_prompt()

    assert processes[0].communicated_input == prompt


@pytest.mark.asyncio
async def test_rejects_duplicate_recording_and_stop_without_recording():
    async def process_factory(*args, **kwargs):
        Path(args[-1]).write_bytes(make_wav())
        return FakeProcess()

    controller = LocalAudioController(process_factory=process_factory)

    with pytest.raises(LocalAudioNotRecording):
        await controller.stop_recording()

    await controller.start_recording()
    with pytest.raises(LocalAudioBusy):
        await controller.start_recording()
    await controller.abort_recording()

    assert not controller.is_recording


@pytest.mark.asyncio
async def test_start_failure_removes_temporary_file():
    paths = []

    async def process_factory(*args, **kwargs):
        paths.append(Path(args[-1]))
        return FakeProcess(returncode=1, communicate_returncode=1)

    controller = LocalAudioController(process_factory=process_factory)

    with pytest.raises(LocalAudioError, match="录音设备"):
        await controller.start_recording()

    assert not paths[0].exists()
    assert not controller.is_recording


@pytest.mark.asyncio
async def test_playback_failure_resets_playing_state():
    async def process_factory(*args, **kwargs):
        return FakeProcess(communicate_returncode=1)

    controller = LocalAudioController(process_factory=process_factory)

    with pytest.raises(LocalAudioError, match="扬声器"):
        await controller.play(make_wav())

    assert not controller.is_playing


@pytest.mark.asyncio
async def test_aclose_stops_recording_and_removes_file():
    paths = []
    process = FakeProcess()

    async def process_factory(*args, **kwargs):
        path = Path(args[-1])
        path.write_bytes(make_wav())
        paths.append(path)
        return process

    controller = LocalAudioController(process_factory=process_factory)
    await controller.start_recording()

    await controller.aclose()

    assert process.signals == [signal.SIGINT]
    assert not paths[0].exists()
    assert not controller.is_recording
