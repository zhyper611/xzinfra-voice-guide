import pytest

from showroom_guide.models import InteractionMode, VerdictPhase
from showroom_guide.state import GuideStateStore
from showroom_guide.verdict import Verdict
from showroom_guide.verdict_motion import WebSimulationOutput


@pytest.mark.asyncio
async def test_web_output_publishes_actions_without_hardware():
    state = GuideStateStore()
    await state.set_interaction_mode(InteractionMode.VERDICT)
    output = WebSimulationOutput(state)

    await output.thinking(generation=3)
    assert state.snapshot.verdict_phase is VerdictPhase.THINKING
    assert state.snapshot.verdict_generation == 3

    await output.show_verdict(Verdict.YES, generation=3)
    assert state.snapshot.verdict_phase is VerdictPhase.HOLDING
    assert state.snapshot.verdict is Verdict.YES


@pytest.mark.asyncio
async def test_web_output_ignores_stale_generation():
    state = GuideStateStore()
    output = WebSimulationOutput(state)
    await output.thinking(generation=5)

    await output.show_verdict(Verdict.NO, generation=4)

    assert state.snapshot.verdict_phase is VerdictPhase.THINKING
    assert state.snapshot.verdict is Verdict.NEUTRAL


@pytest.mark.asyncio
async def test_web_reset_returns_to_idle_neutral():
    state = GuideStateStore()
    output = WebSimulationOutput(state)
    await output.thinking(generation=1)
    await output.show_verdict(Verdict.NO, generation=1)

    await output.reset()

    assert state.snapshot.verdict_phase is VerdictPhase.IDLE
    assert state.snapshot.verdict is Verdict.NEUTRAL
