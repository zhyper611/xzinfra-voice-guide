from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class GuidePhase(StrEnum):
    IDLE = "idle"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    SPEAKING = "speaking"
    DEGRADED = "degraded"
    ERROR = "error"


class GuideSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    phase: GuidePhase = GuidePhase.IDLE
    transcript: str = ""
    answer: str = ""
    message: str = "输入问题开始讲解"
