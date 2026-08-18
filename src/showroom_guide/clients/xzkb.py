import json
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

import httpx


@dataclass(frozen=True)
class ChatStreamEvent:
    text: str


class XzkbStreamMilestone(str, Enum):
    RESPONSE_HEADERS = "response_headers"
    FIRST_SSE = "first_sse"


class XzkbClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0) -> None:
        self._url = (
            f"{base_url.rstrip('/')}"
            "/kb-matrix/data-infra/v1/chat/completions"
        )
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    async def __aenter__(self) -> "XzkbClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def stream_chat(
        self,
        messages: Sequence[dict[str, Any]],
        max_tokens: int | None = None,
        observer: Callable[[XzkbStreamMilestone], None] | None = None,
    ) -> AsyncIterator[ChatStreamEvent]:
        payload = {"messages": list(messages), "stream": True}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        async with self._client.stream(
            "POST",
            self._url,
            json=payload,
        ) as response:
            response.raise_for_status()
            if observer is not None:
                observer(XzkbStreamMilestone.RESPONSE_HEADERS)
            first_sse_seen = False
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                if not first_sse_seen:
                    first_sse_seen = True
                    if observer is not None:
                        observer(XzkbStreamMilestone.FIRST_SSE)
                payload = line[5:].lstrip()
                if payload == "[DONE]":
                    return
                data = json.loads(payload)
                choices = data.get("choices") or [{}]
                delta = choices[0].get("delta") or {}
                text = delta.get("content", "")
                if text:
                    yield ChatStreamEvent(text=text)
