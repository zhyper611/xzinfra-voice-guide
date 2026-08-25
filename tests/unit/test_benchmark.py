from __future__ import annotations

import asyncio
import io
import wave
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

import showroom_guide.benchmark as benchmark
from showroom_guide.benchmark import (
    AudioInfo,
    BenchmarkAudio,
    BenchmarkRunner,
    BenchmarkSample,
    DeviceApiClient,
    SCENARIOS,
    ScenarioRunStats,
    SubprocessServer,
    ensure_question_audio,
    formal_records,
    load_and_validate_audio,
    latency_values,
    nearest_rank,
    redact_text,
    settings_to_child_env,
    verify_asr_and_cache,
    write_outputs,
)
from showroom_guide.config import Settings
from showroom_guide.faq_audio import WavMetadata


def make_wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x01" * 160)
    return output.getvalue()


def latest_for(
    scenario_name: str,
    *,
    served_from: str | None = None,
    turn_id: str = "turn-1",
    outcome: str = "success",
) -> dict[str, object]:
    scenario = SCENARIOS[scenario_name]
    return {
        "turn_id": turn_id,
        "outcome": outcome,
        "cache_hit": scenario.expected_cache_hit,
        "cache_entry_id": scenario.expected_cache_entry_id,
        "served_from": served_from or scenario.expected_served_from,
        "asr_ms": 10,
        "xzkb_queue_ms": 20,
        "xzkb_headers_ms": 30,
        "xzkb_first_sse_ms": 4,
        "xzkb_first_content_ms": 5,
        "xzkb_ttft_ms": 9,
        "xzkb_generation_ms": 11,
        "xzkb_total_ms": 50,
        "tts_queue_ms": 2,
        "tts_synthesis_ms": 15,
        "server_pipeline_total_ms": 70,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario_name", ["xzkb_online_tts", "faq_online_tts", "prepared_audio"]
)
async def test_three_served_from_values_are_validated(scenario_name: str) -> None:
    calls: list[str] = []
    metrics_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal metrics_calls
        calls.append(request.url.path)
        if request.url.path.endswith("/turn"):
            return httpx.Response(
                200, json={"transcript": "ignored", "answer": "ignored"}
            )
        if request.url.path.endswith("/metrics"):
            metrics_calls += 1
            turn_id = "previous" if metrics_calls == 1 else "turn-1"
            return httpx.Response(
                200,
                json={"latest": latest_for(scenario_name, turn_id=turn_id)},
            )
        return httpx.Response(204)

    transport_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = DeviceApiClient("http://fake", "device-secret", client=transport_client)
    async with client:
        sample = await client.run_turn(
            make_wav(),
            SCENARIOS[scenario_name],
            run_number=1,
            warmup=False,
        )
    await transport_client.aclose()

    assert sample.valid is True
    assert sample.served_from == SCENARIOS[scenario_name].expected_served_from
    assert calls == [
        "/api/device/metrics",
        "/api/device/turn",
        "/api/device/metrics",
        "/api/device/playback-finished",
    ]


@pytest.mark.asyncio
async def test_degraded_outcome_passes_routing_validation() -> None:
    metrics_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal metrics_calls
        if request.url.path.endswith("/metrics"):
            metrics_calls += 1
            return httpx.Response(
                200,
                json={
                    "latest": latest_for(
                        "prepared_audio",
                        turn_id="old" if metrics_calls == 1 else "new",
                        outcome="degraded",
                    )
                },
            )
        if request.url.path.endswith("/turn"):
            return httpx.Response(200, json={})
        return httpx.Response(204)

    transport_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = DeviceApiClient("http://fake", "device-secret", client=transport_client)
    async with client:
        sample = await client.run_turn(
            make_wav(),
            SCENARIOS["prepared_audio"],
            run_number=1,
            warmup=False,
        )
    await transport_client.aclose()

    assert sample.valid is True
    assert sample.outcome == "degraded"
    assert latency_values([sample], "server_pipeline_total_ms") == []


@pytest.mark.asyncio
async def test_served_from_mismatch_is_invalid_and_excluded() -> None:
    metrics_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal metrics_calls
        if request.url.path.endswith("/turn"):
            return httpx.Response(200, json={"transcript": "ignored"})
        if request.url.path.endswith("/metrics"):
            metrics_calls += 1
            return httpx.Response(
                200,
                json={
                    "latest": latest_for(
                        "prepared_audio",
                        served_from="faq_online_tts",
                        turn_id="previous" if metrics_calls == 1 else "turn-1",
                    )
                },
            )
        return httpx.Response(204)

    transport_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = DeviceApiClient("http://fake", "device-secret", client=transport_client)
    async with client:
        sample = await client.run_turn(
            make_wav(),
            SCENARIOS["prepared_audio"],
            run_number=1,
            warmup=False,
        )
    await transport_client.aclose()

    assert sample.valid is False
    assert sample.invalid_reason == "served_from_mismatch"
    assert latency_samples([sample]) == []


def latency_samples(records: list[BenchmarkSample]) -> list[float]:
    return [
        record.server_pipeline_total_ms
        for record in records
        if record.valid and record.server_pipeline_total_ms is not None
    ]


def test_degraded_is_valid_but_excluded_from_latency_values() -> None:
    degraded = BenchmarkSample(
        scenario="prepared_audio",
        run_number=1,
        warmup=False,
        outcome="degraded",
        valid=True,
        server_pipeline_total_ms=99,
    )
    success = BenchmarkSample(
        scenario="prepared_audio",
        run_number=2,
        warmup=False,
        outcome="success",
        valid=True,
        server_pipeline_total_ms=10,
    )

    assert latency_values([degraded, success], "server_pipeline_total_ms") == [10.0]


