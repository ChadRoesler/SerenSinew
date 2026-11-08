"""seren_sinew.soma: the shapes, the bundle, the clients, and the contract with SerenSoma's own source.

The contract half runs when a SerenSoma checkout sits beside this repo (SerenCore/SerenSoma); the firmware
is C++, so its side is read from the source: every key its status and settings answers add must be a field
here, and the voice server's settings defaults must parse strictly as VoiceSettings.
"""
from __future__ import annotations

import ast
import asyncio
import json
import re
from pathlib import Path

import pytest

from seren_sinew.cluster.dtos import WireDrift, from_dict, to_dict
from seren_sinew.soma import (
    MAX_BUNDLE, AppliedSettings, NightCap, NightSettings, RobotSettings, RobotStatus, TouchFeed, TouchSpots,
    VoiceSettings, crc32_hex, pack, unpack,
)

# A real /api/status, 7 Nov 2026, with the token-free fields as they came.
STATUS = {
    "ok": True, "firmware": "1.6.2", "uptimeMs": 444000, "resetReason": "software",
    "leds": "000000000000431530431530431530431530000000000000431530431530431530431530",
    "rig": "0,0 0,0 0,0 0,0 -1", "resting": "", "facePush": "installed a new face: 88 files", "twitches": 3,
    "settings": {"dimmer": 100, "volume": 70, "brightness": 75, "night": None,
                 "applied": {"dimmer": 100, "volume": 70, "brightness": 75}},
    "batteryLevel": 100, "charging": True, "freeInternalHeap": 44459, "freePsram": 6000000,
    "yaw": 0, "pitch": 0, "face": "studio",
}
FEED = {
    "events": [
        {"seq": 1, "t": 1762560000.5, "t_ms": 400755, "ms": 450, "kind": "stroke", "way": "front_to_back",
         "showed": "loving", "overwhelmed": False},
        {"seq": 2, "t": 1762560001.0, "t_ms": 401200, "ms": 0, "kind": "touch_spot", "index": 0, "spot": "boop",
         "showed": None, "overwhelmed": False},
    ],
    "last": 2, "overwhelm": {"running": True, "progress": 0.42},
}


def test_status_parses_strictly():
    s = from_dict(STATUS, RobotStatus, strict=True)
    assert s.settings.applied.volume == 70 and s.settings.night is None
    assert s.led(2) == (0x43, 0x15, 0x30) and s.led(0) == (0, 0, 0)
    assert to_dict(s) == STATUS


def test_an_unknown_status_key_is_drift_when_strict_and_dropped_when_not():
    with pytest.raises(WireDrift, match="cameraOn"):
        from_dict({**STATUS, "cameraOn": True}, RobotStatus, strict=True)
    assert from_dict({**STATUS, "cameraOn": True}, RobotStatus).ok


def test_settings_with_a_night_cap():
    s = from_dict({"dimmer": 50, "volume": 70, "brightness": 75, "night": {"dimmer": 10, "volume": 20, "brightness": 30},
                   "applied": {"dimmer": 10, "volume": 20, "brightness": 30}}, RobotSettings, strict=True)
    assert s.night == NightCap(10, 20, 30) and s.applied == AppliedSettings(10, 20, 30)


def test_touch_spots_with_and_without_head():
    t = from_dict({"spots": ["boop"], "head": {"scritch": {"pose": "melty", "ms": 4000}}}, TouchSpots, strict=True)
    assert t.head["scritch"].pose == "melty"
    assert from_dict({"spots": [], "head": None}, TouchSpots, strict=True).head is None


def test_touch_feed_keeps_what_a_kind_adds():
    f = from_dict(FEED, TouchFeed, strict=True)
    assert f.events[0].way == "front_to_back" and f.events[0].showed == "loving"
    assert f.events[1].spot == "boop" and f.overwhelm.progress == 0.42
    novel = from_dict({**FEED["events"][0], "pressure": 3}, type(f.events[0]))
    assert novel.extra == {"pressure": 3}


def test_bundle_round_trip_and_limits():
    files = {"face.json": b"{}", "eye.bin": bytes(range(256)) * 4}
    blob = pack(files)
    assert unpack(blob) == files
    assert blob[0] == len("eye.bin")                 # name order: the same face is the same bytes
    assert crc32_hex(b"") == "00000000" and len(crc32_hex(blob)) == 8
    with pytest.raises(ValueError, match="robot holds"):
        pack({"big.bin": bytes(MAX_BUNDLE)})
    with pytest.raises(ValueError, match="file name"):
        pack({"dir/x.bin": b""})


# ── the clients, against a fake robot and a fake voice server ──────────────

def _run(coro):
    return asyncio.run(coro)


def test_robot_client_sends_its_token_and_reads_settings():
    httpx = pytest.importorskip("httpx")
    from seren_sinew.soma.client import SomaRobot
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, request.headers.get("X-StackChan-Token"),
                     json.loads(request.content or b"null") if request.method == "POST" else None))
        if request.url.path == "/api/status":
            return httpx.Response(200, json=STATUS)
        return httpx.Response(200, json={"ok": True, "settings": STATUS["settings"]})

    async def go():
        robot = SomaRobot("http://bot:8080", "tok", transport=httpx.MockTransport(handler))
        st = await robot.status()
        s = await robot.settings(dimmer=40)
        await robot.night(NightCap(10, 20, 30))
        await robot.night(None)
        await robot.head(yaw=30)
        await robot.aclose()
        return st, s

    st, s = _run(go())
    assert st.face == "studio" and s.dimmer == 100
    assert all(h == "tok" for _, _, h, _ in seen)
    assert seen[1][1:] == ("/api/settings", "tok", {"dimmer": 40})
    assert seen[2][3] == {"night": {"dimmer": 10, "volume": 20, "brightness": 30}}
    assert seen[3][3] == {"night": None}
    assert seen[4][1:] == ("/api/head", "tok", {"speed": 150, "yaw": 30})      # pitch left where it is


