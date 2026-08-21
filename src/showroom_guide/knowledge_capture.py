import io
import re
from dataclasses import dataclass

import httpx

from showroom_guide.device import NO_SPEECH_MESSAGE, NoSpeechDetected


_TERMINAL_PUNCTUATION = ("。", "！", "？", ".", "!", "?")


class KnowledgeAsrUnavailable(RuntimeError):
    pass


class KnowledgeTtsUnavailable(RuntimeError):
    pass


def normalize_knowledge_text(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    if normalized and not normalized.endswith(_TERMINAL_PUNCTUATION):
        normalized += "。"
    return normalized


@dataclass(frozen=True)
class KnowledgeDraft:
    text: str
    audio: bytes


class KnowledgeCaptureSession:
    def __init__(self, speech, outbox, sync_service) -> None:
        self._speech = speech
        self._outbox = outbox
        self._sync_service = sync_service
        self._draft: KnowledgeDraft | None = None

    @property
    def has_draft(self) -> bool:
        return self._draft is not None

    @property
    def draft_text(self) -> str | None:
        return self._draft.text if self._draft is not None else None

    async def transcribe(self, audio: bytes) -> str:
        try:
            transcript = await self._speech.transcribe(io.BytesIO(audio))
        except (httpx.HTTPError, ValueError) as error:
            raise KnowledgeAsrUnavailable from error
        normalized = normalize_knowledge_text(transcript)
        if not normalized:
            raise NoSpeechDetected(NO_SPEECH_MESSAGE)
        return normalized

    async def synthesize_review(self, text: str) -> KnowledgeDraft:
        try:
            review_audio = await self._speech.synthesize(
                f"您刚才补充的是：{text}"
            )
        except (httpx.HTTPError, ValueError) as error:
            raise KnowledgeTtsUnavailable from error
        return KnowledgeDraft(text=text, audio=review_audio)

    async def review(self, audio: bytes) -> KnowledgeDraft:
        transcript = await self.transcribe(audio)
        return await self.synthesize_review(transcript)

    def accept(self, draft: KnowledgeDraft) -> None:
        self._draft = draft

    def save(self):
        if self._draft is None:
            raise ValueError("当前没有可保存的知识草稿")
        entry = self._outbox.enqueue(self._draft.text)
        self._draft = None
        self._sync_service.wake()
        return entry

    def clear(self) -> None:
        self._draft = None
