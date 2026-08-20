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
