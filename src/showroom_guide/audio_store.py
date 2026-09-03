import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass


class AudioNotFound(KeyError):
    pass


class AudioTooLarge(ValueError):
    pass


class AudioByteBudget:
    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max_bytes
        self._total_bytes = 0
        self._items: OrderedDict[
            tuple[object, str],
            tuple[int, Callable[[str], None]],
        ] = OrderedDict()

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    def register(
        self,
        owner: object,
        audio_id: str,
        size: int,
        evict: Callable[[str], None],
    ) -> None:
        key = (owner, audio_id)
        self._items[key] = (size, evict)
        self._total_bytes += size
        while self._total_bytes > self._max_bytes and self._items:
            (_owner, expired_id), (expired_size, callback) = (
                self._items.popitem(last=False)
            )
            self._total_bytes -= expired_size
            callback(expired_id)

    def unregister(self, owner: object, audio_id: str) -> None:
        entry = self._items.pop((owner, audio_id), None)
        if entry is not None:
            self._total_bytes -= entry[0]


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
        max_item_bytes: int = 8 * 1024 * 1024,
        budget: AudioByteBudget | None = None,
    ) -> None:
        self._max_items = max_items
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._max_item_bytes = max_item_bytes
        self._budget = budget
        self._budget_owner = object()
        self._items: OrderedDict[str, AudioEntry] = OrderedDict()

    def put(self, audio: bytes) -> str:
        if len(audio) > self._max_item_bytes:
            raise AudioTooLarge("生成的语音文件过大")
        self.prune()
        audio_id = secrets.token_urlsafe(18)
        self._items[audio_id] = AudioEntry(audio, self._clock())
        if self._budget is not None:
            self._budget.register(
                self._budget_owner,
                audio_id,
                len(audio),
                self._evict_from_budget,
            )
        while len(self._items) > self._max_items:
            oldest_id = next(iter(self._items))
            self._remove(oldest_id)
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
            self._remove(audio_id)

    def clear(self) -> None:
        for audio_id in tuple(self._items):
            self._remove(audio_id)

    def _remove(self, audio_id: str) -> None:
        if self._items.pop(audio_id, None) is not None and self._budget is not None:
            self._budget.unregister(self._budget_owner, audio_id)

    def _evict_from_budget(self, audio_id: str) -> None:
        self._items.pop(audio_id, None)
