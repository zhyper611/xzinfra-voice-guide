import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Protocol

from gpiozero import AngularServo

from showroom_guide.verdict import Verdict


logger = logging.getLogger(__name__)


class ServoDriver(Protocol):
    def set_angle(self, angle: float) -> None: ...

    def disable(self) -> None: ...

    def close(self) -> None: ...


class GpioZeroServoDriver:
    def __init__(
        self,
        *,
        pin: int,
        min_angle: float,
        max_angle: float,
        min_pulse_width: float,
        max_pulse_width: float,
        factory: Callable[..., AngularServo] = AngularServo,
    ) -> None:
        self._min_angle = min_angle
        self._max_angle = max_angle
        self._servo = factory(
            pin,
            min_angle=min_angle,
            max_angle=max_angle,
            min_pulse_width=min_pulse_width,
            max_pulse_width=max_pulse_width,
            initial_angle=None,
        )

    def set_angle(self, angle: float) -> None:
        self._servo.angle = min(self._max_angle, max(self._min_angle, angle))

    def disable(self) -> None:
        self._servo.detach()

    def close(self) -> None:
        self._servo.close()


class ServoMotionOutput:
    def __init__(
        self,
        driver: ServoDriver,
        *,
        yes_angle: float = 20,
        neutral_angle: float = 75,
        no_angle: float = 130,
        thinking_offset: float = 20,
        thinking_step_seconds: float = 0.7,
        settle_seconds: float = 0.25,
        neutral_step_seconds: float = 0.2,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._driver = driver
        self._angles = {
            Verdict.YES: yes_angle,
            Verdict.NEUTRAL: neutral_angle,
            Verdict.NO: no_angle,
        }
        self._thinking_angles = (
            neutral_angle - thinking_offset,
            neutral_angle + thinking_offset,
        )
        self._neutral_angles = (
            neutral_angle - thinking_offset * 0.75,
            neutral_angle + thinking_offset * 0.75,
            neutral_angle,
        )
        self._thinking_step_seconds = thinking_step_seconds
        self._settle_seconds = settle_seconds
        self._neutral_step_seconds = neutral_step_seconds
        self._sleep = sleep
        self._generation = 0
        self._thinking_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._pwm_active = False
        self._available = True
        self._closed = False

    @property
    def available(self) -> bool:
        return self._available

    async def enter_mode(self) -> None:
        async with self._lock:
            await self._cancel_thinking(disable=False)
            await self._position_and_release(self._angles[Verdict.NEUTRAL])

    async def prepare_recording(self) -> None:
        async with self._lock:
            await self._cancel_thinking(disable=True)

    async def thinking(self, generation: int) -> None:
        async with self._lock:
            if not self._can_move:
                return
            await self._cancel_thinking(disable=False)
            self._generation = generation
            if not self._set_angle_safely(self._thinking_angles[0]):
                return
            self._thinking_task = asyncio.create_task(
                self._thinking_loop(generation),
                name=f"servo-thinking-{generation}",
            )

    async def show_verdict(
        self,
        verdict: Verdict,
        generation: int,
    ) -> None:
        if verdict is Verdict.NEUTRAL:
            raise ValueError("show_verdict requires yes or no")
        async with self._lock:
            if not self._can_move or generation != self._generation:
                return
            await self._cancel_thinking(disable=False)
            opposite = Verdict.NO if verdict is Verdict.YES else Verdict.YES
            if not self._set_angle_safely(self._angles[opposite]):
                return
            if not self._set_angle_safely(self._angles[verdict]):
                return
            await self._sleep(self._settle_seconds)
            self._disable_safely()

    async def show_neutral(self, generation: int) -> None:
        async with self._lock:
            if not self._can_move or generation != self._generation:
                return
            await self._cancel_thinking(disable=False)
            for index, angle in enumerate(self._neutral_angles):
                if not self._set_angle_safely(angle):
                    return
                if index < len(self._neutral_angles) - 1:
                    await self._sleep(self._neutral_step_seconds)
            await self._sleep(self._settle_seconds)
            self._disable_safely()

    async def reset(self) -> None:
        async with self._lock:
            self._generation += 1
            await self._cancel_thinking(disable=False)
            await self._position_and_release(self._angles[Verdict.NEUTRAL])

    async def leave_mode(self) -> None:
        await self.reset()

    async def aclose(self) -> None:
        if self._closed:
            return
        async with self._lock:
            await self._cancel_thinking(disable=False)
            if self._available:
                await self._position_and_release(self._angles[Verdict.NEUTRAL])
            self._closed = True
            self._disable_safely()
            try:
                self._driver.close()
            except Exception:
                logger.exception("servo_driver_close_failed")

    @property
    def _can_move(self) -> bool:
        return not self._closed and self._available

    async def _thinking_loop(self, generation: int) -> None:
        index = 1
        try:
            while self._can_move and generation == self._generation:
                await self._sleep(self._thinking_step_seconds)
                if not self._set_angle_safely(self._thinking_angles[index]):
                    return
                index = 1 - index
        except asyncio.CancelledError:
            raise

    async def _position_and_release(self, angle: float) -> None:
        if not self._can_move or not self._set_angle_safely(angle):
            return
        await self._sleep(self._settle_seconds)
        self._disable_safely()

    def _set_angle_safely(self, angle: float) -> bool:
        try:
            self._driver.set_angle(angle)
            self._pwm_active = True
            return True
        except Exception:
            logger.exception("servo_motion_failed")
            self._available = False
            self._disable_safely()
            return False

    async def _cancel_thinking(self, *, disable: bool) -> None:
        task = self._thinking_task
        self._thinking_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if disable and self._pwm_active:
            self._disable_safely()

    def _disable_safely(self) -> None:
        if not self._pwm_active:
            return
        try:
            self._driver.disable()
        except Exception:
            logger.exception("servo_pwm_disable_failed")
        finally:
            self._pwm_active = False
