import asyncio
import io
import time
import wave
from dataclasses import dataclass

import httpx

from showroom_guide.audio_store import AudioStore
from showroom_guide.controller import GuideController, QuestionInProgress
from showroom_guide.latency import DeviceLatencyRecorder
from showroom_guide.models import GuidePhase, GuideSnapshot
from showroom_guide.state import GuideStateStore


class InvalidDeviceAudio(ValueError):
    pass


class DeviceTranscriptionUnavailable(RuntimeError):
    pass


NO_SPEECH_MESSAGE = "没有听清您的声音，请靠近麦克风后再试一次。"


class NoSpeechDetected(RuntimeError):
    pass


@dataclass(frozen=True)
class DeviceTurnResult:
    transcript: str
    answer: str
    audio_id: str | None
    warning: str | None = None


def validate_wav(audio: bytes) -> None:
    if len(audio) < 12 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
        raise InvalidDeviceAudio("必须上传有效的 WAV 文件")
    try:
        with wave.open(io.BytesIO(audio), "rb") as source:
            channels = source.getnchannels()
            sample_width = source.getsampwidth()
            sample_rate = source.getframerate()
            compression = source.getcomptype()
    except (EOFError, wave.Error) as error:
        message = "WAV 必须使用 PCM 编码" if b"fmt " in audio else "WAV 文件已损坏"
        raise InvalidDeviceAudio(message) from error

    if compression != "NONE":
        raise InvalidDeviceAudio("WAV 必须使用 PCM 编码")
    if channels != 1:
        raise InvalidDeviceAudio("WAV 必须为单声道")
    if sample_width != 2:
        raise InvalidDeviceAudio("WAV 必须为 16-bit")
    if sample_rate != 16000:
        raise InvalidDeviceAudio("WAV 采样率必须为 16 kHz")


class DeviceVoiceSession:
    def __init__(
        self,
        *,
        state,
        controller,
        speech,
        audio,
        metrics: DeviceLatencyRecorder | None = None,
        clock=time.perf_counter,
    ) -> None:
        self._state: GuideStateStore = state
        self._controller: GuideController = controller
        self._speech = speech
        self._audio: AudioStore = audio
        self._metrics = metrics or DeviceLatencyRecorder()
        self._clock = clock
        self._turn_lock = asyncio.Lock()

    @property
    def snapshot(self) -> GuideSnapshot:
        return self._state.snapshot

    async def begin_recording(self) -> None:
        if self._turn_lock.locked() or self._controller.is_busy:
            raise QuestionInProgress("已有设备问题正在处理中")
        await self._state.start_recording()

    async def process_wav(self, audio: bytes) -> DeviceTurnResult:
        return await self._run_timed_turn(audio, start_recording=True)

    async def process_recorded_wav(self, audio: bytes) -> DeviceTurnResult:
        return await self._run_timed_turn(audio, start_recording=False)

    async def _run_timed_turn(
        self,
        audio: bytes,
        *,
        start_recording: bool,
    ) -> DeviceTurnResult:
        timing = self._metrics.start_turn(self._clock)
        outcome = "error"
        try:
            result = await self._process_wav(
                audio,
                timing,
                start_recording=start_recording,
            )
        except BaseException as error:
            if timing.error_type is None:
                timing.fail("pipeline", error)
            raise
        else:
            outcome = "degraded" if result.warning is not None else "success"
            return result
        finally:
            timing.finish_pipeline()
            self._metrics.record(
                timing,
                outcome,
                complete_success=outcome == "success" and result.audio_id is not None,
            )

    async def _process_wav(
        self,
        audio: bytes,
        timing,
        *,
        start_recording: bool,
    ) -> DeviceTurnResult:
        if self._turn_lock.locked():
            raise QuestionInProgress("已有设备问题正在处理中")

        async with self._turn_lock:
            validate_wav(audio)
            if start_recording:
                await self._state.start_recording()
            elif self._state.snapshot.phase is not GuidePhase.RECORDING:
                raise InvalidDeviceAudio("设备当前没有正在进行的录音")
            await self._state.transition(GuidePhase.TRANSCRIBING)
            await self._state.set_message("正在识别您的问题")
            timing.start_asr()
            try:
                transcript = (await self._speech.transcribe(io.BytesIO(audio))).strip()
                if not transcript:
                    raise NoSpeechDetected(NO_SPEECH_MESSAGE)
            except httpx.HTTPError as error:
                timing.fail("asr", error)
                await self._state.transition(GuidePhase.ERROR)
                await self._state.set_message("语音识别暂时不可用，请稍后重试")
                raise DeviceTranscriptionUnavailable from error
            except NoSpeechDetected as error:
                timing.fail("asr", error)
                await self._state.transition(GuidePhase.ERROR)
                await self._state.set_message(NO_SPEECH_MESSAGE)
                raise
            finally:
                timing.finish_asr()
            await self._state.set_transcript(transcript)
            result = await self._controller.ask_text(transcript, timing=timing)
            audio_id = self._audio.put(result.audio) if result.audio is not None else None
            return DeviceTurnResult(
                transcript=transcript,
                answer=result.answer,
                audio_id=audio_id,
                warning=result.warning,
            )

    def metrics_snapshot(self) -> dict[str, object]:
        return self._metrics.snapshot()

    def get_audio(self, audio_id: str) -> bytes:
        return self._audio.get(audio_id)

    async def finish_playback(self) -> None:
        await self._controller.finish_playback()

    async def fail_playback(self) -> None:
        if self._state.snapshot.phase is GuidePhase.SPEAKING:
            await self._state.transition(GuidePhase.ERROR)
        await self._state.set_message("扬声器播放失败，请检查默认输出设备")

    async def fail_recording(self, message: str) -> None:
        if self._state.snapshot.phase is GuidePhase.RECORDING:
            await self._state.transition(GuidePhase.ERROR)
        await self._state.set_message(message)

    async def reset(self) -> None:
        if self._turn_lock.locked():
            raise QuestionInProgress("设备问题正在处理中")
        await self._controller.reset()
        self._audio.clear()

    async def clear(self) -> None:
        await self.reset()
