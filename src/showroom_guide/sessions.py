import asyncio
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

from showroom_guide.audio_store import AudioStore
from showroom_guide.controller import GuideController
from showroom_guide.state import GuideStateStore


class SessionCapacityReached(RuntimeError):
    pass


@dataclass
class GuideSession:
    session_id: str
    state: GuideStateStore
    controller: GuideController
    audio: AudioStore
    created_at: float
    last_active_at: float
    connected_clients: int = 0

    @property
    def protected(self) -> bool:
        return self.connected_clients > 0 or self.controller.is_busy


class SessionManager:
    def __init__(
        self,
        controller_factory: Callable[[GuideStateStore], GuideController],
        max_sessions: int,
        idle_seconds: float,
        audio_ttl_seconds: float,
        audio_items_per_session: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._controller_factory = controller_factory
        self._max_sessions = max_sessions
        self._idle_seconds = idle_seconds
        self._audio_ttl_seconds = audio_ttl_seconds
        self._audio_items_per_session = audio_items_per_session
        self._clock = clock
        self._sessions: dict[str, GuideSession] = {}
        self._lock = asyncio.Lock()

    def get(self, session_id: str | None) -> GuideSession | None:
        if session_id is None:
            return None
        return self._sessions.get(session_id)

    async def get_or_create(
        self,
        session_id: str | None,
    ) -> tuple[GuideSession, bool]:
        async with self._lock:
            now = self._clock()
            existing = self.get(session_id)
            if existing is not None:
                existing.last_active_at = now
                return existing, False
            if len(self._sessions) >= self._max_sessions:
                candidates = [
                    item for item in self._sessions.values() if not item.protected
                ]
                if not candidates:
                    raise SessionCapacityReached
                oldest = min(candidates, key=lambda item: item.last_active_at)
                oldest.audio.clear()
                self._sessions.pop(oldest.session_id, None)
            state = GuideStateStore()
            new_id = secrets.token_urlsafe(32)
            session = GuideSession(
                session_id=new_id,
                state=state,
                controller=self._controller_factory(state),
                audio=AudioStore(
                    max_items=self._audio_items_per_session,
                    ttl_seconds=self._audio_ttl_seconds,
                    clock=self._clock,
                ),
                created_at=now,
                last_active_at=now,
            )
            self._sessions[new_id] = session
            return session, True

    def touch(self, session: GuideSession) -> None:
        session.last_active_at = self._clock()

    async def connect(self, session: GuideSession) -> None:
        async with self._lock:
            session.connected_clients += 1
            self.touch(session)

    async def disconnect(self, session: GuideSession) -> None:
        async with self._lock:
            session.connected_clients = max(0, session.connected_clients - 1)
            self.touch(session)

    async def reset(self, session: GuideSession) -> None:
        await session.controller.reset()
        session.audio.clear()
        self.touch(session)

    async def prune(self) -> list[str]:
        async with self._lock:
            cutoff = self._clock() - self._idle_seconds
            removed = [
                item.session_id
                for item in self._sessions.values()
                if not item.protected and item.last_active_at <= cutoff
            ]
            for session_id in removed:
                session = self._sessions.pop(session_id)
                session.audio.clear()
            for session in self._sessions.values():
                session.audio.prune()
            return removed

    async def clear(self) -> None:
        async with self._lock:
            for session in self._sessions.values():
                session.audio.clear()
            self._sessions.clear()
