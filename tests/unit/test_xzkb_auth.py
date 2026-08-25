import asyncio

import httpx
import pytest
import respx

from showroom_guide.clients.xzkb_auth import (
    XzkbAuthenticationError,
    XzkbLocalAccountAuth,
)


LOGIN_URL = "http://xzkb.test/kb-matrix-base/v1/auth/login-local"
ERROR_MESSAGE = "XZKB 专用账号登录失败，知识已本地保存，等待同步"
USERNAME = "sync-account@example.test"
PASSWORD = "fictional-password"
SENSITIVE_TOKEN = "secret-token"


def test_authentication_error_is_a_value_error():
    assert issubclass(XzkbAuthenticationError, ValueError)


def make_auth() -> XzkbLocalAccountAuth:
    return XzkbLocalAccountAuth(
        "http://xzkb.test/",
        USERNAME,
        PASSWORD,
        timeout=1.5,
    )


@pytest.mark.asyncio
@respx.mock
async def test_token_logs_in_once_and_caches_stripped_access_token():
    route = respx.post(LOGIN_URL).mock(
        return_value=httpx.Response(
            200,
            json={"code": 200, "data": {"accessToken": "  access-token  "}},
        )
    )
    auth = make_auth()

    try:
        first = await auth.token()
        second = await auth.token()
    finally:
        await auth.aclose()

    assert first == "access-token"
    assert second == "access-token"
    assert route.call_count == 1
    assert route.calls[0].request.url == LOGIN_URL
    assert route.calls[0].request.read() == (
        b'{"username":"sync-account@example.test",'
        b'"password":"fictional-password"}'
    )


@pytest.mark.asyncio
@respx.mock
async def test_concurrent_first_token_requests_share_one_login():
    login_started = asyncio.Event()
    release_login = asyncio.Event()

    async def respond(_request: httpx.Request) -> httpx.Response:
        login_started.set()
        await release_login.wait()
        return httpx.Response(
            200,
            json={"code": "200", "data": {"accessToken": "shared-token"}},
        )

    route = respx.post(LOGIN_URL).mock(side_effect=respond)
    auth = make_auth()
    tasks = [asyncio.create_task(auth.token()) for _ in range(10)]

    try:
        await asyncio.wait_for(login_started.wait(), timeout=1)
        await asyncio.sleep(0)
        release_login.set()
        tokens = await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)
    finally:
        release_login.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await auth.aclose()

    assert tokens == ["shared-token"] * 10
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_invalidate_only_clears_matching_cached_token():
    issued_tokens = iter(["first-token", "second-token", "third-token"])

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": "200",
                "data": {"accessToken": next(issued_tokens)},
            },
        )

    route = respx.post(LOGIN_URL).mock(side_effect=respond)
    auth = make_auth()

    try:
        first = await auth.token()
        await auth.invalidate(first)
        second = await auth.token()
        await auth.invalidate(first)
        still_second = await auth.token()
        await auth.invalidate(second)
        third = await auth.token()
    finally:
        await auth.aclose()

    assert (first, second, still_second, third) == (
        "first-token",
        "second-token",
        "second-token",
        "third-token",
    )
    assert route.call_count == 3


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, text="service unavailable"),
        httpx.Response(200, text="not-json"),
        httpx.Response(200, json=[{"code": "200"}]),
        httpx.Response(
            200,
            json={
                "code": "403",
                "message": "bad credentials",
                "data": {"accessToken": SENSITIVE_TOKEN},
            },
        ),
        httpx.Response(200, json={"code": "200", "data": {}}),
        httpx.Response(
            200,
            json={"code": "200", "data": {"accessToken": "   "}},
        ),
        httpx.Response(
            200,
            json={"code": "200", "data": {"accessToken": 123}},
        ),
    ],
    ids=[
        "http-error",
        "invalid-json",
        "non-dict-json",
        "business-error",
        "missing-token",
        "empty-token",
        "non-string-token",
    ],
)
async def test_login_failures_raise_stable_sanitized_error(response):
    respx.post(LOGIN_URL).mock(return_value=response)
    auth = make_auth()

    try:
        with pytest.raises(XzkbAuthenticationError) as caught:
            await auth.token()
    finally:
        await auth.aclose()

    assert str(caught.value) == ERROR_MESSAGE
    assert USERNAME not in str(caught.value)
    assert PASSWORD not in str(caught.value)
    assert SENSITIVE_TOKEN not in str(caught.value)
    assert LOGIN_URL not in str(caught.value)


@pytest.mark.asyncio
@respx.mock
async def test_connection_failure_raises_stable_sanitized_error():
    request = httpx.Request("POST", LOGIN_URL)
    respx.post(LOGIN_URL).mock(
        side_effect=httpx.ConnectError(
            f"could not connect {USERNAME} {PASSWORD}",
            request=request,
        )
    )
    auth = make_auth()

    try:
        with pytest.raises(XzkbAuthenticationError) as caught:
            await auth.token()
    finally:
        await auth.aclose()

    assert str(caught.value) == ERROR_MESSAGE
    assert USERNAME not in str(caught.value)
    assert PASSWORD not in str(caught.value)
    assert LOGIN_URL not in str(caught.value)


@pytest.mark.asyncio
async def test_aclose_closes_http_client():
    auth = make_auth()

    await auth.aclose()

    assert auth._client.is_closed
