import asyncio
import io
import logging
import math
import os
import signal
import struct
import tempfile
import wave
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

ProcessFactory = Callable[..., Awaitable[asyncio.subprocess.Process]]


def _create_chirp_wav(
    *,
    sample_rate: int,
    start_frequency: float,
    end_frequency: float,
    duration_seconds: float = 0.12,
) -> bytes:
    frame_count = round(sample_rate * duration_seconds)
    fade_frames = max(1, round(sample_rate * 0.01))
    amplitude = round(32767 * 0.25)
    phase = 0.0
    frames = bytearray()

    for index in range(frame_count):
        progress = index / max(1, frame_count - 1)
        frequency = start_frequency + (end_frequency - start_frequency) * progress
        phase += 2 * math.pi * frequency / sample_rate
        envelope = min(
            1.0,
            index / fade_frames,
            (frame_count - 1 - index) / fade_frames,
        )
        sample = round(amplitude * envelope * math.sin(phase))
        frames.extend(struct.pack("<h", sample))

    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(frames)
    return output.getvalue()


class LocalAudioError(RuntimeError):
    pass


class LocalAudioBusy(LocalAudioError):
    pass


class LocalAudioNotRecording(LocalAudioError):
    pass


class LocalAudioController:
    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        capture_device: str = "default",
        playback_device: str = "default",
        no_speech_prompt: bytes | None = None,
        process_factory: ProcessFactory = asyncio.create_subprocess_exec,
        process_stop_seconds: float = 5.0,
    ) -> None:
        self._sample_rate = sample_rate
        self._capture_device = capture_device
        self._playback_device = playback_device
        self._no_speech_prompt = no_speech_prompt
        self._process_factory = process_factory
        self._process_stop_seconds = process_stop_seconds
        self._record_process: asyncio.subprocess.Process | None = None
        self._recording_path: Path | None = None
        self._playback_process: asyncio.subprocess.Process | None = None
        self._start_cue = _create_chirp_wav(
            sample_rate=sample_rate,
            start_frequency=700,
            end_frequency=1000,
        )
        self._stop_cue = _create_chirp_wav(
            sample_rate=sample_rate,
            start_frequency=1000,
            end_frequency=700,
        )

    @property
    def is_recording(self) -> bool:
        return self._record_process is not None

    @property
    def is_playing(self) -> bool:
        return self._playback_process is not None

    async def start_recording(self) -> None:
        if self.is_recording or self.is_playing:
            raise LocalAudioBusy("音频设备正在使用")

        descriptor, raw_path = tempfile.mkstemp(suffix=".wav")
        os.close(descriptor)
        path = Path(raw_path)
        path.chmod(0o600)
        command = ["pw-record"]
        if self._capture_device != "default":
            command.extend(["--target", self._capture_device])
        command.extend(
            [
                "--rate",
                str(self._sample_rate),
                "--channels",
                "1",
                "--format",
                "s16",
                str(path),
            ]
        )

        try:
            process = await self._process_factory(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.sleep(0)
            if process.returncode is not None:
                await process.communicate()
                raise LocalAudioError("无法启动录音设备，请检查默认输入")
        except (OSError, LocalAudioError) as error:
            path.unlink(missing_ok=True)
            if isinstance(error, LocalAudioError):
                raise
            raise LocalAudioError("无法启动录音设备，请检查默认输入") from error

        self._record_process = process
        self._recording_path = path

    async def stop_recording(self) -> bytes:
        if self._record_process is None or self._recording_path is None:
            raise LocalAudioNotRecording("当前没有正在进行的录音")

        process = self._record_process
        path = self._recording_path
        self._record_process = None
        self._recording_path = None
        try:
            interrupted = process.returncode is None
            if process.returncode is None:
                process.send_signal(signal.SIGINT)
                await self._wait_for_exit(process)
            accepted_returncodes = {0, -signal.SIGINT, 128 + signal.SIGINT}
            if interrupted:
                accepted_returncodes.add(1)
            if process.returncode not in accepted_returncodes:
                raise LocalAudioError("录音进程异常退出，请检查默认输入")
            audio = path.read_bytes()
            if not audio:
                raise LocalAudioError("录音文件为空，请检查默认输入")
            return audio
        except OSError as error:
            raise LocalAudioError("无法读取录音，请重新尝试") from error
        finally:
            path.unlink(missing_ok=True)

    async def abort_recording(self) -> None:
        process = self._record_process
        path = self._recording_path
        self._record_process = None
        self._recording_path = None
        try:
            if process is not None and process.returncode is None:
                process.send_signal(signal.SIGINT)
                await self._wait_for_exit(process)
        finally:
            if path is not None:
                path.unlink(missing_ok=True)

    async def play(self, audio: bytes) -> None:
        if self.is_recording or self.is_playing:
            raise LocalAudioBusy("音频设备正在使用")

        command = ["pw-play"]
        if self._playback_device != "default":
            command.extend(["--target", self._playback_device])
        command.append("-")
        try:
            process = await self._process_factory(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            raise LocalAudioError("无法启动扬声器，请检查默认输出") from error

        self._playback_process = process
        try:
            await process.communicate(input=audio)
            if process.returncode != 0:
                raise LocalAudioError("扬声器播放失败，请检查默认输出")
        finally:
            self._playback_process = None

    async def play_start_cue(self) -> None:
        await self.play(self._start_cue)

    async def play_stop_cue(self) -> None:
        await self.play(self._stop_cue)

    async def play_no_speech_prompt(self) -> None:
        if self._no_speech_prompt is None:
            raise LocalAudioError("未配置无语音提示音频")
        await self.play(self._no_speech_prompt)

    async def stop_playback(self) -> None:
        process = self._playback_process
        self._playback_process = None
        if process is None or process.returncode is not None:
            return
        process.terminate()
        await self._wait_for_exit(process)

    async def aclose(self) -> None:
        await self.abort_recording()
        await self.stop_playback()

    async def _wait_for_exit(self, process: Any) -> None:
        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=self._process_stop_seconds,
            )
            return
        except TimeoutError:
            logger.warning("audio_process_stop_timeout")

        process.terminate()
        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=self._process_stop_seconds,
            )
            return
        except TimeoutError:
            logger.warning("audio_process_terminate_timeout")

        process.kill()
        await process.wait()
