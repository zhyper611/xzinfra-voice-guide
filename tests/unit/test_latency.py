from contextlib import asynccontextmanager
import logging

import httpx
import pytest

from showroom_guide.audio_store import AudioStore
from showroom_guide.clients.xzkb import ChatStreamEvent, XzkbStreamMilestone
from showroom_guide.concurrency import QueueWaitTimeout
from showroom_guide.controller import GuideController, GuideServiceUnavailable
from showroom_guide.device import DeviceTranscriptionUnavailable, DeviceVoiceSession
from showroom_guide.latency import (
    METRIC_NAMES,
    DeviceLatencyRecorder,
    TurnTiming,
    nearest_rank,
)
from showroom_guide.state import GuideStateStore


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class ClockGate:
    def __init__(self, clock: Clock, wait_seconds: float = 0.0) -> None:
        self.clock = clock
        self.wait_seconds = wait_seconds

    @asynccontextmanager
    async def slot(self, on_wait=None):
        if self.wait_seconds and on_wait is not None:
            result = on_wait()
            if hasattr(result, "__await__"):
                await result
        self.clock.advance(self.wait_seconds)
        yield


class TimeoutGate:
    def __init__(self, clock: Clock, wait_seconds: float) -> None:
        self.clock = clock
        self.wait_seconds = wait_seconds

    @asynccontextmanager
    async def slot(self, on_wait=None):
        if on_wait is not None:
            result = on_wait()
            if hasattr(result, "__await__"):
                await result
        self.clock.advance(self.wait_seconds)
        raise QueueWaitTimeout
        yield


class TimedSpeech:
    def __init__(self, clock: Clock, *, fail_asr: bool = False, fail_tts: bool = False):
        self.clock = clock
        self.fail_asr = fail_asr
        self.fail_tts = fail_tts

    async def transcribe(self, _audio) -> str:
        self.clock.advance(0.01)
        if self.fail_asr:
            raise httpx.ReadTimeout("timeout")
        return "question"

    async def synthesize(self, _text: str) -> bytes:
        self.clock.advance(0.04)
        if self.fail_tts:
            raise httpx.ReadTimeout("timeout")
        return b"RIFF\x04\x00\x00\x00WAVE"


class TimedXzkb:
    def __init__(self, clock: Clock, *, retry_empty: bool = False) -> None:
        self.clock = clock
        self.retry_empty = retry_empty
        self.calls = 0

    async def stream_chat(self, _messages, max_tokens=None, observer=None):
        self.calls += 1
        if self.retry_empty and self.calls == 1:
            self.clock.advance(0.004)
            if observer is not None:
                observer(XzkbStreamMilestone.RESPONSE_HEADERS)
            self.clock.advance(0.006)
            if observer is not None:
                observer(XzkbStreamMilestone.FIRST_SSE)
            return
        self.clock.advance(0.01)
        if observer is not None:
            observer(XzkbStreamMilestone.RESPONSE_HEADERS)
        self.clock.advance(0.01)
        if observer is not None:
            observer(XzkbStreamMilestone.FIRST_SSE)
        yield ChatStreamEvent(text="answer")
        self.clock.advance(0.03)
        yield ChatStreamEvent(text=" more")


class WhitespaceThenTextXzkb:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock

    async def stream_chat(self, _messages, max_tokens=None, observer=None):
        self.clock.advance(0.005)
        if observer is not None:
            observer(XzkbStreamMilestone.RESPONSE_HEADERS)
        self.clock.advance(0.005)
        if observer is not None:
            observer(XzkbStreamMilestone.FIRST_SSE)
        yield ChatStreamEvent(text=" ")
        self.clock.advance(0.02)
        yield ChatStreamEvent(text="answer")


class FailingBeforeHeadersXzkb:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock

    async def stream_chat(self, _messages, max_tokens=None, observer=None):
        self.clock.advance(0.01)
        raise httpx.ReadTimeout("timeout")
        yield ChatStreamEvent(text="")


