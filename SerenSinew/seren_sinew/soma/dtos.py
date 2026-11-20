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
    the voice server sends it again every minute.

    appendages   a ceiling per appendage, by its name: {"light": 28, "fan": 20}, level 1~100.
                 A name the robot hasn't got is ignored. An appendage left out holds the
                 default its own file declares, never "uncapped": only one whose file has
                 no ``night:`` runs without a ceiling (docs/appendages.md, "Night ceilings").
    light        the first slice's one flashlight ceiling (19 Nov 2026), until the firmware
                 reads ``appendages``. GOING: it leaves in the same deploy as the firmware
                 that reads the map, so the flashlight is never without a night limit."""
    dimmer: int = 100
    volume: int = 100
    brightness: int = 100
    light: int = 100
    appendages: Optional[dict[str, int]] = None


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
class RobotPower:
    """``power`` in GET /api/status: what the power chip measures, read on each call.

    The percentage in ``batteryLevel`` is the chip's own gauge and loses its count
    whenever the battery is unplugged; the voltages here do not.

    direction    which way current flows at the chip: its own word for it. It has said
                 "charging" while the battery was losing 170 mA overall (19 Nov 2026, fed
                 through the base's USB port), so for "is the battery gaining" read monitorMa.
    phase        the charger's phase: its own word for it.
    inputLimited the chip is holding the charge back because the input sags.
    raw          the status and measurement bytes the numbers were decoded from, hex.
    monitorMv, monitorMa   a body with its own battery monitor: the battery's voltage
                 and current there. Negative is into the battery. Absent without one.
    """
    batteryMv: int = 0
    usbMv: int = 0
    systemMv: int = 0
    chipTempC: float = 0.0
    usbGood: bool = False
    batteryPresent: bool = False
    inputLimited: bool = False
    direction: str = ""
    phase: str = ""
    raw: str = ""
    monitorMv: Optional[int] = None
    monitorMa: Optional[float] = None


@dataclass
class RobotStatus:
    """GET /api/status. Most keys are there only when they mean something: ``rig``
    and ``resting`` with a sprite face, ``facePush`` after a face was sent, ``power``
    when the power chip answered."""
    ok: bool = False
    firmware: str = ""
    uptimeMs: int = 0
    resetReason: str = ""
    leds: str = ""              # rrggbb per LED from 0, as shown (after the dimmer)
    rig: Optional[str] = None   # "irisL x,y irisR x,y browL y,tilt browR y,tilt mouth"
    resting: Optional[str] = None
    held: Optional[str] = None  # the pose held until let go ("hold" on /api/emotion), "" for none; firmware of 12 Nov 2026 on
    facePush: Optional[str] = None
    twitches: Optional[int] = None
    settings: Optional[RobotSettings] = None
    config: Optional[RobotConfigSource] = None   # firmware 0.2.0 on
    power: Optional[RobotPower] = None
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
class Buildup:
    """One of a face's build-ups (face.yaml buildups:, firmware 0.6 on): a touch kept up for `seconds`,
    with no pause longer than `gap`, tips the face into `pose` for `holdMs` (the rails flash `flash`
    and the face plays `sound` with it), then `after` for `afterMs`, then back; then it rests `rest`
    seconds. touch: "scratching" or a head touch kind. The robot carries these; SerenPsycheion runs them."""
    touch: str = "scratching"
    seconds: float = 15.0
    gap: float = 3.0
    rest: float = 120.0
    pose: str = ""
    holdMs: int = 4000
    after: Optional[str] = None
    afterMs: Optional[int] = None
    flash: Optional[list[int]] = None   # (r, g, b), 0~168
    sound: Optional[str] = None


@dataclass
class TouchSpots:
    """GET /api/touch-spots: the face's screen spots by name, its head touches (None when the face
    says nothing about them), its own sounds and its build-ups (firmware 0.6 on)."""
    spots: list[str] = field(default_factory=list)
    head: Optional[dict[str, HeadTouch]] = None
    sounds: list[str] = field(default_factory=list)
    buildups: Optional[dict[str, Buildup]] = None


# ── appendages: anything bolted onto the StackChan ─────────────────────────
#
# The StackChan itself (servos, rails, head touch, screen, camera, power, light sensor) has named,
# typed shapes above. Anything bolted on is a file (<name>.appendage.yaml, docs/appendages.md):
# a generic kind in the firmware (switch, pwm, i2c, button, llm) filled in by that file. So these
# shapes know kinds and capabilities, never parts: nothing in Sinew knows what a flashlight is.

# The capability words gestures understand, and what each one promises. Any other word in a file's
# ``provides`` is allowed and ignored, so a friend can ship ``provides: [smoke]`` without asking.
CAPABILITIES: dict[str, str] = {
    "light": "on, off and a timed limit; with 'level', 1~100 is a share of that light's own full "
             "output, the same scale on every light (level 1 is 0.6%)",
    "airflow": "on, off and a timed limit; with 'level', 1~100 is a share of full speed",
}


@dataclass
class AppendageNight:
    """``night`` on an appendage whose file declares one: the ceiling its file asks for when nobody
    has set one (``default``), and the one it is holding right now (``holding``; 100 by day)."""
    default: int = 100
    holding: int = 100


@dataclass
class AppendageState:
    """``state`` on an appendage. OPEN: what a kind reports beyond these rides in ``extra``
    (a pwm light's lightPercent, hz and pulseUs, an i2c fan's rpm, a button's lastPress)."""
    on: bool = False
    level: Optional[int] = None    # what it runs at
    asked: Optional[int] = None    # what was asked, when a night ceiling held it lower
    msLeft: int = 0                # until it switches itself off; every on ends by itself
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Appendage:
    """One entry of GET /api/appendages. ``ok`` false means the robot refused it (a pin clash, a base
    pin, a kind this firmware hasn't got) and ``error`` says why in plain words; the robot still boots.

    kind       the firmware driver: switch, pwm, i2c, button, llm
    provides   capability words (CAPABILITIES); [] from a file that says none
    pins       by role, as the file names them: {"en": 9}
    actions    what POST /api/appendages/<name> takes: {"action": one of these, ...}
    file       the catalogue or import it came from, "m5-flashlight-u152"; absent on the first slice"""
    name: str = ""
    kind: str = ""
    ok: bool = False
    provides: list[str] = field(default_factory=list)
    pins: dict[str, int] = field(default_factory=dict)
    actions: list[str] = field(default_factory=list)
    state: Optional[AppendageState] = None
    night: Optional[AppendageNight] = None
    error: Optional[str] = None
    file: Optional[str] = None
    side: Optional[str] = None     # the first slice's flashlight: which side of the head. GOING with it

    def can(self, word: str) -> bool:
        return word in self.provides


@dataclass
class Appendages:
    """GET /api/appendages: what is attached, and the kinds this firmware carries (so an import can say
    "needs a firmware with i2c" instead of half-working)."""
    ok: bool = False
    appendages: list[Appendage] = field(default_factory=list)
    kinds: list[str] = field(default_factory=list)

    def providing(self, word: str, *, prefer: Optional[str] = None) -> Optional[Appendage]:
        """The working appendage that provides ``word``: the one named ``prefer`` if it does, else the
        first in the robot's list. How a gesture finds "a light" without naming one."""
        able = [a for a in self.appendages if a.ok and a.can(word)]
        return next((a for a in able if a.name == prefer), able[0] if able else None)

    def ceilings(self) -> dict[str, AppendageNight]:
        """Every appendage that takes a night ceiling, by name: what a Night card draws a slider for."""
        return {a.name: a.night for a in self.appendages if a.ok and a.night is not None}


# ── the voice server ───────────────────────────────────────────────────────

@dataclass
class NightSettings:
    """The bedroom: from start to end (HH:MM, crossing midnight is fine) the
    robot's lights, speaker and screen are capped at these, and each appendage's
    level at its entry in ``appendages``, by name. Only the ones someone has set are
    here; the rest hold their file's own default on the robot.

    light: the first slice's flashlight ceiling. GOING, with NightCap.light."""
    enabled: bool = False
    start: str = "22:30"
    end: str = "07:00"
    dimmer: int = 15
    volume: int = 30
    brightness: int = 20
    light: int = 28
    appendages: dict[str, int] = field(default_factory=dict)


@dataclass
class WorkHours:
    """Hours the robot never does anything it shouldn't at work. days: Monday 0."""
    days: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])
    start: int = 8
    end: int = 17


