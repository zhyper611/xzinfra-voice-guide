import asyncio
from enum import StrEnum


class ButtonInteractionMode(StrEnum):
    DIALOGUE = "dialogue"
    KNOWLEDGE = "knowledge"


class DeviceButtonWorkflow:
    def __init__(self, local_device, knowledge_workflow) -> None:
        self._local_device = local_device
        self._knowledge = knowledge_workflow
        self._mode = ButtonInteractionMode.DIALOGUE
        self._lock = asyncio.Lock()

    @property
    def mode(self) -> ButtonInteractionMode:
        return self._mode

    async def short_press(self) -> None:
        async with self._lock:
            if self._mode is ButtonInteractionMode.KNOWLEDGE:
                await self._knowledge.short_press()
            elif self._local_device.is_recording:
                await self._local_device.stop_recording()
            else:
                await self._local_device.start_recording()

    async def long_press(self) -> None:
        async with self._lock:
            if self._mode is ButtonInteractionMode.DIALOGUE:
                if not self._local_device.is_idle:
                    return
                if self._knowledge is None:
                    return
                await self._knowledge.enter()
                self._mode = ButtonInteractionMode.KNOWLEDGE
                return
            should_exit = await self._knowledge.long_press()
            if should_exit:
                self._mode = ButtonInteractionMode.DIALOGUE

    async def run_dialogue(self, operation):
        async with self._lock:
            if self._mode is not ButtonInteractionMode.DIALOGUE:
                raise RuntimeError("设备当前处于知识补充模式")
            return await operation()
