import asyncio

from showroom_guide.models import GuidePhase, GuideSnapshot


ALLOWED = {
    GuidePhase.IDLE: {
        GuidePhase.RECORDING,
        GuidePhase.THINKING,
        GuidePhase.DEGRADED,
        GuidePhase.ERROR,
    },
    GuidePhase.RECORDING: {
        GuidePhase.TRANSCRIBING,
        GuidePhase.IDLE,
        GuidePhase.ERROR,
    },
    GuidePhase.TRANSCRIBING: {
        GuidePhase.THINKING,
        GuidePhase.IDLE,
        GuidePhase.ERROR,
    },
    GuidePhase.THINKING: {
        GuidePhase.SPEAKING,
        GuidePhase.IDLE,
        GuidePhase.DEGRADED,
        GuidePhase.ERROR,
    },
    GuidePhase.SPEAKING: {
        GuidePhase.IDLE,
        GuidePhase.RECORDING,
        GuidePhase.THINKING,
        GuidePhase.ERROR,
    },
    GuidePhase.DEGRADED: {
        GuidePhase.IDLE,
        GuidePhase.RECORDING,
        GuidePhase.THINKING,
        GuidePhase.ERROR,
    },
    GuidePhase.ERROR: {
        GuidePhase.IDLE,
        GuidePhase.RECORDING,
        GuidePhase.THINKING,
        GuidePhase.DEGRADED,
    },
}


class InvalidStateTransition(ValueError):
    pass


class GuideStateStore:
    def __init__(self) -> None:
        self._snapshot = GuideSnapshot()
        self._lock = asyncio.Lock()
        self._subscribers: set[asyncio.Queue[GuideSnapshot]] = set()

    @property
    def snapshot(self) -> GuideSnapshot:
        return self._snapshot

    def subscribe(self) -> asyncio.Queue[GuideSnapshot]:
        queue: asyncio.Queue[GuideSnapshot] = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[GuideSnapshot]) -> None:
        self._subscribers.discard(queue)

    async def transition(self, phase: GuidePhase) -> GuideSnapshot:
        async with self._lock:
            current = self._snapshot.phase
            if phase not in ALLOWED[current]:
                raise InvalidStateTransition(f"不能从 {current} 转换到 {phase}")
            return self._update_locked(phase=phase)

    async def start_text_question(self, transcript: str) -> GuideSnapshot:
        async with self._lock:
            current = self._snapshot.phase
            if GuidePhase.THINKING not in ALLOWED[current]:
                raise InvalidStateTransition(
                    f"不能从 {current} 开始文字问答"
                )
            return self._update_locked(
                phase=GuidePhase.THINKING,
                transcript=transcript,
                answer="",
                message="正在查询展项资料",
            )

    async def set_transcript(self, transcript: str) -> GuideSnapshot:
        return await self._update(transcript=transcript)

    async def append_answer(self, text: str) -> GuideSnapshot:
        async with self._lock:
            return self._update_locked(answer=self._snapshot.answer + text)

    async def set_answer(self, answer: str) -> GuideSnapshot:
        return await self._update(answer=answer)

    async def set_message(self, message: str) -> GuideSnapshot:
        return await self._update(message=message)

    async def reset(self) -> GuideSnapshot:
        async with self._lock:
            self._snapshot = GuideSnapshot()
            for queue in tuple(self._subscribers):
                queue.put_nowait(self._snapshot)
            return self._snapshot

    async def _update(self, **changes: object) -> GuideSnapshot:
        async with self._lock:
            return self._update_locked(**changes)

    def _update_locked(self, **changes: object) -> GuideSnapshot:
        self._snapshot = self._snapshot.model_copy(update=changes)
        for queue in tuple(self._subscribers):
            queue.put_nowait(self._snapshot)
        return self._snapshot