class HeadersOnlyXzkb:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock

    async def stream_chat(self, _messages, max_tokens=None, observer=None):
        self.clock.advance(0.01)
        if observer is not None:
            observer(XzkbStreamMilestone.RESPONSE_HEADERS)
        if False:
            yield ChatStreamEvent(text="")


class SseWithoutContentXzkb:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock

    async def stream_chat(self, _messages, max_tokens=None, observer=None):
        self.clock.advance(0.01)
        if observer is not None:
            observer(XzkbStreamMilestone.RESPONSE_HEADERS)
        self.clock.advance(0.01)
        if observer is not None:
            observer(XzkbStreamMilestone.FIRST_SSE)
        yield ChatStreamEvent(text=" ")


def wav() -> bytes:
    return (
        b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00"
        b"\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
    )


def make_session(
    clock: Clock,
    *,
    xzkb_wait: float = 0.0,
    tts_wait: float = 0.0,
    retry_empty: bool = False,
    fail_asr: bool = False,
    fail_tts: bool = False,
):
    state = GuideStateStore()
    speech = TimedSpeech(clock, fail_asr=fail_asr, fail_tts=fail_tts)
    controller = GuideController(
        state,
        TimedXzkb(clock, retry_empty=retry_empty),
        speech,
        xzkb_gate=ClockGate(clock, xzkb_wait),
        tts_gate=ClockGate(clock, tts_wait),
    )
    recorder = DeviceLatencyRecorder()
    return DeviceVoiceSession(
        state=state,
        controller=controller,
        speech=speech,
        audio=AudioStore(),
        metrics=recorder,
        clock=clock,
    ), recorder


@pytest.mark.asyncio
async def test_device_turn_records_stage_boundaries_without_queueing():
    clock = Clock()
    session, recorder = make_session(clock)

    await session.process_wav(wav())

    metrics = recorder.snapshot()["metrics"]
    latest = recorder.snapshot()["latest"]
    assert latest["cache_hit"] is False
    assert latest["cache_entry_id"] is None
    assert latest["served_from"] == "xzkb_online_tts"
    assert metrics["asr_ms"] == {"samples": 1, "p50": 10.0, "p95": 10.0}
    assert metrics["xzkb_queue_ms"] == {"samples": 1, "p50": 0.0, "p95": 0.0}
    assert metrics["xzkb_headers_ms"] == {"samples": 1, "p50": 10.0, "p95": 10.0}
    assert metrics["xzkb_first_sse_ms"] == {"samples": 1, "p50": 10.0, "p95": 10.0}
    assert metrics["xzkb_first_content_ms"] == {"samples": 1, "p50": 0.0, "p95": 0.0}
    assert metrics["xzkb_ttft_ms"] == {"samples": 1, "p50": 20.0, "p95": 20.0}
    assert metrics["xzkb_generation_ms"] == {"samples": 1, "p50": 30.0, "p95": 30.0}
    assert metrics["xzkb_total_ms"] == {"samples": 1, "p50": 50.0, "p95": 50.0}
    assert metrics["tts_queue_ms"] == {"samples": 1, "p50": 0.0, "p95": 0.0}
    assert metrics["tts_synthesis_ms"] == {"samples": 1, "p50": 40.0, "p95": 40.0}
    assert metrics["server_pipeline_total_ms"] == {
        "samples": 1,
        "p50": 100.0,
        "p95": 100.0,
    }


@pytest.mark.asyncio
async def test_device_turn_records_gate_waits_and_empty_result_retry_ttft():
    clock = Clock()
    session, recorder = make_session(
        clock,
        xzkb_wait=0.007,
        tts_wait=0.011,
        retry_empty=True,
    )

    await session.process_wav(wav())

    metrics = recorder.snapshot()["metrics"]
    assert metrics["xzkb_queue_ms"]["p50"] == 7.0
    assert metrics["xzkb_headers_ms"]["p50"] == 4.0
    assert metrics["xzkb_first_sse_ms"]["p50"] == 6.0
    assert metrics["xzkb_first_content_ms"]["p50"] == 20.0
    assert metrics["xzkb_ttft_ms"]["p50"] == 30.0
    assert metrics["tts_queue_ms"]["p50"] == 11.0


