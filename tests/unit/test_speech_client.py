import io

import httpx
import pytest
import respx

from showroom_guide.clients.speech import SpeechClient


def make_client() -> SpeechClient:
    return SpeechClient(
        "http://models.test/",
        "asr-key",
        "company-asr",
        "http://models.test/",
        "tts-key",
        "company-tts",
    )


@pytest.mark.asyncio
@respx.mock
async def test_asr_uses_openai_audio_contract():
    route = respx.post("http://models.test/audio/transcriptions").mock(
        return_value=httpx.Response(200, json={"text": "介绍一下产品"})
    )

    async with make_client() as client:
        text = await client.transcribe(io.BytesIO(b"RIFF-audio"))

    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer asr-key"
    assert b"company-asr" in request.content
    assert text == "介绍一下产品"


@pytest.mark.asyncio
@respx.mock
async def test_asr_server_failure_is_propagated():
    respx.post("http://models.test/audio/transcriptions").mock(
        return_value=httpx.Response(500, text="CUDA out of memory")
    )

    async with make_client() as client:
        with pytest.raises(httpx.HTTPStatusError) as error:
            await client.transcribe(io.BytesIO(b"RIFF-audio"))

    assert error.value.response.status_code == 500


@pytest.mark.asyncio
@respx.mock
async def test_tts_requests_wav():
    wav = b"RIFF\x04\x00\x00\x00WAVE"
    route = respx.post("http://models.test/audio/speech").mock(
        return_value=httpx.Response(200, content=wav)
    )

    async with make_client() as client:
        audio = await client.synthesize("欢迎参观")

    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer tts-key"
    assert b'"response_format":"wav"' in request.content
    assert audio == wav


@pytest.mark.asyncio
@respx.mock
async def test_tts_rejects_non_wav_response():
    respx.post("http://models.test/audio/speech").mock(
        return_value=httpx.Response(200, content=b'{"error":"not audio"}')
    )

    async with make_client() as client:
        with pytest.raises(ValueError, match="WAV"):
            await client.synthesize("欢迎参观")
