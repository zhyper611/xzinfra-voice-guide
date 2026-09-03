from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class Verdict(StrEnum):
    YES = "yes"
    NEUTRAL = "neutral"
    NO = "no"


class VerdictMode(StrEnum):
    EXHIBITION = "exhibition"
    CASUAL = "casual"


class VerdictScope(StrEnum):
    EXHIBITION = "exhibition"
    CASUAL = "casual"
    INVALID = "invalid"


class VerdictBasis(StrEnum):
    KNOWLEDGE_BASE = "knowledge_base"
    GENERAL = "general"
    NONE = "none"


class VerdictFailure(StrEnum):
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    HIGH_RISK = "high_risk"
    INVALID_QUESTION = "invalid_question"
    SERVICE_FAILURE = "service_failure"


@dataclass(frozen=True)
class VerdictDecision:
    mode: VerdictMode
    verdict: Verdict
    reason: str
    spoken_answer: str | None = None
    scope: VerdictScope | None = None
    basis: VerdictBasis = VerdictBasis.NONE
    evidence: str = ""
    failure: VerdictFailure | None = None

    @classmethod
    def mixed(
        cls,
        *,
        scope: VerdictScope,
        verdict: Verdict,
        basis: VerdictBasis,
        reason: str,
        evidence: str = "",
        failure: VerdictFailure | None = None,
    ) -> "VerdictDecision":
        mode = (
            VerdictMode.CASUAL
            if scope is VerdictScope.CASUAL
            else VerdictMode.EXHIBITION
        )
        return cls(
            mode=mode,
            verdict=verdict,
            reason=reason,
            scope=scope,
            basis=basis,
            evidence=evidence,
            failure=failure,
        )

    @classmethod
    def service_failure(cls) -> "VerdictDecision":
        return cls.mixed(
            scope=VerdictScope.INVALID,
            verdict=Verdict.NEUTRAL,
            basis=VerdictBasis.NONE,
            reason="判断服务暂时不可用",
            failure=VerdictFailure.SERVICE_FAILURE,
        )


class VerdictClientError(RuntimeError):
    pass


_DECISION_FIELDS = {"scope", "verdict", "basis", "reason", "evidence"}


def validate_decision(payload: Any) -> VerdictDecision:
    if not isinstance(payload, dict) or set(payload) != _DECISION_FIELDS:
        raise ValueError("verdict response must contain the fixed fields")
    scope = VerdictScope(payload["scope"])
    verdict = Verdict(payload["verdict"])
    basis = VerdictBasis(payload["basis"])
    reason = payload["reason"]
    evidence = payload["evidence"]
    if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 120:
        raise ValueError("invalid verdict reason")
    if not isinstance(evidence, str) or len(evidence.strip()) > 200:
        raise ValueError("invalid verdict evidence")
    reason = reason.strip()
    evidence = evidence.strip()

    if scope is VerdictScope.INVALID:
        if (
            verdict is not Verdict.NEUTRAL
            or basis is not VerdictBasis.NONE
            or evidence
        ):
            raise ValueError("invalid questions must be neutral without evidence")
        return VerdictDecision.mixed(
            scope=scope,
            verdict=verdict,
            basis=basis,
            reason=reason,
            failure=VerdictFailure.INVALID_QUESTION,
        )

    if scope is VerdictScope.CASUAL:
        if basis not in {VerdictBasis.GENERAL, VerdictBasis.NONE} or evidence:
            raise ValueError("casual decisions cannot use knowledge evidence")
        return VerdictDecision.mixed(
            scope=scope,
            verdict=verdict,
            basis=basis,
            reason=reason,
            failure=(
                VerdictFailure.HIGH_RISK
                if verdict is Verdict.NEUTRAL and basis is VerdictBasis.NONE
                else None
            ),
        )

    if basis is VerdictBasis.GENERAL:
        if verdict in {Verdict.YES, Verdict.NO}:
            return VerdictDecision.mixed(
                scope=scope,
                verdict=Verdict.NEUTRAL,
                basis=VerdictBasis.NONE,
                reason="展厅判断缺少可验证的知识库依据",
                failure=VerdictFailure.INSUFFICIENT_EVIDENCE,
            )
        raise ValueError("exhibition decisions cannot use general basis")
    if verdict in {Verdict.YES, Verdict.NO} and (
        basis is not VerdictBasis.KNOWLEDGE_BASE or not evidence
    ):
        return VerdictDecision.mixed(
            scope=scope,
            verdict=Verdict.NEUTRAL,
            basis=VerdictBasis.NONE,
            reason="展厅判断缺少可验证的知识库依据",
            failure=VerdictFailure.INSUFFICIENT_EVIDENCE,
        )
    if basis is VerdictBasis.NONE and evidence:
        raise ValueError("evidence requires a knowledge basis")
    return VerdictDecision.mixed(
        scope=scope,
        verdict=verdict,
        basis=basis,
        reason=reason,
        evidence=evidence,
        failure=(
            VerdictFailure.INSUFFICIENT_EVIDENCE
            if verdict is Verdict.NEUTRAL and basis is VerdictBasis.NONE
            else None
        ),
    )


class VerdictClientProtocol(Protocol):
    async def decide_exhibition(
        self,
        question: str,
        answer: str,
    ) -> VerdictDecision: ...

    async def decide_casual(self, question: str) -> VerdictDecision: ...


_HIGH_RISK_KEYWORDS = (
    "安全帽",
    "危险",
    "触电",
    "消防",
    "股票",
    "基金",
    "投资",
    "借贷",
    "博彩",
    "药",
    "用药",
    "诊断",
    "治疗",
    "法律",
    "违法",
    "起诉",
    "辞退",
    "开除",
    "歧视",
    "人事",
)


def is_high_risk_casual_question(question: str) -> bool:
    normalized = "".join(question.lower().split())
    return any(keyword in normalized for keyword in _HIGH_RISK_KEYWORDS)


class VerdictService:
    def __init__(
        self,
        client: VerdictClientProtocol,
        empty_search_response: str,
    ) -> None:
        self._client = client
        self._empty_search_response = empty_search_response.strip()

    def is_casual_answer(self, answer: str) -> bool:
        return answer.strip() == self._empty_search_response

    async def decide(self, question: str, answer: str) -> VerdictDecision:
        casual = self.is_casual_answer(answer)
        if casual and is_high_risk_casual_question(question):
            return VerdictDecision(
                VerdictMode.CASUAL,
                Verdict.NEUTRAL,
                "高风险问题不执行趣味二元判断",
                "这个问题不适合用趣味是非判断，我先保持中立。",
            )
        try:
            if casual:
                return await self._client.decide_casual(question)
            return await self._client.decide_exhibition(question, answer)
        except VerdictClientError:
            return VerdictDecision(
                VerdictMode.CASUAL if casual else VerdictMode.EXHIBITION,
                Verdict.NEUTRAL,
                "判断服务暂时不可用",
                "趣味判断暂时不可用，我先保持中立。" if casual else None,
            )