@pytest.mark.asyncio
async def test_whitespace_stream_event_does_not_complete_xzkb_ttft():
    clock = Clock()
    state = GuideStateStore()
    speech = TimedSpeech(clock)
    controller = GuideController(state, WhitespaceThenTextXzkb(clock), speech)
    timing = TurnTiming(clock=clock)

    await controller.ask_text("question", timing=timing)

    assert timing.xzkb_headers_ms == 5.0
    assert timing.xzkb_first_sse_ms == 5.0
    assert timing.xzkb_first_content_ms == pytest.approx(20.0)
    assert timing.xzkb_ttft_ms == 30.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("xzkb", "expected"),
    [
        (FailingBeforeHeadersXzkb, (None, None, None)),
        (HeadersOnlyXzkb, (10.0, None, None)),
        (SseWithoutContentXzkb, (10.0, 10.0, None)),
    ],
)
async def test_missing_xzkb_milestones_remain_null(xzkb, expected):
    clock = Clock()
    state = GuideStateStore()
    controller = GuideController(state, xzkb(clock), TimedSpeech(clock))
    timing = TurnTiming(clock=clock)

    with pytest.raises(GuideServiceUnavailable):
        await controller.ask_text("question", timing=timing)

    assert (
        timing.xzkb_headers_ms,
        timing.xzkb_first_sse_ms,
        timing.xzkb_first_content_ms,
    ) == expected


@pytest.mark.asyncio
async def test_xzkb_queue_timeout_records_wait_duration_and_stage():
    clock = Clock()
    state = GuideStateStore()
    speech = TimedSpeech(clock)
    controller = GuideController(
        state,
        TimedXzkb(clock),
        speech,
        xzkb_gate=TimeoutGate(clock, 0.125),
    )
    timing = TurnTiming(clock=clock)

    with pytest.raises(GuideServiceUnavailable):
        await controller.ask_text("question", timing=timing)

    assert timing.xzkb_queue_ms == 125.0
    assert timing.failure_stage == "xzkb_queue"
    assert timing.error_type == "QueueWaitTimeout"


@pytest.mark.asyncio
async def test_tts_queue_timeout_records_wait_duration_and_queue_stage():
    clock = Clock()
    state = GuideStateStore()
    speech = TimedSpeech(clock)
    controller = GuideController(
        state,
        TimedXzkb(clock),
        speech,
        tts_gate=TimeoutGate(clock, 0.125),
    )
    timing = TurnTiming(clock=clock)

    result = await controller.ask_text("question", timing=timing)

    assert result.warning is not None
    assert timing.tts_queue_ms == pytest.approx(125.0)
    assert timing.failure_stage == "tts_queue"
    assert timing.error_type == "QueueWaitTimeout"


@pytest.mark.asyncio
async def test_tts_degradation_is_logged_but_excluded_from_baseline_window():
    clock = Clock()
    session, recorder = make_session(clock, fail_tts=True)

    result = await session.process_wav(wav())

    assert result.warning is not None
    snapshot = recorder.snapshot()
    assert snapshot["window_size"] == 0
    assert snapshot["counts"] == {"success": 0, "degraded": 1, "error": 0}


@pytest.mark.asyncio
async def test_asr_failure_records_elapsed_time_without_a_baseline_sample():
    clock = Clock()
    session, recorder = make_session(clock, fail_asr=True)

    with pytest.raises(DeviceTranscriptionUnavailable):
        await session.process_wav(wav())

    snapshot = recorder.snapshot()
    assert snapshot["window_size"] == 0
    assert snapshot["counts"] == {"success": 0, "degraded": 0, "error": 1}


def test_nearest_rank_handles_empty_and_single_samples():
    assert nearest_rank([], 50) is None
    assert nearest_rank([7.5], 50) == 7.5
    assert nearest_rank([7.5], 95) == 7.5
    assert nearest_rank([1, 2, 3, 4, 5], 50) == 3
    assert nearest_rank([1, 2, 3, 4, 5], 95) == 5


