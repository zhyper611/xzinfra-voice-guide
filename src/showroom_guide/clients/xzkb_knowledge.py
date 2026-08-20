import json
from enum import StrEnum

import httpx

from showroom_guide.knowledge_outbox import KnowledgeEntry


class XzkbKnowledgeError(ValueError):
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
        token: str,
        kb_id: str,
        *,
        folder_id: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        root = base_url.rstrip("/")
        self._upload_url = (
            f"{root}/kb-matrix/data-infra/v1/kb-document/upload-files"
        )
        self._page_url = f"{root}/kb-matrix/data-infra/v1/kb-document/page"
        self._kb_id = kb_id
        self._folder_id = folder_id
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"Authorization": f"Bearer {token}"},
        )

    async def __aenter__(self) -> "XzkbKnowledgeClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def upload(self, entry: KnowledgeEntry) -> None:
        fields = {"kb_id": self._kb_id, "meta": json.dumps({})}
        if self._folder_id:
            fields["folder_id"] = self._folder_id
        markdown = (
            "# 语音补充知识\n\n"
            f"{entry.content}\n\n"
            "来源：树莓派语音补充\n"
        ).encode("utf-8")
        response = await self._client.post(
            self._upload_url,
            data=fields,
            files={"files": (entry.filename, markdown, "text/markdown")},
        )
        response.raise_for_status()
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
        response = await self._client.get(self._page_url, params=params)
        response.raise_for_status()
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
