import asyncio
import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AudioDeviceStatus:
    capture_available: bool
    playback_available: bool
    capture_name: str | None
    playback_name: str | None
    error: str | None

    @classmethod
    def unavailable(cls, error: str) -> "AudioDeviceStatus":
        return cls(False, False, None, None, error)


def _is_virtual(props: dict[str, Any]) -> bool:
    value = props.get("node.virtual", False)
    return value is True or str(value).lower() == "true"


def _matching_device_name(
    nodes: list[object],
    *,
    media_class: str,
    target: str,
) -> str | None:
    for item in nodes:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "PipeWire:Interface:Node":
            continue
        info = item.get("info")
        if not isinstance(info, dict):
            continue
        props = info.get("props")
        if not isinstance(props, dict):
            continue
        if props.get("media.class") != media_class:
            continue
        if _is_virtual(props) or "device.id" not in props:
            continue
        node_name = props.get("node.name")
        if target != "default" and node_name != target:
            continue
        description = (
            props.get("node.description")
            or props.get("node.nick")
            or node_name
        )
        return str(description) if description else None
    return None


def parse_pipewire_devices(
    payload: bytes,
    *,
    capture_target: str = "default",
    playback_target: str = "default",
) -> AudioDeviceStatus:
    try:
        nodes = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("PipeWire 返回了无效状态") from error
    if not isinstance(nodes, list):
        raise ValueError("PipeWire 返回了无效状态")

    capture_name = _matching_device_name(
        nodes,
        media_class="Audio/Source",
        target=capture_target,
    )
    playback_name = _matching_device_name(
        nodes,
        media_class="Audio/Sink",
        target=playback_target,
    )
    return AudioDeviceStatus(
        capture_available=capture_name is not None,
        playback_available=playback_name is not None,
        capture_name=capture_name,
        playback_name=playback_name,
        error=None,
    )


async def probe_pipewire_devices(
    *,
    capture_target: str = "default",
    playback_target: str = "default",
    timeout_seconds: float = 1.0,
) -> AudioDeviceStatus:
    process = await asyncio.create_subprocess_exec(
        "pw-dump",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    if process.returncode != 0:
        raise OSError("pw-dump failed")
    return parse_pipewire_devices(
        stdout,
        capture_target=capture_target,
        playback_target=playback_target,
    )


StatusCallback = Callable[[AudioDeviceStatus], object | Awaitable[object]]
StatusProbe = Callable[[], Awaitable[AudioDeviceStatus]]


class AudioDeviceMonitor:
    def __init__(
        self,
        *,
        probe: StatusProbe,
        on_change: StatusCallback | None = None,
        interval_seconds: float = 2.0,
    ) -> None:
        self._probe = probe
        self._on_change = on_change
        self._interval_seconds = interval_seconds
        self._status = AudioDeviceStatus.unavailable("正在检测音频设备")
        self._published_status: AudioDeviceStatus | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def status(self) -> AudioDeviceStatus:
        return self._status

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(),
                name="audio-device-monitor",
            )

    async def refresh(self) -> AudioDeviceStatus:
        try:
            status = await self._probe()
        except (OSError, TimeoutError, ValueError):
            status = AudioDeviceStatus.unavailable("无法读取音频设备状态")
        self._status = status
        if status != self._published_status:
            self._published_status = status
            if self._on_change is not None:
                result = self._on_change(status)
                if inspect.isawaitable(result):
                    await result
        return status

    async def aclose(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        while True:
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("audio_device_monitor_cycle_failed")
            await asyncio.sleep(self._interval_seconds)