def test_recorder_emits_timing_log_at_warning_level(caplog):
    clock = Clock()
    recorder = DeviceLatencyRecorder(
        utc_now=lambda: "2026-08-18T01:02:03.456Z"
    )
    timing = TurnTiming(clock=clock)
    timing.xzkb_headers_ms = 1.25
    timing.xzkb_first_sse_ms = 2.5
    timing.xzkb_first_content_ms = 3.75
    with caplog.at_level(logging.WARNING, logger="showroom_guide.latency"):
        recorder.record(timing, "error", complete_success=False)

    assert "device_turn_timing" in caplog.text
    assert '"xzkb_headers_ms": 1.25' in caplog.text
    assert '"xzkb_first_sse_ms": 2.5' in caplog.text
    assert '"xzkb_first_content_ms": 3.75' in caplog.text
    assert '"recorded_at": "2026-08-18T01:02:03.456Z"' in caplog.text
    assert "question" not in caplog.text
    assert "answer" not in caplog.text
    assert "api-key" not in caplog.text


def test_latest_starts_empty_and_tracks_all_outcomes_without_affecting_window():
    clock = Clock()
    timestamps = iter(
        [
            "2026-08-18T01:02:03.000Z",
            "2026-08-18T01:02:04.000Z",
            "2026-08-18T01:02:05.000Z",
        ]
    )
    recorder = DeviceLatencyRecorder(utc_now=lambda: next(timestamps))

    assert recorder.snapshot()["latest"] is None

    success = TurnTiming(clock=clock, turn_id="success-turn")
    success.asr_ms = 12.345
    recorder.record(success, "success", complete_success=True)

    first = recorder.snapshot()
    assert first["window_size"] == 1
    assert first["latest"]["recorded_at"] == "2026-08-18T01:02:03.000Z"
    assert first["latest"]["turn_id"] == "success-turn"
    assert first["latest"]["outcome"] == "success"
    assert first["latest"]["asr_ms"] == 12.35
    assert all(name in first["latest"] for name in METRIC_NAMES)

    degraded = TurnTiming(clock=clock, turn_id="degraded-turn")
    degraded.tts_synthesis_ms = 8.0
    recorder.record(degraded, "degraded", complete_success=False)

    second = recorder.snapshot()
    assert second["window_size"] == 1
    assert second["latest"]["turn_id"] == "degraded-turn"
    assert second["latest"]["outcome"] == "degraded"
    assert second["latest"]["tts_synthesis_ms"] == 8.0

    error = TurnTiming(clock=clock, turn_id="error-turn")
    error.fail("asr", ValueError())
    recorder.record(error, "error", complete_success=False)

    latest = recorder.snapshot()["latest"]
    assert latest["turn_id"] == "error-turn"
    assert latest["outcome"] == "error"
    assert latest["recorded_at"] == "2026-08-18T01:02:05.000Z"
    assert latest["failure_stage"] == "asr"
    assert latest["error_type"] == "ValueError"
    assert latest["xzkb_headers_ms"] is None
    assert latest["tts_synthesis_ms"] is None


def test_latest_snapshot_is_a_copy():
    clock = Clock()
    recorder = DeviceLatencyRecorder(
        utc_now=lambda: "2026-08-18T01:02:03.000Z"
    )
    timing = TurnTiming(clock=clock, turn_id="original-turn")
    recorder.record(timing, "success", complete_success=True)

    snapshot = recorder.snapshot()
    snapshot["latest"]["turn_id"] = "modified-outside"

    assert recorder.snapshot()["latest"]["turn_id"] == "original-turn"


def test_latency_recorder_caps_success_window_at_500_and_omits_empty_metrics():
    clock = Clock()
    recorder = DeviceLatencyRecorder()
    for number in range(501):
        timing = TurnTiming(clock=clock)
        timing.asr_ms = float(number)
        recorder.record(timing, "success", complete_success=True)

    snapshot = recorder.snapshot()
    assert snapshot["window_size"] == 500
    assert snapshot["metrics"]["asr_ms"] == {
        "samples": 500,
        "p50": 250.0,
        "p95": 475.0,
    }
    assert snapshot["metrics"]["tts_synthesis_ms"] == {
        "samples": 0,
        "p50": None,
        "p95": None,
    }
