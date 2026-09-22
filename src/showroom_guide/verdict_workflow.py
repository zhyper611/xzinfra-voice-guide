import asyncio
import inspect
import logging
import time
from collections.abc import Callable

from showroom_guide.models import GuidePhase, InteractionMode, VerdictPhase
from showroom_guide.state import GuideStateStore
from showroom_guide.verdict import (
    Verdict,
    VerdictClientError,
    VerdictDecision,
    VerdictFailure,
)
from showroom_guide.verdict_motion import (
    VerdictMotionEvent,
    VerdictMotionOutput,
)


logger = logging.getLogger(__name__)


class VerdictInProgress(RuntimeError):
    pass


_PROMPTS = {
    VerdictFailure.INVALID_QUESTION: (
        "verdict-invalid",
        "这个问题不适合进行是非判断，我先保持中立。",
    ),
    VerdictFailure.INSUFFICIENT_EVIDENCE: (
        "verdict-insufficient-evidence",
        "知识库依据不足，我先保持中立。",
    ),
    VerdictFailure.HIGH_RISK: (
        "verdict-high-risk",
        "这个问题风险较高，不适合进行是非判断，我先保持中立。",
    ),
    VerdictFailure.SERVICE_FAILURE: (
        "verdict-unavailable",
        "判断服务暂时不可用，我先保持中立。",
    ),
}


class VerdictWorkflow:
    def __init__(
        self,
        client,
        motion: VerdictMotionOutput,
        state: GuideStateStore,
        *,
        play_prompt: Callable[[str], object] | None = None,
        speak_prompt: Callable[[str], object] | None = None,
        timeout_seconds: float = 30.0,
        prompt_timeout_seconds: float = 5.0,
        speech_timeout_seconds: float = 20.0,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._client = client
        self._motion = motion
        self._state = state
        self._play_prompt = play_prompt
        self._speak_prompt = speak_prompt
        self._timeout_seconds = timeout_seconds
        self._prompt_timeout_seconds = prompt_timeout_seconds
        self._speech_timeout_seconds = speech_timeout_seconds
        self._clock = clock
        self._generation = 0
        self._thinking_generation: int | None = None
        self._run_lock = asyncio.Lock()

    @property
    def motion(self) -> VerdictMotionOutput:
        return self._motion

    @property
    def is_busy(self) -> bool:
        return self._run_lock.locked()

    async def enter(self) -> None:
        self._generation += 1
        self._thinking_generation = None
        await self._motion.reset()
        await self._state.set_interaction_mode(InteractionMode.VERDICT)

    async def start_thinking(self) -> int:
        if self._thinking_generation is not None:
            return self._thinking_generation
        self._generation += 1
        generation = self._generation
        self._thinking_generation = generation
        await self._motion.thinking(generation)
        return generation

    async def cancel_thinking(self, generation: int) -> bool:
        if self._thinking_generation != generation:
            return False
        self._thinking_generation = None
        self._generation += 1
        await self._motion.reset()
        return True

    async def run(
        self,
        transcript: str,
        *,
        thinking_generation: int | None = None,
    ) -> VerdictDecision:
        if self._run_lock.locked():
            raise VerdictInProgress("已有是非判断正在处理中")
        async with self._run_lock:
            return await self._run(
                transcript,
                thinking_generation=thinking_generation,
            )

    async def _run(
        self,
        transcript: str,
        *,
        thinking_generation: int | None,
    ) -> VerdictDecision:
        question = transcript.strip()
        if not question:
            raise ValueError("transcript must not be empty")
        prestarted = thinking_generation is not None
        if thinking_generation is None:
            self._generation += 1
            generation = self._generation
        else:
            if self._thinking_generation != thinking_generation:
                raise asyncio.CancelledError
            generation = thinking_generation
            self._thinking_generation = None
        started_at = self._clock()
        await self._state.begin_verdict(question, generation=generation)
        if not prestarted:
            await self._motion.thinking(generation)
        try:
            async with asyncio.timeout(self._timeout_seconds):
                decision = await self._client.decide(question)
        except TimeoutError:
            logger.warning(
                "verdict_workflow_timeout timeout_seconds=%s",
                self._timeout_seconds,
            )
            decision = VerdictDecision.service_failure()
        except VerdictClientError:
            decision = VerdictDecision.service_failure()
        if generation != self._generation:
            raise asyncio.CancelledError

        if decision.verdict is Verdict.NEUTRAL:
            await self._motion.show_neutral(generation)
            phase = (
                VerdictPhase.FAILED
                if decision.failure is VerdictFailure.SERVICE_FAILURE
                else VerdictPhase.NEUTRAL
            )
        else:
            await self._motion.show_verdict(decision.verdict, generation)
            phase = VerdictPhase.HOLDING
        elapsed_ms = (self._clock() - started_at) * 1000
        await self._state.finish_verdict(
            decision,
            phase,
            elapsed_ms=elapsed_ms,
        )
        await self._play_failure_prompt(decision.failure)
        return decision

    async def leave(self) -> None:
        self._generation += 1
        self._thinking_generation = None
        await self._motion.reset()
        await self._state.set_verdict_motion(
            VerdictMotionEvent(
                phase=VerdictPhase.IDLE,
                verdict=Verdict.NEUTRAL,
                generation=self._generation,
            )
        )
        await self._state.set_interaction_mode(InteractionMode.CONVERSATION)
        if self._state.snapshot.phase is not GuidePhase.IDLE:
            await self._state.transition(GuidePhase.IDLE)
        await self._state.set_message("输入问题开始讲解")

    async def _play_failure_prompt(
        self,
        failure: VerdictFailure | None,
    ) -> None:
        if failure is None:
            return
        prompt, text = _PROMPTS[failure]
        if self._speak_prompt is not None:
            try:
                result = self._speak_prompt(text)
                if inspect.isawaitable(result):
                    async with asyncio.timeout(self._speech_timeout_seconds):
                        await result
                return
            except Exception:
                logger.exception(
                    "verdict_tts_playback_failed prompt=%s",
                    prompt,
                )
        if self._play_prompt is None:
            return
        try:
            result = self._play_prompt(prompt)
            if inspect.isawaitable(result):
                async with asyncio.timeout(self._prompt_timeout_seconds):
                    await result
        except Exception:
            logger.exception("verdict_prompt_playback_failed prompt=%s", prompt)
