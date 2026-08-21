import asyncio
import io
import logging
import math
import struct
import wave
from dataclasses import dataclass
from enum import StrEnum

from showroom_guide.device import (
    InvalidDeviceAudio,
    NO_SPEECH_MESSAGE,
    NoSpeechDetected,
    validate_wav,
)
from showroom_guide.knowledge_capture import (
    KnowledgeAsrUnavailable,
    KnowledgeTtsUnavailable,
)
from showroom_guide.knowledge_outbox import KnowledgeEntry
from showroom_guide.local_audio import LocalAudioError


logger = logging.getLogger(__name__)


class KnowledgeModeState(StrEnum):
    INACTIVE = "inactive"
    READY = "ready"
    RECORDING = "recording"
    PROCESSING = "processing"
    CONFIRMING = "confirming"


class KnowledgeProcessingStage(StrEnum):
    TRANSCRIBING = "transcribing"
    SYNTHESIZING = "synthesizing"
    PLAYING_REVIEW = "playing_review"


@dataclass(frozen=True)
class KnowledgeLongPressResult:
    exited: bool
    saved_entry: KnowledgeEntry | None


class KnowledgeModeWorkflow:
    def __init__(
        self,
        audio,
        capture_session,
        *,
        max_recording_seconds: float = 60.0,
        min_recording_seconds: float = 0.5,
        min_recording_dbfs: float = -45.0,
    ) -> None:
        self._audio = audio
        self._capture = capture_session
        self._max_recording_seconds = max_recording_seconds
        self._min_recording_seconds = min_recording_seconds
        self._min_recording_dbfs = min_recording_dbfs
        self._state = KnowledgeModeState.INACTIVE
        self._operation_lock = asyncio.Lock()
        self._timeout_task: asyncio.Task[None] | None = None
        self._processing_stage: KnowledgeProcessingStage | None = None

    @property
    def state(self) -> KnowledgeModeState:
        return self._state

    @property
    def processing_stage(self) -> KnowledgeProcessingStage | None:
        return self._processing_stage

    @property
    def draft_text(self) -> str | None:
        return self._capture.draft_text

    async def enter(self) -> None:
        async with self._operation_lock:
            if self._state is not KnowledgeModeState.INACTIVE:
                raise RuntimeError("知识补充模式已经启用")
            self._capture.clear()
            self._state = KnowledgeModeState.READY
            await self._play_named_prompt_safely("knowledge-mode")

    async def short_press(self) -> None:
        async with self._operation_lock:
            if self._state in {
                KnowledgeModeState.READY,
                KnowledgeModeState.CONFIRMING,
            }:
                await self._start_recording()
                return
            if self._state is KnowledgeModeState.RECORDING:
                await self._finish_recording()

    async def long_press(self) -> KnowledgeLongPressResult:
        async with self._operation_lock:
            if self._state is KnowledgeModeState.READY and not self._capture.has_draft:
                self._capture.clear()
                self._state = KnowledgeModeState.INACTIVE
                await self._play_cue_safely(self._audio.play_stop_cue)
                return KnowledgeLongPressResult(exited=True, saved_entry=None)
            if (
                self._state is KnowledgeModeState.CONFIRMING
                and self._capture.has_draft
            ):
                entry = self._capture.save()
                self._state = KnowledgeModeState.INACTIVE
                await self._play_named_prompt_safely("knowledge-saved")
                return KnowledgeLongPressResult(exited=True, saved_entry=entry)
            return KnowledgeLongPressResult(exited=False, saved_entry=None)

    async def cancel(self) -> None:
        caller_cancellation = None
        timeout_task = self._take_timeout_task()
        if timeout_task is not None and timeout_task is not asyncio.current_task():
            timeout_task.cancel()
            try:
                await timeout_task
            except asyncio.CancelledError as error:
                if asyncio.current_task().cancelling():
                    caller_cancellation = error
        try:
            async with self._operation_lock:
                try:
                    if self._state is KnowledgeModeState.RECORDING:
                        await self._audio.abort_recording()
                finally:
                    self._capture.clear()
                    self._processing_stage = None
                    self._state = KnowledgeModeState.INACTIVE
        finally:
            if caller_cancellation is not None:
                raise caller_cancellation

    async def aclose(self) -> None:
        await self.cancel()

    async def _start_recording(self) -> None:
        await self._play_cue_safely(self._audio.play_start_cue)
        await self._audio.start_recording()
        self._state = KnowledgeModeState.RECORDING
        self._timeout_task = asyncio.create_task(
            self._stop_after_timeout(),
            name="knowledge-recording-timeout",
        )

    async def _finish_recording(self) -> None:
        timeout_task = self._timeout_task
        if timeout_task is not None and timeout_task is not asyncio.current_task():
            self._timeout_task = None
            timeout_task.cancel()
        self._state = KnowledgeModeState.PROCESSING
        fallback = (
            KnowledgeModeState.CONFIRMING
            if self._capture.has_draft
            else KnowledgeModeState.READY
        )
        try:
            captured = await self._audio.stop_recording()
            await self._play_cue_safely(self._audio.play_stop_cue)
            if self._duration_seconds(captured) < self._min_recording_seconds:
                raise InvalidDeviceAudio("录音时间太短，请重新录入。")
            if self._dbfs(captured) < self._min_recording_dbfs:
                raise NoSpeechDetected(NO_SPEECH_MESSAGE)
            self._processing_stage = KnowledgeProcessingStage.TRANSCRIBING
            transcript = await self._capture.transcribe(captured)
            self._processing_stage = KnowledgeProcessingStage.SYNTHESIZING
            draft = await self._capture.synthesize_review(transcript)
            self._processing_stage = KnowledgeProcessingStage.PLAYING_REVIEW
            await self._audio.play(draft.audio)
            self._capture.accept(draft)
            self._state = KnowledgeModeState.CONFIRMING
        except NoSpeechDetected:
            self._state = fallback
            await self._play_cue_safely(self._audio.play_no_speech_prompt)
            raise
        except InvalidDeviceAudio:
            self._state = fallback
            raise
        except KnowledgeAsrUnavailable:
            self._state = fallback
            await self._play_named_prompt_safely("asr-unavailable")
            raise
        except KnowledgeTtsUnavailable:
            self._state = fallback
            await self._play_named_prompt_safely("tts-unavailable")
            raise
        except LocalAudioError:
            self._state = fallback
            await self._play_named_prompt_safely("guide-unavailable")
            raise
        except Exception:
            self._state = fallback
            await self._play_named_prompt_safely("guide-unavailable")
            raise
        except BaseException:
            self._state = fallback
            raise
        finally:
            self._processing_stage = None

    async def _stop_after_timeout(self) -> None:
        try:
            await asyncio.sleep(self._max_recording_seconds)
            async with self._operation_lock:
                if self._state is KnowledgeModeState.RECORDING:
                    await self._finish_recording()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("knowledge_recording_timeout_processing_failed")
        finally:
            if self._timeout_task is asyncio.current_task():
                self._timeout_task = None

    def _take_timeout_task(self) -> asyncio.Task[None] | None:
        task = self._timeout_task
        self._timeout_task = None
        return task

    @staticmethod
    async def _play_cue_safely(cue) -> None:
        try:
            await cue()
        except Exception:
            pass

    async def _play_named_prompt_safely(self, name: str) -> None:
        prompt = getattr(self._audio, "play_prompt", None)
        if prompt is not None:
            await self._play_cue_safely(lambda: prompt(name))

    @staticmethod
    def _duration_seconds(audio: bytes) -> float:
        validate_wav(audio)
        with wave.open(io.BytesIO(audio), "rb") as source:
            return source.getnframes() / source.getframerate()

    @staticmethod
    def _dbfs(audio: bytes) -> float:
        with wave.open(io.BytesIO(audio), "rb") as source:
            frames = source.readframes(source.getnframes())
        samples = [sample for (sample,) in struct.iter_unpack("<h", frames)]
        if not samples:
            return -math.inf
        rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
        if rms == 0:
            return -math.inf
        return 20 * math.log10(rms / 32768)
