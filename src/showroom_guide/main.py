import asyncio
import secrets
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Awaitable

from fastapi import (
    Depends,
    File,
    FastAPI,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr, field_validator

from showroom_guide.audio_store import AudioNotFound, AudioStore
from showroom_guide.clients.speech import SpeechClient
from showroom_guide.clients.xzkb import XzkbClient
from showroom_guide.concurrency import AsyncGate
from showroom_guide.config import Settings
from showroom_guide.controller import (
    GuideController,
    GuideServiceUnavailable,
    QuestionInProgress,
)
from showroom_guide.device import (
    DeviceTranscriptionUnavailable,
    DeviceTurnResult,
    DeviceVoiceSession,
    InvalidDeviceAudio,
    NO_SPEECH_MESSAGE,
    NoSpeechDetected,
)
from showroom_guide.faq_cache import FaqCache, load_cache
from showroom_guide.local_audio import LocalAudioController, LocalAudioError
from showroom_guide.local_device import (
    LastRecordingNotFound,
    LocalDeviceWorkflow,
)
from showroom_guide.models import GuideSnapshot
from showroom_guide.sessions import (
    GuideSession,
    SessionCapacityReached,
    SessionManager,
)
from showroom_guide.state import GuideStateStore


WEB_DIR = Path(__file__).parent / "web"
NO_SPEECH_PROMPT_PATH = (
    Path(__file__).parent / "assets" / "no-speech-detected.wav"
)
SESSION_COOKIE = "showroom_session"


class QuestionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)

    @field_validator("question")
    @classmethod
    def normalize_question(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("问题不能为空")
        return normalized


class QuestionResponse(BaseModel):
    answer: str
    audio_url: str | None
    warning: str | None


class DeviceTurnResponse(BaseModel):
    transcript: str
    answer: str
    audio_url: str | None
    warning: str | None


class DeviceStateResponse(GuideSnapshot):
    has_last_recording: bool


@dataclass
class Runtime:
    sessions: SessionManager
    device: DeviceVoiceSession
    local_device: LocalDeviceWorkflow
    device_api_key: SecretStr
    device_max_upload_bytes: int
    xzkb: XzkbClient
    speech: SpeechClient
    cleanup_seconds: float
    faq_cache: FaqCache | None = None

    async def aclose(self) -> None:
        await self.sessions.clear()
        await self.local_device.aclose()
        await self.xzkb.aclose()
        await self.speech.aclose()


def create_runtime(settings: Settings | None = None) -> Runtime:
    configured = settings or Settings()
    faq_cache = (
        load_cache(configured.faq_cache_file)
        if configured.faq_cache_enabled
        else None
    )
    xzkb = XzkbClient(
        configured.xzkb_base_url,
        configured.xzkb_api_key.get_secret_value(),
        configured.request_timeout_seconds,
        empty_search_response=configured.xzkb_empty_search_response,
    )
    speech = SpeechClient(
        configured.asr_base_url,
        configured.asr_api_key.get_secret_value(),
        configured.asr_model,
        configured.tts_base_url,
        configured.tts_api_key.get_secret_value(),
        configured.tts_model,
        configured.tts_voice,
        configured.tts_speed,
        configured.request_timeout_seconds,
    )
    xzkb_gate = AsyncGate(
        configured.xzkb_concurrency,
        configured.queue_timeout_seconds,
    )
    tts_gate = AsyncGate(
        configured.tts_concurrency,
        configured.queue_timeout_seconds,
    )

    def controller_factory(state: GuideStateStore) -> GuideController:
        return GuideController(
            state,
            xzkb,
            speech,
            xzkb_gate=xzkb_gate,
            tts_gate=tts_gate,
            faq_cache=faq_cache,
        )

    sessions = SessionManager(
        controller_factory=controller_factory,
        max_sessions=configured.max_active_sessions,
        idle_seconds=configured.session_idle_seconds,
        audio_ttl_seconds=configured.audio_ttl_seconds,
        audio_items_per_session=configured.audio_items_per_session,
    )
    device_state = GuideStateStore()
    device = DeviceVoiceSession(
        state=device_state,
        controller=controller_factory(device_state),
        speech=speech,
        audio=AudioStore(
            max_items=configured.audio_items_per_session,
            ttl_seconds=configured.audio_ttl_seconds,
        ),
    )
    local_device = LocalDeviceWorkflow(
        session=device,
        audio=LocalAudioController(
            sample_rate=configured.sample_rate,
            capture_device=configured.capture_device,
            playback_device=configured.playback_device,
            no_speech_prompt=NO_SPEECH_PROMPT_PATH.read_bytes(),
        ),
        max_recording_seconds=configured.local_recording_max_seconds,
        min_recording_seconds=configured.local_recording_min_seconds,
        min_recording_dbfs=configured.local_recording_min_dbfs,
    )
    return Runtime(
        sessions=sessions,
        device=device,
        local_device=local_device,
        device_api_key=configured.device_api_key,
        device_max_upload_bytes=configured.device_max_upload_bytes,
        xzkb=xzkb,
        speech=speech,
        faq_cache=faq_cache,
        cleanup_seconds=configured.session_cleanup_seconds,
    )


def set_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=session_id,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )


