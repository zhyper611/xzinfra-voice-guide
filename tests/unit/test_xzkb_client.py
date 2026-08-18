import httpx
import json
import pytest
import respx

from showroom_guide.clients.xzkb import XzkbClient, XzkbStreamMilestone


@pytest.mark.asyncio
@respx.mock
async def test_chat_yields_text_from_sse():
    body = "\n".join(
        [
            ": keep-alive",
            'data: {"choices":[{"delta":{"content":"第一句。"}}]}',
            'data: {"choices":[{"delta":{"content":"第二句。"}}]}',
            "data: [DONE]",
            "",
        ]
    )
    route = respx.post(
        "http://xzkb.test/kb-matrix/data-infra/v1/chat/completions"
    ).mock(
        return_value=httpx.Response(
            200,
            text=body,
            headers={"content-type": "text/event-stream"},
        )
    )

    async with XzkbClient("http://xzkb.test/", "device-key") as client:
        events = [
            event
            async for event in client.stream_chat(
                [{"role": "user", "content": "问题"}]
            )
        ]

    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer device-key"
    assert request.url.path == "/kb-matrix/data-infra/v1/chat/completions"
    assert [event.text for event in events] == ["第一句。", "第二句。"]


@pytest.mark.asyncio
@respx.mock
async def test_observer_marks_first_sse_before_reasoning_or_content():
    body = "\n".join(
        [
            'data: {"choices":[{"delta":{"reasoning":"thinking"}}]}',
            'data: {"choices":[{"delta":{"content":" "}}]}',
            "data: [DONE]",
            "",
        ]
    )
    respx.post(
        "http://xzkb.test/kb-matrix/data-infra/v1/chat/completions"
    ).mock(
        return_value=httpx.Response(
            200,
            text=body,
            headers={"content-type": "text/event-stream"},
        )
    )
    milestones = []

    async with XzkbClient("http://xzkb.test", "device-key") as client:
        events = [
            event
            async for event in client.stream_chat(
                [{"role": "user", "content": "question"}],
                observer=milestones.append,
            )
        ]

    assert [event.text for event in events] == [" "]
    assert milestones == [
        XzkbStreamMilestone.RESPONSE_HEADERS,
        XzkbStreamMilestone.FIRST_SSE,
    ]


@pytest.mark.asyncio
@respx.mock
async def test_observer_does_not_mark_headers_when_status_check_fails():
    respx.post(
        "http://xzkb.test/kb-matrix/data-infra/v1/chat/completions"
    ).mock(return_value=httpx.Response(503, text="unavailable"))
    milestones = []

    async with XzkbClient("http://xzkb.test", "device-key") as client:
        with pytest.raises(httpx.HTTPStatusError):
            _ = [
                event
                async for event in client.stream_chat(
                    [{"role": "user", "content": "question"}],
                    observer=milestones.append,
                )
            ]

    assert milestones == []


@pytest.mark.asyncio
@respx.mock
async def test_observer_marks_headers_and_sse_without_body_content():
    respx.post(
        "http://xzkb.test/kb-matrix/data-infra/v1/chat/completions"
    ).mock(
        return_value=httpx.Response(
            200,
            text='data: {"choices":[{"delta":{"reasoning":"thinking"}}]}\n\ndata: [DONE]\n',
            headers={"content-type": "text/event-stream"},
        )
    )
    milestones = []

    async with XzkbClient("http://xzkb.test", "device-key") as client:
        events = [
            event
            async for event in client.stream_chat(
                [{"role": "user", "content": "question"}],
                observer=milestones.append,
            )
        ]

    assert events == []
    assert milestones == [
        XzkbStreamMilestone.RESPONSE_HEADERS,
        XzkbStreamMilestone.FIRST_SSE,
    ]


@pytest.mark.asyncio
@respx.mock
async def test_observer_marks_headers_when_response_has_no_sse_event():
    respx.post(
        "http://xzkb.test/kb-matrix/data-infra/v1/chat/completions"
    ).mock(
        return_value=httpx.Response(
            200,
            text=": keep-alive\n\n",
            headers={"content-type": "text/event-stream"},
        )
    )
    milestones = []

    async with XzkbClient("http://xzkb.test", "device-key") as client:
        events = [
            event
            async for event in client.stream_chat(
                [{"role": "user", "content": "question"}],
                observer=milestones.append,
            )
        ]

    assert events == []
    assert milestones == [XzkbStreamMilestone.RESPONSE_HEADERS]


@pytest.mark.asyncio
@respx.mock
async def test_chat_raises_on_http_error():
    respx.post(
        "http://xzkb.test/kb-matrix/data-infra/v1/chat/completions"
    ).mock(return_value=httpx.Response(503, text="unavailable"))

    async with XzkbClient("http://xzkb.test", "device-key") as client:
        with pytest.raises(httpx.HTTPStatusError):
            _ = [
                event
                async for event in client.stream_chat(
                    [{"role": "user", "content": "问题"}]
                )
            ]


@pytest.mark.asyncio
@respx.mock
async def test_chat_can_override_max_tokens_for_empty_content_retry():
    route = respx.post(
        "http://xzkb.test/kb-matrix/data-infra/v1/chat/completions"
    ).mock(
        return_value=httpx.Response(
            200,
            text='data: {"choices":[{"delta":{"content":"回答"}}]}\n\ndata: [DONE]\n',
            headers={"content-type": "text/event-stream"},
        )
    )

    async with XzkbClient("http://xzkb.test", "device-key") as client:
        events = [
            event
            async for event in client.stream_chat(
                [{"role": "user", "content": "问题"}],
                max_tokens=8000,
            )
        ]

    assert [event.text for event in events] == ["回答"]
    assert json.loads(route.calls[0].request.content)["max_tokens"] == 8000
