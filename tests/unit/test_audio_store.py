import pytest

from showroom_guide.audio_store import AudioNotFound, AudioStore


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
