import pytest

from showroom_guide.verdict import (
    Verdict,
    VerdictBasis,
    VerdictClientError,
    VerdictDecision,
    VerdictFailure,
    VerdictMode,
    VerdictScope,
    VerdictService,
    validate_decision,
)


class FakeVerdictClient:
    def __init__(self, *, exhibition=None, casual=None, error=None):
        self.exhibition = exhibition
        self.casual = casual
        self.error = error
        self.exhibition_calls = []
        self.casual_questions = []

    async def decide_exhibition(self, question, answer):
        self.exhibition_calls.append((question, answer))
        if self.error:
            raise self.error
        return self.exhibition

    async def decide_casual(self, question):
        self.casual_questions.append(question)
        if self.error:
            raise self.error
        return self.casual


@pytest.mark.asyncio
async def test_non_empty_xzkb_answer_uses_exhibition_evidence():
    expected = VerdictDecision(
        VerdictMode.EXHIBITION,
        Verdict.NO,
        "资料明确说明尚未支持",
    )
    client = FakeVerdictClient(exhibition=expected)
    service = VerdictService(client, "知识库中无相关内容，请补充")

    decision = await service.decide("支持无人运行吗？", "目前不支持")

    assert decision == expected
    assert client.exhibition_calls == [("支持无人运行吗？", "目前不支持")]


@pytest.mark.asyncio
async def test_empty_xzkb_answer_routes_low_risk_question_to_casual_client():
    expected = VerdictDecision(
        VerdictMode.CASUAL,
        Verdict.YES,
        "适合放松一下",
        "趣味判断：是，今天适合出去走走。",
    )
    client = FakeVerdictClient(casual=expected)
    service = VerdictService(client, "知识库中无相关内容，请补充")

    decision = await service.decide(
        "今天适合出去玩吗？",
        "  知识库中无相关内容，请补充  ",
    )

    assert decision == expected
    assert client.casual_questions == ["今天适合出去玩吗？"]
    assert client.exhibition_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "我可以不戴安全帽吗？",
        "这只股票明天会涨吗？",
        "我应该停止服用这个药吗？",
        "这个员工是不是应该被辞退？",
    ],
)
async def test_high_risk_casual_question_stays_neutral_without_model(question):
    client = FakeVerdictClient()
    service = VerdictService(client, "空回复")

    decision = await service.decide(question, "空回复")

    assert decision.verdict is Verdict.NEUTRAL
    assert decision.spoken_answer == "这个问题不适合用趣味是非判断，我先保持中立。"
    assert client.casual_questions == []


@pytest.mark.asyncio
async def test_client_failure_preserves_exhibition_answer_as_neutral():
    client = FakeVerdictClient(error=VerdictClientError("timeout"))
    service = VerdictService(client, "空回复")

    decision = await service.decide("产品支持离线运行吗？", "资料未明确说明")

    assert decision.verdict is Verdict.NEUTRAL
    assert decision.spoken_answer is None


@pytest.mark.asyncio
async def test_client_failure_replaces_casual_empty_reply_with_neutral_message():
    client = FakeVerdictClient(error=VerdictClientError("timeout"))
    service = VerdictService(client, "空回复")

    decision = await service.decide("今天适合喝咖啡吗？", "空回复")

    assert decision.verdict is Verdict.NEUTRAL
    assert decision.spoken_answer == "趣味判断暂时不可用，我先保持中立。"


def test_valid_casual_decision_uses_general_basis():
    decision = validate_decision(
        {
            "scope": "casual",
            "verdict": "yes",
            "basis": "general",
            "reason": "低风险日常判断",
            "evidence": "",
        }
    )

    assert decision.scope is VerdictScope.CASUAL
    assert decision.verdict is Verdict.YES
    assert decision.basis is VerdictBasis.GENERAL
    assert decision.failure is None


def test_exhibition_binary_without_knowledge_evidence_fails_closed():
    decision = validate_decision(
        {
            "scope": "exhibition",
            "verdict": "yes",
            "basis": "general",
            "reason": "模型凭常识认为支持",
            "evidence": "",
        }
    )

    assert decision.scope is VerdictScope.EXHIBITION
    assert decision.verdict is Verdict.NEUTRAL
    assert decision.basis is VerdictBasis.NONE
    assert decision.evidence == ""
    assert decision.failure is VerdictFailure.INSUFFICIENT_EVIDENCE


@pytest.mark.parametrize(
    "payload",
    [
        {
            "scope": "invalid",
            "verdict": "yes",
            "basis": "none",
            "reason": "不是是非问题",
            "evidence": "",
        },
        {
            "scope": "casual",
            "verdict": "no",
            "basis": "knowledge_base",
            "reason": "错误依据类型",
            "evidence": "片段",
        },
    ],
)
def test_inconsistent_decision_combinations_are_rejected(payload):
    with pytest.raises(ValueError):
        validate_decision(payload)


def test_decision_text_limits_are_enforced():
    with pytest.raises(ValueError):
        validate_decision(
            {
                "scope": "casual",
                "verdict": "yes",
                "basis": "general",
                "reason": "理" * 121,
                "evidence": "",
            }
        )