@dataclass
class ChannelSettings:
    """The voice channel: how long it waits with no words before it hangs up (10 to 600 s).
    Live: the next silence uses it (seren-soma 1.2)."""
    dead_air_seconds: int = 60
    # How much quiet after talking counts as "finished" (300 to 2500 ms). Every turn waits this long
    # before it can be answered. Live: the next thing said uses it (seren-soma 1.4).
    end_silence_ms: int = 900


@dataclass
class VoiceSettings:
    """GET /settings (POST takes any part of it and answers the whole)."""
    night: NightSettings = field(default_factory=NightSettings)
    work_hours: WorkHours = field(default_factory=WorkHours)
    channel: ChannelSettings = field(default_factory=ChannelSettings)
    night_now: Optional[bool] = None   # GET only: whether the cap is on right now


@dataclass
class HandState:
    """The head touch the hand hasn't lifted from yet, on the robot's clock (seren-soma 1.2). What lets a
    build-up keep counting through one long, unbroken scratch."""
    down: bool = False
    from_ms: Optional[int] = None
    to_ms: Optional[int] = None


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
    scratching: Optional[bool] = None  # head touches: part of scratching, the touch or its burst (seren-soma 1.2)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class TouchFeed:
    """GET /touch?after=<seq>"""
    events: list[TouchEvent] = field(default_factory=list)
    last: int = 0
    hand: HandState = field(default_factory=HandState)


@dataclass
class HeardLine:
    """One thing the robot's microphone heard, transcribed."""
    t: float = 0.0                     # when it was heard, unix seconds: what `after` takes
    seconds: float = 0.0               # how long the talking was
    text: str = ""


@dataclass
class Heard:
    """GET /heard?after=<t>&wait=<seconds> (seren-soma 1.4). With wait, an empty answer is held open
    until something is heard or the wait runs out, so words arrive the moment they are transcribed."""
    entries: list[HeardLine] = field(default_factory=list)
    last: float = 0.0                  # the newest entry's t, or the `after` that was asked when there is none
    now: float = 0.0                   # the voice server's clock, to tell how old an entry is


@dataclass
class TalkState:
    """GET /talk, and what POST /talk answers (seren-soma 1.4). Back and forth: for a while the robot
    keeps listening without a tap and the voice stays loaded."""
    on: bool = False
    until: Optional[float] = None      # unix seconds it ends by itself; None when off
    voice: str = ""                    # "held" when the voice is loaded and kept, else why it isn't
    channel_open: bool = False
    listening: bool = False
    minutes_range: list[int] = field(default_factory=list)


@dataclass
class VoiceStatus:
    """GET /status"""
    ok: bool = False
    device: bool = False               # the robot has its voice channel open
    speaking: bool = False
