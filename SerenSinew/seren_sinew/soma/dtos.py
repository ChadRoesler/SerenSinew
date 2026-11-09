"""
The shapes on the wire to a Seren body: the robot's control API (firmware,
port 8080, header ``X-StackChan-Token``) and the voice server's (port 7480,
``Authorization: Bearer``).

Field names are the wire's own, camelCase where the firmware sends camelCase,
so a strict parse needs no aliases. Parse and dump with
``seren_sinew.cluster.dtos.from_dict`` / ``to_dict``: lenient by default,
strict in the contract tests.

FIXED shapes (no ``extra``): a key the shape does not know is drift, and the
strict parse names it. OPEN shapes (``extra``): a touch event, whose keys
depend on its kind.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# ── the robot (firmware) ───────────────────────────────────────────────────

@dataclass
class NightCap:
    """A cap over the day's settings, while it's night. Never saved on the robot:
    the voice server sends it again every minute."""
    dimmer: int = 100
    volume: int = 100
    brightness: int = 100


@dataclass
class AppliedSettings:
    """What the robot is doing now: the day's settings with a night cap over them."""
    dimmer: int = 100
    volume: int = 0
    brightness: int = 0


@dataclass
class RobotSettings:
    """POST /api/settings, and ``settings`` in GET /api/status.

    dimmer       0~100 over every light on the rails. The mic, reply and camera
                 lights never go dark under it: dimmed, never hidden.
    volume       the speaker, 0~100.
    brightness   the screen, 5~100 in the day (0 only under a night cap).
    """
    dimmer: int = 100
    volume: int = 0
    brightness: int = 0
    night: Optional[NightCap] = None
    applied: AppliedSettings = field(default_factory=AppliedSettings)


@dataclass
class RobotConfigSource:
    """Where the robot's per-body settings came from: "saved" (soma.yaml or /api/config),
    "build", or for the token "none" (open on the LAN). Never the values."""
    token: str = ""
    voice: str = ""


@dataclass
class RobotStatus:
    """GET /api/status. Most keys are there only when they mean something: ``rig``
    and ``resting`` with a sprite face, ``facePush`` after a face was sent."""
    ok: bool = False
    firmware: str = ""
    uptimeMs: int = 0
    resetReason: str = ""
    leds: str = ""              # rrggbb per LED from 0, as shown (after the dimmer)
    rig: Optional[str] = None   # "irisL x,y irisR x,y browL y,tilt browR y,tilt mouth"
    resting: Optional[str] = None
    facePush: Optional[str] = None
    twitches: Optional[int] = None
    settings: Optional[RobotSettings] = None
    config: Optional[RobotConfigSource] = None   # firmware 0.2.0 on
    batteryLevel: int = 0
    charging: bool = False
    freeInternalHeap: int = 0
    freePsram: int = 0
    yaw: Optional[int] = None
    pitch: Optional[int] = None
    face: Optional[str] = None

    def led(self, index: int) -> tuple[int, int, int]:
        """One LED as (r, g, b)."""
        h = self.leds[index * 6:index * 6 + 6]
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)) if len(h) == 6 else (0, 0, 0)


@dataclass
class HeadTouch:
    """What a kind of touch on top of the head shows."""
    pose: str = ""
    ms: int = 2000


@dataclass
class TouchSpots:
    """GET /api/touch-spots: the face's screen spots by name, and its head touches
    (None when the face says nothing about them)."""
    spots: list[str] = field(default_factory=list)
    head: Optional[dict[str, HeadTouch]] = None


# ── the voice server ───────────────────────────────────────────────────────

@dataclass
class NightSettings:
    """The bedroom: from start to end (HH:MM, crossing midnight is fine) the
    robot's lights, speaker and screen are capped at these."""
    enabled: bool = False
    start: str = "22:30"
    end: str = "07:00"
    dimmer: int = 15
    volume: int = 30
    brightness: int = 20


@dataclass
class WorkHours:
    """Hours the robot never does anything it shouldn't at work. days: Monday 0."""
    days: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])
    start: int = 8
    end: int = 17


@dataclass
class OverwhelmSettings:
    """How long unbroken scratching takes to tip the face over, how long a pause
    breaks it, and how long it rests after. pose, after and clip name the face's
    poses and a saved clip (blank: skipped); off until they're set (seren-soma 1.1)."""
    enabled: bool = False
    seconds: int = 15
    gap_seconds: int = 3
    rest_seconds: int = 120
    pose: str = ""
    after: str = ""
    clip: str = ""


@dataclass
class ChannelSettings:
    """The voice channel: how long it waits with no words before it hangs up (10 to 600 s).
    Live: the next silence uses it (seren-soma 1.2)."""
    dead_air_seconds: int = 60


@dataclass
class VoiceSettings:
    """GET /settings (POST takes any part of it and answers the whole)."""
    night: NightSettings = field(default_factory=NightSettings)
    work_hours: WorkHours = field(default_factory=WorkHours)
    overwhelm: OverwhelmSettings = field(default_factory=OverwhelmSettings)
    channel: ChannelSettings = field(default_factory=ChannelSettings)
    night_now: Optional[bool] = None   # GET only: whether the cap is on right now


@dataclass
class OverwhelmState:
    running: bool = False
    progress: float = 0.0              # 0..1 toward tipping over


@dataclass
class TouchEvent:
    """One touch, what it was read as and what the face showed. OPEN: the keys
    depend on the kind (a poke says ``where``, a stroke ``way``, a screen spot
    ``spot`` and ``index``)."""
    seq: int = 0
    t: float = 0.0                     # when the voice server saw it, unix seconds
    kind: str = ""                     # stroke poke scritch hand finger_rest touch_spot nose_boop forehead_kiss
    t_ms: int = 0                      # the robot's clock
    ms: int = 0
    where: Optional[str] = None
    way: Optional[str] = None
    spot: Optional[str] = None
    index: Optional[int] = None
    showed: Optional[str] = None       # the pose it brought up, if any
    overwhelmed: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class TouchFeed:
    """GET /touch?after=<seq>"""
    events: list[TouchEvent] = field(default_factory=list)
    last: int = 0
    overwhelm: OverwhelmState = field(default_factory=OverwhelmState)


@dataclass
class VoiceStatus:
    """GET /status"""
    ok: bool = False
    device: bool = False               # the robot has its voice channel open
    speaking: bool = False
