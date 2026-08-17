import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass


class AudioNotFound(KeyError):
    pass


@dataclass(frozen=True)
class AudioEntry:
    content: bytes
    created_at: float


class AudioStore:
    def __init__(
        self,
        max_items: int = 3,
        ttl_seconds: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_items = max_items
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._items: OrderedDict[str, AudioEntry] = OrderedDict()

    def put(self, audio: bytes) -> str:
        self.prune()
        audio_id = secrets.token_urlsafe(18)
        self._items[audio_id] = AudioEntry(audio, self._clock())
        while len(self._items) > self._max_items:
            self._items.popitem(last=False)
        return audio_id

    def get(self, audio_id: str) -> bytes:
        self.prune()
        entry = self._items.get(audio_id)
        if entry is None:
            raise AudioNotFound(audio_id)
        return entry.content

    def prune(self) -> None:
        cutoff = self._clock() - self._ttl_seconds
        expired = [
            audio_id
            for audio_id, entry in self._items.items()
            if entry.created_at <= cutoff
        ]
        for audio_id in expired:
            self._items.pop(audio_id, None)

    def clear(self) -> None:
        self._items.clear()