async def cleanup_sessions(runtime: Runtime) -> None:
    while True:
        await asyncio.sleep(runtime.cleanup_seconds)
        await runtime.sessions.prune()


def create_app(runtime: Runtime) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        cleanup_task = asyncio.create_task(cleanup_sessions(runtime))
        try:
            yield
        finally:
            cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await cleanup_task
            await runtime.aclose()

    app = FastAPI(title="展厅 AI 讲解", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    async def establish_http_session(
        request: Request,
        response: Response,
    ) -> GuideSession:
        try:
            session, created = await runtime.sessions.get_or_create(
                request.cookies.get(SESSION_COOKIE)
            )
        except SessionCapacityReached as error:
            raise HTTPException(
                status_code=503,
                detail="当前访问人数较多，请稍后再试",
            ) from error
        if created:
            set_session_cookie(response, session.session_id)
        runtime.sessions.touch(session)
        return session

    def require_http_session(request: Request) -> GuideSession:
        session = runtime.sessions.get(request.cookies.get(SESSION_COOKIE))
        if session is None:
            raise HTTPException(
                status_code=401,
                detail="会话已失效，请刷新页面",
            )
        runtime.sessions.touch(session)
        return session

    def require_device_key(
        x_device_key: str | None = Header(default=None, alias="X-Device-Key"),
    ) -> None:
        expected = runtime.device_api_key.get_secret_value().encode("utf-8")
        provided = (x_device_key or "").encode("utf-8")
        if not secrets.compare_digest(provided, expected):
            raise HTTPException(status_code=401, detail="设备凭证无效")

    def device_turn_response(result: DeviceTurnResult) -> DeviceTurnResponse:
        audio_url = (
            f"/api/device/audio/{result.audio_id}"
            if result.audio_id is not None
            else None
        )
        return DeviceTurnResponse(
            transcript=result.transcript,
            answer=result.answer,
            audio_url=audio_url,
            warning=result.warning,
        )

    async def execute_device_turn(
        operation: Awaitable[DeviceTurnResult],
        *,
        busy_detail: str = "已有设备问题正在处理中",
    ) -> DeviceTurnResponse:
        try:
            result = await operation
        except InvalidDeviceAudio as error:
            raise HTTPException(status_code=415, detail=str(error)) from error
        except QuestionInProgress as error:
            detail = str(error) or busy_detail
            raise HTTPException(status_code=409, detail=detail) from error
        except NoSpeechDetected as error:
            raise HTTPException(
                status_code=422,
                detail=str(error) or NO_SPEECH_MESSAGE,
            ) from error
        except DeviceTranscriptionUnavailable as error:
            raise HTTPException(
                status_code=503,
                detail="语音识别暂时不可用，请稍后重试",
            ) from error
        except GuideServiceUnavailable as error:
            detail = (
                "当前使用人数较多，请稍后重试"
                if error.service == "capacity"
                else "知识库暂时不可用，请稍后重试"
            )
            raise HTTPException(status_code=503, detail=detail) from error
        except LocalAudioError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return device_turn_response(result)

    @app.get("/", include_in_schema=False)
    async def index(request: Request) -> FileResponse:
        response = FileResponse(WEB_DIR / "index.html", media_type="text/html")
        await establish_http_session(request, response)
        return response

    @app.get("/device-test", include_in_schema=False)
    async def device_test_page() -> FileResponse:
        response = FileResponse(WEB_DIR / "device-test.html", media_type="text/html")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/questions", response_model=QuestionResponse)
    async def ask_question(
        payload: QuestionRequest,
        request: Request,
    ) -> QuestionResponse:
        session = require_http_session(request)
        try:
            result = await session.controller.ask_text(payload.question)
        except QuestionInProgress as error:
            raise HTTPException(409, "已有问题正在处理中") from error
        except GuideServiceUnavailable as error:
            detail = (
                "当前使用人数较多，请稍后重试"
                if error.service == "capacity"
                else "知识库暂时不可用，请稍后重试"
            )
            raise HTTPException(503, detail) from error

        runtime.sessions.touch(session)
        audio_url = None
        if result.audio is not None:
            audio_url = f"/api/audio/{session.audio.put(result.audio)}"
        return QuestionResponse(
            answer=result.answer,
            audio_url=audio_url,
            warning=result.warning,
        )

    @app.get("/api/audio/{audio_id}")
    async def get_audio(audio_id: str, request: Request) -> Response:
        session = require_http_session(request)
        try:
            audio = session.audio.get(audio_id)
        except AudioNotFound as error:
            raise HTTPException(status_code=404, detail="语音已过期") from error
        return Response(
            content=audio,
            media_type="audio/wav",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/playback-finished", status_code=204)
    async def playback_finished(request: Request) -> Response:
        session = require_http_session(request)
        await session.controller.finish_playback()
        runtime.sessions.touch(session)
        return Response(status_code=204)

    @app.post("/api/session/reset", status_code=204)
    async def reset_session(request: Request) -> Response:
        session = require_http_session(request)
        try:
            await runtime.sessions.reset(session)
        except QuestionInProgress as error:
            raise HTTPException(409, "问题正在处理中，暂时不能重置") from error
        return Response(status_code=204)

    @app.get(
        "/api/device/state",
        response_model=DeviceStateResponse,
        dependencies=[Depends(require_device_key)],
    )
    async def get_device_state() -> DeviceStateResponse:
        return DeviceStateResponse(
            **runtime.device.snapshot.model_dump(),
            has_last_recording=runtime.local_device.has_last_recording,
        )

    @app.get(
        "/api/device/metrics",
        dependencies=[Depends(require_device_key)],
    )
    async def get_device_metrics() -> dict[str, object]:
        return runtime.device.metrics_snapshot()

    @app.post(
        "/api/device/recording/start",
        response_model=GuideSnapshot,
        dependencies=[Depends(require_device_key)],
    )
    async def start_device_recording() -> GuideSnapshot:
        try:
            return await runtime.local_device.start_recording()
        except QuestionInProgress as error:
            detail = str(error) or "设备正在处理上一轮录音"
            raise HTTPException(status_code=409, detail=detail) from error
        except LocalAudioError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.post(
        "/api/device/recording/stop",
        response_model=DeviceTurnResponse,
        dependencies=[Depends(require_device_key)],
    )
    async def stop_device_recording() -> DeviceTurnResponse:
        return await execute_device_turn(
            runtime.local_device.stop_recording(),
            busy_detail="设备正在处理上一轮录音",
        )

    @app.post(
        "/api/device/recording/replay",
        status_code=204,
        dependencies=[Depends(require_device_key)],
    )
    async def replay_device_recording() -> Response:
        try:
            await runtime.local_device.replay_last_recording()
        except LastRecordingNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except QuestionInProgress as error:
            raise HTTPException(
                status_code=409,
                detail="设备正在使用，暂时不能播放录音",
            ) from error
        except LocalAudioError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return Response(status_code=204)

    @app.post(
        "/api/device/turn",
        response_model=DeviceTurnResponse,
        dependencies=[Depends(require_device_key)],
    )
    async def process_device_turn(file: UploadFile = File(...)) -> DeviceTurnResponse:
        try:
            audio = await file.read(runtime.device_max_upload_bytes + 1)
        finally:
            await file.close()
        if len(audio) > runtime.device_max_upload_bytes:
            raise HTTPException(status_code=413, detail="录音文件过大")

        return await execute_device_turn(
            runtime.local_device.process_upload(audio)
        )

    @app.get(
        "/api/device/audio/{audio_id}",
        dependencies=[Depends(require_device_key)],
    )
    async def get_device_audio(audio_id: str) -> Response:
        try:
            audio = runtime.device.get_audio(audio_id)
        except AudioNotFound as error:
            raise HTTPException(status_code=404, detail="语音已过期") from error
        return Response(
            content=audio,
            media_type="audio/wav",
            headers={"Cache-Control": "no-store"},
        )

    @app.post(
        "/api/device/playback-finished",
        status_code=204,
        dependencies=[Depends(require_device_key)],
    )
    async def device_playback_finished() -> Response:
        await runtime.device.finish_playback()
        return Response(status_code=204)

    @app.post(
        "/api/device/reset",
        status_code=204,
        dependencies=[Depends(require_device_key)],
    )
    async def reset_device() -> Response:
        try:
            await runtime.local_device.reset()
        except QuestionInProgress as error:
            raise HTTPException(
                status_code=409,
                detail="设备问题正在处理中，暂时不能重置",
            ) from error
        return Response(status_code=204)

    @app.websocket("/ws")
    async def state_stream(websocket: WebSocket) -> None:
        session = runtime.sessions.get(websocket.cookies.get(SESSION_COOKIE))
        if session is None:
            await websocket.accept()
            await websocket.close(code=1008)
            return
        await websocket.accept()
        await runtime.sessions.connect(session)
        queue = session.state.subscribe()
        try:
            await websocket.send_json(
                session.state.snapshot.model_dump(mode="json")
            )
            while True:
                snapshot = await queue.get()
                await websocket.send_json(snapshot.model_dump(mode="json"))
        except WebSocketDisconnect:
            pass
        finally:
            session.state.unsubscribe(queue)
            await runtime.sessions.disconnect(session)

    return app


def create_configured_app() -> FastAPI:
    return create_app(create_runtime())
