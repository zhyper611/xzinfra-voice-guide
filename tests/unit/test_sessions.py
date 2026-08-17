from unittest.mock import AsyncMock

import pytest

from showroom_guide.sessions import SessionCapacityReached, SessionManager


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FakeController:
    def __init__(self, state) -> None:
        self.state = state
        self.is_busy = False
        self.reset = AsyncMock()


def make_manager(clock, max_sessions=100, idle_seconds=30):
    return SessionManager(
        controller_factory=FakeController,
        max_sessions=max_sessions,
        idle_seconds=idle_seconds,
        audio_ttl_seconds=60,
        audio_items_per_session=3,
        clock=clock,
    )


@pytest.mark.asyncio
async def test_missing_id_creates_random_session_and_known_id_is_reused():
    clock = Clock()
    manager = make_manager(clock)

    first, created = await manager.get_or_create(None)
    same, created_again = await manager.get_or_create(first.session_id)

    assert created is True
    assert created_again is False
    assert same is first
    assert len(first.session_id) >= 32


@pytest.mark.asyncio
async def test_connected_or_busy_sessions_are_not_pruned():
    clock = Clock()
    manager = make_manager(clock, idle_seconds=10)
    connected, _ = await manager.get_or_create(None)
    busy, _ = await manager.get_or_create(None)
    idle, _ = await manager.get_or_create(None)
    await manager.connect(connected)
    busy.controller.is_busy = True
    clock.now = 11

    removed = await manager.prune()

    assert idle.session_id in removed
    assert manager.get(connected.session_id) is connected
    assert manager.get(busy.session_id) is busy


@pytest.mark.asyncio
async def test_capacity_evicts_oldest_safe_idle_session():
    clock = Clock()
    manager = make_manager(clock, max_sessions=2)
    oldest, _ = await manager.get_or_create(None)
    clock.now = 1
    newest, _ = await manager.get_or_create(None)
    clock.now = 2

    replacement, created = await manager.get_or_create(None)

    assert created is True
    assert manager.get(oldest.session_id) is None
    assert manager.get(newest.session_id) is newest
    assert manager.get(replacement.session_id) is replacement


@pytest.mark.asyncio
async def test_capacity_rejects_when_every_session_is_protected():
    clock = Clock()
    manager = make_manager(clock, max_sessions=1)
    protected, _ = await manager.get_or_create(None)
    await manager.connect(protected)

    with pytest.raises(SessionCapacityReached):
        await manager.get_or_create(None)


@pytest.mark.asyncio
async def test_reset_clears_only_selected_session():
    clock = Clock()
    manager = make_manager(clock)
    first, _ = await manager.get_or_create(None)
    second, _ = await manager.get_or_create(None)
    first.audio.put(b"first")
    second_audio = second.audio.put(b"second")

    await manager.reset(first)

    first.controller.reset.assert_awaited_once()
    assert second.audio.get(second_audio) == b"second"
