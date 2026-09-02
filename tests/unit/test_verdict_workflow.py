import asyncio

import pytest

from showroom_guide.models import VerdictPhase
from showroom_guide.state import GuideStateStore
from showroom_guide.verdict import (
    Verdict,
    VerdictBasis,
    VerdictClientError,
    VerdictDecision,
    VerdictFailure,
    VerdictScope,
)
from showroom_guide.verdict_workflow import VerdictWorkflow


class FakeClient:
    def __init__(self, decision=None, error=None):
        self.decision = decision
        self.error = error
        self.questions = []

    async def decide(self, question):
        self.questions.append(question)
        if self.error is not None:
            raise self.error
        return self.decision


class DeferredClient:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def decide(self, _question):
        self.started.set()
        await self.release.wait()
        return exhibition_yes()


class FakeMotion:
    def __init__(self):
        self.calls = []

    async def thinking(self, generation):
        self.calls.append(("thinking", generation))

    async def show_verdict(self, verdict, generation):
        self.calls.append(("show_verdict", verdict, generation))

    async def show_neutral(self, generation):
        self.calls.append(("show_neutral", generation))

    async def reset(self):
        self.calls.append(("reset",))


def exhibition_yes():
    return VerdictDecision.mixed(
        scope=VerdictScope.EXHIBITION,
        verdict=Verdict.YES,
        basis=VerdictBasis.KNOWLEDGE_BASE,
        reason="知识库明确支持",
        evidence="支持国产算力适配",
    )


@pytest.mark.asyncio
async def test_run_starts_thinking_calls_application_once_and_holds_yes():
    state = GuideStateStore()
    client = FakeClient(exhibition_yes())
    motion = FakeMotion()
    workflow = VerdictWorkflow(client, motion, state)

    result = await workflow.run("该产品支持国产算力吗？")

    assert result.verdict is Verdict.YES
    assert motion.calls == [
        ("thinking", 1),
        ("show_verdict", Verdict.YES, 1),
    ]
    assert client.questions == ["该产品支持国产算力吗？"]
    assert state.snapshot.verdict_phase is VerdictPhase.HOLDING
    assert state.snapshot.verdict_evidence == "支持国产算力适配"
    assert state.snapshot.answer == ""


@pytest.mark.asyncio
async def test_invalid_question_moves_neutral_and_plays_local_prompt():
    decision = VerdictDecision.mixed(
        scope=VerdictScope.INVALID,
        verdict=Verdict.NEUTRAL,
        basis=VerdictBasis.NONE,
        reason="不是是非问题",
        failure=VerdictFailure.INVALID_QUESTION,
    )
    prompts = []
    workflow = VerdictWorkflow(
        FakeClient(decision),
        FakeMotion(),
        GuideStateStore(),
        play_prompt=lambda name: prompts.append(name),
    )

    result = await workflow.run("介绍一下八大车间")

    assert result.verdict is Verdict.NEUTRAL
    assert workflow.motion.calls[-1] == ("show_neutral", 1)
    assert prompts == ["verdict-invalid"]


@pytest.mark.asyncio
async def test_client_failure_degrades_to_neutral_without_escaping():
    prompts = []
    state = GuideStateStore()
    workflow = VerdictWorkflow(
        FakeClient(error=VerdictClientError("timeout")),
        FakeMotion(),
        state,
        play_prompt=lambda name: prompts.append(name),
    )

    result = await workflow.run("今天适合散步吗？")

    assert result.verdict is Verdict.NEUTRAL
    assert result.failure is VerdictFailure.SERVICE_FAILURE
    assert state.snapshot.verdict_phase is VerdictPhase.FAILED
    assert prompts == ["verdict-unavailable"]


@pytest.mark.asyncio
async def test_hard_timeout_stops_thinking_and_degrades():
    client = DeferredClient()
    motion = FakeMotion()
    workflow = VerdictWorkflow(
        client,
        motion,
        GuideStateStore(),
        timeout_seconds=0.01,
    )

    result = await workflow.run("问题")

    assert result.failure is VerdictFailure.SERVICE_FAILURE
    assert motion.calls[-1] == ("show_neutral", 1)


@pytest.mark.asyncio
async def test_leave_invalidates_late_result_and_resets_motion():
    client = DeferredClient()
    motion = FakeMotion()
    state = GuideStateStore()
    workflow = VerdictWorkflow(client, motion, state)
    task = asyncio.create_task(workflow.run("问题"))
    await client.started.wait()

    await workflow.leave()
    client.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert motion.calls[-1] == ("reset",)
    assert state.snapshot.verdict_phase is VerdictPhase.IDLE
