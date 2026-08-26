import json
from enum import StrEnum
from typing import Awaitable, Callable

import httpx

from showroom_guide.clients.xzkb_auth import XzkbLocalAccountAuth
from showroom_guide.knowledge_outbox import KnowledgeEntry


class XzkbKnowledgeError(ValueError):
    pass


class XzkbKnowledgeAuthenticationError(XzkbKnowledgeError):
    pass


class XzkbKnowledgePermissionError(XzkbKnowledgeError):
    pass


class XzkbKnowledgeUnavailableError(XzkbKnowledgeError):
    pass


class DocumentProcessingState(StrEnum):
    NOT_FOUND = "not_found"
    PENDING = "pending"
    SUCCESS = "success"
    FAILURE = "failure"


class XzkbKnowledgeClient:
    def __init__(
        self,
        base_url: str,
        auth: XzkbLocalAccountAuth,
        kb_id: str,
        *,
        folder_id: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        root = base_url.rstrip("/")
        self._upload_url = f"{root}/kb-matrix/data-infra/v1/kb-document/upload-files"
        self._page_url = f"{root}/kb-matrix/data-infra/v1/kb-document/page"
        self._auth = auth
        self._kb_id = kb_id
        self._folder_id = folder_id
        self._client = httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> "XzkbKnowledgeClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        finally:
            await self._auth.aclose()

    async def upload(self, entry: KnowledgeEntry) -> None:
        fields = {"kb_id": self._kb_id, "meta": json.dumps({})}
        if self._folder_id:
            fields["folder_id"] = self._folder_id
        markdown = (
            f"# {entry.title}\n\n{entry.content}\n\n来源：树莓派语音补充\n"
        ).encode("utf-8")

        async def send(token: str) -> httpx.Response:
            return await self._client.post(
                self._upload_url,
                headers={"Authorization": f"Bearer {token}"},
                data=fields,
                files={"files": (entry.filename, markdown, "text/markdown")},
            )

        response = await self._authenticated_request(send)
        self._business_payload(response)

    async def document_state(self, filename: str) -> DocumentProcessingState:
        params = {
            "current": 1,
            "size": 10,
            "kb_id": self._kb_id,
            "name": filename,
        }
        if self._folder_id:
            params["folder_id"] = self._folder_id

        async def send(token: str) -> httpx.Response:
            return await self._client.get(
                self._page_url,
                headers={"Authorization": f"Bearer {token}"},
                params=params,
            )

        response = await self._authenticated_request(send)
        payload = self._business_payload(response)
        page = payload.get("data")
        if not isinstance(page, dict) or not isinstance(page.get("data"), list):
            raise XzkbKnowledgeError("XZKB 文档状态响应格式无效")
        documents = [
            item
            for item in page["data"]
            if isinstance(item, dict) and item.get("name") == filename
        ]
        if not documents:
            return DocumentProcessingState.NOT_FOUND
        status_meta = documents[0].get("status_meta")
        if not isinstance(status_meta, dict):
            return DocumentProcessingState.PENDING
        states = {
            name: value.get("state")
            for name, value in status_meta.items()
            if isinstance(value, dict)
        }
        required = [states.get("Parse"), states.get("EMBEDDING")]
        if "FAILURE" in required:
            return DocumentProcessingState.FAILURE
        if required == ["SUCCESS", "SUCCESS"]:
            return DocumentProcessingState.SUCCESS
        return DocumentProcessingState.PENDING

    async def _authenticated_request(
        self,
        send: Callable[[str], Awaitable[httpx.Response]],
    ) -> httpx.Response:
        token = await self._auth.token()
        for attempt in range(2):
            try:
                response = await send(token)
            except httpx.RequestError:
                raise XzkbKnowledgeUnavailableError(
                    "XZKB 服务暂时不可用，知识已本地保存，等待同步"
                ) from None
            if response.status_code == 401:
                await self._auth.invalidate(token)
                if attempt == 1:
                    raise XzkbKnowledgeAuthenticationError(
                        "XZKB 专用账号认证失败，知识已本地保存，等待同步"
                    )
                token = await self._auth.token()
                continue
            if response.status_code == 403:
                raise XzkbKnowledgePermissionError(
                    "XZKB 专用账号无知识库权限，知识已本地保存，等待同步"
                )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError:
                raise XzkbKnowledgeUnavailableError(
                    "XZKB 服务暂时不可用，知识已本地保存，等待同步"
                ) from None
            return response
        raise AssertionError("unreachable")

    @staticmethod
    def _business_payload(response: httpx.Response) -> dict[str, object]:
        try:
            payload = response.json()
        except ValueError as error:
            raise XzkbKnowledgeError("XZKB 返回了无效 JSON") from error
        if not isinstance(payload, dict):
            raise XzkbKnowledgeError("XZKB 返回格式无效")
        if str(payload.get("code")) not in {"0", "200"}:
            message = str(payload.get("message") or "XZKB 操作失败")
            raise XzkbKnowledgeError(message)
        return payload
