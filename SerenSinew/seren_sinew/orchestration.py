"""
seren_sinew.orchestration
════════════════════════════════════════════════════════════════════════

"I need this service up" and "I am done with it", said the same way by
everyone who says it.

THE CHAIN:

    hippocampus ──ensure──▶ Lodestar ──ensure──▶ Observatory ──▶ start llama
                                                      │  waits until llama
                                                      ▼  answers its health
    hippocampus ◀──ready, at this address── Lodestar ◀── ready
    hippocampus does its work
    hippocampus ──release─▶ Lodestar ──stop──▶ Observatory   (if nobody else
                                                              holds it)

A service on a node - llama, kokoro, whisper, comfy - is started when someone
needs it and stopped when nobody does, so one GPU serves the whole stack
without two of them loaded at once. The caller never learns which node, how
the service is started, or how long a model takes to load: it asks, it waits
for one answer, and the answer says where to send its requests.

WHAT IS HERE: the messages (plain dataclasses, to_dict / from_dict, tolerant
of fields a newer or older peer adds or lacks), the two route shapes, and the
book of who holds what. No HTTP client: each service already has one, with
its own auth and timeouts.

    POST /api/v1/service/{service}/ensure      EnsureRequest  -> EnsureResult
    POST /api/v1/service/{service}/release     ReleaseRequest -> ReleaseResult

    The Observatory answers `ensure` for the services on its node (it starts
    the service and waits for it). Lodestar answers both for the cluster: it
    picks the node, forwards `ensure`, adds the address, and keeps the leases.

LEASES. An ensure names its HOLDER ("seren-hippocampus"). Lodestar keeps who
holds each service on each node. A release drops one holder; the service is
stopped only when the last holder lets go AND an ensure is what started it -
a service that was already running when the first ensure arrived belongs to
whoever started it, and is never stopped by a release.
"""
from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Optional

ENSURE_PATH = "/api/v1/service/{service}/ensure"
RELEASE_PATH = "/api/v1/service/{service}/release"

DEFAULT_WAIT_SECONDS = 240.0     # a model loading from an SD card on a Nano
MAX_WAIT_SECONDS = 900.0


def _known(cls, d: Optional[dict[str, Any]]) -> dict[str, Any]:
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in (d or {}).items() if k in names}


def _num(v: Any, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


@dataclass
class EnsureRequest:
    """Make this service ready to take a call, and say when it is."""
    holder: str = ""                       # who is asking; the lease is in this name
    reason: str = ""                       # for the logs: "a sleep", "a redraft"
    wait_seconds: float = DEFAULT_WAIT_SECONDS   # how long to wait for ready
    node: str = ""                         # only this node; blank = Lodestar chooses

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "EnsureRequest":
        k = _known(cls, d)
        return cls(holder=str(k.get("holder") or "")[:120], reason=str(k.get("reason") or "")[:200],
                   wait_seconds=max(0.0, min(MAX_WAIT_SECONDS, _num(k.get("wait_seconds"), DEFAULT_WAIT_SECONDS))),
                   node=str(k.get("node") or "")[:120])


@dataclass
class EnsureResult:
    """ok = the question was answered; ready = the service answers its health
    check now. started = THIS ensure started it. base_url is where to send
    requests (Lodestar fills it in; an Observatory leaves it blank and gives
    the port)."""
    ok: bool = False
    service: str = ""
    node: str = ""
    ready: bool = False
    started: bool = False
    already_running: bool = False
    base_url: str = ""
    port: int = 0
    health_path: str = ""
    waited_seconds: float = 0.0
    holders: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "EnsureResult":
        k = _known(cls, d)
        return cls(ok=bool(k.get("ok")), service=str(k.get("service") or ""), node=str(k.get("node") or ""),
                   ready=bool(k.get("ready")), started=bool(k.get("started")),
                   already_running=bool(k.get("already_running")), base_url=str(k.get("base_url") or ""),
                   port=int(_num(k.get("port"), 0)), health_path=str(k.get("health_path") or ""),
                   waited_seconds=round(_num(k.get("waited_seconds"), 0.0), 2),
                   holders=[str(h) for h in (k.get("holders") or []) if h], error=str(k.get("error") or ""))

    @classmethod
    def failed(cls, service: str, error: str, node: str = "") -> "EnsureResult":
        return cls(ok=False, service=service, node=node, error=error)


@dataclass
class ReleaseRequest:
    """I am done with this service."""
    holder: str = ""
    reason: str = ""
    node: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "ReleaseRequest":
        k = _known(cls, d)
        return cls(holder=str(k.get("holder") or "")[:120], reason=str(k.get("reason") or "")[:200],
                   node=str(k.get("node") or "")[:120])


@dataclass
class ReleaseResult:
    """stopped = this release stopped the service. holders = who still has it."""
    ok: bool = False
    service: str = ""
    node: str = ""
    stopped: bool = False
    holders: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "ReleaseResult":
        k = _known(cls, d)
        return cls(ok=bool(k.get("ok")), service=str(k.get("service") or ""), node=str(k.get("node") or ""),
                   stopped=bool(k.get("stopped")), holders=[str(h) for h in (k.get("holders") or []) if h],
                   error=str(k.get("error") or ""))


class Leases:
    """Who holds which service on which node. In memory, on purpose: after a
    restart the book is empty, and an empty book stops nothing - the safe way
    to be wrong, since a service nobody holds a lease on is left running."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # (node, service) -> {"holders": {holder: since}, "started": bool}
        self._book: dict[tuple[str, str], dict[str, Any]] = {}

    def acquire(self, node: str, service: str, holder: str, started: bool) -> list[str]:
        """Record the holder. `started` is sticky: once an ensure has started
        the service, the lease owns stopping it. Returns the holders now."""
        with self._lock:
            e = self._book.setdefault((node, service), {"holders": {}, "started": False})
            e["holders"].setdefault(holder or "anonymous", time.time())
            e["started"] = bool(e["started"] or started)
            return sorted(e["holders"])

    def release(self, node: str, service: str, holder: str) -> tuple[list[str], bool]:
        """Drop the holder. Returns (holders left, should_stop): should_stop
        is true only when nobody is left AND an ensure started the service.
        The entry is forgotten then; a holder that was never there changes
        nothing."""
        with self._lock:
            e = self._book.get((node, service))
            if e is None:
                return [], False
            e["holders"].pop(holder or "anonymous", None)
            left = sorted(e["holders"])
            if left:
                return left, False
            started = bool(e["started"])
            del self._book[(node, service)]
            return [], started

    def node_of(self, service: str, holder: str) -> Optional[str]:
        """The node this holder has the service on, if it holds it anywhere."""
        with self._lock:
            for (node, svc), e in self._book.items():
                if svc == service and (holder or "anonymous") in e["holders"]:
                    return node
        return None

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [{"node": n, "service": s, "holders": sorted(e["holders"]), "started_by_lease": bool(e["started"])}
                    for (n, s), e in sorted(self._book.items())]


__all__ = ["ENSURE_PATH", "RELEASE_PATH", "DEFAULT_WAIT_SECONDS", "MAX_WAIT_SECONDS",
           "EnsureRequest", "EnsureResult", "ReleaseRequest", "ReleaseResult", "Leases"]
