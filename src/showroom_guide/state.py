import asyncio

from showroom_guide.models import (
    GuidePhase,
    GuideSnapshot,
    InteractionMode,
    VerdictPhase,
)
from showroom_guide.verdict import Verdict, VerdictBasis, VerdictDecision


ALLOWED = {
    GuidePhase.IDLE: {
        GuidePhase.RECORDING,
        GuidePhase.THINKING,
        GuidePhase.DEGRADED,
        GuidePhase.ERROR,
    },
    GuidePhase.RECORDING: {
        GuidePhase.TRANSCRIBING,
        GuidePhase.IDLE,
        GuidePhase.ERROR,
    },
    GuidePhase.TRANSCRIBING: {
        GuidePhase.THINKING,
        GuidePhase.IDLE,
        GuidePhase.ERROR,
    },
    GuidePhase.THINKING: {
        GuidePhase.SPEAKING,
        GuidePhase.IDLE,
        GuidePhase.DEGRADED,
        GuidePhase.ERROR,
    },
    GuidePhase.SPEAKING: {
        GuidePhase.IDLE,
        GuidePhase.RECORDING,
        GuidePhase.THINKING,
        GuidePhase.ERROR,
    },
    GuidePhase.DEGRADED: {
        GuidePhase.IDLE,
        GuidePhase.RECORDING,
        GuidePhase.THINKING,
        GuidePhase.ERROR,
    },
    GuidePhase.ERROR: {
        GuidePhase.IDLE,
        GuidePhase.RECORDING,
        GuidePhase.THINKING,
        GuidePhase.DEGRADED,
    },
}


class InvalidStateTransition(ValueError):
    pass


class GuideStateStore:
    def __init__(self) -> None:
        self._snapshot = GuideSnapshot()
        self._lock = asyncio.Lock()
        self._subscribers: set[asyncio.Queue[GuideSnapshot]] = set()

    @property
    def snapshot(self) -> GuideSnapshot:
        return self._snapshot

    def subscribe(self) -> asyncio.Queue[GuideSnapshot]:
        queue: asyncio.Queue[GuideSnapshot] = asyncio.Queue(maxsize=1)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[GuideSnapshot]) -> None:
        self._subscribers.discard(queue)

    async def transition(self, phase: GuidePhase) -> GuideSnapshot:
        async with self._lock:
            current = self._snapshot.phase
            if phase not in ALLOWED[current]:
                raise InvalidStateTransition(f"不能从 {current} 转换到 {phase}")
            return self._update_locked(phase=phase)

    async def start_text_question(self, transcript: str) -> GuideSnapshot:
        async with self._lock:
            current = self._snapshot.phase
            if GuidePhase.THINKING not in ALLOWED[current]:
                raise InvalidStateTransition(
                    f"不能从 {current} 开始文字问答"
                )
            return self._update_locked(
                phase=GuidePhase.THINKING,
                transcript=transcript,
                answer="",
                message="正在查询展项资料",
                verdict=Verdict.NEUTRAL,
            )

    async def start_recording(self) -> GuideSnapshot:
        async with self._lock:
            current = self._snapshot.phase
            if GuidePhase.RECORDING not in ALLOWED[current]:
                raise InvalidStateTransition(f"不能从 {current} 开始录音")
            return self._update_locked(
                phase=GuidePhase.RECORDING,
                transcript="",
                answer="",
                message="正在录音，再次点击后提交",
                verdict=Verdict.NEUTRAL,
            )

    async def set_transcript(self, transcript: str) -> GuideSnapshot:
        return await self._update(transcript=transcript)

    async def append_answer(self, text: str) -> GuideSnapshot:
        async with self._lock:
            return self._update_locked(answer=self._snapshot.answer + text)

    async def set_answer(self, answer: str) -> GuideSnapshot:
        return await self._update(answer=answer)

    async def set_message(self, message: str) -> GuideSnapshot:
        return await self._update(message=message)

    async def set_verdict(self, verdict: Verdict) -> GuideSnapshot:
        return await self._update(verdict=verdict)

    async def set_interaction_mode(
        self,
        mode: InteractionMode,
    ) -> GuideSnapshot:
        return await self._update(interaction_mode=mode)

    async def begin_verdict(
        self,
        transcript: str,
        *,
        generation: int,
    ) -> GuideSnapshot:
        return await self._update(
            phase=GuidePhase.THINKING,
            interaction_mode=InteractionMode.VERDICT,
            verdict_phase=VerdictPhase.THINKING,
            transcript=transcript,
            answer="",
            message="正在进行是非判断",
            verdict_scope=None,
            verdict_basis=VerdictBasis.NONE,
            verdict_reason="",
            verdict_evidence="",
            verdict_failure=None,
            verdict_generation=generation,
            verdict_elapsed_ms=None,
        )

    async def finish_verdict(
        self,
        decision: VerdictDecision,
        phase: VerdictPhase,
        *,
        elapsed_ms: float | None = None,
    ) -> GuideSnapshot:
        if decision.scope is None:
            raise ValueError("mixed verdict decision requires a scope")
        return await self._update(
            phase=GuidePhase.IDLE,
            verdict_phase=phase,
            verdict=decision.verdict,
            verdict_scope=decision.scope,
            verdict_basis=decision.basis,
            verdict_reason=decision.reason,
            verdict_evidence=decision.evidence,
            verdict_failure=decision.failure,
            verdict_elapsed_ms=elapsed_ms,
            message="判断完成",
        )

    async def set_verdict_motion(self, event: object) -> GuideSnapshot:
        phase = getattr(event, "phase", None)
        verdict = getattr(event, "verdict", None)
        generation = getattr(event, "generation", None)
        if not isinstance(phase, VerdictPhase):
            raise TypeError("motion event requires a verdict phase")
        if not isinstance(verdict, Verdict):
            raise TypeError("motion event requires a verdict")
        if not isinstance(generation, int) or generation < 0:
            raise TypeError("motion event requires a generation")
        return await self._update(
            verdict_phase=phase,
            verdict=verdict,
            verdict_generation=generation,
        )

    async def reset(self) -> GuideSnapshot:
        async with self._lock:
            self._snapshot = GuideSnapshot()
            for queue in tuple(self._subscribers):
                self._publish(queue, self._snapshot)
            return self._snapshot

    async def _update(self, **changes: object) -> GuideSnapshot:
        async with self._lock:
            return self._update_locked(**changes)

    def _update_locked(self, **changes: object) -> GuideSnapshot:
        self._snapshot = self._snapshot.model_copy(update=changes)
        for queue in tuple(self._subscribers):
            self._publish(queue, self._snapshot)
        return self._snapshot

    @staticmethod
    def _publish(
        queue: asyncio.Queue[GuideSnapshot],
        snapshot: GuideSnapshot,
    ) -> None:
        if queue.full():
            queue.get_nowait()
        queue.put_nowait(snapshot)
