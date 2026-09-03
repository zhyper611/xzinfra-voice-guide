import asyncio

import pytest

from showroom_guide.gpio_button import GpioButtonService


class FakeButton:
    def __init__(self, pin, **kwargs):
        self.pin = pin
        self.kwargs = kwargs
        self.when_held = None
        self.when_released = None
        self.closed = False

    def close(self):
        self.closed = True


class FakeWorkflow:
    def __init__(self):
        self.events = []
        self.ready = asyncio.Event()

    async def short_press(self):
        self.events.append("short")
        self.ready.set()

    async def long_press(self):
        self.events.append("long")
        self.ready.set()


class BlockingWorkflow(FakeWorkflow):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def short_press(self):
        self.events.append("short")
        self.started.set()
        await self.release.wait()


@pytest.mark.asyncio
async def test_release_dispatches_short_press_and_held_release_is_not_duplicated():
    workflow = FakeWorkflow()
    service = GpioButtonService(
        pin=17,
        hold_seconds=1.2,
        workflow=workflow,
        button_factory=FakeButton,
    )
    service.start()

    service.button.when_released()
    await asyncio.wait_for(workflow.ready.wait(), timeout=0.2)
    assert workflow.events == ["short"]

    workflow.ready.clear()
    service.button.when_held()
    service.button.when_released()
    await asyncio.wait_for(workflow.ready.wait(), timeout=0.2)
    await asyncio.sleep(0)
    assert workflow.events == ["short", "long"]

    button = service.button
    await service.aclose()
    assert button.closed is True


@pytest.mark.asyncio
async def test_busy_button_keeps_at_most_one_pending_event():
    workflow = BlockingWorkflow()
    service = GpioButtonService(
        pin=17,
        hold_seconds=1.2,
        workflow=workflow,
        button_factory=FakeButton,
    )
    service.start()
    try:
        service._dispatch("short")
        await asyncio.wait_for(workflow.started.wait(), timeout=0.2)
        service._dispatch("short")
        service._dispatch("short")
        service._dispatch("short")
        await asyncio.sleep(0)

        assert service._queue.qsize() == 1
        workflow.release.set()
        for _ in range(10):
            if len(workflow.events) == 2:
                break
            await asyncio.sleep(0)
        assert workflow.events == ["short", "short"]
    finally:
        workflow.release.set()
        await service.aclose()


@pytest.mark.asyncio
async def test_partial_start_failure_closes_button_and_resets_service():
    created = []

    class FailingCallbackButton(FakeButton):
        def __setattr__(self, name, value):
            if name == "when_held" and value is not None:
                raise OSError("callback unavailable")
            super().__setattr__(name, value)

    def factory(pin, **kwargs):
        button = FailingCallbackButton(pin, **kwargs)
        created.append(button)
        return button

    service = GpioButtonService(
        pin=17,
        hold_seconds=1.2,
        workflow=FakeWorkflow(),
        button_factory=factory,
    )

    with pytest.raises(OSError, match="callback unavailable"):
        service.start()

    assert service.button is None
    assert created[0].closed is True
    await service.aclose()
