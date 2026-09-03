import asyncio
import io
import logging
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
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr, field_validator
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from showroom_guide.audio_store import AudioByteBudget, AudioNotFound, AudioStore
from showroom_guide.audio_devices import (
    AudioDeviceMonitor,
    AudioDeviceStatus,
    probe_pipewire_devices,
)
from showroom_guide.async_outbox import AsyncKnowledgeOutbox
from showroom_guide.button_workflow import DeviceButtonWorkflow
from showroom_guide.clients.speech import SpeechClient
from showroom_guide.clients.xzkb import XzkbClient
from showroom_guide.clients.verdict import VerdictClient
from showroom_guide.clients.xzkb_auth import XzkbLocalAccountAuth
from showroom_guide.clients.xzkb_knowledge import XzkbKnowledgeClient
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
    validate_wav,
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
from showroom_guide.gpio_button import GpioButtonService
from showroom_guide.knowledge_capture import KnowledgeCaptureSession
from showroom_guide.knowledge_mode import (
    KnowledgeModeState,
    KnowledgeModeWorkflow,
    KnowledgeProcessingStage,
)
from showroom_guide.knowledge_outbox import KnowledgeOutbox
from showroom_guide.knowledge_sync import KnowledgeSyncService
from showroom_guide.knowledge_web import (
    KnowledgeControlState,
    KnowledgeEntrySnapshot,
    KnowledgeSyncState,
    KnowledgeWebController,
    KnowledgeWebError,
    KnowledgeWebState,
)
from showroom_guide.local_audio import LocalAudioController, LocalAudioError
from showroom_guide.local_device import (
    LastRecordingNotFound,
    LocalDeviceWorkflow,
)
from showroom_guide.models import GuideSnapshot, InteractionMode
from showroom_guide.prepared_audio import PreparedAudioStore
from showroom_guide.sessions import (
    GuideSession,
    SessionCapacityReached,
    SessionManager,
)
from showroom_guide.servo_motion import (
    GpioZeroServoDriver,
    ServoMotionOutput,
)
from showroom_guide.state import GuideStateStore
from showroom_guide.verdict_motion import WebSimulationOutput
from showroom_guide.verdict_workflow import VerdictInProgress, VerdictWorkflow


WEB_DIR = Path(__file__).parent / "web"
NO_SPEECH_PROMPT_PATH = (
    Path(__file__).parent / "assets" / "no-speech-detected.wav"
)
LOCAL_PROMPT_PATHS = {
    "asr-unavailable": Path(__file__).parent / "assets" / "asr-unavailable.wav",
    "guide-unavailable": Path(__file__).parent / "assets" / "guide-unavailable.wav",
    "tts-unavailable": Path(__file__).parent / "assets" / "tts-unavailable.wav",
    "knowledge-mode": Path(__file__).parent / "assets" / "knowledge-mode.wav",
    "knowledge-saved": Path(__file__).parent / "assets" / "knowledge-saved.wav",
    "verdict-invalid": Path(__file__).parent / "assets" / "verdict-invalid.wav",
    "verdict-insufficient-evidence": (
        Path(__file__).parent / "assets" / "verdict-insufficient-evidence.wav"
    ),
    "verdict-high-risk": Path(__file__).parent / "assets" / "verdict-high-risk.wav",
    "verdict-unavailable": (
        Path(__file__).parent / "assets" / "verdict-unavailable.wav"
    ),
}
SESSION_COOKIE = "showroom_session"
logger = logging.getLogger(__name__)


class QuestionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)

    @field_validator("question")
    @classmethod
    def normalize_question(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("问题不能为空")
        return normalized


class _KnowledgeUploadTooLarge(MultiPartException):
    pass


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
    capture_available: bool
    playback_available: bool
    capture_name: str | None
    playback_name: str | None
    audio_device_error: str | None


class KnowledgeStateResponse(BaseModel):
    enabled: bool
    mode_state: KnowledgeModeState
    processing_stage: KnowledgeProcessingStage | None
    control_state: KnowledgeControlState
    lease_expires_at: float | None
    draft_text: str | None
    last_entry_id: str | None


class KnowledgeAcquireResponse(BaseModel):
    lease_token: str
    knowledge_state: KnowledgeStateResponse


class KnowledgeUploadResponse(BaseModel):
    knowledge_state: KnowledgeStateResponse
    review_audio_url: str


class KnowledgeEntryResponse(BaseModel):
    entry_id: str
    sync_state: KnowledgeSyncState
    attempts: int
    last_error: str | None
    next_attempt_at: float
    updated_at: float


class KnowledgeErrorResponse(BaseModel):
    code: str
    detail: str
    knowledge_state: KnowledgeStateResponse


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
    knowledge_outbox: AsyncKnowledgeOutbox | None
    knowledge_web: KnowledgeWebController
    audio_devices: AudioDeviceMonitor | None = None
    faq_cache: FaqCache | None = None
    prepared_audio: PreparedAudioStore | None = None
    faq_admin_service: FaqCacheReadService | None = None
    faq_admin_api_key: SecretStr | None = None
    knowledge_mode: KnowledgeModeWorkflow | None = None
    knowledge_sync: KnowledgeSyncService | None = None
    button_workflow: DeviceButtonWorkflow | None = None
    gpio_button: GpioButtonService | None = None
    servo_motion: ServoMotionOutput | None = None
    verdict_client: VerdictClient | None = None
    verdict_workflow: VerdictWorkflow | None = None
    cleanup_task: asyncio.Task[None] | None = None

    async def aclose(self) -> None:
        errors: list[BaseException] = []
        cleanup_task = asyncio.create_task(
            self._close_resources(errors),
            name="runtime-close-cleanup",
        )
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError as error:
                if not errors:
                    errors.append(error)
                if cleanup_task.cancelled():
                    break
            except BaseException as error:
                if not errors:
                    errors.append(error)
                break
        if cleanup_task.done():
            try:
                cleanup_task.result()
            except BaseException as error:
                if not errors:
                    errors.append(error)
        if errors:
            raise errors[0]

    async def _close_resources(self, errors: list[BaseException]) -> None:
        async def attempt(operation: Awaitable[None]) -> None:
            try:
                await operation
            except BaseException as error:
                errors.append(error)

        if self.gpio_button is not None:
            await attempt(self.gpio_button.aclose())
        if self.verdict_workflow is not None:
            await attempt(self.verdict_workflow.leave())
        if self.servo_motion is not None:
            await attempt(self.servo_motion.aclose())
        if self.verdict_client is not None:
            await attempt(self.verdict_client.aclose())
        if self.audio_devices is not None:
            await attempt(self.audio_devices.aclose())
        await attempt(self.knowledge_web.aclose())
        if self.knowledge_mode is not None:
            await attempt(self.knowledge_mode.aclose())
        await attempt(self.sessions.clear())
        await attempt(self.local_device.aclose())
        if self.knowledge_sync is not None:
            await attempt(self.knowledge_sync.aclose())
        if self.knowledge_outbox is not None:
            await attempt(self.knowledge_outbox.aclose())
        await attempt(self.xzkb.aclose())
        await attempt(self.speech.aclose())
        if errors:
            raise errors[0]


def create_runtime(settings: Settings | None = None) -> Runtime:
    configured = settings or Settings()
    knowledge_credentials = None
    if configured.knowledge_capture_enabled:
        username = configured.xzkb_username
        password = configured.xzkb_password
        kb_id = configured.xzkb_knowledge_base_id
        if (
            username is None
            or not username.strip()
            or password is None
            or not password.get_secret_value().strip()
            or kb_id is None
        ):
            raise ValueError(
                "启用知识补充时必须配置 XZKB 专用账号、密码和知识库 ID"
            )
        knowledge_credentials = (username, password, kb_id)
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
        configured.asr_timeout_seconds,
        configured.tts_timeout_seconds,
    )
    faq_admin_service = (
        FaqCacheReadService(
            configured.faq_cache_file,
            tts_profile,
            speech,
            pending_dir=configured.faq_pending_audio_dir,
            migrate_legacy_pending=True,
        )
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

    audio_budget = AudioByteBudget(configured.audio_total_bytes)

    def controller_factory(state: GuideStateStore) -> GuideController:
        return GuideController(
            state,
            xzkb,
            speech,
            xzkb_gate=xzkb_gate,
            tts_gate=tts_gate,
            faq_cache=faq_cache,
            prepared_audio=prepared_audio,
            playback_timeout_seconds=configured.playback_timeout_seconds,
            xzkb_total_timeout_seconds=configured.xzkb_total_timeout_seconds,
            answer_max_chars=configured.answer_max_chars,
            tts_audio_max_bytes=configured.audio_max_item_bytes,
        )

    verdict_client = None
    if configured.verdict_enabled:
        assert configured.verdict_base_url is not None
        assert configured.verdict_api_key is not None
        verdict_client = VerdictClient(
            configured.verdict_base_url,
            configured.verdict_api_key.get_secret_value(),
            configured.verdict_timeout_seconds,
        )

    def web_verdict_factory(state: GuideStateStore) -> VerdictWorkflow:
        assert verdict_client is not None
        return VerdictWorkflow(
            verdict_client,
            WebSimulationOutput(state),
            state,
            timeout_seconds=configured.verdict_timeout_seconds,
        )

    sessions = SessionManager(
        controller_factory=controller_factory,
        max_sessions=configured.max_active_sessions,
        idle_seconds=configured.session_idle_seconds,
        audio_ttl_seconds=configured.audio_ttl_seconds,
        audio_items_per_session=configured.audio_items_per_session,
        verdict_factory=(
            web_verdict_factory if verdict_client is not None else None
        ),
        audio_max_item_bytes=configured.audio_max_item_bytes,
        audio_budget=audio_budget,
    )
    device_state = GuideStateStore()
    servo_motion = None
    if configured.servo_enabled:
        try:
            servo_driver = GpioZeroServoDriver(
                pin=configured.servo_pin,
                min_angle=configured.servo_min_angle,
                max_angle=configured.servo_max_angle,
                min_pulse_width=(
                    configured.servo_min_pulse_width_seconds
                ),
                max_pulse_width=(
                    configured.servo_max_pulse_width_seconds
                ),
            )
            servo_motion = ServoMotionOutput(
                servo_driver,
                yes_angle=configured.servo_yes_angle,
                neutral_angle=configured.servo_neutral_angle,
                no_angle=configured.servo_no_angle,
            )
        except Exception:
            logger.exception("servo_initialization_failed")

    async def probe_audio_devices() -> AudioDeviceStatus:
        return await probe_pipewire_devices(
            capture_target=configured.capture_device,
            playback_target=configured.playback_device,
        )

    audio_devices = AudioDeviceMonitor(probe=probe_audio_devices)

    async def ensure_local_audio_available() -> None:
        status = await audio_devices.refresh()
        if not status.capture_available:
            raise LocalAudioError("未检测到可用麦克风")
        if not status.playback_available:
            raise LocalAudioError("未检测到可用扬声器")

    async def ensure_playback_available() -> None:
        status = await audio_devices.refresh()
        if not status.playback_available:
            raise LocalAudioError("未检测到可用扬声器")

    local_audio = LocalAudioController(
        sample_rate=configured.sample_rate,
        capture_device=configured.capture_device,
        playback_device=configured.playback_device,
        no_speech_prompt=NO_SPEECH_PROMPT_PATH.read_bytes(),
        prompts={
            name: path.read_bytes()
            for name, path in LOCAL_PROMPT_PATHS.items()
            if path.exists()
        },
        ensure_capture_available=ensure_local_audio_available,
        ensure_playback_available=ensure_playback_available,
        max_recording_bytes=configured.local_recording_max_bytes,
    )
    verdict_workflow = None
    if verdict_client is not None:
        verdict_workflow = VerdictWorkflow(
            verdict_client,
            servo_motion or WebSimulationOutput(device_state),
            device_state,
            play_prompt=local_audio.play_prompt,
            timeout_seconds=configured.verdict_timeout_seconds,
        )
    device = DeviceVoiceSession(
        state=device_state,
        controller=controller_factory(device_state),
        speech=speech,
        audio=AudioStore(
            max_items=configured.audio_items_per_session,
            ttl_seconds=configured.audio_ttl_seconds,
            max_item_bytes=configured.audio_max_item_bytes,
            budget=audio_budget,
        ),
        verdict_workflow=verdict_workflow,
    )
    local_device = LocalDeviceWorkflow(
        session=device,
        audio=local_audio,
        max_recording_seconds=configured.local_recording_max_seconds,
        min_recording_seconds=configured.local_recording_min_seconds,
        min_recording_dbfs=configured.local_recording_min_dbfs,
        before_recording=(
            servo_motion.prepare_recording
            if servo_motion is not None
            else None
        ),
    )
    knowledge_mode = None
    knowledge_outbox = None
    knowledge_sync = None
    if knowledge_credentials is not None:
        username, password, kb_id = knowledge_credentials
        knowledge_outbox = AsyncKnowledgeOutbox(
            KnowledgeOutbox(configured.knowledge_outbox_path)
        )
        knowledge_auth = XzkbLocalAccountAuth(
            configured.xzkb_base_url,
            username,
            password.get_secret_value(),
            timeout=configured.request_timeout_seconds,
        )
        knowledge_client = XzkbKnowledgeClient(
            configured.xzkb_base_url,
            knowledge_auth,
            str(kb_id),
            folder_id=(
                str(configured.xzkb_knowledge_folder_id)
                if configured.xzkb_knowledge_folder_id is not None
                else None
            ),
            timeout=configured.request_timeout_seconds,
        )
        knowledge_sync = KnowledgeSyncService(
            knowledge_outbox,
            knowledge_client,
            poll_seconds=configured.knowledge_sync_interval_seconds,
        )
        capture_session = KnowledgeCaptureSession(
            speech,
            knowledge_outbox,
            knowledge_sync,
        )
        knowledge_mode = KnowledgeModeWorkflow(
            local_audio,
            capture_session,
            max_recording_seconds=configured.local_recording_max_seconds,
            min_recording_seconds=configured.local_recording_min_seconds,
            min_recording_dbfs=configured.local_recording_min_dbfs,
            before_recording=(
                servo_motion.prepare_recording
                if servo_motion is not None
                else None
            ),
        )
    button_workflow = None
    if (
        configured.gpio_button_enabled
        or knowledge_mode is not None
        or verdict_workflow is not None
    ):
        button_workflow = DeviceButtonWorkflow(
            local_device,
            knowledge_mode,
            verdict_workflow,
            state=device_state,
        )
    gpio_button = None
    if configured.gpio_button_enabled:
        gpio_button = GpioButtonService(
            pin=configured.ptt_pin,
            hold_seconds=configured.button_hold_seconds,
            workflow=button_workflow,
        )
    knowledge_web = KnowledgeWebController(
        button_workflow if knowledge_mode is not None else None,
        knowledge_mode,
        knowledge_outbox,
        lease_seconds=configured.knowledge_web_lease_seconds,
    )
    return Runtime(
        sessions=sessions,
        device=device,
        local_device=local_device,
        device_api_key=configured.device_api_key,
        device_max_upload_bytes=configured.device_max_upload_bytes,
        xzkb=xzkb,
        speech=speech,
        knowledge_outbox=knowledge_outbox,
        knowledge_web=knowledge_web,
        audio_devices=audio_devices,
        faq_cache=faq_cache,
        prepared_audio=prepared_audio,
        faq_admin_service=faq_admin_service,
        faq_admin_api_key=faq_admin_api_key,
        cleanup_seconds=configured.session_cleanup_seconds,
        knowledge_mode=knowledge_mode,
        knowledge_sync=knowledge_sync,
        button_workflow=button_workflow,
        gpio_button=gpio_button,
        servo_motion=servo_motion,
        verdict_client=verdict_client,
        verdict_workflow=verdict_workflow,
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
    retry_seconds = runtime.cleanup_seconds
    while True:
        await asyncio.sleep(retry_seconds)
        try:
            await runtime.sessions.prune()
            retry_seconds = runtime.cleanup_seconds
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("session_cleanup_cycle_failed")
            retry_seconds = min(
                retry_seconds * 2,
                runtime.cleanup_seconds * 8,
            )


def create_app(runtime: Runtime) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        cleanup_task = asyncio.create_task(cleanup_sessions(runtime))
        runtime.cleanup_task = cleanup_task
        knowledge_sync = getattr(runtime, "knowledge_sync", None)
        gpio_button = getattr(runtime, "gpio_button", None)
        servo_motion = getattr(runtime, "servo_motion", None)
        audio_devices = getattr(runtime, "audio_devices", None)
        if knowledge_sync is not None:
            knowledge_sync.start()
        if gpio_button is not None:
            try:
                gpio_button.start()
            except Exception:
                logger.exception("gpio_button_start_failed")
        if servo_motion is not None:
            await servo_motion.enter_mode()
        if audio_devices is not None:
            audio_devices.start()
        try:
            yield
        finally:
            cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await cleanup_task
            runtime.cleanup_task = None
            await runtime.aclose()

    app = FastAPI(title="展厅 AI 讲解", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    def knowledge_state_response(
        state: KnowledgeWebState,
    ) -> KnowledgeStateResponse:
        return KnowledgeStateResponse.model_validate(state, from_attributes=True)

    def knowledge_entry_response(
        entry: KnowledgeEntrySnapshot,
    ) -> KnowledgeEntryResponse:
        return KnowledgeEntryResponse.model_validate(entry, from_attributes=True)

    @app.exception_handler(KnowledgeWebError)
    async def handle_knowledge_web_error(
        _request: Request,
        error: KnowledgeWebError,
    ) -> JSONResponse:
        payload = KnowledgeErrorResponse(
            code=error.code,
            detail=error.detail,
            knowledge_state=knowledge_state_response(error.state),
        )
        return JSONResponse(
            status_code=error.status_code,
            content=payload.model_dump(mode="json"),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/healthz", include_in_schema=False)
    async def healthcheck() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readiness() -> JSONResponse:
        cleanup_task = getattr(runtime, "cleanup_task", None)
        checks = {
            "session_cleanup": (
                "ok"
                if cleanup_task is not None and not cleanup_task.done()
                else "failed"
            ),
            "knowledge_sync": "disabled",
            "knowledge_outbox": "disabled",
        }
        knowledge_sync = getattr(runtime, "knowledge_sync", None)
        if knowledge_sync is not None:
            checks["knowledge_sync"] = (
                "ok" if knowledge_sync.is_running else "failed"
            )
        knowledge_outbox = getattr(runtime, "knowledge_outbox", None)
        if knowledge_outbox is not None:
            try:
                await knowledge_outbox.ping()
            except Exception:
                checks["knowledge_outbox"] = "failed"
            else:
                checks["knowledge_outbox"] = "ok"
        ready = all(value != "failed" for value in checks.values())
        monitor = getattr(runtime, "audio_devices", None)
        audio_status = (
            monitor.status
            if monitor is not None
            else AudioDeviceStatus.unavailable("未配置音频设备监测")
        )
        return JSONResponse(
            {
                "status": "ready" if ready else "not_ready",
                "checks": checks,
                "local_audio": {
                    "capture_available": audio_status.capture_available,
                    "playback_available": audio_status.playback_available,
                    "capture_name": audio_status.capture_name,
                    "playback_name": audio_status.playback_name,
                    "error": audio_status.error,
                },
            },
            status_code=200 if ready else 503,
            headers={"Cache-Control": "no-store"},
        )

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

    def knowledge_lease_header(
        x_knowledge_lease: str | None = Header(
            default=None,
            alias="X-Knowledge-Lease",
        ),
    ) -> str | None:
        return x_knowledge_lease

    async def require_knowledge_lease(lease_token: str | None) -> str:
        if lease_token is not None:
            return lease_token
        state = await runtime.knowledge_web.state()
        raise KnowledgeWebError(
            "knowledge_lease_expired",
            "知识补充控制权已过期",
            409,
            state,
        )

    async def read_knowledge_upload(request: Request) -> bytes:
        request_limit = runtime.device_max_upload_bytes + 64 * 1024

        async def limited_stream() -> AsyncIterator[bytes]:
            received = 0
            async for chunk in request.stream():
                received += len(chunk)
                if received > request_limit:
                    raise _KnowledgeUploadTooLarge("录音文件过大")
                yield chunk

        form = None
        try:
            parser = MultiPartParser(
                request.headers,
                limited_stream(),
                max_files=1,
                max_fields=0,
            )
            parser.spool_max_size = request_limit
            parser.max_file_size = request_limit
            try:
                form = await parser.parse()
            except _KnowledgeUploadTooLarge as error:
                raise HTTPException(
                    status_code=413,
                    detail="录音文件过大",
                ) from error
            except MultiPartException as error:
                raise HTTPException(
                    status_code=400,
                    detail="上传表单格式无效",
                ) from error
            except (KeyError, ValueError) as error:
                raise HTTPException(
                    status_code=400,
                    detail="上传表单格式无效",
                ) from error

            items = form.multi_items()
            if (
                len(items) != 1
                or items[0][0] != "file"
                or not isinstance(items[0][1], StarletteUploadFile)
            ):
                raise HTTPException(
                    status_code=400,
                    detail="上传表单格式无效",
                )
            upload = items[0][1]
            return await upload.read(runtime.device_max_upload_bytes + 1)
        finally:
            if form is not None:
                await form.close()

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

    async def run_device_dialogue(operation):
        button_workflow = getattr(runtime, "button_workflow", None)
        if button_workflow is None:
            return await operation()
        try:
            return await button_workflow.run_dialogue(operation)
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

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
        monitor = getattr(runtime, "audio_devices", None)
        audio_status = (
            monitor.status
            if monitor is not None
            else AudioDeviceStatus.unavailable("未配置音频设备监测")
        )
        return DeviceStateResponse(
            **runtime.device.snapshot.model_dump(),
            has_last_recording=runtime.local_device.has_last_recording,
            capture_available=audio_status.capture_available,
            playback_available=audio_status.playback_available,
            capture_name=audio_status.capture_name,
            playback_name=audio_status.playback_name,
            audio_device_error=audio_status.error,
        )

    @app.post(
        "/api/device/verdict/turn",
        response_model=GuideSnapshot,
        dependencies=[Depends(require_device_key)],
    )
    async def process_web_verdict_turn(
        request: Request,
        response: Response,
        file: UploadFile = File(...),
    ) -> GuideSnapshot:
        session = await establish_http_session(request, response)
        async with runtime.sessions.protect(session):
            workflow = session.verdict_workflow
            if workflow is None:
                raise HTTPException(status_code=404, detail="是非判断功能未启用")
            try:
                audio = await file.read(runtime.device_max_upload_bytes + 1)
            finally:
                await file.close()
            if len(audio) > runtime.device_max_upload_bytes:
                raise HTTPException(status_code=413, detail="录音文件过大")
            try:
                validate_wav(audio)
                transcript = (
                    await runtime.speech.transcribe(io.BytesIO(audio))
                ).strip()
            except InvalidDeviceAudio as error:
                raise HTTPException(status_code=415, detail=str(error)) from error
            except (httpx.HTTPError, ValueError) as error:
                raise HTTPException(
                    status_code=503,
                    detail="语音识别暂时不可用，请稍后重试",
                ) from error
            if not transcript:
                raise HTTPException(status_code=422, detail=NO_SPEECH_MESSAGE)
            if session.state.snapshot.interaction_mode is not InteractionMode.VERDICT:
                await workflow.enter()
            try:
                await workflow.run(transcript)
            except VerdictInProgress as error:
                raise HTTPException(
                    status_code=409,
                    detail="已有是非判断正在处理中",
                ) from error
            return session.state.snapshot

    @app.post(
        "/api/device/knowledge/acquire",
        response_model=KnowledgeAcquireResponse,
        dependencies=[Depends(require_device_key)],
    )
    async def acquire_knowledge_control() -> KnowledgeAcquireResponse:
        lease_token, state = await runtime.knowledge_web.acquire()
        return KnowledgeAcquireResponse(
            lease_token=lease_token,
            knowledge_state=knowledge_state_response(state),
        )

    @app.get(
        "/api/device/knowledge/state",
        response_model=KnowledgeStateResponse,
        dependencies=[Depends(require_device_key)],
    )
    async def get_knowledge_state(
        lease_token: str | None = Depends(knowledge_lease_header),
    ) -> KnowledgeStateResponse:
        state = await runtime.knowledge_web.state(lease_token)
        return knowledge_state_response(state)

    @app.post(
        "/api/device/knowledge/upload",
        response_model=KnowledgeUploadResponse,
        dependencies=[Depends(require_device_key)],
    )
    async def upload_knowledge(
        request: Request,
        response: Response,
        lease_token: str | None = Depends(knowledge_lease_header),
    ) -> KnowledgeUploadResponse:
        token = await require_knowledge_lease(lease_token)
        audio = await read_knowledge_upload(request)
        if len(audio) > runtime.device_max_upload_bytes:
            raise HTTPException(status_code=413, detail="录音文件过大")

        state = await runtime.knowledge_web.review_upload(token, audio)
        response.headers["Cache-Control"] = "no-store"
        return KnowledgeUploadResponse(
            knowledge_state=knowledge_state_response(state),
            review_audio_url="/api/device/knowledge/review-audio",
        )

    @app.get(
        "/api/device/knowledge/review-audio",
        dependencies=[Depends(require_device_key)],
    )
    async def get_knowledge_review_audio(
        lease_token: str | None = Depends(knowledge_lease_header),
    ) -> Response:
        token = await require_knowledge_lease(lease_token)
        audio = await runtime.knowledge_web.review_audio(token)
        return Response(
            content=audio,
            media_type="audio/wav",
            headers={"Cache-Control": "no-store"},
        )

    @app.post(
        "/api/device/knowledge/short-press",
        response_model=KnowledgeStateResponse,
        dependencies=[Depends(require_device_key)],
    )
    async def short_press_knowledge(
        lease_token: str | None = Depends(knowledge_lease_header),
    ) -> KnowledgeStateResponse:
        token = await require_knowledge_lease(lease_token)
        state = await runtime.knowledge_web.short_press(token)
        return knowledge_state_response(state)

    @app.post(
        "/api/device/knowledge/long-press",
        response_model=KnowledgeStateResponse,
        dependencies=[Depends(require_device_key)],
    )
    async def long_press_knowledge(
        lease_token: str | None = Depends(knowledge_lease_header),
    ) -> KnowledgeStateResponse:
        token = await require_knowledge_lease(lease_token)
        state = await runtime.knowledge_web.long_press(token)
        return knowledge_state_response(state)

    @app.post(
        "/api/device/knowledge/release",
        response_model=KnowledgeStateResponse,
        dependencies=[Depends(require_device_key)],
    )
    async def release_knowledge_control(
        lease_token: str | None = Depends(knowledge_lease_header),
    ) -> KnowledgeStateResponse:
        token = await require_knowledge_lease(lease_token)
        state = await runtime.knowledge_web.release(token)
        return knowledge_state_response(state)

    @app.get(
        "/api/device/knowledge/entries/{entry_id}",
        response_model=KnowledgeEntryResponse,
        dependencies=[Depends(require_device_key)],
    )
    async def get_knowledge_entry(entry_id: str) -> KnowledgeEntryResponse:
        entry = await runtime.knowledge_web.entry(entry_id)
        return knowledge_entry_response(entry)

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
            return await run_device_dialogue(
                runtime.local_device.start_recording
            )
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
        return await run_device_dialogue(
            lambda: execute_device_turn(
                runtime.local_device.stop_recording(),
                busy_detail="设备正在处理上一轮录音",
            )
        )

    @app.post(
        "/api/device/recording/replay",
        status_code=204,
        dependencies=[Depends(require_device_key)],
    )
    async def replay_device_recording() -> Response:
        try:
            await run_device_dialogue(
                runtime.local_device.replay_last_recording
            )
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

        return await run_device_dialogue(
            lambda: execute_device_turn(
                runtime.local_device.process_upload(audio)
            )
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
            await run_device_dialogue(runtime.local_device.reset)
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