@pytest.mark.asyncio
async def test_warmup_records_are_excluded_from_formal_statistics() -> None:
    class FakeServer:
        starts = 0

        async def __aenter__(self) -> str:
            self.starts += 1
            return "http://fake"

        async def __aexit__(self, *_: object) -> None:
            return None

    class FakeClient:
        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def run_turn(self, audio, scenario, *, run_number, warmup):
            return BenchmarkSample(
                scenario=scenario.name,
                run_number=run_number,
                warmup=warmup,
                outcome="success",
                valid=True,
                served_from=scenario.expected_served_from,
                server_pipeline_total_ms=float(run_number),
            )

    server = FakeServer()
    progress: list[str] = []
    records = await BenchmarkRunner(
        server_factory=lambda _scenario: server,
        client_factory=lambda _url: FakeClient(),
        interval_seconds=0,
        scenario_cooldown_seconds=0,
        progress=progress.append,
    ).run(
        make_wav(),
        [SCENARIOS["xzkb_online_tts"], SCENARIOS["faq_online_tts"]],
        runs=2,
        warmup=1,
    )

    assert len(records) == 6
    assert len(formal_records(records)) == 4
    assert sum(record.warmup for record in records) == 2
    assert server.starts == 2
    request_starts = [
        message
        for message in progress
        if (" warmup " in message or " 正式 " in message) and message.endswith("开始")
    ]
    request_completions = [
        message
        for message in progress
        if (" warmup " in message or " 正式 " in message) and " 完成：" in message
    ]
    assert len(request_starts) == 6
    assert len(request_completions) == 6
    assert any("正式 2/2 开始" in message for message in progress)
    assert any(
        "完成：success" in message and "served_from=faq_online_tts" in message
        for message in progress
    )
    assert any(
        "[faq_online_tts] 场景完成：正式成功 2/2" in message for message in progress
    )


def test_nearest_rank_p50_p95_matches_project_definition() -> None:
    values = [1.0, 2.0, 3.0, 4.0]
    assert nearest_rank(values, 50) == 2.0
    assert nearest_rank(values, 95) == 4.0


class TrackingServer:
    def __init__(self, index: int, events: list[tuple[str, int]]) -> None:
        self.index = index
        self.events = events

    async def __aenter__(self) -> str:
        self.events.append(("start", self.index))
        return f"http://fake-{self.index}"

    async def __aexit__(self, *_: object) -> None:
        self.events.append(("stop", self.index))


class SequenceClient:
    def __init__(self, outcomes: list[BenchmarkSample]) -> None:
        self.outcomes = outcomes

    async def __aenter__(self) -> "SequenceClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def run_turn(self, _audio, _scenario, *, run_number, warmup):
        sample = self.outcomes.pop(0)
        sample.run_number = run_number
        sample.warmup = warmup
        return sample


def runner_sample(
    *,
    valid: bool,
    error_type: str | None = None,
    invalid_reason: str | None = None,
    request_completed: bool = False,
) -> BenchmarkSample:
    return BenchmarkSample(
        scenario="xzkb_online_tts",
        run_number=0,
        warmup=False,
        valid=valid,
        outcome="success" if valid else None,
        error_type=error_type,
        invalid_reason=invalid_reason,
        turn_request_sent=request_completed,
        turn_response_received=request_completed,
        request_completed=request_completed,
        server_pipeline_total_ms=10 if valid else None,
    )


def make_sequence_runner(
    outcomes: list[BenchmarkSample],
    *,
    max_consecutive_errors: int = 3,
) -> tuple[BenchmarkRunner, list[tuple[str, int]]]:
    events: list[tuple[str, int]] = []
    servers: list[TrackingServer] = []

    def server_factory(_scenario):
        server = TrackingServer(len(servers), events)
        servers.append(server)
        return server

    def client_factory(_url):
        return SequenceClient(outcomes)

    return (
        BenchmarkRunner(
            server_factory=server_factory,
            client_factory=client_factory,
            max_consecutive_errors=max_consecutive_errors,
            interval_seconds=0,
            scenario_cooldown_seconds=0,
        ),
        events,
    )


@pytest.mark.asyncio
async def test_client_factory_failure_closes_started_server() -> None:
    events: list[tuple[str, int]] = []
    server = TrackingServer(0, events)

    def client_factory(_url: str):
        raise RuntimeError("client construction failed")

    runner = BenchmarkRunner(
        server_factory=lambda _scenario: server,
        client_factory=client_factory,
        max_consecutive_errors=1,
        interval_seconds=0,
        scenario_cooldown_seconds=0,
    )
    records = await runner.run(
        make_wav(), [SCENARIOS["prepared_audio"]], runs=1, warmup=0
    )

    assert len(records) == 1
    assert records[0].invalid_reason == "server_startup_failed"
    assert events == [("start", 0), ("stop", 0)]
    stats = runner.run_stats["prepared_audio"]
    assert stats.sample_attempts == 1
    assert stats.turn_requests_sent == 0
    assert stats.turn_responses_received == 0


