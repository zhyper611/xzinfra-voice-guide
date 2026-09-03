import asyncio
import json

import pytest

from showroom_guide.audio_devices import (
    AudioDeviceMonitor,
    AudioDeviceStatus,
    parse_pipewire_devices,
)


def pipewire_dump(*nodes: dict[str, object]) -> bytes:
    return json.dumps(list(nodes)).encode()


def node(
    media_class: str,
    name: str,
    description: str,
    *,
    device_id: int | None = 10,
    virtual: bool = False,
) -> dict[str, object]:
    props: dict[str, object] = {
        "media.class": media_class,
        "node.name": name,
        "node.description": description,
    }
    if device_id is not None:
        props["device.id"] = device_id
    if virtual:
        props["node.virtual"] = True
    return {
        "type": "PipeWire:Interface:Node",
        "info": {"props": props},
    }


def test_parse_pipewire_devices_reports_physical_input_and_output():
    payload = pipewire_dump(
        node("Audio/Source", "alsa_input.usb-mic", "USB Microphone"),
        node("Audio/Sink", "alsa_output.usb-speaker", "USB Speaker"),
    )

    status = parse_pipewire_devices(payload)

    assert status == AudioDeviceStatus(
        capture_available=True,
        playback_available=True,
        capture_name="USB Microphone",
        playback_name="USB Speaker",
        error=None,
    )


def test_parse_pipewire_devices_ignores_dummy_and_monitor_nodes():
    payload = pipewire_dump(
        node(
            "Audio/Sink",
            "auto_null",
            "Dummy Output",
            device_id=None,
            virtual=True,
        ),
        node(
            "Audio/Source",
            "auto_null.monitor",
            "Monitor of Dummy Output",
            device_id=None,
            virtual=True,
        ),
    )

    status = parse_pipewire_devices(payload)

    assert status.capture_available is False
    assert status.playback_available is False
    assert status.capture_name is None
    assert status.playback_name is None


def test_parse_pipewire_devices_requires_configured_targets_when_explicit():
    payload = pipewire_dump(
        node("Audio/Source", "alsa_input.other", "Other Microphone"),
        node("Audio/Sink", "alsa_output.other", "Other Speaker"),
    )

    status = parse_pipewire_devices(
        payload,
        capture_target="alsa_input.expected",
        playback_target="alsa_output.expected",
    )

    assert status.capture_available is False
    assert status.playback_available is False


def test_parse_pipewire_devices_rejects_invalid_json():
    with pytest.raises(ValueError, match="PipeWire 返回了无效状态"):
        parse_pipewire_devices(b"not-json")


@pytest.mark.asyncio
async def test_monitor_publishes_changes_without_republishing_same_snapshot():
    snapshots = [
        AudioDeviceStatus.unavailable("PipeWire 暂不可用"),
        AudioDeviceStatus.unavailable("PipeWire 暂不可用"),
        AudioDeviceStatus(
            capture_available=True,
            playback_available=False,
            capture_name="USB Microphone",
            playback_name=None,
            error=None,
        ),
    ]
    published = []

    async def probe() -> AudioDeviceStatus:
        return snapshots.pop(0)

    monitor = AudioDeviceMonitor(probe=probe, on_change=published.append)

    await monitor.refresh()
    await monitor.refresh()
    await monitor.refresh()

    assert published == [
        AudioDeviceStatus.unavailable("PipeWire 暂不可用"),
        AudioDeviceStatus(
            capture_available=True,
            playback_available=False,
            capture_name="USB Microphone",
            playback_name=None,
            error=None,
        ),
    ]


@pytest.mark.asyncio
async def test_monitor_turns_probe_failure_into_unavailable_status():
    async def probe() -> AudioDeviceStatus:
        raise asyncio.TimeoutError

    monitor = AudioDeviceMonitor(probe=probe)

    status = await monitor.refresh()

    assert status.capture_available is False
    assert status.playback_available is False
    assert status.error == "无法读取音频设备状态"
