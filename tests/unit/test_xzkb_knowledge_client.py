import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from showroom_guide.clients.xzkb_auth import XzkbLocalAccountAuth
from showroom_guide.clients.xzkb_knowledge import (
    DocumentProcessingState,
    XzkbKnowledgeClient,
    XzkbKnowledgeAuthenticationError,
    XzkbKnowledgeError,
    XzkbKnowledgePermissionError,
    XzkbKnowledgeUnavailableError,
)
from showroom_guide.knowledge_outbox import KnowledgeEntry, OutboxState


def make_entry(state=OutboxState.PENDING):
    return KnowledgeEntry(
        id="entry-id",
        content="总装车间采用柔性生产线。",
        title="总装车间柔性生产线",
        filename="voice-knowledge-entry-id.md",
        state=state,
        attempts=0,
        next_attempt_at=0,
        last_error=None,
    )


def make_client(auth=None):
    if auth is None:
        auth = AsyncMock()
        auth.token.return_value = "user-token"
    client = XzkbKnowledgeClient(
        "http://xzkb.test",
        auth,
        "11111111-1111-1111-1111-111111111111",
        folder_id="22222222-2222-2222-2222-222222222222",
    )
    return client, auth


@pytest.mark.asyncio
@respx.mock
async def test_upload_uses_stable_multipart_endpoint_and_checks_business_code():
    route = respx.post(
        "http://xzkb.test/kb-matrix/data-infra/v1/kb-document/upload-files"
    ).mock(
        return_value=httpx.Response(
            200, json={"code": "200", "message": "success", "data": None}
        )
    )

    client, auth = make_client()
    async with client:
        await client.upload(make_entry())

    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer user-token"
    assert b"voice-knowledge-entry-id.md" in request.content
    assert b"11111111-1111-1111-1111-111111111111" in request.content
    assert b"22222222-2222-2222-2222-222222222222" in request.content
    assert "# 总装车间柔性生产线".encode() in request.content
    assert "总装车间采用柔性生产线".encode() in request.content
    auth.token.assert_awaited_once_with()


@pytest.mark.asyncio
@respx.mock
async def test_upload_rejects_http_success_with_business_failure():
    respx.post(
        "http://xzkb.test/kb-matrix/data-infra/v1/kb-document/upload-files"
    ).mock(
        return_value=httpx.Response(
            200, json={"code": "403", "message": "无写入权限", "data": None}
        )
    )

    client, _ = make_client()
    async with client:
        with pytest.raises(XzkbKnowledgeError, match="无写入权限"):
            await client.upload(make_entry())


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    ("status_meta", "expected"),
    [
        (
            {"Parse": {"state": "STARTED"}, "EMBEDDING": {"state": "PENDING"}},
            DocumentProcessingState.PENDING,
        ),
        (
            {"Parse": {"state": "SUCCESS"}, "EMBEDDING": {"state": "SUCCESS"}},
            DocumentProcessingState.SUCCESS,
        ),
        (
            {"Parse": {"state": "SUCCESS"}, "EMBEDDING": {"state": "FAILURE"}},
            DocumentProcessingState.FAILURE,
        ),
    ],
)
async def test_document_state_tracks_parse_and_embedding(status_meta, expected):
    respx.get("http://xzkb.test/kb-matrix/data-infra/v1/kb-document/page").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": "200",
                "message": "success",
                "data": {
                    "current": 1,
                    "total": 1,
                    "data": [
                        {
                            "name": "voice-knowledge-entry-id.md",
                            "status_meta": status_meta,
                        }
                    ],
                },
            },
        )
    )

    client, _ = make_client()
    async with client:
        state = await client.document_state("voice-knowledge-entry-id.md")

    assert state is expected


@pytest.mark.asyncio
@respx.mock
async def test_upload_refreshes_token_once_and_rebuilds_complete_multipart():
    route = respx.post(
        "http://xzkb.test/kb-matrix/data-infra/v1/kb-document/upload-files"
    ).mock(
        side_effect=[
            httpx.Response(401),
            httpx.Response(200, json={"code": "200", "data": None}),
        ]
    )
    auth = AsyncMock()
    auth.token.side_effect = ["expired-token", "fresh-token"]
    client, _ = make_client(auth)

    async with client:
        await client.upload(make_entry())

    assert len(route.calls) == 2
    first, second = [call.request for call in route.calls]
    assert first.headers["Authorization"] == "Bearer expired-token"
    assert second.headers["Authorization"] == "Bearer fresh-token"
    for request in (first, second):
        assert b"voice-knowledge-entry-id.md" in request.content
        assert b"11111111-1111-1111-1111-111111111111" in request.content
        assert b"22222222-2222-2222-2222-222222222222" in request.content
        assert "总装车间采用柔性生产线".encode() in request.content
    auth.invalidate.assert_awaited_once_with("expired-token")
    assert auth.token.await_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_document_state_refreshes_token_after_unauthorized_response():
    route = respx.get("http://xzkb.test/kb-matrix/data-infra/v1/kb-document/page").mock(
        side_effect=[
            httpx.Response(401),
            httpx.Response(
                200,
                json={
                    "code": "200",
                    "data": {"data": []},
                },
            ),
        ]
    )
    auth = AsyncMock()
    auth.token.side_effect = ["expired-token", "fresh-token"]
    client, _ = make_client(auth)

    async with client:
        state = await client.document_state("missing.md")

    assert state is DocumentProcessingState.NOT_FOUND
    assert [call.request.headers["Authorization"] for call in route.calls] == [
        "Bearer expired-token",
        "Bearer fresh-token",
    ]
    auth.invalidate.assert_awaited_once_with("expired-token")