@pytest.mark.asyncio
async def test_client_enter_failure_closes_started_server() -> None:
    events: list[tuple[str, int]] = []
    server = TrackingServer(0, events)

    class FailingClient:
        async def __aenter__(self):
            raise RuntimeError("client enter failed")

        async def __aexit__(self, *_: object) -> None:
            return None

    runner = BenchmarkRunner(
        server_factory=lambda _scenario: server,
        client_factory=lambda _url: FailingClient(),
        max_consecutive_errors=1,
        interval_seconds=0,
        scenario_cooldown_seconds=0,
    )
    records = await runner.run(
        make_wav(), [SCENARIOS["prepared_audio"]], runs=1, warmup=0
    )

    assert len(records) == 1
    assert records[0].invalid_reason == "server_startup_failed"
    assert events == [("start", 0), ("stop", 0)]


@pytest.mark.asyncio
async def test_timeout_restarts_old_service_before_next_sample() -> None:
    runner, events = make_sequence_runner(
        [
            runner_sample(
                valid=False,
                error_type="ReadTimeout",
                invalid_reason="turn_request_failed",
            ),
            runner_sample(valid=True, request_completed=True),
        ]
    )

    records = await runner.run(
        make_wav(), [SCENARIOS["xzkb_online_tts"]], runs=2, warmup=0
    )

    assert [record.valid for record in records] == [False, True]
    assert events == [("start", 0), ("stop", 0), ("start", 1), ("stop", 1)]
    assert runner.run_stats["xzkb_online_tts"].timeout_count == 1
    assert runner.run_stats["xzkb_online_tts"].restart_count == 1


@pytest.mark.asyncio
async def test_409_busy_restarts_service() -> None:
    runner, events = make_sequence_runner(
        [
            runner_sample(
                valid=False,
                error_type="HTTP409Busy",
                invalid_reason="turn_busy_409",
            ),
            runner_sample(valid=True),
        ]
    )

    records = await runner.run(
        make_wav(), [SCENARIOS["prepared_audio"]], runs=2, warmup=0
    )

    assert records[0].invalid_reason == "turn_busy_409"
    assert records[1].valid is True
    assert events == [("start", 0), ("stop", 0), ("start", 1), ("stop", 1)]


@pytest.mark.asyncio
async def test_three_consecutive_errors_trigger_circuit_breaker() -> None:
    runner, events = make_sequence_runner(
        [
            runner_sample(valid=False, error_type="ReadTimeout"),
            runner_sample(valid=False, error_type="ConnectTimeout"),
            runner_sample(valid=False, error_type="RemoteProtocolError"),
            runner_sample(valid=True),
        ],
        max_consecutive_errors=3,
    )

    records = await runner.run(
        make_wav(), [SCENARIOS["xzkb_online_tts"]], runs=4, warmup=0
    )

    stats = runner.run_stats["xzkb_online_tts"]
    assert len(records) == 3
    assert stats.circuit_breaker_triggered is True
    assert stats.timeout_count == 2
    assert events == [
        ("start", 0),
        ("stop", 0),
        ("start", 1),
        ("stop", 1),
        ("start", 2),
        ("stop", 2),
    ]


@pytest.mark.asyncio
async def test_warmup_failures_do_not_start_formal_samples() -> None:
    runner, _events = make_sequence_runner(
        [
            runner_sample(valid=False, error_type="ReadTimeout"),
            runner_sample(valid=False, error_type="ReadTimeout"),
            runner_sample(valid=False, error_type="ReadTimeout"),
        ],
        max_consecutive_errors=3,
    )

    records = await runner.run(
        make_wav(), [SCENARIOS["xzkb_online_tts"]], runs=5, warmup=1
    )

    stats = runner.run_stats["xzkb_online_tts"]
    assert len(records) == 3
    assert all(record.warmup for record in records)
    assert stats.formal_attempts == 0
    assert stats.circuit_breaker_triggered is True


@pytest.mark.asyncio
async def test_formal_failures_are_not_replaced_by_successful_retries() -> None:
    runner, _events = make_sequence_runner(
        [
            runner_sample(valid=False, error_type="ReadTimeout"),
            runner_sample(valid=True),
            runner_sample(valid=False, error_type="ConnectTimeout"),
            runner_sample(valid=True),
        ]
    )

    records = await runner.run(
        make_wav(), [SCENARIOS["xzkb_online_tts"]], runs=3, warmup=0
    )

    assert [record.run_number for record in records] == [1, 2, 3]
    assert [record.valid for record in records] == [False, True, False]
    assert runner.run_stats["xzkb_online_tts"].formal_attempts == 3


@pytest.mark.asyncio
async def test_request_counters_distinguish_sample_attempts_from_turn_requests() -> (
    None
):
    runner, _events = make_sequence_runner(
        [
            runner_sample(valid=False, invalid_reason="previous_metrics_failed"),
            runner_sample(valid=True, request_completed=True),
        ]
    )

    await runner.run(make_wav(), [SCENARIOS["xzkb_online_tts"]], runs=2, warmup=0)

    stats = runner.run_stats["xzkb_online_tts"]
    assert stats.sample_attempts == 2
    assert stats.turn_requests_sent == 1
    assert stats.turn_responses_received == 1
    assert stats.successful_voice_turns == 1


@pytest.mark.asyncio
async def test_non_200_response_reads_safe_failure_metrics_without_playback() -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/turn"):
            return httpx.Response(503, json={"detail": "not saved"})
        if request.url.path.endswith("/metrics"):
            return httpx.Response(200, json={"latest": None})
        return httpx.Response(204)

    transport_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = DeviceApiClient("http://fake", "device-secret", client=transport_client)
    async with client:
        sample = await client.run_turn(
            make_wav(),
            SCENARIOS["xzkb_online_tts"],
            run_number=1,
            warmup=False,
        )
    await transport_client.aclose()

    assert sample.valid is False
    assert sample.invalid_reason == "turn_http_status"
    assert sample.failure_stage == "unknown"
    assert sample.error_type == "HTTP503"
    assert sample.turn_request_sent is True
    assert sample.turn_response_received is True
    assert calls == [
        "/api/device/metrics",
        "/api/device/turn",
        "/api/device/metrics",
    ]


