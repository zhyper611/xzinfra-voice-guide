import asyncio
from enum import StrEnum

from showroom_guide.knowledge_mode import (
    KnowledgeLongPressResult,
    KnowledgeModeState,
)


class ButtonInteractionMode(StrEnum):
    DIALOGUE = "dialogue"
    KNOWLEDGE = "knowledge"


class DeviceButtonWorkflow:
    def __init__(self, local_device, knowledge_workflow) -> None:
        self._local_device = local_device
        self._knowledge = knowledge_workflow
        self._mode = ButtonInteractionMode.DIALOGUE
        self._lock = asyncio.Lock()
        self._knowledge_owner = None

    @property
    def mode(self) -> ButtonInteractionMode:
        return self._mode

    async def short_press(self) -> None:
        async with self._lock:
            if self._knowledge_owner is not None:
                return
            if self._mode is ButtonInteractionMode.KNOWLEDGE:
                await self._knowledge.short_press()
            elif self._local_device.is_recording:
                await self._local_device.stop_recording()
            else:
                await self._local_device.start_recording()

    async def long_press(self) -> KnowledgeLongPressResult | None:
        async with self._lock:
            if self._knowledge_owner is not None:
                return None
            try:
                if self._mode is ButtonInteractionMode.DIALOGUE:
                    if not self._local_device.is_idle:
                        return None
                    if self._knowledge is None:
                        return None
                    await self._knowledge.enter()
                    self._mode = ButtonInteractionMode.KNOWLEDGE
                    return None
                result = await self._knowledge.long_press()
                if result.exited:
                    self._mode = ButtonInteractionMode.DIALOGUE
                return result
            except asyncio.CancelledError:
                try:
                    await self._knowledge.cancel()
                finally:
                    self._mode = ButtonInteractionMode.DIALOGUE
                raise

    async def acquire_knowledge_control(self, owner: object) -> bool:
        async with self._lock:
            if (
                self._knowledge_owner is not None
                or self._mode is not ButtonInteractionMode.DIALOGUE
                or not self._local_device.is_idle
                or self._knowledge is None
                or self._knowledge.state is not KnowledgeModeState.INACTIVE
            ):
                return False
            try:
                await self._knowledge.enter()
            except BaseException:
                try:
                    await self._knowledge.cancel()
                finally:
                    if self._knowledge.state is KnowledgeModeState.INACTIVE:
                        self._mode = ButtonInteractionMode.DIALOGUE
                        self._knowledge_owner = None
                    else:
                        self._mode = ButtonInteractionMode.KNOWLEDGE
                        self._knowledge_owner = owner
                raise
            if self._knowledge.state is KnowledgeModeState.INACTIVE:
                return False
            self._mode = ButtonInteractionMode.KNOWLEDGE
            self._knowledge_owner = owner
            return True

    async def knowledge_short_press(self, owner: object) -> None:
        async with self._lock:
            self._require_knowledge_owner(owner)
            await self._knowledge.short_press()

    async def knowledge_long_press(
        self,
        owner: object,
    ) -> KnowledgeLongPressResult:
        async with self._lock:
            self._require_knowledge_owner(owner)
            try:
                result = await self._knowledge.long_press()
            except BaseException:
                if self._knowledge.state is KnowledgeModeState.INACTIVE:
                    self._mode = ButtonInteractionMode.DIALOGUE
                    self._knowledge_owner = None
                raise
            if result.exited:
                self._mode = ButtonInteractionMode.DIALOGUE
                self._knowledge_owner = None
            return result

    async def release_knowledge_control(self, owner: object) -> bool:
        async with self._lock:
            if self._knowledge_owner is not owner:
                return False
            try:
                await self._knowledge.cancel()
            except BaseException:
                if self._knowledge.state is KnowledgeModeState.INACTIVE:
                    self._mode = ButtonInteractionMode.DIALOGUE
                    self._knowledge_owner = None
                raise
            if self._knowledge.state is not KnowledgeModeState.INACTIVE:
                return False
            self._mode = ButtonInteractionMode.DIALOGUE
            self._knowledge_owner = None
            return True

    async def cancel_knowledge(self) -> None:
        async with self._lock:
            if (
                self._mode is not ButtonInteractionMode.KNOWLEDGE
                or self._knowledge_owner is not None
            ):
                return
            try:
                await self._knowledge.cancel()
            finally:
                self._mode = ButtonInteractionMode.DIALOGUE

    async def run_dialogue(self, operation):
        async with self._lock:
            if self._mode is not ButtonInteractionMode.DIALOGUE:
                raise RuntimeError("设备当前处于知识补充模式")
            return await operation()

    def _require_knowledge_owner(self, owner: object) -> None:
        if self._knowledge_owner is not owner:
            raise RuntimeError("知识补充控制权不匹配")
