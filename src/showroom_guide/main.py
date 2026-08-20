import asyncio
import secrets
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Awaitable, Literal

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
from starlette.concurrency import run_in_threadpool

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
from showroom_guide.faq_audio import tts_profile_from_settings
from showroom_guide.faq_admin import (
    FaqAdminAudioAction,
    FaqAdminConfigError,
    FaqAdminDeleteRequest,
    FaqAdminEntryAction,
    FaqAdminEntryCreate,
    FaqAdminEntryNotFound,
    FaqAdminEntryUpdate,
    FaqAdminOperationConflict,
    FaqAdminSynthesisError,
    FaqAdminUnavailable,
    FaqCacheReadService,
    FaqCacheSnapshot,
)
from showroom_guide.local_audio import LocalAudioController, LocalAudioError
from showroom_guide.local_device import (
    LastRecordingNotFound,
    LocalDeviceWorkflow,
)
from showroom_guide.models import GuideSnapshot
from showroom_guide.prepared_audio import PreparedAudioStore
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
    prepared_audio: PreparedAudioStore | None = None
    faq_admin_service: FaqCacheReadService | None = None
    faq_admin_api_key: SecretStr | None = None

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
    tts_profile = tts_profile_from_settings(configured)
    prepared_audio = (
        PreparedAudioStore(
            faq_cache,
            configured.faq_cache_file,
            tts_profile,
        )
        if faq_cache is not None and configured.faq_prepared_audio_enabled
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
    faq_admin_service = (
        FaqCacheReadService(configured.faq_cache_file, tts_profile, speech)
        if configured.faq_admin_enabled
        else None
    )
    faq_admin_api_key = (
        configured.faq_admin_api_key if configured.faq_admin_enabled else None
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
            prepared_audio=prepared_audio,
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
        prepared_audio=prepared_audio,
        faq_admin_service=faq_admin_service,
        faq_admin_api_key=faq_admin_api_key,
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

    def require_faq_admin_key(
        response: Response,
        x_faq_admin_key: str | None = Header(
            default=None,
            alias="X-FAQ-Admin-Key",
        ),
    ) -> None:
        response.headers["Cache-Control"] = "no-store"
        if runtime.faq_admin_service is None or runtime.faq_admin_api_key is None:
            raise HTTPException(
                status_code=404,
                detail="Not Found",
                headers={"Cache-Control": "no-store"},
            )
        expected = runtime.faq_admin_api_key.get_secret_value().encode("utf-8")
        provided = (x_faq_admin_key or "").encode("utf-8")
        if not secrets.compare_digest(provided, expected):
            raise HTTPException(
                status_code=401,
                detail="FAQ admin key invalid",
                headers={"Cache-Control": "no-store"},
            )

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

    @app.get("/faq-cache", include_in_schema=False)
    async def faq_cache_page() -> FileResponse:
        response = FileResponse(WEB_DIR / "faq-cache.html", media_type="text/html")
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

    @app.get(
        "/api/faq-cache",
        response_model=FaqCacheSnapshot,
        dependencies=[Depends(require_faq_admin_key)],
    )
    async def get_faq_cache(response: Response) -> FaqCacheSnapshot:
        service = runtime.faq_admin_service
        if service is None:
            raise HTTPException(
                status_code=404,
                detail="Not Found",
                headers={"Cache-Control": "no-store"},
            )
        try:
            snapshot = await run_in_threadpool(service.snapshot)
        except FaqAdminConfigError as error:
            raise HTTPException(
                status_code=422,
                detail="\u9ad8\u9891\u95ee\u7b54\u914d\u7f6e\u6821\u9a8c\u5931\u8d25",
                headers={"Cache-Control": "no-store"},
            ) from error
        except FaqAdminUnavailable as error:
            raise HTTPException(
                status_code=503,
                detail="\u9ad8\u9891\u95ee\u7b54\u914d\u7f6e\u6682\u65f6\u65e0\u6cd5\u8bfb\u53d6",
                headers={"Cache-Control": "no-store"},
            ) from error
        response.headers["Cache-Control"] = "no-store"
        return snapshot

    @app.post(
        "/api/faq-cache/entries",
        response_model=FaqAdminEntryAction,
        dependencies=[Depends(require_faq_admin_key)],
    )
    async def create_faq_entry(
        payload: FaqAdminEntryCreate,
        response: Response,
    ) -> FaqAdminEntryAction:
        service = runtime.faq_admin_service
        assert service is not None
        try:
            result = await service.create_entry(payload)
        except FaqAdminOperationConflict as error:
            raise HTTPException(
                409,
                "条目 ID 已存在",
                headers={"Cache-Control": "no-store"},
            ) from error
        except FaqAdminConfigError as error:
            raise HTTPException(
                422,
                "高频问答配置校验失败",
                headers={"Cache-Control": "no-store"},
            ) from error
        except FaqAdminUnavailable as error:
            raise HTTPException(
                503,
                "高频问答配置暂时无法写入",
                headers={"Cache-Control": "no-store"},
            ) from error
        response.headers["Cache-Control"] = "no-store"
        return result

    @app.put(
        "/api/faq-cache/{entry_id}",
        response_model=FaqAdminEntryAction,
        dependencies=[Depends(require_faq_admin_key)],
    )
    async def update_faq_entry(
        entry_id: str,
        payload: FaqAdminEntryUpdate,
        response: Response,
    ) -> FaqAdminEntryAction:
        service = runtime.faq_admin_service
        assert service is not None
        try:
            result = await service.update_entry(entry_id, payload)
        except FaqAdminEntryNotFound as error:
            raise HTTPException(
                404,
                "高频问答条目不存在",
                headers={"Cache-Control": "no-store"},
            ) from error
        except FaqAdminOperationConflict as error:
            raise HTTPException(
                409,
                "条目已被其他操作修改，请刷新后重试",
                headers={"Cache-Control": "no-store"},
            ) from error
        except FaqAdminConfigError as error:
            raise HTTPException(
                422,
                "高频问答配置校验失败，请检查别名和匹配规则",
                headers={"Cache-Control": "no-store"},
            ) from error
        except FaqAdminUnavailable as error:
            raise HTTPException(
                503,
                "高频问答配置暂时无法写入",
                headers={"Cache-Control": "no-store"},
            ) from error
        response.headers["Cache-Control"] = "no-store"
        return result

    @app.delete(
        "/api/faq-cache/{entry_id}",
        response_model=FaqAdminEntryAction,
        dependencies=[Depends(require_faq_admin_key)],
    )
    async def delete_faq_entry(
        entry_id: str,
        payload: FaqAdminDeleteRequest,
        response: Response,
    ) -> FaqAdminEntryAction:
        service = runtime.faq_admin_service
        assert service is not None
        try:
            result = await service.delete_entry(
                entry_id,
                payload.expected_edit_token,
            )
        except FaqAdminEntryNotFound as error:
            raise HTTPException(
                404,
                "高频问答条目不存在",
                headers={"Cache-Control": "no-store"},
            ) from error
        except FaqAdminOperationConflict as error:
            raise HTTPException(
                409,
                "条目已变化或不能删除最后一个条目",
                headers={"Cache-Control": "no-store"},
            ) from error
        except FaqAdminConfigError as error:
            raise HTTPException(
                422,
                "高频问答配置校验失败",
                headers={"Cache-Control": "no-store"},
            ) from error
        except FaqAdminUnavailable as error:
            raise HTTPException(
                503,
                "高频问答配置暂时无法写入",
                headers={"Cache-Control": "no-store"},
            ) from error
        response.headers["Cache-Control"] = "no-store"
        return result

    @app.post(
        "/api/faq-cache/{entry_id}/audio/generate",
        response_model=FaqAdminAudioAction,
        dependencies=[Depends(require_faq_admin_key)],
    )
    async def generate_faq_audio(
        entry_id: str,
        response: Response,
    ) -> FaqAdminAudioAction:
        service = runtime.faq_admin_service
        assert service is not None
        try:
            result = await service.generate_draft(entry_id)
        except FaqAdminEntryNotFound as error:
            raise HTTPException(404, "高频问答条目不存在", headers={"Cache-Control": "no-store"}) from error
        except FaqAdminOperationConflict as error:
            raise HTTPException(409, "当前条目不能生成语音", headers={"Cache-Control": "no-store"}) from error
        except FaqAdminSynthesisError as error:
            raise HTTPException(503, "语音合成暂时不可用", headers={"Cache-Control": "no-store"}) from error
        except (FaqAdminConfigError, FaqAdminUnavailable) as error:
            raise HTTPException(503, "高频问答配置暂时无法读取", headers={"Cache-Control": "no-store"}) from error
        response.headers["Cache-Control"] = "no-store"
        return result

    @app.get(
        "/api/faq-cache/{entry_id}/audio",
        dependencies=[Depends(require_faq_admin_key)],
    )
    async def get_faq_admin_audio(
        entry_id: str,
        source: Literal["draft", "active"] = "draft",
    ) -> Response:
        service = runtime.faq_admin_service
        assert service is not None
        try:
            content = await run_in_threadpool(service.get_audio, entry_id, source)
        except FaqAdminEntryNotFound as error:
            raise HTTPException(404, "高频问答条目不存在", headers={"Cache-Control": "no-store"}) from error
        except FaqAdminOperationConflict as error:
            raise HTTPException(404, "可试听语音不存在", headers={"Cache-Control": "no-store"}) from error
        except (FaqAdminConfigError, FaqAdminUnavailable) as error:
            raise HTTPException(503, "高频问答配置暂时无法读取", headers={"Cache-Control": "no-store"}) from error
        return Response(
            content=content,
            media_type="audio/wav",
            headers={"Cache-Control": "no-store"},
        )

    @app.post(
        "/api/faq-cache/{entry_id}/audio/approve",
        response_model=FaqAdminAudioAction,
        dependencies=[Depends(require_faq_admin_key)],
    )
    async def approve_faq_audio(
        entry_id: str,
        response: Response,
    ) -> FaqAdminAudioAction:
        service = runtime.faq_admin_service
        assert service is not None
        try:
            result = await service.approve_draft(entry_id)
        except FaqAdminEntryNotFound as error:
            raise HTTPException(404, "高频问答条目不存在", headers={"Cache-Control": "no-store"}) from error
        except FaqAdminOperationConflict as error:
            raise HTTPException(409, "没有可审批的最新语音草稿", headers={"Cache-Control": "no-store"}) from error
        except (FaqAdminConfigError, FaqAdminUnavailable) as error:
            raise HTTPException(503, "语音安装失败，原正式文件保持不变", headers={"Cache-Control": "no-store"}) from error
        response.headers["Cache-Control"] = "no-store"
        return result

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
