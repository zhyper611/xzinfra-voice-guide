from unittest.mock import AsyncMock, MagicMock

import pytest

from showroom_guide.button_workflow import ButtonInteractionMode, DeviceButtonWorkflow


@pytest.mark.asyncio
async def test_short_press_toggles_dialogue_recording():
    local_device = MagicMock()
    local_device.is_recording = False
    local_device.is_idle = True
    local_device.start_recording = AsyncMock()
    local_device.stop_recording = AsyncMock()
    knowledge = MagicMock()
    workflow = DeviceButtonWorkflow(local_device, knowledge)

    await workflow.short_press()
    local_device.start_recording.assert_awaited_once_with()

    local_device.is_recording = True
    await workflow.short_press()
    local_device.stop_recording.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_long_press_enters_knowledge_mode_and_confirmation_saves_once():
    local_device = MagicMock(is_idle=True, is_recording=False)
    knowledge = MagicMock()
    knowledge.enter = AsyncMock()
    knowledge.long_press = AsyncMock(return_value=True)
    workflow = DeviceButtonWorkflow(local_device, knowledge)

    await workflow.long_press()

    assert workflow.mode is ButtonInteractionMode.KNOWLEDGE
    knowledge.enter.assert_awaited_once_with()

    await workflow.long_press()

    knowledge.long_press.assert_awaited_once_with()
    assert workflow.mode is ButtonInteractionMode.DIALOGUE


@pytest.mark.asyncio
async def test_web_operation_is_rejected_while_in_knowledge_mode():
    local_device = MagicMock(is_idle=True, is_recording=False)
    knowledge = MagicMock()
    knowledge.enter = AsyncMock()
    workflow = DeviceButtonWorkflow(local_device, knowledge)
    await workflow.long_press()
    operation = AsyncMock()

    with pytest.raises(RuntimeError, match="知识补充模式"):
        await workflow.run_dialogue(operation)

    operation.assert_not_awaited()