def test_robot_client_sends_a_face_with_its_crc():
    httpx = pytest.importorskip("httpx")
    from seren_sinew.soma.client import SomaRobot
    got = {}

    def handler(request):
        got.update(path=request.url.path, crc=request.headers["X-Face-Crc32"], body=request.content)
        return httpx.Response(200, json={"ok": True})

    blob = pack({"face.json": b"{}"})

    async def go():
        robot = SomaRobot("http://bot:8080", "tok", transport=httpx.MockTransport(handler))
        await robot.send_face(blob)
        await robot.aclose()

    _run(go())
    assert got == {"path": "/api/face", "crc": crc32_hex(blob), "body": blob}


def test_voice_client_bearer_and_settings():
    httpx = pytest.importorskip("httpx")
    from seren_sinew.soma.client import SomaVoice
    seen = []
    defaults = {**to_dict(VoiceSettings()), "night_now": False}

    def handler(request):
        seen.append((request.method, request.url.path, request.headers.get("Authorization")))
        if request.url.path == "/touch":
            assert request.url.params["after"] == "7"
            return httpx.Response(200, json=FEED)
        return httpx.Response(200, json=defaults)

    async def go():
        voice = SomaVoice("http://127.0.0.1:7480", "vt", transport=httpx.MockTransport(handler))
        s = await voice.set_settings({"night": {"enabled": True}})
        f = await voice.touch(after=7)
        await voice.aclose()
        return s, f

    s, f = _run(go())
    assert isinstance(s.night, NightSettings) and f.last == 2
    assert all(a == "Bearer vt" for _, _, a in seen)


# ── the contract with SerenSoma's own source ───────────────────────────────

SOMA = Path(__file__).resolve().parents[3] / "SerenSoma"
FIRMWARE = SOMA / "firmware" / "main" / "hal" / "hal_control_server.cpp"
VOICE = SOMA / "SerenSoma" / "seren_soma" / "voice.py"


def _function(source: str, signature: str) -> str:
    start = source.index(signature)
    depth, i = 0, source.index("{", start)
    for j in range(i, len(source)):
        depth += {"{": 1, "}": -1}.get(source[j], 0)
        if depth == 0:
            return source[i:j + 1]
    raise AssertionError(f"{signature} never closes")


def _keys(body: str, obj: str) -> set[str]:
    return set(re.findall(r'cJSON_Add\w+ToObject\(\s*' + obj + r'\s*,\s*"(\w+)"', body))


@pytest.mark.skipif(not FIRMWARE.exists(), reason="no SerenSoma checkout beside this one")
def test_every_key_the_firmware_sends_is_a_field():
    src = FIRMWARE.read_text(encoding="utf-8")
    status = _function(src, "esp_err_t handle_status(httpd_req_t* req)")
    settings = _function(src, "void add_settings(cJSON* root)")
    fields = lambda dto: {f for f in dto.__dataclass_fields__}  # noqa: E731
    assert _keys(status, "root") <= fields(RobotStatus), _keys(status, "root") - fields(RobotStatus)
    assert "settings" in _keys(settings, "root")
    assert _keys(settings, "s") <= fields(RobotSettings), _keys(settings, "s") - fields(RobotSettings)
    assert _keys(settings, "n") == fields(NightCap)
    assert _keys(settings, "a") == fields(AppliedSettings)


@pytest.mark.skipif(not VOICE.exists(), reason="no SerenSoma checkout beside this one")
def test_the_voice_servers_settings_parse_strictly():
    tree = ast.parse(VOICE.read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "DEFAULT_SETTINGS")
    defaults = ast.literal_eval(node.value)
    s = from_dict({**defaults, "night_now": None}, VoiceSettings, strict=True)
    # and the other way: every field here is one the server knows, so a console can't send one it would refuse
    assert {k for k in to_dict(s) if k != "night_now"} == set(defaults)
    for section in defaults:
        assert set(to_dict(getattr(s, section))) == set(defaults[section]), section
    # the shapes' own defaults are the server's
    assert {k: v for k, v in to_dict(VoiceSettings()).items() if k != "night_now"} == defaults


@pytest.mark.skipif(not VOICE.exists(), reason="no SerenSoma checkout beside this one")
def test_the_touch_feed_keys_are_fields():
    src = VOICE.read_text(encoding="utf-8")
    feed = src[src.index("async def touch_feed"):src.index("async def status")]
    from seren_sinew.soma import TouchEvent
    for key in ("events", "last", "overwhelm"):
        assert f'"{key}"' in feed, key
    events = src[src.index("self.feed.append("):]
    events = events[:events.index("\n            self.feed = ")]
    for key in re.findall(r'"(\w+)":', events):
        assert key in TouchEvent.__dataclass_fields__, key
