import asyncio
import logging
from contextlib import suppress
from typing import Callable

from gpiozero import Button


logger = logging.getLogger(__name__)


class GpioButtonService:
    def __init__(
        self,
        *,
        pin: int,
        hold_seconds: float,
        workflow,
        button_factory: Callable[..., object] = Button,
    ) -> None:
        self._pin = pin
        self._hold_seconds = hold_seconds
        self._workflow = workflow
        self._button_factory = button_factory
        self._button = None
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._held = False

    @property
    def button(self):
        return self._button

    def start(self) -> None:
        if self._button is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._button = self._button_factory(
            self._pin,
            pull_up=True,
            bounce_time=0.05,
            hold_time=self._hold_seconds,
            hold_repeat=False,
        )
        self._button.when_held = self._on_held
        self._button.when_released = self._on_released
        self._worker = asyncio.create_task(
            self._run(),
            name="gpio-button-events",
        )

    async def aclose(self) -> None:
        button = self._button
        self._button = None
        if button is not None:
            button.when_held = None
            button.when_released = None
            button.close()
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker

    def _on_held(self) -> None:
        self._held = True
        self._dispatch("long")

    def _on_released(self) -> None:
        if self._held:
            self._held = False
            return
        self._dispatch("short")

    def _dispatch(self, event: str) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, event)

    async def _run(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                if event == "long":
                    await self._workflow.long_press()
                else:
                    await self._workflow.short_press()
            except Exception:
                logger.exception("gpio_button_operation_failed", extra={"event": event})
