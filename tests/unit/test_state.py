import pytest

from showroom_guide.models import GuidePhase, InteractionMode, VerdictPhase
from showroom_guide.state import GuideStateStore, InvalidStateTransition
from showroom_guide.verdict import (
    Verdict,
    VerdictBasis,
    VerdictDecision,
    VerdictScope,
)


@pytest.mark.asyncio
async def test_recording_to_thinking_to_speaking():
    store = GuideStateStore()

    await store.transition(GuidePhase.RECORDING)
    await store.transition(GuidePhase.TRANSCRIBING)
    await store.transition(GuidePhase.THINKING)
    await store.transition(GuidePhase.SPEAKING)

    assert store.snapshot.phase is GuidePhase.SPEAKING


@pytest.mark.asyncio
async def test_idle_cannot_jump_directly_to_speaking():
    store = GuideStateStore()

    with pytest.raises(InvalidStateTransition):
        await store.transition(GuidePhase.SPEAKING)


@pytest.mark.asyncio
async def test_updates_are_published_to_subscribers():
    store = GuideStateStore()
    queue = store.subscribe()

    await store.set_transcript("介绍一下产品")

    event = await queue.get()
    assert event.transcript == "介绍一下产品"


@pytest.mark.asyncio
async def test_slow_subscriber_keeps_only_latest_snapshot():
    store = GuideStateStore()
    queue = store.subscribe()

    await store.set_message("第一步")
    await store.set_message("第二步")
    await store.set_message("最终状态")

    assert queue.qsize() == 1
    assert (await queue.get()).message == "最终状态"


@pytest.mark.asyncio
async def test_unsubscribe_stops_future_updates():
    store = GuideStateStore()
    queue = store.subscribe()
    store.unsubscribe(queue)

    await store.set_message("服务暂不可用")

    assert queue.empty()


def test_initial_message_is_readable_chinese():
    store = GuideStateStore()

    assert store.snapshot.message == "输入问题开始讲解"


@pytest.mark.asyncio
async def test_text_question_can_start_thinking_and_reset_previous_content():
    store = GuideStateStore()
    await store.set_transcript("旧问题")
    await store.append_answer("旧答案")

    await store.start_text_question("新问题")

    assert store.snapshot.phase is GuidePhase.THINKING
    assert store.snapshot.transcript == "新问题"
    assert store.snapshot.answer == ""
    assert store.snapshot.message == "正在查询展项资料"


@pytest.mark.asyncio
async def test_thinking_can_degrade_when_remote_service_fails():
    store = GuideStateStore()
    await store.start_text_question("问题")

    await store.transition(GuidePhase.DEGRADED)

    assert store.snapshot.phase is GuidePhase.DEGRADED


@pytest.mark.asyncio
async def test_reset_restores_initial_snapshot_and_notifies_subscribers():
    store = GuideStateStore()
    queue = store.subscribe()
    await store.start_text_question("旧问题")
    await queue.get()

    await store.reset()

    snapshot = await queue.get()
    assert snapshot.phase is GuidePhase.IDLE
    assert snapshot.transcript == ""
    assert snapshot.answer == ""
    assert snapshot.message == "输入问题开始讲解"


@pytest.mark.asyncio
async def test_error_can_start_a_new_recording():
    store = GuideStateStore()
    await store.transition(GuidePhase.ERROR)

    await store.transition(GuidePhase.RECORDING)

    assert store.snapshot.phase is GuidePhase.RECORDING


@pytest.mark.asyncio
async def test_start_recording_clears_previous_result_atomically():
    store = GuideStateStore()
    await store.start_text_question("旧问题")
    await store.set_answer("旧答案")
    await store.transition(GuidePhase.ERROR)

    snapshot = await store.start_recording()

    assert snapshot.phase is GuidePhase.RECORDING
    assert snapshot.transcript == ""
    assert snapshot.answer == ""
    assert snapshot.message == "正在录音，再次点击后提交"


@pytest.mark.asyncio
async def test_new_turn_clears_previous_verdict_and_publishes_neutral():
    store = GuideStateStore()
    queue = store.subscribe()
    await store.set_verdict(Verdict.YES)
    await queue.get()

    snapshot = await store.start_recording()

    assert snapshot.verdict is Verdict.NEUTRAL
    assert (await queue.get()).verdict is Verdict.NEUTRAL


def test_snapshot_defaults_to_conversation_without_active_verdict():
    snapshot = GuideStateStore().snapshot

    assert snapshot.interaction_mode is InteractionMode.CONVERSATION
    assert snapshot.verdict_phase is VerdictPhase.IDLE
    assert snapshot.verdict is Verdict.NEUTRAL
    assert snapshot.verdict_reason == ""
    assert snapshot.verdict_evidence == ""


@pytest.mark.asyncio
async def test_begin_and_finish_verdict_publish_structured_state_atomically():
    store = GuideStateStore()
    await store.set_interaction_mode(InteractionMode.VERDICT)

    thinking = await store.begin_verdict("该产品支持国产算力吗？", generation=7)

    assert thinking.verdict_phase is VerdictPhase.THINKING
    assert thinking.transcript == "该产品支持国产算力吗？"
    assert thinking.verdict_generation == 7
    assert thinking.answer == ""

    decision = VerdictDecision.mixed(
        scope=VerdictScope.EXHIBITION,
        verdict=Verdict.YES,
        basis=VerdictBasis.KNOWLEDGE_BASE,
        reason="知识库明确支持",
        evidence="支持国产算力适配",
    )
    finished = await store.finish_verdict(decision, VerdictPhase.HOLDING)

    assert finished.verdict is Verdict.YES
    assert finished.verdict_scope is VerdictScope.EXHIBITION
    assert finished.verdict_basis is VerdictBasis.KNOWLEDGE_BASE
    assert finished.verdict_reason == "知识库明确支持"
    assert finished.verdict_evidence == "支持国产算力适配"
    assert finished.answer == ""
