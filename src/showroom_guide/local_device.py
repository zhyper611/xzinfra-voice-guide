import asyncio
import io
import logging
import math
import struct
import wave
from collections.abc import Awaitable, Callable
from contextlib import suppress
from enum import StrEnum

from showroom_guide.controller import (
    GuideServiceUnavailable,
    QuestionInProgress,
)
from showroom_guide.device import (
    DeviceTranscriptionUnavailable,
    DeviceTurnResult,
    DeviceVoiceSession,
    InvalidDeviceAudio,
    NO_SPEECH_MESSAGE,
    NoSpeechDetected,
    validate_wav,
)
from showroom_guide.local_audio import (
    LocalAudioController,
    LocalAudioError,
    LocalAudioNotRecording,
)
from showroom_guide.models import GuidePhase, GuideSnapshot


logger = logging.getLogger(__name__)


class LastRecordingNotFound(RuntimeError):
    pass


class LocalDeviceMode(StrEnum):
    IDLE = "idle"
    RECORDING = "recording"
    PROCESSING = "processing"
    PLAYING = "playing"
    RESETTING = "resetting"


class LocalDeviceWorkflow:
    def __init__(
        self,
        *,
        session: DeviceVoiceSession,
        audio: LocalAudioController,
        max_recording_seconds: float = 60.0,
        min_recording_seconds: float = 0.5,
        min_recording_dbfs: float = -45.0,
    ) -> None:
        self._session = session
        self._audio = audio
        self._max_recording_seconds = max_recording_seconds
        self._min_recording_seconds = min_recording_seconds
        self._min_recording_dbfs = min_recording_dbfs
        self._mode = LocalDeviceMode.IDLE
        self._lifecycle_lock = asyncio.Lock()
        self._timeout_task: asyncio.Task[None] | None = None
        self._playback_task: asyncio.Task[None] | None = None
        self._cue_task: asyncio.Task[None] | None = None
        self._last_recording: bytes | None = None

    @property
    def is_recording(self) -> bool:
        return self._mode is LocalDeviceMode.RECORDING

    @property
    def is_idle(self) -> bool:
        return self._mode is LocalDeviceMode.IDLE and self._session.snapshot.phase in {
            GuidePhase.IDLE,
            GuidePhase.ERROR,
            GuidePhase.DEGRADED,
        }

    @property
    def has_last_recording(self) -> bool:
        return self._last_recording is not None

    async def start_recording(self) -> GuideSnapshot:
        async with self._lifecycle_lock:
            self._ensure_can_start()
            self._mode = LocalDeviceMode.RECORDING
            try:
                await self._play_cue_safely(
                    self._audio.play_start_cue,
                    "start",
                )
                await self._audio.start_recording()
                self._last_recording = None
                await self._session.begin_recording()
            except BaseException:
                await self._audio.abort_recording()
                self._mode = LocalDeviceMode.IDLE
                raise
            self._timeout_task = asyncio.create_task(
                self._stop_after_timeout(),
                name="local-recording-timeout",
            )
            return self._session.snapshot

    async def stop_recording(self) -> DeviceTurnResult:
        return await self._complete_recording()

    async def replay_last_recording(self) -> None:
        async with self._lifecycle_lock:
            self._ensure_can_start()
            if self._last_recording is None:
                raise LastRecordingNotFound("没有可播放的录音")
            recording = self._last_recording
            self._mode = LocalDeviceMode.PLAYING

        try:
            await self._audio.play(recording)
        finally:
            async with self._lifecycle_lock:
                if self._mode is LocalDeviceMode.PLAYING:
                    self._mode = LocalDeviceMode.IDLE

    async def process_upload(self, audio: bytes) -> DeviceTurnResult:
        async with self._lifecycle_lock:
            self._ensure_can_start()
            self._mode = LocalDeviceMode.PROCESSING
        try:
            return await self._session.process_wav(audio)
        finally:
            async with self._lifecycle_lock:
                self._mode = LocalDeviceMode.IDLE

    async def reset(self) -> None:
        async with self._lifecycle_lock:
            if self._mode is LocalDeviceMode.PROCESSING:
                raise QuestionInProgress("设备问题正在处理中")
            previous_mode = self._mode
            self._mode = LocalDeviceMode.RESETTING
            timeout_task = self._take_timeout_task()
            playback_task = self._playback_task
            self._playback_task = None
            cue_task = self._take_cue_task()

        await self._cancel_task(timeout_task)
        if previous_mode is LocalDeviceMode.RECORDING:
            await self._audio.abort_recording()
        if previous_mode is LocalDeviceMode.PLAYING:
            if playback_task is not None:
                playback_task.cancel()
            await self._audio.stop_playback()
            await self._cancel_task(playback_task)
        if cue_task is not None:
            await self._audio.stop_playback()
            await self._cancel_task(cue_task)
        try:
            await self._session.reset()
        finally:
            async with self._lifecycle_lock:
                self._last_recording = None
                self._mode = LocalDeviceMode.IDLE

    async def aclose(self) -> None:
        async with self._lifecycle_lock:
            self._mode = LocalDeviceMode.RESETTING
            timeout_task = self._take_timeout_task()
            playback_task = self._playback_task
            self._playback_task = None
            cue_task = self._take_cue_task()
        await self._cancel_task(timeout_task)
        if playback_task is not None:
            playback_task.cancel()
        if cue_task is not None:
            await self._audio.stop_playback()
            cue_task.cancel()
        await self._audio.aclose()
        await self._cancel_task(playback_task)
        await self._cancel_task(cue_task)
        await self._session.clear()
        async with self._lifecycle_lock:
            self._last_recording = None
            self._mode = LocalDeviceMode.IDLE

    async def _complete_recording(self) -> DeviceTurnResult:
        async with self._lifecycle_lock:
            if self._mode is LocalDeviceMode.IDLE:
                raise LocalAudioNotRecording("当前没有正在进行的录音")
            if self._mode is not LocalDeviceMode.RECORDING:
                raise QuestionInProgress("设备正在处理上一轮录音")
            self._mode = LocalDeviceMode.PROCESSING
            timeout_task = self._take_timeout_task()

        current_task = asyncio.current_task()
        if timeout_task is not current_task:
            await self._cancel_task(timeout_task)

        cue_task = None
        try:
            captured = await self._audio.stop_recording()
            cue_task = self._start_cue_task(
                self._audio.play_stop_cue,
                "stop",
            )
            duration_seconds = self._wav_duration_seconds(captured)
            self._last_recording = captured
            if duration_seconds < self._min_recording_seconds:
                raise InvalidDeviceAudio(
                    "录音时间太短，请听到开始提示音后再说话。"
                )
            if self._wav_dbfs(captured) < self._min_recording_dbfs:
                raise NoSpeechDetected(NO_SPEECH_MESSAGE)
        except NoSpeechDetected as error:
            await self._wait_for_cue(cue_task)
            await self._session.fail_recording(str(error))
            await self._play_no_speech_prompt_safely()
            async with self._lifecycle_lock:
                self._mode = LocalDeviceMode.IDLE
            raise
        except (InvalidDeviceAudio, LocalAudioError) as error:
            await self._wait_for_cue(cue_task)
            await self._session.fail_recording(str(error))
            async with self._lifecycle_lock:
                self._mode = LocalDeviceMode.IDLE
            raise

        try:
            result = await self._session.process_recorded_wav(captured)
        except NoSpeechDetected:
            await self._wait_for_cue(cue_task)
            await self._play_no_speech_prompt_safely()
            async with self._lifecycle_lock:
                self._mode = LocalDeviceMode.IDLE
            raise
        except DeviceTranscriptionUnavailable:
            await self._wait_for_cue(cue_task)
            await self._play_prompt_safely("asr-unavailable")
            async with self._lifecycle_lock:
                self._mode = LocalDeviceMode.IDLE
            raise
        except GuideServiceUnavailable:
            await self._wait_for_cue(cue_task)
            await self._play_prompt_safely("guide-unavailable")
            async with self._lifecycle_lock:
                self._mode = LocalDeviceMode.IDLE
            raise
        except BaseException:
            await self._wait_for_cue(cue_task)
            async with self._lifecycle_lock:
                self._mode = LocalDeviceMode.IDLE
            raise

        await self._wait_for_cue(cue_task)

        if result.audio_id is None:
            if result.warning is not None:
                await self._play_prompt_safely("tts-unavailable")
            await self._session.finish_playback()
            async with self._lifecycle_lock:
                self._mode = LocalDeviceMode.IDLE
            return result

        playback_audio = self._session.get_audio(result.audio_id)
        async with self._lifecycle_lock:
            self._mode = LocalDeviceMode.PLAYING
            self._playback_task = asyncio.create_task(
                self._play_and_finish(playback_audio),
                name="local-device-playback",
            )
        return result

    async def _play_and_finish(self, audio: bytes) -> None:
        try:
            await self._audio.play(audio)
            await self._session.finish_playback()
        except LocalAudioError:
            logger.exception("local_device_playback_failed")
            await self._session.fail_playback()
        finally:
            async with self._lifecycle_lock:
                self._playback_task = None
                if self._mode is LocalDeviceMode.PLAYING:
                    self._mode = LocalDeviceMode.IDLE

    async def _stop_after_timeout(self) -> None:
        try:
            await asyncio.sleep(self._max_recording_seconds)
            await self._complete_recording()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("local_recording_timeout_processing_failed")

    def _ensure_can_start(self) -> None:
        if self._mode is not LocalDeviceMode.IDLE:
            raise QuestionInProgress("设备正在处理上一轮录音")
        if self._session.snapshot.phase not in {
            GuidePhase.IDLE,
            GuidePhase.ERROR,
            GuidePhase.DEGRADED,
        }:
            raise QuestionInProgress("设备正在处理上一轮录音")

    def _take_timeout_task(self) -> asyncio.Task[None] | None:
        task = self._timeout_task
        self._timeout_task = None
        return task

    def _start_cue_task(
        self,
        cue: Callable[[], Awaitable[None]],
        cue_name: str,
    ) -> asyncio.Task[None]:
        task = asyncio.create_task(
            self._play_cue_safely(cue, cue_name),
            name=f"local-{cue_name}-cue",
        )
        self._cue_task = task
        return task

    async def _wait_for_cue(self, task: asyncio.Task[None] | None) -> None:
        if task is None:
            return
        try:
            await task
        finally:
            if self._cue_task is task:
                self._cue_task = None

    def _take_cue_task(self) -> asyncio.Task[None] | None:
        task = self._cue_task
        self._cue_task = None
        return task

    @staticmethod
    async def _play_cue_safely(
        cue: Callable[[], Awaitable[None]],
        cue_name: str,
    ) -> None:
        try:
            await cue()
        except Exception:
            logger.warning(
                "local_audio_cue_failed",
                extra={"cue": cue_name},
                exc_info=True,
            )

    async def _play_no_speech_prompt_safely(self) -> None:
        try:
            await self._audio.play_no_speech_prompt()
        except Exception:
            logger.warning(
                "local_no_speech_prompt_failed",
                exc_info=True,
            )

    async def _play_prompt_safely(self, name: str) -> None:
        try:
            await self._audio.play_prompt(name)
        except Exception:
            logger.warning(
                "local_audio_prompt_failed",
                extra={"prompt": name},
                exc_info=True,
            )

    @staticmethod
    async def _cancel_task(task: asyncio.Task[None] | None) -> None:
        if task is None or task is asyncio.current_task():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    @staticmethod
    def _wav_duration_seconds(audio: bytes) -> float:
        validate_wav(audio)
        with wave.open(io.BytesIO(audio), "rb") as source:
            return source.getnframes() / source.getframerate()

    @staticmethod
    def _wav_dbfs(audio: bytes) -> float:
        with wave.open(io.BytesIO(audio), "rb") as source:
            frames = source.readframes(source.getnframes())
        sample_count = len(frames) // 2
        if sample_count == 0:
            return -math.inf
        square_sum = sum(
            sample * sample
            for (sample,) in struct.iter_unpack("<h", frames)
        )
        rms = math.sqrt(square_sum / sample_count)
        if rms == 0:
            return -math.inf
        return 20 * math.log10(rms / 32768)
