"""seren_sinew.soma - the wire to a Seren body, spelled once.

SerenSoma is a body: a Stack-chan robot's firmware, and the voice server that
is its nervous system (touch, the ears and the mouth, night mode). SerenPsycheion
watches it and makes its faces. The two never spell what passes between them
themselves; it is spelled here, and both sides' contract tests read their own
side against these shapes.

Three modules:

- ``dtos``    the dataclasses: the robot's status and settings, the voice
              server's settings and touch feed. Standard library only, so a
              service can import the shapes without an HTTP client.
- ``bundle``  a face, packed the way the firmware unpacks it, and its CRC.
              Standard library only.
- ``client``  ``SomaRobot`` (the firmware's control API) and ``SomaVoice`` (the
              voice server's). Needs httpx (the ``client`` extra).

WHAT IS NOT HERE, ON PURPOSE: the camera. A photo is taken only when the
person in the room says so, and a console is not that person.

The firmware is C++ and cannot import this, so its side is pinned by a
contract test that reads the firmware's own source for the keys it sends,
whenever a SerenSoma checkout sits beside this one.
"""
from __future__ import annotations

from .bundle import MAX_BUNDLE, crc32_hex, pack, unpack  # noqa: F401
from .dtos import (  # noqa: F401
    AppliedSettings,
    ChannelSettings,
    HeadTouch,
    NightCap,
    NightSettings,
    OverwhelmSettings,
    OverwhelmState,
    RobotConfigSource,
    RobotSettings,
    RobotStatus,
    TouchEvent,
    TouchFeed,
    TouchSpots,
    VoiceSettings,
    VoiceStatus,
    WorkHours,
)

__all__ = [
    "AppliedSettings", "ChannelSettings", "HeadTouch", "MAX_BUNDLE", "NightCap", "NightSettings", "OverwhelmSettings",
    "OverwhelmState", "RobotConfigSource", "RobotSettings", "RobotStatus", "TouchEvent", "TouchFeed", "TouchSpots",
    "VoiceSettings", "VoiceStatus", "WorkHours", "crc32_hex", "pack", "unpack",
]
