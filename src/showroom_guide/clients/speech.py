from typing import BinaryIO

import httpx


class SpeechClient:
    _MAX_ATTEMPTS = 2

    def __init__(
        self,
        asr_base_url: str,
        asr_api_key: str,
        asr_model: str,
        tts_base_url: str,
        tts_api_key: str,
        tts_model: str,
        voice: str = "alloy",
        speed: float = 1.0,
        timeout: float = 30.0,
    ) -> None:
        self._asr_url = f"{asr_base_url.rstrip('/')}/audio/transcriptions"
        self._tts_url = f"{tts_base_url.rstrip('/')}/audio/speech"
        self._asr_headers = {"Authorization": f"Bearer {asr_api_key}"}
        self._tts_headers = {"Authorization": f"Bearer {tts_api_key}"}
        self._asr_model = asr_model
        self._tts_model = tts_model
        self._voice = voice
        self._speed = speed
        self._client = httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> "SpeechClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def transcribe(self, audio: BinaryIO) -> str:
        audio_bytes = audio.read()
        response = await self._post_with_retry(
            self._asr_url,
            headers=self._asr_headers,
            files={"file": ("question.wav", audio_bytes, "audio/wav")},
            data={"model": self._asr_model},
        )
        try:
            payload = response.json()
        except ValueError as error:
            raise ValueError("ASR endpoint returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise ValueError("ASR endpoint returned an invalid response")
        text = payload.get("text", "")
        if not isinstance(text, str):
            raise ValueError("ASR endpoint returned invalid text")
        return text.strip()

    async def synthesize(self, text: str) -> bytes:
        response = await self._post_with_retry(
            self._tts_url,
            headers=self._tts_headers,
            json={
                "model": self._tts_model,
                "input": text,
                "voice": self._voice,
                "speed": self._speed,
                "response_format": "wav",
            },
        )
        audio = response.content
        if len(audio) < 12 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
            raise ValueError("TTS endpoint did not return valid WAV audio")
        return audio

    async def _post_with_retry(self, url: str, **kwargs: object) -> httpx.Response:
        for attempt in range(self._MAX_ATTEMPTS):
            try:
                response = await self._client.post(url, **kwargs)
                response.raise_for_status()
                return response
            except httpx.TransportError:
                if attempt + 1 >= self._MAX_ATTEMPTS:
                    raise
            except httpx.HTTPStatusError as error:
                status = error.response.status_code
                transient = status in {408, 429} or status >= 500
                if not transient or attempt + 1 >= self._MAX_ATTEMPTS:
                    raise
        raise RuntimeError("speech request retry exhausted")
