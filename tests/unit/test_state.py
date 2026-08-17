import pytest

from showroom_guide.models import GuidePhase
from showroom_guide.state import GuideStateStore, InvalidStateTransition


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
