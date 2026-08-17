from typing import BinaryIO

import httpx


class SpeechClient:
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
        response = await self._client.post(
            self._asr_url,
            headers=self._asr_headers,
            files={"file": ("question.wav", audio, "audio/wav")},
            data={"model": self._asr_model},
        )
        response.raise_for_status()
        text = response.json().get("text", "").strip()
        if not text:
            raise ValueError("ASR returned an empty transcription")
        return text

    async def synthesize(self, text: str) -> bytes:
        response = await self._client.post(
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
        response.raise_for_status()
        audio = response.content
        if len(audio) < 12 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
            raise ValueError("TTS endpoint did not return valid WAV audio")
        return audio
