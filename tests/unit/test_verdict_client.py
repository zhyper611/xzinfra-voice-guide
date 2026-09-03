import json

import httpx
import pytest

from showroom_guide.clients.verdict import VerdictClient
from showroom_guide.verdict import (
    Verdict,
    VerdictBasis,
    VerdictClientError,
    VerdictScope,
)


def response_content(payload):
    return {
        "choices": [
            {"message": {"content": json.dumps(payload, ensure_ascii=False)}}
        ]
    }


@pytest.mark.asyncio
async def test_decide_posts_one_question_to_xzkb_application(respx_mock):
    route = respx_mock.post(
        "https://kb.test/kb-matrix/data-infra/v1/chat/completions"
    ).mock(
        return_value=httpx.Response(
            200,
            json=response_content(
                {
                    "scope": "casual",
                    "verdict": "yes",
                    "basis": "general",
                    "reason": "低风险日常判断",
                    "evidence": "",
                }
            ),
        )
    )
    client = VerdictClient("https://kb.test/", "secret-key", timeout=15)

    decision = await client.decide("今天适合喝咖啡吗？")

    assert decision.scope is VerdictScope.CASUAL
    assert decision.verdict is Verdict.YES
    assert decision.basis is VerdictBasis.GENERAL
    assert len(route.calls) == 1
    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer secret-key"
    assert json.loads(request.content) == {
        "messages": [
            {"role": "user", "content": "今天适合喝咖啡吗？"}
        ],
        "stream": False,
    }
    await client.aclose()


@pytest.mark.asyncio
async def test_exhibition_without_knowledge_evidence_fails_closed(respx_mock):
    respx_mock.post(
        "https://kb.test/kb-matrix/data-infra/v1/chat/completions"
    ).mock(
        return_value=httpx.Response(
            200,
            json=response_content(
                {
                    "scope": "exhibition",
                    "verdict": "no",
                    "basis": "general",
                    "reason": "模型推测不支持",
                    "evidence": "",
                }
            ),
        )
    )
    client = VerdictClient("https://kb.test", "secret-key")

    decision = await client.decide("这个产品支持离线运行吗？")

    assert decision.verdict is Verdict.NEUTRAL
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "```json\n{}\n```",
        "{}",
        json.dumps(
            {
                "scope": "casual",
                "verdict": "maybe",
                "basis": "general",
                "reason": "x",
                "evidence": "",
            }
        ),
    ],
)
async def test_invalid_content_raises_verdict_client_error(
    respx_mock,
    content,
):
    respx_mock.post(
        "https://kb.test/kb-matrix/data-infra/v1/chat/completions"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )
    )
    client = VerdictClient("https://kb.test", "secret-key")

    with pytest.raises(VerdictClientError):
        await client.decide("问题")
    await client.aclose()


@pytest.mark.asyncio
async def test_http_failure_is_wrapped(respx_mock):
    respx_mock.post(
        "https://kb.test/kb-matrix/data-infra/v1/chat/completions"
    ).mock(return_value=httpx.Response(503))
    client = VerdictClient("https://kb.test", "secret-key")

    with pytest.raises(VerdictClientError):
        await client.decide("今天适合散步吗？")
    await client.aclose()
