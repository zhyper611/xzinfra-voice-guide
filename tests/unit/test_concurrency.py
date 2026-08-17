import asyncio

import pytest

from showroom_guide.concurrency import AsyncGate, QueueWaitTimeout


@pytest.mark.asyncio
async def test_gate_limits_parallel_work_and_notifies_waiter():
    gate = AsyncGate(limit=1, timeout_seconds=1)
    release = asyncio.Event()
    first_started = asyncio.Event()
    waited = asyncio.Event()

    async def first():
        async with gate.slot():
            first_started.set()
            await release.wait()

    async def second():
        await first_started.wait()
        async with gate.slot(on_wait=waited.set):
            return "done"

    first_task = asyncio.create_task(first())
    second_task = asyncio.create_task(second())
    await asyncio.wait_for(waited.wait(), timeout=0.2)
    assert not second_task.done()

    release.set()
    assert await second_task == "done"
    await first_task


@pytest.mark.asyncio
async def test_gate_times_out_without_leaking_capacity():
    gate = AsyncGate(limit=1, timeout_seconds=0.01)

    async with gate.slot():
        with pytest.raises(QueueWaitTimeout):
            async with gate.slot():
                pass

    async with gate.slot():
        pass
