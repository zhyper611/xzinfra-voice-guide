import pytest

from showroom_guide.audio_store import (
    AudioByteBudget,
    AudioNotFound,
    AudioStore,
    AudioTooLarge,
)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_put_keeps_multiple_recent_audio_items():
    store = AudioStore(max_items=3, ttl_seconds=60)

    first_id = store.put(b"first")
    second_id = store.put(b"second")

    assert store.get(first_id) == b"first"
    assert store.get(second_id) == b"second"


def test_put_evicts_oldest_item_when_capacity_is_reached():
    store = AudioStore(max_items=2, ttl_seconds=60)
    first_id = store.put(b"first")
    second_id = store.put(b"second")
    third_id = store.put(b"third")

    with pytest.raises(AudioNotFound):
        store.get(first_id)
    assert store.get(second_id) == b"second"
    assert store.get(third_id) == b"third"


def test_expired_audio_is_not_returned():
    clock = Clock()
    store = AudioStore(max_items=3, ttl_seconds=10, clock=clock)
    audio_id = store.put(b"audio")
    clock.now = 11

    with pytest.raises(AudioNotFound):
        store.get(audio_id)


def test_clear_removes_every_audio_item():
    store = AudioStore(max_items=3, ttl_seconds=60)
    first_id = store.put(b"first")
    second_id = store.put(b"second")

    store.clear()

    for audio_id in (first_id, second_id):
        with pytest.raises(AudioNotFound):
            store.get(audio_id)


def test_put_rejects_single_audio_larger_than_limit():
    store = AudioStore(max_item_bytes=4)

    with pytest.raises(AudioTooLarge, match="过大"):
        store.put(b"12345")


def test_shared_budget_evicts_oldest_audio_across_stores():
    budget = AudioByteBudget(max_bytes=8)
    first_store = AudioStore(budget=budget)
    second_store = AudioStore(budget=budget)
    first_id = first_store.put(b"first")
    second_id = second_store.put(b"two")

    third_id = second_store.put(b"last")

    with pytest.raises(AudioNotFound):
        first_store.get(first_id)
    assert second_store.get(second_id) == b"two"
    assert second_store.get(third_id) == b"last"
    assert budget.total_bytes == 7


def test_prune_and_clear_release_shared_budget_bytes():
    clock = Clock()
    budget = AudioByteBudget(max_bytes=100)
    store = AudioStore(ttl_seconds=10, clock=clock, budget=budget)
    store.put(b"audio")
    assert budget.total_bytes == 5

    clock.now = 11
    store.prune()
    assert budget.total_bytes == 0

    store.put(b"next")
    store.clear()
    assert budget.total_bytes == 0
