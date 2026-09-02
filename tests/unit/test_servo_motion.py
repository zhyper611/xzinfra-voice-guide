import asyncio

import pytest

from showroom_guide.verdict import Verdict


class FakeAngularServo:
    def __init__(self, pin, **kwargs):
        self.angle = None
        self.detached = False
        self.closed = False

    def detach(self):
        self.detached = True

    def close(self):
        self.closed = True


def test_gpio_driver_clamps_angle_and_releases_pwm():
    from showroom_guide.servo_motion import GpioZeroServoDriver

    created = []
    driver = GpioZeroServoDriver(
        pin=18,
        min_angle=10,
        max_angle=140,
        min_pulse_width=0.0005,
        max_pulse_width=0.0025,
        factory=lambda pin, **kwargs: created.append(
            FakeAngularServo(pin, **kwargs)
        )
        or created[-1],
    )
    driver.set_angle(200)
    assert created[0].angle == 140
    driver.set_angle(-20)
    assert created[0].angle == 10
    driver.disable()
    driver.close()
    assert created[0].detached and created[0].closed


class RecordingDriver:
    def __init__(self, fail=False):
        self.events = []
        self.fail = fail

    def set_angle(self, angle):
        if self.fail:
            raise OSError("unavailable")
        self.events.append(("angle", angle))

    def disable(self):
        self.events.append(("disable", None))

    def close(self):
        self.events.append(("close", None))


async def instant_sleep(_):
    await asyncio.sleep(0)


async def wait_for(predicate):
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not reached")


@pytest.mark.asyncio
async def test_enter_mode_centers_then_disables_servo():
    from showroom_guide.servo_motion import ServoMotionOutput

    driver = RecordingDriver()
    output = ServoMotionOutput(driver, sleep=instant_sleep)

    await output.enter_mode()

    assert driver.events == [("angle", 75), ("disable", None)]
    await output.aclose()


@pytest.mark.asyncio
async def test_yes_moves_to_no_side_then_immediately_to_yes_and_holds():
    from showroom_guide.servo_motion import ServoMotionOutput

    driver = RecordingDriver()
    output = ServoMotionOutput(driver, sleep=instant_sleep)
    await output.thinking(generation=1)
    await wait_for(lambda: len(driver.events) >= 2)
    driver.events.clear()

    await output.show_verdict(Verdict.YES, generation=1)

    assert driver.events == [
        ("angle", 130),
        ("angle", 20),
        ("disable", None),
    ]
    await output.aclose()


@pytest.mark.asyncio
async def test_no_moves_to_yes_side_then_immediately_to_no_and_holds():
    from showroom_guide.servo_motion import ServoMotionOutput

    driver = RecordingDriver()
    output = ServoMotionOutput(driver, sleep=instant_sleep)
    await output.thinking(generation=2)
    await wait_for(lambda: len(driver.events) >= 2)
    driver.events.clear()

    await output.show_verdict(Verdict.NO, generation=2)

    assert driver.events == [
        ("angle", 20),
        ("angle", 130),
        ("disable", None),
    ]
    await output.aclose()


@pytest.mark.asyncio
async def test_recording_keeps_previous_result_without_motion():
    from showroom_guide.servo_motion import ServoMotionOutput

    driver = RecordingDriver()
    output = ServoMotionOutput(driver, sleep=instant_sleep)
    await output.thinking(generation=1)
    await output.show_verdict(Verdict.NO, generation=1)
    before = list(driver.events)

    await output.prepare_recording()

    assert driver.events == before
    await output.aclose()


@pytest.mark.asyncio
async def test_neutral_hesitates_then_centers():
    from showroom_guide.servo_motion import ServoMotionOutput

    driver = RecordingDriver()
    output = ServoMotionOutput(driver, sleep=instant_sleep)
    await output.thinking(generation=1)
    driver.events.clear()

    await output.show_neutral(generation=1)

    assert driver.events == [
        ("angle", 60),
        ("angle", 90),
        ("angle", 75),
        ("disable", None),
    ]
    await output.aclose()


@pytest.mark.asyncio
async def test_stale_result_does_not_interrupt_current_thinking():
    from showroom_guide.servo_motion import ServoMotionOutput

    driver = RecordingDriver()
    output = ServoMotionOutput(driver, sleep=instant_sleep)
    await output.thinking(generation=4)
    await wait_for(lambda: len(driver.events) >= 2)
    before = list(driver.events)

    await output.show_verdict(Verdict.YES, generation=3)

    assert driver.events[: len(before)] == before
    await output.aclose()


@pytest.mark.asyncio
async def test_driver_failure_fuses_without_escaping():
    from showroom_guide.servo_motion import ServoMotionOutput

    output = ServoMotionOutput(RecordingDriver(fail=True), sleep=instant_sleep)

    await output.enter_mode()
    await output.thinking(generation=1)

    assert output.available is False
    await output.aclose()
