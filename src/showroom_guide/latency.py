import json
import logging
import math
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field


METRIC_NAMES = (
    "asr_ms",
    "xzkb_queue_ms",
    "xzkb_ttft_ms",
    "xzkb_generation_ms",
    "xzkb_total_ms",
    "tts_queue_ms",
    "tts_synthesis_ms",
    "server_pipeline_total_ms",
)

logger = logging.getLogger(__name__)


@dataclass
class TurnTiming:
    """Per-device-turn timings. Values are milliseconds and contain no content."""

    clock: Callable[[], float] = time.perf_counter
    turn_id: str = field(default_factory=lambda: secrets.token_hex(12))
    started_at: float = field(init=False)
    asr_ms: float | None = None
    xzkb_queue_ms: float | None = None
    xzkb_ttft_ms: float | None = None
    xzkb_generation_ms: float | None = None
    xzkb_total_ms: float | None = None
    tts_queue_ms: float | None = None
    tts_synthesis_ms: float | None = None
    server_pipeline_total_ms: float | None = None
    failure_stage: str | None = None
    error_type: str | None = None
    _asr_started_at: float | None = field(default=None, init=False)
    _xzkb_queue_started_at: float | None = field(default=None, init=False)
    _xzkb_started_at: float | None = field(default=None, init=False)
    _xzkb_first_text_at: float | None = field(default=None, init=False)
    _tts_queue_started_at: float | None = field(default=None, init=False)
    _tts_started_at: float | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.started_at = self.clock()

    def now(self) -> float:
        return self.clock()

    @staticmethod
    def _milliseconds(started_at: float, ended_at: float) -> float:
        return (ended_at - started_at) * 1000

    def start_asr(self) -> None:
        self._asr_started_at = self.now()

    def finish_asr(self) -> None:
        if self._asr_started_at is not None:
            self.asr_ms = self._milliseconds(self._asr_started_at, self.now())

    def start_xzkb_queue(self) -> None:
        self._xzkb_queue_started_at = self.now()

    def enter_xzkb_slot(self) -> None:
        self.finish_xzkb_queue()

    def finish_xzkb_queue(self) -> None:
        if self._xzkb_queue_started_at is not None:
            self.xzkb_queue_ms = self._milliseconds(
                self._xzkb_queue_started_at,
                self.now(),
            )

    def start_xzkb_request(self) -> None:
        if self._xzkb_started_at is None:
            self._xzkb_started_at = self.now()

    def receive_xzkb_text(self) -> None:
        if self._xzkb_started_at is None or self._xzkb_first_text_at is not None:
            return
        self._xzkb_first_text_at = self.now()
        self.xzkb_ttft_ms = self._milliseconds(self._xzkb_started_at, self._xzkb_first_text_at)

    def finish_xzkb(self) -> None:
        if self._xzkb_started_at is None:
            return
        ended_at = self.now()
        self.xzkb_total_ms = self._milliseconds(self._xzkb_started_at, ended_at)
        if self._xzkb_first_text_at is not None:
            self.xzkb_generation_ms = self._milliseconds(self._xzkb_first_text_at, ended_at)

    def start_tts_queue(self) -> None:
        self._tts_queue_started_at = self.now()

    def enter_tts_slot(self) -> None:
        self.finish_tts_queue()

    def finish_tts_queue(self) -> None:
        if self._tts_queue_started_at is not None:
            self.tts_queue_ms = self._milliseconds(
                self._tts_queue_started_at,
                self.now(),
            )

    def start_tts_synthesis(self) -> None:
        self._tts_started_at = self.now()

    def finish_tts_synthesis(self) -> None:
        if self._tts_started_at is not None:
            self.tts_synthesis_ms = self._milliseconds(self._tts_started_at, self.now())

    def finish_pipeline(self) -> None:
        self.server_pipeline_total_ms = self._milliseconds(self.started_at, self.now())

    def fail(self, stage: str, error: BaseException) -> None:
        self.failure_stage = stage
        self.error_type = type(error).__name__

    def log_payload(self, outcome: str) -> dict[str, object]:
        payload: dict[str, object] = {
            "turn_id": self.turn_id,
            "outcome": outcome,
            "failure_stage": self.failure_stage,
            "error_type": self.error_type,
        }
        payload.update({name: None if (value := getattr(self, name)) is None else round(value, 2) for name in METRIC_NAMES})
        return payload


def nearest_rank(samples: list[float], percentile: int) -> float | None:
    if not samples:
        return None
    rank = math.ceil(percentile / 100 * len(samples))
    return sorted(samples)[rank - 1]


class DeviceLatencyRecorder:
    """Thread-safe aggregate metrics for recent complete successful device turns."""

    def __init__(self, max_samples: int = 500) -> None:
        self._samples: deque[TurnTiming] = deque(maxlen=max_samples)
        self._counts = {"success": 0, "degraded": 0, "error": 0}
        self._lock = threading.Lock()

    def start_turn(self, clock: Callable[[], float] = time.perf_counter) -> TurnTiming:
        return TurnTiming(clock=clock)

    def record(self, timing: TurnTiming, outcome: str, complete_success: bool) -> None:
        with self._lock:
            self._counts[outcome] += 1
            if complete_success:
                self._samples.append(timing)
        logger.warning(
            "device_turn_timing %s",
            json.dumps(timing.log_payload(outcome), sort_keys=True),
        )

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            samples = list(self._samples)
            counts = dict(self._counts)
        metrics: dict[str, dict[str, float | int | None]] = {}
        for name in METRIC_NAMES:
            values = [value for item in samples if (value := getattr(item, name)) is not None]
            metrics[name] = {
                "samples": len(values),
                "p50": self._rounded(nearest_rank(values, 50)),
                "p95": self._rounded(nearest_rank(values, 95)),
            }
        return {"window_size": len(samples), "counts": counts, "metrics": metrics}

    @staticmethod
    def _rounded(value: float | None) -> float | None:
        return None if value is None else round(value, 2)
