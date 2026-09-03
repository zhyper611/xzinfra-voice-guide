from dataclasses import dataclass
from typing import Protocol

from showroom_guide.models import VerdictPhase
from showroom_guide.state import GuideStateStore
from showroom_guide.verdict import Verdict


@dataclass(frozen=True)
class VerdictMotionEvent:
    phase: VerdictPhase
    verdict: Verdict
    generation: int


class VerdictMotionOutput(Protocol):
    async def thinking(self, generation: int) -> None: ...

    async def show_verdict(
        self,
        verdict: Verdict,
        generation: int,
    ) -> None: ...

    async def show_neutral(self, generation: int) -> None: ...

    async def reset(self) -> None: ...


class WebSimulationOutput:
    def __init__(self, state: GuideStateStore) -> None:
        self._state = state
        self._generation = 0

    async def thinking(self, generation: int) -> None:
        self._generation = generation
        await self._publish(VerdictPhase.THINKING, Verdict.NEUTRAL, generation)

    async def show_verdict(
        self,
        verdict: Verdict,
        generation: int,
    ) -> None:
        if verdict is Verdict.NEUTRAL:
            raise ValueError("show_verdict requires yes or no")
        await self._publish(VerdictPhase.HOLDING, verdict, generation)

    async def show_neutral(self, generation: int) -> None:
        await self._publish(VerdictPhase.NEUTRAL, Verdict.NEUTRAL, generation)

    async def reset(self) -> None:
        self._generation += 1
        await self._state.set_verdict_motion(
            VerdictMotionEvent(
                phase=VerdictPhase.IDLE,
                verdict=Verdict.NEUTRAL,
                generation=self._generation,
            )
        )

    async def _publish(
        self,
        phase: VerdictPhase,
        verdict: Verdict,
        generation: int,
    ) -> None:
        if generation != self._generation:
            return
        await self._state.set_verdict_motion(
            VerdictMotionEvent(
                phase=phase,
                verdict=verdict,
                generation=generation,
            )
        )
