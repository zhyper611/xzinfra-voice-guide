import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from showroom_guide.clients.xzkb import ChatStreamEvent
from showroom_guide.concurrency import AsyncGate
from showroom_guide.controller import (
    GuideController,
    GuideServiceUnavailable,
    QuestionInProgress,
)
from showroom_guide.models import GuidePhase
from showroom_guide.state import GuideStateStore


async def async_events(*texts: str):
    for text in texts:
        yield ChatStreamEvent(text=text)


def make_controller(xzkb_gate=None, tts_gate=None):
    state = GuideStateStore()
    xzkb = MagicMock()
    speech = AsyncMock()
    controller = GuideController(
        state,
        xzkb,
        speech,
        xzkb_gate=xzkb_gate,
        tts_gate=tts_gate,
    )
    return controller, state, xzkb, speech


@pytest.mark.asyncio
async def test_text_question_streams_answer_and_returns_wav():
    controller, state, xzkb, speech = make_controller()
    xzkb.stream_chat.return_value = async_events("第一段。", "第二段。")
    wav = b"RIFF\x04\x00\x00\x00WAVE"
    speech.synthesize.return_value = wav

    result = await controller.ask_text(" 介绍一下矿山巡检系统 ")

    assert state.snapshot.transcript == "介绍一下矿山巡检系统"
    assert state.snapshot.answer == "第一段。第二段。"
    assert state.snapshot.phase is GuidePhase.SPEAKING
    assert result.answer == "第一段。第二段。"
    assert result.audio == wav
    speech.synthesize.assert_awaited_once_with("第一段。第二段。")


@pytest.mark.asyncio
async def test_text_question_preserves_conversation_context():
    controller, _, xzkb, speech = make_controller()
    xzkb.stream_chat.side_effect = [
        async_events("这是展项甲。"),
        async_events("它支持远程巡检。"),
    ]
    speech.synthesize.return_value = b"RIFF\x04\x00\x00\x00WAVE"

    await controller.ask_text("介绍展项甲")
    await controller.finish_playback()
    await controller.ask_text("它有什么特点？")

    second_messages = xzkb.stream_chat.call_args_list[1].args[0]
    assert second_messages == [
        {"role": "user", "content": "介绍展项甲"},
        {"role": "assistant", "content": "这是展项甲。"},
        {"role": "user", "content": "它有什么特点？\n/no_think"},
    ]


@pytest.mark.asyncio
async def test_current_model_request_disables_thinking_without_polluting_history():
    controller, _, xzkb, speech = make_controller()
    xzkb.stream_chat.side_effect = [
        async_events("第一个回答。"),
        async_events("第二个回答。"),
    ]
    speech.synthesize.return_value = b"RIFF\x04\x00\x00\x00WAVE"

    await controller.ask_text("介绍展项甲")
    await controller.finish_playback()
    await controller.ask_text("它有什么特点？")

    first_messages = xzkb.stream_chat.call_args_list[0].args[0]
    second_messages = xzkb.stream_chat.call_args_list[1].args[0]
    assert first_messages == [
        {"role": "user", "content": "介绍展项甲\n/no_think"}
    ]
    assert second_messages[0]["content"] == "介绍展项甲"
    assert second_messages[-1]["content"] == "它有什么特点？\n/no_think"


@pytest.mark.asyncio
async def test_xzkb_failure_enters_degraded_state():
    controller, state, xzkb, _ = make_controller()

    async def failed_stream(_messages):
        raise httpx.ReadTimeout("timeout")
        yield

    xzkb.stream_chat.side_effect = failed_stream

    with pytest.raises(GuideServiceUnavailable) as error:
        await controller.ask_text("动态问题")

    assert error.value.service == "xzkb"
    assert state.snapshot.phase is GuidePhase.DEGRADED
    assert state.snapshot.message == "知识库暂时不可用，请稍后重试"


@pytest.mark.asyncio
async def test_empty_xzkb_answer_retries_once_with_larger_output_budget():
    controller, state, xzkb, speech = make_controller()
    xzkb.stream_chat.side_effect = [
        async_events(),
        async_events("重试后的回答。"),
    ]
    speech.synthesize.return_value = b"RIFF\x04\x00\x00\x00WAVE"

    result = await controller.ask_text("介绍展项甲")

    assert result.answer == "重试后的回答。"
    assert state.snapshot.phase is GuidePhase.SPEAKING
    assert xzkb.stream_chat.call_count == 2
    assert xzkb.stream_chat.call_args_list[1].kwargs == {"max_tokens": 8000}


@pytest.mark.asyncio
async def test_tts_failure_keeps_answer_and_returns_no_audio():
    controller, state, xzkb, speech = make_controller()
    xzkb.stream_chat.return_value = async_events("文字答案。")
    speech.synthesize.side_effect = httpx.ReadTimeout("timeout")

    result = await controller.ask_text("问题")

    assert result.answer == "文字答案。"
    assert result.audio is None
    assert result.warning == "语音暂时不可用，您仍可阅读文字答案"
    assert state.snapshot.phase is GuidePhase.DEGRADED


