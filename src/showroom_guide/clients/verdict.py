import json
from typing import Any

import httpx

from showroom_guide.verdict import (
    VerdictClientError,
    VerdictDecision,
    validate_decision,
)


class VerdictClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 15.0,
    ) -> None:
        self._url = (
            f"{base_url.rstrip('/')}"
            "/kb-matrix/data-infra/v1/chat/completions"
        )
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def decide(self, question: str) -> VerdictDecision:
        normalized = question.strip()
        if not normalized:
            raise ValueError("question must not be empty")
        try:
            response = await self._client.post(
                self._url,
                json={
                    "messages": [
                        {"role": "user", "content": normalized},
                    ],
                    "stream": False,
                },
            )
            response.raise_for_status()
            content = self._response_content(response.json())
            return validate_decision(json.loads(content))
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as error:
            raise VerdictClientError("判断应用返回无效响应") from error

    @staticmethod
    def _response_content(payload: Any) -> str:
        if not isinstance(payload, dict):
            raise ValueError("verdict response must be an object")
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("verdict response requires choices")
        choice = choices[0]
        if not isinstance(choice, dict):
            raise ValueError("invalid verdict choice")
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ValueError("invalid verdict message")
        content = message.get("content")
        if not isinstance(content, str):
            raise ValueError("invalid verdict content")
        return content
