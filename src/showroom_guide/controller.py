import asyncio
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

import httpx

from showroom_guide.clients.xzkb import XzkbStreamMilestone
from showroom_guide.concurrency import AsyncGate, QueueWaitTimeout
from showroom_guide.faq_cache import FaqCache
from showroom_guide.latency import TurnTiming
from showroom_guide.models import GuidePhase
from showroom_guide.prepared_audio import PreparedAudioStore
from showroom_guide.state import GuideStateStore


@dataclass(frozen=True)
class TextQuestionResult:
    answer: str
    audio: bytes | None
    warning: str | None = None


class QuestionInProgress(RuntimeError):
    pass


class GuideServiceUnavailable(RuntimeError):
    def __init__(self, service: str) -> None:
        self.service = service
        super().__init__(f"{service} service unavailable")


class UnlimitedGate:
    @asynccontextmanager
    async def slot(self, on_wait=None) -> AsyncIterator[None]:
        yield


class GuideController:
    _MAX_ANSWER_CHARS = 1000
    _TRUNCATION_MARK = "……"
    _UNANCHORED_REFERENCES = (
        "这个",
        "那个",
        "它",
        "该产品",
        "该展项",
        "这里",
    )

    def __init__(
        self,
        state,
        xzkb,
        speech_client,
        xzkb_gate: AsyncGate | None = None,
        tts_gate: AsyncGate | None = None,
        faq_cache: FaqCache | None = None,
        prepared_audio: PreparedAudioStore | None = None,
        playback_timeout_seconds: float = 300.0,
    ) -> None:
        self._state: GuideStateStore = state
        self._xzkb = xzkb
        self._speech_client = speech_client
        self._xzkb_gate = xzkb_gate or UnlimitedGate()
        self._tts_gate = tts_gate or UnlimitedGate()
        self._faq_cache = faq_cache
        self._prepared_audio = prepared_audio
        self._playback_timeout_seconds = playback_timeout_seconds
        self._playback_timeout_handle: asyncio.TimerHandle | None = None
        self._playback_generation = 0
        self._messages: list[dict[str, str]] = []
        self._question_lock = asyncio.Lock()

    @property
    def is_busy(self) -> bool:
        return self._question_lock.locked()

    async def reset(self) -> None:
        if self._question_lock.locked():
            raise QuestionInProgress("问题正在处理中")
        self._cancel_playback_timeout()
        self._messages.clear()
        await self._state.reset()

    async def ask_text(
        self,
        question: str,
        timing: TurnTiming | None = None,
    ) -> TextQuestionResult:
        normalized = question.strip()
        if not normalized:
            raise ValueError("问题不能为空")
        if self._question_lock.locked():
            raise QuestionInProgress("已有问题正在处理中")

        async with self._question_lock:
            self._cancel_playback_timeout()
            return await self._ask_text_locked(normalized, timing)

    async def finish_playback(self) -> None:
        self._cancel_playback_timeout()
        if self._state.snapshot.phase in {
            GuidePhase.SPEAKING,
            GuidePhase.DEGRADED,
            GuidePhase.ERROR,
        }:
            await self._state.transition(GuidePhase.IDLE)
            await self._state.set_message("输入问题开始讲解")

    async def _ask_text_locked(
        self,
        question: str,
        timing: TurnTiming | None = None,
    ) -> TextQuestionResult:
        await self._state.start_text_question(question)
        if self._faq_cache is not None:
            cached_entry = self._faq_cache.match(question)
            if cached_entry is not None:
                await self._state.set_message("正在准备讲解内容")
                answer = cached_entry.answer
                await self._state.set_answer(answer)
                self._remember_exchange(question, answer)
                prepared_audio = (
                    self._prepared_audio.get(cached_entry.id)
                    if self._prepared_audio is not None
                    else None
                )
                if prepared_audio is not None:
                    if timing is not None:
                        timing.mark_faq(cached_entry.id, "prepared_audio")
                    await self._state.transition(GuidePhase.SPEAKING)
                    await self._state.set_message("正在播放讲解")
                    self._arm_playback_timeout()
                    return TextQuestionResult(answer=answer, audio=prepared_audio)
                if timing is not None:
                    timing.mark_faq(cached_entry.id, "faq_online_tts")
                return await self._synthesize_answer(answer, timing)

        if self._needs_exhibit_clarification(question):
            answer = "您指的是哪个产品或展项？"
            await self._state.append_answer(answer)
            if timing is not None:
                timing.mark_local_online_tts()
            return await self._synthesize_answer(answer, timing)

        request_messages = [
            *self._messages,
            {"role": "user", "content": question},
        ]
        if timing is not None:
            timing.mark_xzkb_online_tts()

        try:
            if timing is not None:
                timing.start_xzkb_queue()
            async with self._xzkb_gate.slot(
                on_wait=lambda: self._state.set_message(
                    "当前使用人数较多，正在排队查询资料"
                )
            ):
                if timing is not None:
                    timing.enter_xzkb_slot()
                await self._state.set_message("正在查询展项资料")
                observer = self._xzkb_observer(timing)
                for max_tokens in (None, 8000):
                    if timing is not None:
                        timing.start_xzkb_request()
                    stream = self._start_xzkb_stream(
                        request_messages,
                        max_tokens,
                        observer,
                    )
                    async with aclosing(stream):
                        async for event in stream:
                            if timing is not None and event.text.strip():
                                timing.receive_xzkb_text()
                            combined = self._state.snapshot.answer + event.text
                            if len(combined) > self._MAX_ANSWER_CHARS:
                                content_limit = self._MAX_ANSWER_CHARS - len(
                                    self._TRUNCATION_MARK
                                )
                                bounded = (
                                    combined[:content_limit].rstrip()
                                    + self._TRUNCATION_MARK
                                )
                                await self._state.set_answer(bounded)
                                break
                            await self._state.append_answer(event.text)
                    if self._state.snapshot.answer.strip():
                        break
                    await self._state.set_message(
                        "知识库正在重新生成讲解内容"
                    )
        except QueueWaitTimeout as error:
            if timing is not None:
                timing.finish_xzkb_queue()
                timing.fail("xzkb_queue", error)
            await self._degrade("当前使用人数较多，请稍后重试")
            raise GuideServiceUnavailable("capacity") from error
        except (httpx.HTTPError, ValueError, TypeError) as error:
            if timing is not None:
                timing.fail("xzkb", error)
            await self._degrade("知识库暂时不可用，请稍后重试")
            raise GuideServiceUnavailable("xzkb") from error
        finally:
            if timing is not None:
                timing.finish_xzkb()

        answer = self._state.snapshot.answer.strip()
        if not answer:
            if timing is not None:
                timing.fail("xzkb", GuideServiceUnavailable("xzkb"))
            await self._degrade("知识库没有返回有效答案，请换一种问法")
            raise GuideServiceUnavailable("xzkb")

        self._remember_exchange(question, answer)
        return await self._synthesize_answer(answer, timing)

    def _remember_exchange(self, question: str, answer: str) -> None:
        self._messages.extend(
            [
                {"role": "user", "content": question},
                {"role": "assistant", "content": answer},
            ]
        )
        self._messages = self._messages[-20:]

    def _needs_exhibit_clarification(self, question: str) -> bool:
        return not self._messages and any(
            reference in question for reference in self._UNANCHORED_REFERENCES
        )

    def _start_xzkb_stream(self, messages, max_tokens, observer):
        if observer is None:
            if max_tokens is None:
                return self._xzkb.stream_chat(messages)
            return self._xzkb.stream_chat(messages, max_tokens=max_tokens)
        if max_tokens is None:
            return self._xzkb.stream_chat(messages, observer=observer)
        return self._xzkb.stream_chat(
            messages,
            max_tokens=max_tokens,
            observer=observer,
        )

    @staticmethod
    def _xzkb_observer(timing: TurnTiming | None):
        if timing is None:
            return None

        def observe(milestone: XzkbStreamMilestone) -> None:
            if milestone is XzkbStreamMilestone.RESPONSE_HEADERS:
                timing.receive_xzkb_headers()
            elif milestone is XzkbStreamMilestone.FIRST_SSE:
                timing.receive_xzkb_first_sse()

        return observe

    async def _synthesize_answer(
        self,
        answer: str,
        timing: TurnTiming | None = None,
    ) -> TextQuestionResult:
        try:
            if timing is not None:
                timing.start_tts_queue()
            async with self._tts_gate.slot(
                on_wait=lambda: self._state.set_message("正在排队生成语音")
            ):
                if timing is not None:
                    timing.enter_tts_slot()
                await self._state.set_message("正在生成讲解语音")
                if timing is not None:
                    timing.start_tts_synthesis()
                audio = await self._speech_client.synthesize(answer)
        except QueueWaitTimeout as error:
            if timing is not None:
                timing.finish_tts_queue()
                timing.fail("tts_queue", error)
            warning = "语音暂时不可用，您仍可阅读文字答案"
            await self._degrade(warning)
            return TextQuestionResult(answer=answer, audio=None, warning=warning)
        except (httpx.HTTPError, ValueError) as error:
            if timing is not None:
                timing.fail("tts", error)
            warning = "语音暂时不可用，您仍可阅读文字答案"
            await self._degrade(warning)
            return TextQuestionResult(answer=answer, audio=None, warning=warning)

        finally:
            if timing is not None:
                timing.finish_tts_synthesis()
        await self._state.transition(GuidePhase.SPEAKING)
        await self._state.set_message("正在播放讲解")
        self._arm_playback_timeout()
        return TextQuestionResult(answer=answer, audio=audio)

    async def _degrade(self, message: str) -> None:
        await self._state.transition(GuidePhase.DEGRADED)
        await self._state.set_message(message)

    def _arm_playback_timeout(self) -> None:
        self._cancel_playback_timeout()
        generation = self._playback_generation
        loop = asyncio.get_running_loop()
        self._playback_timeout_handle = loop.call_later(
            self._playback_timeout_seconds,
            self._schedule_playback_expiry,
            generation,
        )

    def _cancel_playback_timeout(self) -> None:
        self._playback_generation += 1
        if self._playback_timeout_handle is not None:
            self._playback_timeout_handle.cancel()
            self._playback_timeout_handle = None

    def _schedule_playback_expiry(self, generation: int) -> None:
        if generation != self._playback_generation:
            return
        self._playback_timeout_handle = None
        asyncio.create_task(self._expire_playback(generation))

    async def _expire_playback(self, generation: int) -> None:
        if generation != self._playback_generation:
            return
        await self.finish_playback()
