from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from showroom_guide.verdict import (
    Verdict,
    VerdictBasis,
    VerdictFailure,
    VerdictScope,
)


class GuidePhase(StrEnum):
    IDLE = "idle"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    SPEAKING = "speaking"
    DEGRADED = "degraded"
    ERROR = "error"


class InteractionMode(StrEnum):
    CONVERSATION = "conversation"
    VERDICT = "verdict"
    KNOWLEDGE = "knowledge"


class VerdictPhase(StrEnum):
    IDLE = "idle"
    THINKING = "thinking"
    WINDUP = "windup"
    DECISIVE = "decisive"
    HOLDING = "holding"
    NEUTRAL = "neutral"
    FAILED = "failed"


class GuideSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    phase: GuidePhase = GuidePhase.IDLE
    transcript: str = ""
    answer: str = ""
    message: str = "输入问题开始讲解"
    interaction_mode: InteractionMode = InteractionMode.CONVERSATION
    verdict_phase: VerdictPhase = VerdictPhase.IDLE
    verdict: Verdict = Verdict.NEUTRAL
    verdict_scope: VerdictScope | None = None
    verdict_basis: VerdictBasis = VerdictBasis.NONE
    verdict_reason: str = ""
    verdict_evidence: str = ""
    verdict_failure: VerdictFailure | None = None
    verdict_generation: int = 0
    verdict_elapsed_ms: float | None = None