@pytest.mark.asyncio
@respx.mock
async def test_second_unauthorized_response_raises_authentication_error_once():
    route = respx.get("http://xzkb.test/kb-matrix/data-infra/v1/kb-document/page").mock(
        side_effect=[httpx.Response(401), httpx.Response(401)]
    )
    auth = AsyncMock()
    auth.token.side_effect = ["fictional-expired-token", "fictional-fresh-token"]
    client, _ = make_client(auth)

    async with client:
        with pytest.raises(XzkbKnowledgeAuthenticationError) as raised:
            await client.document_state("secret.md")

    assert len(route.calls) == 2
    assert [awaited.args for awaited in auth.invalidate.await_args_list] == [
        ("fictional-expired-token",),
        ("fictional-fresh-token",),
    ]
    assert auth.token.await_count == 2
    assert "fictional-expired-token" not in str(raised.value)
    assert "fictional-fresh-token" not in str(raised.value)
    assert "http://xzkb.test" not in str(raised.value)


@pytest.mark.asyncio
@respx.mock
async def test_concurrent_unauthorized_requests_share_one_token_refresh():
    login_count = 0
    old_request_count = 0
    both_old_requests_started = asyncio.Event()
    fresh_login_completed = asyncio.Event()

    def login(_request: httpx.Request) -> httpx.Response:
        nonlocal login_count
        login_count += 1
        token = "expired-token" if login_count == 1 else "fresh-token"
        if login_count == 2:
            fresh_login_completed.set()
        return httpx.Response(
            200,
            json={"code": "200", "data": {"accessToken": token}},
        )

    async def page(request: httpx.Request) -> httpx.Response:
        nonlocal old_request_count
        if request.headers["Authorization"] == "Bearer fresh-token":
            return httpx.Response(
                200,
                json={"code": "200", "data": {"data": []}},
            )
        old_request_count += 1
        if old_request_count == 1:
            await both_old_requests_started.wait()
        else:
            both_old_requests_started.set()
            await fresh_login_completed.wait()
        return httpx.Response(401)

    login_route = respx.post(
        "http://xzkb.test/kb-matrix-base/v1/auth/login-local"
    ).mock(side_effect=login)
    page_route = respx.get(
        "http://xzkb.test/kb-matrix/data-infra/v1/kb-document/page"
    ).mock(side_effect=page)
    auth = XzkbLocalAccountAuth(
        "http://xzkb.test",
        "sync-account@example.test",
        "fictional-password",
    )
    client, _ = make_client(auth)

    try:
        assert await auth.token() == "expired-token"
        states = await asyncio.wait_for(
            asyncio.gather(
                client.document_state("first.md"),
                client.document_state("second.md"),
            ),
            timeout=1,
        )
    finally:
        both_old_requests_started.set()
        fresh_login_completed.set()
        await client.aclose()

    assert states == [
        DocumentProcessingState.NOT_FOUND,
        DocumentProcessingState.NOT_FOUND,
    ]
    assert login_route.call_count == 2
    assert login_count == 2
    assert page_route.call_count == 4
    assert [call.request.headers["Authorization"] for call in page_route.calls].count(
        "Bearer fresh-token"
    ) == 2


@pytest.mark.asyncio
@respx.mock
async def test_http_forbidden_does_not_refresh_or_invalidate_authentication():
    route = respx.post(
        "http://xzkb.test/kb-matrix/data-infra/v1/kb-document/upload-files"
    ).mock(return_value=httpx.Response(403))
    client, auth = make_client()

    async with client:
        with pytest.raises(XzkbKnowledgePermissionError):
            await client.upload(make_entry())

    assert len(route.calls) == 1
    auth.token.assert_awaited_once_with()
    auth.invalidate.assert_not_awaited()


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("failure", [httpx.Response(503), httpx.Response(500)])
async def test_http_server_error_uses_safe_unavailable_error(failure):
    respx.get("http://xzkb.test/kb-matrix/data-infra/v1/kb-document/page").mock(
        return_value=failure
    )
    auth = AsyncMock()
    auth.token.return_value = "fictional-secret-token"
    client, _ = make_client(auth)

    async with client:
        with pytest.raises(XzkbKnowledgeUnavailableError) as raised:
            await client.document_state("secret.md")

    assert "fictional-secret-token" not in str(raised.value)
    assert "http://xzkb.test" not in str(raised.value)


@pytest.mark.asyncio
@respx.mock
async def test_request_error_uses_safe_unavailable_error():
    request = httpx.Request(
        "GET",
        "http://xzkb.test/kb-matrix/data-infra/v1/kb-document/page",
        headers={"Authorization": "Bearer fictional-secret-token"},
    )
    respx.get(request.url).mock(
        side_effect=httpx.ConnectError("connection failed", request=request)
    )
    auth = AsyncMock()
    auth.token.return_value = "fictional-secret-token"
    client, _ = make_client(auth)

    async with client:
        with pytest.raises(XzkbKnowledgeUnavailableError) as raised:
            await client.document_state("secret.md")

    assert "fictional-secret-token" not in str(raised.value)
    assert str(request.url) not in str(raised.value)


@pytest.mark.asyncio
async def test_aclose_attempts_auth_close_when_knowledge_client_close_fails():
    client, auth = make_client()
    client._client.aclose = AsyncMock(side_effect=RuntimeError("close failed"))

    with pytest.raises(RuntimeError, match="close failed"):
        await client.aclose()

    auth.aclose.assert_awaited_once_with()
