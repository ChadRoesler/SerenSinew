"""Typed clients for a Seren body. Needs httpx (the ``client`` extra).

    robot = SomaRobot("http://192.168.0.201:8080", token)     # the firmware
    voice = SomaVoice("http://127.0.0.1:7480", token)         # the voice server

Both are async. The tokens are the person's own (the robot's control token and
the voice server's bearer); nothing here stores or logs them.

No photo method, on purpose: see the package docstring.
"""
from __future__ import annotations

from typing import Any, Optional

import httpx

from ..cluster.dtos import from_dict, to_dict
from .bundle import crc32_hex
from .dtos import NightCap, RobotSettings, RobotStatus, TouchFeed, TouchSpots, VoiceSettings, VoiceStatus


class SomaRobot:
    """The robot's own control API."""

    def __init__(self, base_url: str, token: str, *, timeout: float = 10.0,
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/") + "/api/", timeout=timeout,
                                         headers={"X-StackChan-Token": token} if token else {}, transport=transport)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str) -> Any:
        r = await self._client.get(path)
        r.raise_for_status()
        return r.json()

    async def _post(self, path: str, body: Any = None, **kw: Any) -> Any:
        r = await self._client.post(path, json=body if body is not None else {}, **kw)
        r.raise_for_status()
        return r.json()

    async def status(self) -> RobotStatus:
        return from_dict(await self._get("status"), RobotStatus)

    async def settings(self, *, dimmer: Optional[int] = None, volume: Optional[int] = None,
                       brightness: Optional[int] = None) -> RobotSettings:
        """Reads them, and saves any that are given (on the robot, across reboots)."""
        change = {k: v for k, v in (("dimmer", dimmer), ("volume", volume), ("brightness", brightness)) if v is not None}
        return from_dict((await self._post("settings", change))["settings"], RobotSettings)

    async def night(self, cap: Optional[NightCap]) -> RobotSettings:
        """Puts a night cap on, or takes it off with None. Never saved: the voice
        server owns night and sends it again every minute."""
        body = {"night": to_dict(cap) if cap is not None else None}
        return from_dict((await self._post("settings", body))["settings"], RobotSettings)

    async def touch_spots(self) -> TouchSpots:
        return from_dict(await self._get("touch-spots"), TouchSpots)

    async def touch_raw(self, since: int = 0) -> list[tuple[int, ...]]:
        """The head sensor's readings since then: (ms, zone0, zone1, zone2, gesture)."""
        r = await self._client.get("touch-raw", params={"since": since})
        r.raise_for_status()
        return [tuple(int(x) for x in line.split()) for line in r.text.splitlines() if len(line.split()) == 5]

    async def emotion(self, name: str, *, rest: bool = False, **trial: Any) -> None:
        """Shows a pose. rest: it stands in for neutral until the next emotion. trial:
        what Face Studio sends to try a pose without sending the face (lights,
        lightsPulse, look, brow, mouthCycle, roll)."""
        await self._post("emotion", {"name": name, **({"rest": True} if rest else {}), **trial})

    async def react(self, name: str, ms: int = 2000) -> None:
        """A pose for a moment, then back to what was showing."""
        await self._post("react", {"name": name, "ms": ms})

    async def leds(self, frame: list[tuple[int, int, int]]) -> None:
        """Every LED from 0 up, each (r, g, b) 0~168; the robot clamps past that."""
        await self._post("leds", {"frame": [list(c) for c in frame]})

    async def head(self, *, yaw: Optional[int] = None, pitch: Optional[int] = None, speed: int = 150) -> None:
        """Turns the head: yaw -128 (its right) to 128, pitch 0 (level) to 90, speed 100 to 1000.
        Either can be left where it is."""
        body: dict[str, int] = {"speed": speed}
        if yaw is not None:
            body["yaw"] = yaw
        if pitch is not None:
            body["pitch"] = pitch
        await self._post("head", body)

    async def send_face(self, bundle: bytes) -> None:
        """Hands a packed face over; it goes in on the next boot (reboot() to see it now)."""
        r = await self._client.post("face", content=bundle, timeout=240.0,
                                    headers={"X-Face-Crc32": crc32_hex(bundle), "Content-Type": "application/octet-stream"})
        r.raise_for_status()

    async def restore_face(self) -> None:
        """Swaps the face on the card with the one before it, on the next boot."""
        await self._post("face-restore")

    async def reboot(self) -> None:
        await self._post("reboot")


class SomaVoice:
    """The voice server: night, work hours, overwhelm, the touch feed, and the mouth."""

    def __init__(self, base_url: str, token: str, *, timeout: float = 10.0,
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/") + "/", timeout=timeout,
                                         headers={"Authorization": f"Bearer {token}"} if token else {}, transport=transport)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def status(self) -> VoiceStatus:
        r = await self._client.get("status")
        r.raise_for_status()
        return from_dict(r.json(), VoiceStatus)

    async def settings(self) -> VoiceSettings:
        r = await self._client.get("settings")
        r.raise_for_status()
        return from_dict(r.json(), VoiceSettings)

    async def set_settings(self, change: dict[str, dict[str, Any]]) -> VoiceSettings:
        """Only what's given changes, e.g. {"night": {"enabled": True, "start": "23:00"}}.
        A value the server won't take is a 400 with the reason in its text."""
        r = await self._client.post("settings", json=change)
        r.raise_for_status()
        return from_dict(r.json(), VoiceSettings)

    async def touch(self, after: int = 0) -> TouchFeed:
        r = await self._client.get("touch", params={"after": after})
        r.raise_for_status()
        return from_dict(r.json(), TouchFeed)

    async def say(self, text: str, *, emotion: Optional[str] = None, wake: bool = False) -> float:
        """Speaks it through the robot; seconds of audio. wake: open the channel to do it."""
        r = await self._client.post("say", json={"text": text, **({"emotion": emotion} if emotion else {}), "wake": wake},
                                    timeout=240.0)
        r.raise_for_status()
        return r.json().get("seconds", 0.0)
