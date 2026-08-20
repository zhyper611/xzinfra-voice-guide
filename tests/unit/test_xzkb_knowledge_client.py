import httpx
import pytest
import respx

from showroom_guide.clients.xzkb_knowledge import (
    DocumentProcessingState,
    XzkbKnowledgeClient,
    XzkbKnowledgeError,
)
from showroom_guide.knowledge_outbox import KnowledgeEntry, OutboxState


def make_entry(state=OutboxState.PENDING):
    return KnowledgeEntry(
        id="entry-id",
        content="总装车间采用柔性生产线。",
        filename="voice-knowledge-entry-id.md",
        state=state,
        attempts=0,
        next_attempt_at=0,
        last_error=None,
    )


def make_client():
    return XzkbKnowledgeClient(
        "http://xzkb.test",
        "user-token",
        "11111111-1111-1111-1111-111111111111",
        folder_id="22222222-2222-2222-2222-222222222222",
    )


@pytest.mark.asyncio
@respx.mock
async def test_upload_uses_stable_multipart_endpoint_and_checks_business_code():
    route = respx.post(
        "http://xzkb.test/kb-matrix/data-infra/v1/kb-document/upload-files"
    ).mock(return_value=httpx.Response(200, json={"code": "200", "message": "success", "data": None}))

    async with make_client() as client:
        await client.upload(make_entry())

    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer user-token"
    assert b'voice-knowledge-entry-id.md' in request.content
    assert b'11111111-1111-1111-1111-111111111111' in request.content
    assert b'22222222-2222-2222-2222-222222222222' in request.content
    assert "总装车间采用柔性生产线".encode() in request.content


@pytest.mark.asyncio
@respx.mock
async def test_upload_rejects_http_success_with_business_failure():
    respx.post(
        "http://xzkb.test/kb-matrix/data-infra/v1/kb-document/upload-files"
    ).mock(return_value=httpx.Response(200, json={"code": "403", "message": "无写入权限", "data": None}))

    async with make_client() as client:
        with pytest.raises(XzkbKnowledgeError, match="无写入权限"):
            await client.upload(make_entry())


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    ("status_meta", "expected"),
    [
        ({"Parse": {"state": "STARTED"}, "EMBEDDING": {"state": "PENDING"}}, DocumentProcessingState.PENDING),
        ({"Parse": {"state": "SUCCESS"}, "EMBEDDING": {"state": "SUCCESS"}}, DocumentProcessingState.SUCCESS),
        ({"Parse": {"state": "SUCCESS"}, "EMBEDDING": {"state": "FAILURE"}}, DocumentProcessingState.FAILURE),
    ],
)
async def test_document_state_tracks_parse_and_embedding(status_meta, expected):
    respx.get(
        "http://xzkb.test/kb-matrix/data-infra/v1/kb-document/page"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "code": "200",
                "message": "success",
                "data": {
                    "current": 1,
                    "total": 1,
                    "data": [{"name": "voice-knowledge-entry-id.md", "status_meta": status_meta}],
                },
            },
        )
    )

    async with make_client() as client:
        state = await client.document_state("voice-knowledge-entry-id.md")

    assert state is expected
