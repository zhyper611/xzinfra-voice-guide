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


_PROMPTS = {
    VerdictFailure.INVALID_QUESTION: "verdict-invalid",
    VerdictFailure.INSUFFICIENT_EVIDENCE: "verdict-insufficient-evidence",
    VerdictFailure.HIGH_RISK: "verdict-high-risk",
    VerdictFailure.SERVICE_FAILURE: "verdict-unavailable",
}


class VerdictWorkflow:
    def __init__(
        self,
        client,
        motion: VerdictMotionOutput,
        state: GuideStateStore,
        *,
        play_prompt: Callable[[str], object] | None = None,
        timeout_seconds: float = 15.0,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._client = client
        self._motion = motion
        self._state = state
        self._play_prompt = play_prompt
        self._timeout_seconds = timeout_seconds
        self._clock = clock
        self._generation = 0

    @property
    def motion(self) -> VerdictMotionOutput:
        return self._motion

    async def enter(self) -> None:
        self._generation += 1
        await self._motion.reset()
        await self._state.set_interaction_mode(InteractionMode.VERDICT)

    async def run(self, transcript: str) -> VerdictDecision:
        question = transcript.strip()
        if not question:
            raise ValueError("transcript must not be empty")
        self._generation += 1
        generation = self._generation
        started_at = self._clock()
        await self._state.begin_verdict(question, generation=generation)
        await self._motion.thinking(generation)
        try:
            async with asyncio.timeout(self._timeout_seconds):
                decision = await self._client.decide(question)
        except (TimeoutError, VerdictClientError):
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
        if failure is None or self._play_prompt is None:
            return
        prompt = _PROMPTS[failure]
        try:
            result = self._play_prompt(prompt)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("verdict_prompt_playback_failed prompt=%s", prompt)
