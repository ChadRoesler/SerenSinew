"""Typed clients for a Seren body. Needs httpx (the ``client`` extra).

    robot = SomaRobot("http://192.0.2.21:8080", token)      # the firmware
    voice = SomaVoice("http://127.0.0.1:7480", token)         # the voice server

Both are async. The tokens are the person's own (the robot's control token and
the voice server's bearer); nothing here stores or logs them.

A token can be a string, or something to call for it (``token_file(path)``, or
any zero-argument function). A called one is asked again when an answer is 401
and the request is sent once more with what it says now, so a token rotated
under a long-running client (the voice server setting a robot up) doesn't need
a restart to pick up.

No photo method, on purpose: see the package docstring.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional, Union

import httpx

from ..cluster.dtos import from_dict, to_dict
from .bundle import crc32_hex
from .dtos import NightCap, RobotSettings, RobotStatus, TouchFeed, TouchSpots, VoiceSettings, VoiceStatus


TokenSource = Union[str, Callable[[], str]]


def token_file(path: Union[str, Path]) -> Callable[[], str]:
    """A token source that reads the file each time it's asked (which is once, then on a 401)."""
    p = Path(path).expanduser()

    def read() -> str:
        try:
            return p.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return read


class _TokenAuth(httpx.Auth):
    """Puts the token on every request; on a 401, asks a callable source again and retries once
    if it has something new. A plain string is just put on."""

    requires_request_body = True   # a retried POST sends its body again

    def __init__(self, header: str, prefix: str, source: TokenSource):
        self._header, self._prefix, self._source = header, prefix, source
        self._token = source() if callable(source) else (source or "")

    def auth_flow(self, request):
        if self._token:
            request.headers[self._header] = self._prefix + self._token
        response = yield request
        if response.status_code == 401 and callable(self._source):
            fresh = self._source() or ""
            if fresh and fresh != self._token:
                self._token = fresh
                request.headers[self._header] = self._prefix + fresh
                yield request


class SomaRobot:
    """The robot's own control API."""

    def __init__(self, base_url: str, token: TokenSource, *, timeout: float = 10.0,
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/") + "/api/", timeout=timeout,
                                         auth=_TokenAuth("X-StackChan-Token", "", token), transport=transport)

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

    def __init__(self, base_url: str, token: TokenSource, *, timeout: float = 10.0,
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/") + "/", timeout=timeout,
                                         auth=_TokenAuth("Authorization", "Bearer ", token), transport=transport)

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

    async def setup_robot(self, keys: dict[str, Any], *, rotate_token: bool = False,
                          reboot: bool = True) -> dict[str, Any]:
        """Sets the robot up through the voice server (its /robot/config): soma.yaml's keys, e.g.
        {"wifi": {"ssid": ..., "password": ...}, "voice": "auto"}, with the voice server filling
        voice in when it's "auto" or left out. rotate_token: a new robot token, which the voice
        server writes down only once the robot has it. Answers with the names of what the robot
        saved ({"changed": [...], "rebooting": ..., "token_rotated": ...}), never values. A 400
        says what the robot can't take; a 502, what went wrong reaching it."""
        r = await self._client.post("robot/config", json={**keys, "rotate_token": rotate_token, "reboot": reboot},
                                    timeout=30.0)
        r.raise_for_status()
        return r.json()

    async def robot_card(self, keys: dict[str, Any]) -> str:
        """soma.yaml for a robot's SD card, with the voice server's blanks filled (itself as the voice
        server, and the token it holds for the robot). It holds a token: hand it straight to the
        person, don't keep it."""
        r = await self._client.post("robot/soma.yaml", json=keys)
        r.raise_for_status()
        return r.text

    async def firmware(self) -> dict[str, Any]:
        """What the robot runs, what's staged on the voice server, and what's armed for its next check-in."""
        r = await self._client.get("firmware")
        r.raise_for_status()
        return r.json()

    async def fetch_firmware(self, version: str = "latest") -> dict[str, Any]:
        """Has the voice server fetch a fw-v* release's app image, checked against its SHA256SUMS."""
        r = await self._client.post("firmware/fetch", json={"version": version}, timeout=180.0)
        r.raise_for_status()
        return r.json()

    async def upload_firmware(self, version: str, image: bytes) -> dict[str, Any]:
        """Hands the voice server an app image of your own (stack-chan.bin from a build) to stage."""
        r = await self._client.put("firmware/upload", params={"version": version}, content=image,
                                   headers={"Content-Type": "application/octet-stream"}, timeout=120.0)
        r.raise_for_status()
        return r.json()

    async def install_firmware(self, version: str, *, reboot: bool = True) -> dict[str, Any]:
        """Arms a staged image for the robot's next check-in (once) and reboots it so that's now."""
        r = await self._client.post("firmware/install", json={"version": version, "reboot": reboot})
        r.raise_for_status()
        return r.json()