@pytest.mark.asyncio
async def test_http_failure_records_correlated_safe_stage_and_error_type() -> None:
    metrics_reads = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal metrics_reads
        if request.url.path.endswith("/turn"):
            return httpx.Response(
                503,
                json={"detail": "语音识别暂时不可用，请稍后重试"},
            )
        if request.url.path.endswith("/metrics"):
            metrics_reads += 1
            latest = None
            if metrics_reads == 2:
                latest = {
                    "turn_id": "failed-turn",
                    "outcome": "error",
                    "failure_stage": "asr",
                    "error_type": "ConnectError",
                    "asr_ms": 123.0,
                }
            return httpx.Response(200, json={"latest": latest})
        return httpx.Response(204)

    transport_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = DeviceApiClient("http://fake", "device-secret", client=transport_client)
    async with client:
        sample = await client.run_turn(
            make_wav(),
            SCENARIOS["prepared_audio"],
            run_number=1,
            warmup=False,
        )
    await transport_client.aclose()

    assert sample.valid is False
    assert sample.failure_stage == "asr"
    assert sample.error_type == "ConnectError"
    assert sample.turn_id == "failed-turn"
    assert sample.asr_ms == 123.0


@pytest.mark.asyncio
async def test_interleaved_order_rotates_scenarios_each_round() -> None:
    events: list[tuple[str, bool, int, bytes]] = []

    class FakeServer:
        def __init__(self, scenario_name: str) -> None:
            self.scenario_name = scenario_name

        async def __aenter__(self) -> str:
            return f"http://{self.scenario_name}"

        async def __aexit__(self, *_: object) -> None:
            return None

    class FakeClient:
        def __init__(self, scenario_name: str) -> None:
            self.scenario_name = scenario_name

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def run_turn(self, audio, scenario, *, run_number, warmup):
            events.append((scenario.name, warmup, run_number, audio))
            return BenchmarkSample(
                scenario=scenario.name,
                run_number=run_number,
                warmup=warmup,
                outcome="success",
                served_from=scenario.expected_served_from,
                valid=True,
            )

    scenarios = list(SCENARIOS.values())
    audio_metadata = WavMetadata(
        sample_rate=16000,
        channels=1,
        sample_width=2,
        frames=1,
        duration_seconds=0.0000625,
    )
    fixtures = [
        BenchmarkAudio(
            b"first",
            AudioInfo("first.wav", "1" * 64, audio_metadata),
            "first",
        ),
        BenchmarkAudio(
            b"second",
            AudioInfo("second.wav", "2" * 64, audio_metadata),
            "second",
        ),
    ]
    runner = BenchmarkRunner(
        server_factory=lambda scenario: FakeServer(scenario.name),
        client_factory=lambda url: FakeClient(url.removeprefix("http://")),
        interval_seconds=0,
        scenario_cooldown_seconds=0,
        execution_order="interleaved",
    )
    records = await runner.run(fixtures, scenarios, runs=2, warmup=1)

    expected_round = [scenario.name for scenario in scenarios]
    assert [event[0] for event in events] == expected_round * 3
    assert [event[1:3] for event in events[:3]] == [(True, 1)] * 3
    assert [event[1:3] for event in events[3:6]] == [(False, 1)] * 3
    assert [event[1:3] for event in events[6:9]] == [(False, 2)] * 3
    assert [event[3] for event in events] == [b"first"] * 6 + [b"second"] * 3
    assert records[0].scenario == scenarios[0].name
    assert records[0].audio_file_name == "first.wav"
    assert records[-1].audio_file_name == "second.wav"


@pytest.mark.asyncio
async def test_reliability_mode_completes_fixed_attempts_without_breaker() -> None:
    class FakeServer:
        async def __aenter__(self) -> str:
            return "http://fake"

        async def __aexit__(self, *_: object) -> None:
            return None

    class FailingClient:
        async def __aenter__(self) -> "FailingClient":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def run_turn(self, _audio, scenario, *, run_number, warmup):
            return BenchmarkSample(
                scenario=scenario.name,
                run_number=run_number,
                warmup=warmup,
                status_code=503,
                turn_request_sent=True,
                turn_response_received=True,
                request_completed=True,
                failure_stage="asr",
                error_type="HTTP503",
                invalid_reason="turn_http_status",
            )

    scenarios = [SCENARIOS["prepared_audio"], SCENARIOS["faq_online_tts"]]
    runner = BenchmarkRunner(
        server_factory=lambda _scenario: FakeServer(),
        client_factory=lambda _url: FailingClient(),
        interval_seconds=0,
        scenario_cooldown_seconds=0,
        benchmark_mode="reliability",
        execution_order="interleaved",
    )
    records = await runner.run(make_wav(), scenarios, runs=4, warmup=1)

    assert len(records) == 10
    for scenario in scenarios:
        stats = runner.run_stats[scenario.name]
        assert stats.warmup_attempts == 1
        assert stats.formal_attempts == 4
        assert stats.circuit_breaker_triggered is False


