from unittest.mock import AsyncMock, MagicMock

import pytest

from showroom_guide.knowledge_capture import (
    KnowledgeCaptureSession,
    normalize_knowledge_text,
)


def test_normalize_knowledge_text_only_changes_whitespace_and_terminal_punctuation():
    assert normalize_knowledge_text("  总装车间   采用柔性生产线  ") == "总装车间 采用柔性生产线。"
    assert normalize_knowledge_text("已有事实！") == "已有事实！"


@pytest.mark.asyncio
async def test_review_transcribes_and_synthesizes_without_ai_rewrite():
    speech = AsyncMock()
    speech.transcribe.return_value = "  总装车间   采用柔性生产线  "
    speech.synthesize.return_value = b"review-wav"
    outbox = MagicMock()
    sync = MagicMock()
    session = KnowledgeCaptureSession(speech, outbox, sync)

    draft = await session.review(b"recorded-wav")

    assert draft.text == "总装车间 采用柔性生产线。"
    assert draft.audio == b"review-wav"
    speech.synthesize.assert_awaited_once_with(
        "您刚才补充的是：总装车间 采用柔性生产线。"
    )
    assert session.has_draft is False


@pytest.mark.asyncio
async def test_failed_rerecord_keeps_previous_accepted_draft():
    speech = AsyncMock()
    speech.transcribe.side_effect = ["第一版事实", ValueError("ASR failed")]
    speech.synthesize.return_value = b"review-wav"
    session = KnowledgeCaptureSession(speech, MagicMock(), MagicMock())
    first = await session.review(b"first")
    session.accept(first)

    with pytest.raises(ValueError):
        await session.review(b"second")

    assert session.draft_text == "第一版事实。"


def test_save_persists_locally_before_waking_background_sync():
    outbox = MagicMock()
    outbox.enqueue.return_value = MagicMock(id="entry-id")
    sync = MagicMock()
    session = KnowledgeCaptureSession(AsyncMock(), outbox, sync)
    session.accept(MagicMock(text="确认后的知识。", audio=b"wav"))

    entry = session.save()

    outbox.enqueue.assert_called_once_with("确认后的知识。")
    sync.wake.assert_called_once_with()
    assert entry.id == "entry-id"
    assert session.has_draft is False