@pytest.mark.asyncio
async def test_empty_question_is_rejected_before_remote_calls():
    controller, _, xzkb, _ = make_controller()

    with pytest.raises(ValueError, match="不能为空"):
        await controller.ask_text("  ")

    xzkb.stream_chat.assert_not_called()


@pytest.mark.asyncio
async def test_concurrent_question_is_rejected():
    controller, _, xzkb, speech = make_controller()
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_stream(_messages):
        started.set()
        await release.wait()
        yield ChatStreamEvent(text="回答。")

    xzkb.stream_chat.side_effect = blocking_stream
    speech.synthesize.return_value = b"RIFF\x04\x00\x00\x00WAVE"
    first = asyncio.create_task(controller.ask_text("第一个问题"))
    await started.wait()

    with pytest.raises(QuestionInProgress):
        await controller.ask_text("另一个问题")

    release.set()
    await first


@pytest.mark.asyncio
async def test_unanchored_reference_asks_for_exhibit_without_calling_xzkb():
    controller, state, xzkb, speech = make_controller()
    wav = b"RIFF\x04\x00\x00\x00WAVE"
    speech.synthesize.return_value = wav

    result = await controller.ask_text("这个有什么特点？")

    assert result.answer == "您指的是哪个产品或展项？"
    assert result.audio == wav
    assert state.snapshot.answer == "您指的是哪个产品或展项？"
    xzkb.stream_chat.assert_not_called()
    speech.synthesize.assert_awaited_once_with("您指的是哪个产品或展项？")


@pytest.mark.asyncio
async def test_oversized_xzkb_answer_is_bounded_for_spoken_explanation():
    controller, state, xzkb, speech = make_controller()
    xzkb.stream_chat.return_value = async_events("甲" * 500, "乙" * 500)
    speech.synthesize.return_value = b"RIFF\x04\x00\x00\x00WAVE"

    result = await controller.ask_text("介绍矿山巡检系统")

    assert len(result.answer) <= 321
    assert result.answer.endswith("……")
    assert state.snapshot.answer == result.answer
    speech.synthesize.assert_awaited_once_with(result.answer)


@pytest.mark.asyncio
async def test_reset_clears_conversation_context():
    controller, state, xzkb, speech = make_controller()
    xzkb.stream_chat.side_effect = [
        async_events("第一个回答。"),
        async_events("第二个回答。"),
    ]
    speech.synthesize.return_value = b"RIFF\x04\x00\x00\x00WAVE"
    await controller.ask_text("介绍展项甲")
    await controller.finish_playback()

    await controller.reset()
    await controller.ask_text("介绍展项乙")

    second_messages = xzkb.stream_chat.call_args_list[1].args[0]
    assert second_messages == [
        {"role": "user", "content": "介绍展项乙\n/no_think"},
    ]
    assert state.snapshot.transcript == "介绍展项乙"


@pytest.mark.asyncio
async def test_reset_is_rejected_while_question_is_running():
    controller, _, xzkb, speech = make_controller()
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_stream(_messages):
        started.set()
        await release.wait()
        yield ChatStreamEvent(text="回答。")

    xzkb.stream_chat.side_effect = blocking_stream
    speech.synthesize.return_value = b"RIFF\x04\x00\x00\x00WAVE"
    task = asyncio.create_task(controller.ask_text("问题"))
    await started.wait()

    with pytest.raises(QuestionInProgress):
        await controller.reset()

    release.set()
    await task


@pytest.mark.asyncio
async def test_xzkb_gate_updates_waiting_message():
    gate = AsyncGate(limit=1, timeout_seconds=1)
    controller, state, xzkb, speech = make_controller(xzkb_gate=gate)

    async with gate.slot():
        xzkb.stream_chat.return_value = async_events("回答。")
        speech.synthesize.return_value = b"RIFF\x04\x00\x00\x00WAVE"
        task = asyncio.create_task(controller.ask_text("问题"))
        for _ in range(20):
            if state.snapshot.message == "当前使用人数较多，正在排队查询资料":
                break
            await asyncio.sleep(0)
        assert state.snapshot.message == "当前使用人数较多，正在排队查询资料"

    await task


@pytest.mark.asyncio
async def test_tts_queue_timeout_keeps_text_answer():
    tts_gate = AsyncGate(limit=1, timeout_seconds=0.01)
    controller, state, xzkb, speech = make_controller(tts_gate=tts_gate)
    xzkb.stream_chat.return_value = async_events("文字答案。")

    async with tts_gate.slot():
        result = await controller.ask_text("问题")

    assert result.answer == "文字答案。"
    assert result.audio is None
    assert result.warning == "语音暂时不可用，您仍可阅读文字答案"
    assert state.snapshot.phase is GuidePhase.DEGRADED