@pytest.mark.asyncio
async def test_timeout_is_an_error_without_saving_exception_text() -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/turn"):
            raise httpx.ReadTimeout(
                "Authorization: Bearer device-secret", request=request
            )
        if request.url.path.endswith("/metrics"):
            return httpx.Response(200, json={"latest": None})
        return httpx.Response(204)

    transport_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = DeviceApiClient("http://fake", "device-secret", client=transport_client)
    async with client:
        sample = await client.run_turn(
            make_wav(),
            SCENARIOS["xzkb_online_tts"],
            run_number=1,
            warmup=False,
        )
    await transport_client.aclose()

    assert sample.valid is False
    assert sample.invalid_reason == "turn_request_failed"
    assert sample.error_type == "ReadTimeout"
    assert sample.turn_request_sent is True
    assert sample.turn_response_received is False
    assert "device-secret" not in str(sample.as_dict())
    assert calls == ["/api/device/metrics", "/api/device/turn"]


@pytest.mark.asyncio
async def test_previous_and_latest_same_turn_id_is_stale() -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/metrics"):
            return httpx.Response(
                200,
                json={"latest": latest_for("prepared_audio", turn_id="same-turn")},
            )
        if request.url.path.endswith("/turn"):
            return httpx.Response(200, json={})
        return httpx.Response(204)

    transport_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = DeviceApiClient("http://fake", "device-secret", client=transport_client)
    async with client:
        sample = await client.run_turn(
            make_wav(),
            SCENARIOS["prepared_audio"],
            run_number=1,
            warmup=False,
        )
    await transport_client.aclose()

    assert sample.stale_metrics is True
    assert sample.invalid_reason == "stale_metrics"
    assert sample.valid is False
    assert calls == [
        "/api/device/metrics",
        "/api/device/turn",
        "/api/device/metrics",
        "/api/device/playback-finished",
    ]


@pytest.mark.asyncio
async def test_409_busy_does_not_read_metrics_or_finish_playback() -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/metrics"):
            return httpx.Response(
                200,
                json={"latest": latest_for("xzkb_online_tts", turn_id="old")},
            )
        if request.url.path.endswith("/turn"):
            return httpx.Response(409, json={"detail": "device is processing"})
        return httpx.Response(204)

    transport_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = DeviceApiClient("http://fake", "device-secret", client=transport_client)
    async with client:
        sample = await client.run_turn(
            make_wav(),
            SCENARIOS["xzkb_online_tts"],
            run_number=1,
            warmup=False,
        )
    await transport_client.aclose()

    assert sample.error_type == "HTTP409Busy"
    assert sample.restart_required is True
    assert calls == ["/api/device/metrics", "/api/device/turn"]


class FakeProcess:
    def __init__(self) -> None:
        self.alive = True
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return None if self.alive else 0

    def terminate(self) -> None:
        self.terminated = True
        self.alive = False

    def kill(self) -> None:
        self.killed = True
        self.alive = False

    def wait(self, timeout: float | None = None) -> int:
        self.alive = False
        return 0


@pytest.mark.asyncio
async def test_subprocess_is_cleaned_on_success_startup_failure_and_cancel() -> None:
    processes: list[FakeProcess] = []

    def factory(*_args, **_kwargs) -> FakeProcess:
        process = FakeProcess()
        processes.append(process)
        return process

    async def healthy(_url: str, _timeout: float) -> None:
        return None

    async with SubprocessServer(
        project_root=Path("."),
        child_env={},
        startup_timeout=1,
        process_factory=factory,
        health_check=healthy,
    ):
        pass
    assert processes[-1].terminated is True

    async def broken(_url: str, _timeout: float) -> None:
        raise RuntimeError("startup failed")

    with pytest.raises(RuntimeError):
        async with SubprocessServer(
            project_root=Path("."),
            child_env={},
            startup_timeout=1,
            process_factory=factory,
            health_check=broken,
        ):
            pass
    assert processes[-1].terminated is True

    with pytest.raises(asyncio.CancelledError):
        async with SubprocessServer(
            project_root=Path("."),
            child_env={},
            startup_timeout=1,
            process_factory=factory,
            health_check=healthy,
        ):
            raise asyncio.CancelledError
    assert processes[-1].terminated is True


@pytest.mark.asyncio
async def test_subprocess_uses_devnull_and_cleans_stale_tmp_logs(
    tmp_path: Path,
) -> None:
    process = FakeProcess()

    def factory(*_args, **kwargs) -> FakeProcess:
        assert kwargs["stdout"] == benchmark.subprocess.DEVNULL
        assert kwargs["stderr"] == benchmark.subprocess.DEVNULL
        return process

    async def healthy(_url: str, _timeout: float) -> None:
        return None

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    stale = log_dir / "old.tmp.log"
    stale.write_text("Authorization: Bearer api-secret", encoding="utf-8")
    async with SubprocessServer(
        project_root=Path("."),
        child_env={},
        startup_timeout=1,
        process_factory=factory,
        health_check=healthy,
        log_dir=log_dir,
    ):
        assert not stale.exists()
    assert not list(log_dir.glob("*.tmp.log"))


@pytest.mark.asyncio
async def test_historical_tmp_logs_are_removed_before_start(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    stale = log_dir / "old.tmp.log"
    stale.write_text("Authorization: Bearer old-secret", encoding="utf-8")

    async def healthy(_url: str, _timeout: float) -> None:
        return None

    async with SubprocessServer(
        project_root=Path("."),
        child_env={},
        startup_timeout=1,
        process_factory=lambda *_args, **_kwargs: FakeProcess(),
        health_check=healthy,
        log_dir=log_dir,
    ):
        assert not stale.exists()
    assert not list(log_dir.glob("*.tmp.log"))


def test_wav_validation_and_report_generation(tmp_path: Path) -> None:
    audio_path = tmp_path / "benchmark-fixtures" / "question.wav"
    audio_path.parent.mkdir()
    audio_path.write_bytes(make_wav())
    content, audio_info = load_and_validate_audio(audio_path, tmp_path)
    assert content == audio_path.read_bytes()
    assert audio_info.metadata.sample_rate == 16000
    assert len(audio_info.sha256) == 64

    records = [
        BenchmarkSample(
            scenario=scenario.name,
            run_number=1,
            warmup=False,
            status_code=200,
            metrics_status_code=200,
            playback_status_code=204,
            outcome="success",
            cache_hit=scenario.expected_cache_hit,
            cache_entry_id=scenario.expected_cache_entry_id,
            served_from=scenario.expected_served_from,
            valid=True,
            client_roundtrip_ms=10,
            server_pipeline_total_ms=8,
            tts_synthesis_ms=4,
        )
        for scenario in SCENARIOS.values()
    ]
    records.append(
        BenchmarkSample(
            scenario="prepared_audio",
            run_number=2,
            warmup=False,
            status_code=503,
            failure_stage="asr",
            error_type="ConnectError",
            invalid_reason="turn_http_status",
        )
    )
    fake_settings = type(
        "FakeSettings",
        (),
        {"tts_model": "test-model", "tts_voice": "alloy", "tts_speed": 1.0},
    )()
    report_stats = {
        scenario.name: ScenarioRunStats(
            scenario.name,
            planned_warmup_requests=1,
            planned_formal_requests=1,
            sample_attempts=4,
            turn_requests_sent=3,
            turn_responses_received=3,
            successful_voice_turns=3,
            warmup_attempts=2,
            formal_attempts=2,
            restart_count=1,
        )
        for scenario in SCENARIOS.values()
    }
    output_dir = tmp_path / "results"
    csv_path, json_path, report_path = write_outputs(
        output_dir,
        records=records,
        audio_info=audio_info,
        transcript="请介绍一下八大车间。",
        settings=fake_settings,
        scenarios=list(SCENARIOS.values()),
        runs=1,
        warmup=1,
        startup_timeout=1,
        run_stats=report_stats,
        project_root=tmp_path,
    )

    assert csv_path.is_file() and json_path.is_file() and report_path.is_file()
    report = report_path.read_text(encoding="utf-8")
    raw = json_path.read_text(encoding="utf-8")
    assert "client_roundtrip_ms" in report
    assert "server_pipeline_total_ms" in report
    assert "成功语音样本 P50/P95" in report
    report_lines = report.splitlines()
    result_separator = "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"
    separator_index = report_lines.index(result_separator)
    assert report_lines[separator_index + 1].startswith("| `")
    assert "sample_attempts" in report
    assert "请求成功率" in report
    assert "## 失败分布" in report
    assert "| `prepared_audio` | formal | 503 | `asr` | `ConnectError` | 1 |" in report
    assert "turn_requests_sent" in report
    assert "| `prepared_audio` | 1 | 2 | 1 | 2 | 4 | 3 | 3 | 3 | 1 |" in report
    assert "测试采用同一组 AI 合成的标准化问题音频" in report
    assert "本次新生成测试问题音频 `0` 条，复用 `1` 条" in report
    assert "另有 1 次测试问题 TTS 音频生成" not in report
    assert "api-key-secret" not in report + raw
    assert "answer_content_saved" in raw


def test_secret_redaction_and_protected_audio_path(tmp_path: Path) -> None:
    assert "top-secret" not in redact_text(
        "Authorization: Bearer top-secret api_key=top-secret",
        ["top-secret"],
    )
    protected = tmp_path / "config" / "prepared_audio" / "bad.wav"
    with pytest.raises(RuntimeError):
        load_and_validate_audio(protected, tmp_path)

    with pytest.raises(RuntimeError):
        write_outputs(
            tmp_path / "config" / "prepared_audio",
            records=[],
            audio_info=AudioInfo(
                file_name="question.wav",
                sha256="0" * 64,
                metadata=WavMetadata(
                    sample_rate=16000,
                    channels=1,
                    sample_width=2,
                    frames=1,
                    duration_seconds=0.0000625,
                ),
            ),
            transcript="question",
            settings=type(
                "FakeSettings",
                (),
                {"tts_model": "m", "tts_voice": "v", "tts_speed": 1.0},
            )(),
            scenarios=[SCENARIOS["prepared_audio"]],
            runs=1,
            warmup=0,
            startup_timeout=1,
            project_root=tmp_path,
        )


@pytest.mark.asyncio
async def test_question_audio_is_synthesized_once_and_then_reused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FakeSpeech:
        calls = 0

        async def synthesize(self, text: str) -> bytes:
            assert text == benchmark.QUESTION
            self.calls += 1
            return make_wav()

        async def aclose(self) -> None:
            return None

    speech = FakeSpeech()
    monkeypatch.setattr(
        benchmark, "speech_client_from_settings", lambda _settings: speech
    )
    target = tmp_path / "benchmark-fixtures" / "question.wav"

    first = await ensure_question_audio(
        target,
        object(),
        confirm_live_requests=True,
        project_root=tmp_path,
    )
    second = await ensure_question_audio(
        target,
        object(),
        confirm_live_requests=True,
        project_root=tmp_path,
    )

    assert first[2] is True
    assert second[2] is False
    assert first[1].sha256 == second[1].sha256
    assert speech.calls == 1


@pytest.mark.asyncio
async def test_asr_smoke_requires_target_cache_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSpeech:
        async def transcribe(self, audio: io.BytesIO) -> str:
            assert audio.read() == make_wav()
            return benchmark.QUESTION

        async def aclose(self) -> None:
            return None

    class FakeCache:
        def match(self, transcript: str):
            assert transcript == benchmark.QUESTION
            return type("Entry", (), {"id": "eight_workshops_overview"})()

    monkeypatch.setattr(
        benchmark, "speech_client_from_settings", lambda _settings: FakeSpeech()
    )
    monkeypatch.setattr(benchmark, "load_cache", lambda _path: FakeCache())
    settings = type("SettingsProbe", (), {"faq_cache_file": Path("cache.yaml")})()

    assert await verify_asr_and_cache(make_wav(), settings) == benchmark.QUESTION


def make_settings_probe() -> Settings:
    return Settings(
        xzkb_base_url="https://xzkb.invalid",
        xzkb_api_key=SecretStr("x" * 16),
        xzkb_empty_search_response="empty",
        asr_base_url="https://asr.invalid",
        asr_api_key=SecretStr("a" * 16),
        asr_model="asr-model",
        tts_base_url="https://tts.invalid",
        tts_api_key=SecretStr("t" * 16),
        tts_model="tts-model",
        device_api_key=SecretStr("d" * 16),
    )


def test_child_environment_contains_only_scenario_switches_and_safe_timeout() -> None:
    settings = make_settings_probe()
    child = settings_to_child_env(settings, SCENARIOS["prepared_audio"])

    assert child["GUIDE_FAQ_CACHE_ENABLED"] == "true"
    assert child["GUIDE_FAQ_PREPARED_AUDIO_ENABLED"] == "true"
    assert child["GUIDE_FAQ_ADMIN_ENABLED"] == "false"
    assert child["GUIDE_KNOWLEDGE_CAPTURE_ENABLED"] == "false"
    assert (
        float(child["GUIDE_REQUEST_TIMEOUT_SECONDS"])
        == settings.request_timeout_seconds
    )
    assert "GUIDE_DEVICE_API_KEY" in child


def test_cli_requires_explicit_live_confirmation_and_hides_paths_and_secrets(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        benchmark, "load_validated_settings", lambda _path: make_settings_probe()
    )
    exit_code = benchmark.main(
        [
            "--audio",
            str(tmp_path / "missing.wav"),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "--confirm-live-requests" in captured.err
    assert str(tmp_path) not in captured.err
    assert "api_key" not in captured.err.lower()


def test_scenario_parser_accepts_comma_and_space_separated_values() -> None:
    assert [
        item.name
        for item in benchmark.parse_scenarios(
            ["xzkb_online_tts,faq_online_tts", "prepared_audio"]
        )
    ] == [
        "xzkb_online_tts",
        "faq_online_tts",
        "prepared_audio",
    ]


def test_parser_has_independent_timeout_and_recovery_defaults() -> None:
    args = benchmark.build_parser().parse_args([])

    assert args.turn_timeout == 240.0
    assert args.max_consecutive_errors == 3
    assert args.interval_seconds == 1.0
    assert args.scenario_cooldown_seconds == 10.0
    assert args.mode == "latency"
    assert args.execution_order == "interleaved"
    assert args.audio is None
    assert args.standard_fixtures is False
    assert args.prepare_audio_only is False


def test_standard_fixture_paths_map_to_distinct_questions(tmp_path: Path) -> None:
    questions = [
        benchmark.question_for_audio_path(path, tmp_path)
        for path, _question in benchmark.STANDARD_AUDIO_QUESTIONS
    ]

    assert questions == [
        question for _path, question in benchmark.STANDARD_AUDIO_QUESTIONS
    ]
    assert len(set(questions)) == 5


def test_prepare_audio_only_validates_all_standard_fixtures_without_runner(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prepared_questions: list[str] = []
    asr_calls = 0

    async def fake_audio(path, _settings, **kwargs):
        prepared_questions.append(kwargs["question"])
        info = AudioInfo(
            file_name=Path(path).as_posix(),
            sha256=str(len(prepared_questions)) * 64,
            metadata=WavMetadata(
                sample_rate=16000,
                channels=1,
                sample_width=2,
                frames=1,
                duration_seconds=0.0000625,
            ),
        )
        return make_wav(), info, True

    async def fake_asr(*_args, **_kwargs):
        nonlocal asr_calls
        asr_calls += 1
        return benchmark.QUESTION

    class ForbiddenRunner:
        def __init__(self, **_kwargs) -> None:
            raise AssertionError("prepare-only must not start benchmark runner")

    monkeypatch.setattr(
        benchmark,
        "load_validated_settings",
        lambda _path: make_settings_probe(),
    )
    monkeypatch.setattr(benchmark, "ensure_question_audio", fake_audio)
    monkeypatch.setattr(benchmark, "verify_asr_and_cache", fake_asr)
    monkeypatch.setattr(benchmark, "BenchmarkRunner", ForbiddenRunner)

    exit_code = benchmark.main(
        [
            "--standard-fixtures",
            "--prepare-audio-only",
            "--confirm-live-requests",
        ]
    )

    assert exit_code == 0
    assert prepared_questions == [
        question for _path, question in benchmark.STANDARD_AUDIO_QUESTIONS
    ]
    assert asr_calls == 5
    assert "全部通过 ASR 与 FAQ 命中校验" in capsys.readouterr().out


def test_reliability_completion_depends_on_attempt_count_not_success() -> None:
    scenario = SCENARIOS["xzkb_online_tts"]
    records = [
        BenchmarkSample(
            scenario=scenario.name,
            run_number=run_number,
            warmup=False,
            status_code=503,
            failure_stage="asr",
            error_type="HTTP503",
            invalid_reason="turn_http_status",
        )
        for run_number in range(1, 4)
    ]
    stats = {
        scenario.name: ScenarioRunStats(
            scenario.name,
            planned_formal_requests=3,
            formal_attempts=3,
        )
    }

    assert (
        benchmark.benchmark_incomplete(
            records,
            [scenario],
            stats,
            runs=3,
            benchmark_mode="reliability",
        )
        is False
    )
    assert (
        benchmark.benchmark_incomplete(
            records,
            [scenario],
            stats,
            runs=3,
            benchmark_mode="latency",
        )
        is True
    )


def test_cli_returns_nonzero_when_formal_valid_samples_are_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = make_settings_probe()

    async def fake_audio(*_args, **_kwargs):
        info = AudioInfo(
            file_name="benchmark-fixtures/question.wav",
            sha256="0" * 64,
            metadata=WavMetadata(
                sample_rate=16000,
                channels=1,
                sample_width=2,
                frames=1,
                duration_seconds=0.0000625,
            ),
        )
        return make_wav(), info, False

    async def fake_asr(*_args, **_kwargs):
        return benchmark.QUESTION

    class FakeRunner:
        def __init__(self, **_kwargs) -> None:
            self.run_stats = {
                "xzkb_online_tts": ScenarioRunStats(
                    "xzkb_online_tts", circuit_breaker_triggered=True
                )
            }

        async def run(self, *_args, **_kwargs):
            return [
                BenchmarkSample(
                    scenario="xzkb_online_tts",
                    run_number=1,
                    warmup=False,
                    valid=False,
                    invalid_reason="turn_request_failed",
                    error_type="ReadTimeout",
                )
            ]

    monkeypatch.setattr(benchmark, "load_validated_settings", lambda _path: settings)
    monkeypatch.setattr(benchmark, "ensure_question_audio", fake_audio)
    monkeypatch.setattr(benchmark, "verify_asr_and_cache", fake_asr)
    monkeypatch.setattr(benchmark, "BenchmarkRunner", FakeRunner)
    monkeypatch.setattr(
        benchmark,
        "write_outputs",
        lambda *_args, **_kwargs: (
            tmp_path / "raw.csv",
            tmp_path / "raw.json",
            tmp_path / "report.md",
        ),
    )

    exit_code = benchmark.main(
        [
            "--audio",
            str(tmp_path / "question.wav"),
            "--runs",
            "1",
            "--warmup",
            "0",
            "--scenarios",
            "xzkb_online_tts",
            "--output-dir",
            str(tmp_path / "results"),
            "--interval-seconds",
            "0",
            "--scenario-cooldown-seconds",
            "0",
            "--confirm-live-requests",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code != 0
    assert "没有成功语音样本" in captured.err


def test_cli_returns_nonzero_when_formal_samples_are_only_degraded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = make_settings_probe()

    async def fake_audio(*_args, **_kwargs):
        info = AudioInfo(
            file_name="benchmark-fixtures/question.wav",
            sha256="0" * 64,
            metadata=WavMetadata(
                sample_rate=16000,
                channels=1,
                sample_width=2,
                frames=1,
                duration_seconds=0.0000625,
            ),
        )
        return make_wav(), info, False

    async def fake_asr(*_args, **_kwargs):
        return benchmark.QUESTION

    class FakeRunner:
        def __init__(self, **_kwargs) -> None:
            self.run_stats = {
                "prepared_audio": ScenarioRunStats(
                    "prepared_audio",
                    planned_formal_requests=1,
                    sample_attempts=1,
                    turn_requests_sent=1,
                    turn_responses_received=1,
                    formal_attempts=1,
                )
            }

        async def run(self, *_args, **_kwargs):
            return [
                BenchmarkSample(
                    scenario="prepared_audio",
                    run_number=1,
                    warmup=False,
                    status_code=200,
                    metrics_status_code=200,
                    playback_status_code=204,
                    outcome="degraded",
                    served_from="prepared_audio",
                    valid=True,
                    turn_request_sent=True,
                    turn_response_received=True,
                    client_roundtrip_ms=10,
                    server_pipeline_total_ms=8,
                )
            ]

    monkeypatch.setattr(benchmark, "load_validated_settings", lambda _path: settings)
    monkeypatch.setattr(benchmark, "ensure_question_audio", fake_audio)
    monkeypatch.setattr(benchmark, "verify_asr_and_cache", fake_asr)
    monkeypatch.setattr(benchmark, "BenchmarkRunner", FakeRunner)

    exit_code = benchmark.main(
        [
            "--audio",
            str(tmp_path / "question.wav"),
            "--runs",
            "1",
            "--warmup",
            "0",
            "--scenarios",
            "prepared_audio",
            "--output-dir",
            str(tmp_path / "results"),
            "--interval-seconds",
            "0",
            "--scenario-cooldown-seconds",
            "0",
            "--confirm-live-requests",
        ]
    )

    captured = capsys.readouterr()
    report = (tmp_path / "results" / "report.md").read_text(encoding="utf-8")
    assert exit_code == 1
    assert "成功语音样本" in captured.err
    assert "| `prepared_audio` | 1 | 0 | 0.00% | 0 | 1 | 0 | 0 |" in report
    assert "| `prepared_audio` | `client_roundtrip_ms` | N/A | N/A |" in report
