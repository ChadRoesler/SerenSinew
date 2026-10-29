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

The same holds for the main Seren apps: a RunPolicy per app (below) says
which run all the time and which run as needed, and seren_sinew.ondemand lets
one app call another through its own httpx client and have it started first.

WHAT IS HERE: the messages (plain dataclasses, to_dict / from_dict, tolerant
of fields a newer or older peer adds or lacks), the two route shapes, the
book of who holds what (with lease clocks, idle clocks, and a file to keep it
in), the run policies, and decide() - what to start and stop, from the book,
what is running and the policies. Lodestar runs it; it holds no logic of its
own about this. No HTTP client here: each service already has one, with its
own auth and timeouts (seren_sinew.ondemand wraps it).

    POST /api/v1/service/{service}/ensure      EnsureRequest  -> EnsureResult
    POST /api/v1/service/{service}/release     ReleaseRequest -> ReleaseResult

    The Observatory answers `ensure` for the services on its node (it starts
    the service and waits for it). Lodestar answers both for the cluster: it
    picks the node, forwards `ensure`, adds the address, and keeps the leases.

LEASES. An ensure names its HOLDER ("seren-hippocampus"). Lodestar keeps who
holds each service on each node. A release drops one holder; the service is
stopped only when the last holder lets go AND an ensure is what started it -
a service that was already running when the first ensure arrived belongs to
whoever started it, and is never stopped by a release - unless its RunPolicy
is on_demand, which hands every stop to the policy. A lease runs out after
its ttl unless the holder asks again, so a holder that died lets go.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .atomic import write_json

ENSURE_PATH = "/api/v1/service/{service}/ensure"
RELEASE_PATH = "/api/v1/service/{service}/release"

DEFAULT_WAIT_SECONDS = 240.0     # a model loading from an SD card on a Nano
MAX_WAIT_SECONDS = 900.0
DEFAULT_TTL_SECONDS = 3600.0     # a lease nobody renews lets go after an hour
MAX_TTL_SECONDS = 7 * 86400.0


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
    ttl_seconds: float = 0.0               # how long the lease lasts unless renewed; 0 = the book's default

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "EnsureRequest":
        k = _known(cls, d)
        return cls(holder=str(k.get("holder") or "")[:120], reason=str(k.get("reason") or "")[:200],
                   wait_seconds=max(0.0, min(MAX_WAIT_SECONDS, _num(k.get("wait_seconds"), DEFAULT_WAIT_SECONDS))),
                   node=str(k.get("node") or "")[:120],
                   ttl_seconds=max(0.0, min(MAX_TTL_SECONDS, _num(k.get("ttl_seconds"), 0.0))))


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
    """Who holds which service on which node, until when, and how long each
    service has sat with nobody holding it.

    A lease has a clock: an ensure holds a service for its ttl_seconds (the
    book's default when it names none), and asking again renews it. A holder
    that ends without releasing (a session out of context, a killed process)
    lets go when its lease runs out, instead of holding a GPU forever.

    The book can be kept on disk (save / load) so a Lodestar restart does not
    forget what an ensure started. Expiry keeps that safe: a lease that
    outlived its holder runs out like any other.

    Idle clocks: `tick()` is told what is running; a running service nobody
    holds has an idle_since, and `decide()` reads it against the service's
    RunPolicy. A service that stops has its entry forgotten.
    """

    def __init__(self, default_ttl: float = DEFAULT_TTL_SECONDS) -> None:
        self._lock = threading.Lock()
        self.default_ttl = float(default_ttl) if default_ttl and default_ttl > 0 else DEFAULT_TTL_SECONDS
        # (node, service) -> {"holders": {holder: {"since", "expires"}}, "started": bool, "idle_since": float|None}
        self._book: dict[tuple[str, str], dict[str, Any]] = {}

    def _entry(self, node: str, service: str) -> dict[str, Any]:
        return self._book.setdefault((node, service), {"holders": {}, "started": False, "idle_since": None})

    def acquire(self, node: str, service: str, holder: str, started: bool,
                ttl: Optional[float] = None, now: Optional[float] = None) -> list[str]:
        """Record (or renew) the holder. `started` is sticky: once an ensure
        has started the service, the lease owns stopping it. Returns the
        holders now."""
        now = time.time() if now is None else now
        ttl = float(ttl) if ttl and ttl > 0 else self.default_ttl
        with self._lock:
            e = self._entry(node, service)
            h = e["holders"].setdefault(holder or "anonymous", {"since": now, "expires": now + ttl})
            h["expires"] = max(h["expires"], now + ttl)
            e["started"] = bool(e["started"] or started)
            e["idle_since"] = None
            return sorted(e["holders"])

    def release(self, node: str, service: str, holder: str, linger: bool = False,
                now: Optional[float] = None) -> tuple[list[str], bool]:
        """Drop the holder. Returns (holders left, should_stop): should_stop
        is true only when nobody is left AND an ensure started the service.

        Without linger the entry is forgotten then (the caller stops it now).
        With linger it stays, idle from now, for a RunPolicy to decide."""
        now = time.time() if now is None else now
        with self._lock:
            e = self._book.get((node, service))
            if e is None:
                return [], False
            e["holders"].pop(holder or "anonymous", None)
            left = sorted(e["holders"])
            if left:
                return left, False
            started = bool(e["started"])
            if linger:
                e["idle_since"] = now if e["idle_since"] is None else e["idle_since"]
            else:
                del self._book[(node, service)]
            return [], started

    def forget(self, node: str, service: str) -> None:
        with self._lock:
            self._book.pop((node, service), None)

    def expire(self, now: Optional[float] = None) -> list[dict[str, Any]]:
        """Drop every holder whose lease ran out. A service left with nobody
        is idle from now. Returns what was dropped."""
        now = time.time() if now is None else now
        dropped = []
        with self._lock:
            for (node, svc), e in self._book.items():
                for holder, h in list(e["holders"].items()):
                    if h["expires"] <= now:
                        del e["holders"][holder]
                        dropped.append({"node": node, "service": svc, "holder": holder})
                if not e["holders"] and e["idle_since"] is None:
                    e["idle_since"] = now
        return dropped

    def tick(self, running: dict[tuple[str, str], bool], now: Optional[float] = None) -> None:
        """Start the idle clock of every running service nobody holds; forget
        the entry of every service that is not running and nobody holds. A
        service no node reported is left as it is (its node may be offline)."""
        now = time.time() if now is None else now
        with self._lock:
            for key, up in running.items():
                e = self._book.get(key)
                if up:
                    e = e or self._entry(*key)
                    if not e["holders"] and e["idle_since"] is None:
                        e["idle_since"] = now
                elif e is not None and not e["holders"]:
                    del self._book[key]

    def node_of(self, service: str, holder: str) -> Optional[str]:
        """The node this holder has the service on, if it holds it anywhere."""
        with self._lock:
            for (node, svc), e in self._book.items():
                if svc == service and (holder or "anonymous") in e["holders"]:
                    return node
        return None

    def entries(self) -> dict[tuple[str, str], dict[str, Any]]:
        with self._lock:
            return {k: {"holders": sorted(e["holders"]), "started": bool(e["started"]),
                        "idle_since": e["idle_since"]} for k, e in self._book.items()}

    def snapshot(self, now: Optional[float] = None) -> list[dict[str, Any]]:
        now = time.time() if now is None else now
        with self._lock:
            return [{"node": n, "service": s, "holders": sorted(e["holders"]), "started_by_lease": bool(e["started"]),
                     "expires_in": {h: round(v["expires"] - now) for h, v in sorted(e["holders"].items())},
                     "idle_seconds": round(now - e["idle_since"]) if e["idle_since"] is not None else None}
                    for (n, s), e in sorted(self._book.items())]

    # ── on disk ───────────────────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {"version": 1, "default_ttl": self.default_ttl,
                    "leases": [{"node": n, "service": s, **json.loads(json.dumps(e))}
                               for (n, s), e in sorted(self._book.items())]}

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]], default_ttl: Optional[float] = None) -> "Leases":
        d = d or {}
        book = cls(default_ttl if default_ttl is not None else _num(d.get("default_ttl"), DEFAULT_TTL_SECONDS))
        for row in d.get("leases") or []:
            try:
                holders = {str(h): {"since": float(v["since"]), "expires": float(v["expires"])}
                           for h, v in (row.get("holders") or {}).items()}
                idle = row.get("idle_since")
                book._book[(str(row["node"]), str(row["service"]))] = {
                    "holders": holders, "started": bool(row.get("started")),
                    "idle_since": float(idle) if idle is not None else None}
            except (KeyError, TypeError, ValueError):
                continue                       # one bad row never loses the rest
        return book

    def save(self, path: "os.PathLike[str] | str") -> None:
        """Atomic: a crash mid-write leaves the last good book."""
        write_json(path, self.to_dict())

    @classmethod
    def load(cls, path: "os.PathLike[str] | str", default_ttl: Optional[float] = None) -> "Leases":
        """The book at `path`, or an empty one when there is none or it does
        not parse (an empty book stops only what the policies say)."""
        try:
            return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")), default_ttl)
        except (OSError, ValueError):
            return cls(default_ttl or DEFAULT_TTL_SECONDS)


# ── run policies: which apps run all the time, and which as needed ──────────
#
# The main Seren apps do not all need to run all the time. Probe evaluates
# now and then; Theatre watches a training run; Symposium is open while
# someone talks. A RunPolicy says, per app:
#
#   unmanaged   what the lease rules say and nothing more (the default):
#               a service an ensure started stops when its last holder lets
#               go, after idle_minutes (0 = at once); anything else is left
#               alone
#   always      never stopped by orchestration
#   on_demand   started by an ensure, stopped once it has been running with
#               nobody holding it for idle_minutes, whoever started it
#   scheduled   kept up inside `hours` ("08:00-23:30", may wrap midnight;
#               started when it is down), on_demand outside them
#
# Lodestar and the Observatory are never stopped or started by a policy:
# they are what does the stopping and starting.

RUN_MODES = ("unmanaged", "always", "on_demand", "scheduled")
NEVER_MANAGED = ("lodestar", "observatory")

# The main Seren apps, as their short names: what `short_name` strips an
# install's name down to ("seren-corpus-callosum-wren" -> "corpus-callosum").
SEREN_APPS = ("memory", "loci", "margin", "hippocampus", "corpus-callosum", "workbench", "probe", "theatre",
              "symposium", "lyceum", "lodestar", "observatory")


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def short_name(service: str) -> Optional[str]:
    """'seren-corpus-callosum-wren', 'SerenCorpusCallosum-wren',
    'seren-memory' -> 'corpus-callosum' / 'memory'. None for anything that is
    not one of the main Seren apps."""
    n = _norm(service)
    if not n.startswith("seren"):
        return None
    rest = n[len("seren"):]
    for app in sorted(SEREN_APPS, key=len, reverse=True):
        if rest.startswith(_norm(app)):
            return app
    return None


def matches(service: str, name: str) -> bool:
    """`name` picks `service` by its full name or its short one."""
    return _norm(name) in (_norm(service), _norm(short_name(service) or "\0"))


def resolve(name: str, installed: list[str]) -> list[str]:
    """The installed services `name` means: itself if installed, else every
    one whose short name it is (two instances of Memory on a box -> two)."""
    if name in installed:
        return [name]
    return [s for s in installed if short_name(s) and matches(s, name)]


@dataclass
class RunPolicy:
    mode: str = "unmanaged"
    idle_minutes: float = 0.0
    hours: str = ""

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]], fallback: Optional["RunPolicy"] = None) -> "RunPolicy":
        base = fallback or cls()
        if isinstance(d, str):
            d = {"mode": d}
        d = d or {}
        mode = str(d.get("mode") or base.mode).strip().lower().replace("-", "_")
        if mode not in RUN_MODES:
            mode = base.mode
        if "idle_minutes" in d:
            idle = max(0.0, _num(d.get("idle_minutes"), 0.0))
        elif mode == base.mode:
            idle = base.idle_minutes
        else:
            idle = 15.0 if mode in ("on_demand", "scheduled") else 0.0
        return cls(mode=mode, idle_minutes=idle, hours=str(d.get("hours") or "") if mode == "scheduled" else "")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def in_hours(self, when: datetime) -> bool:
        """`hours` is "HH:MM-HH:MM", local time; a window that ends before it
        starts wraps midnight. No hours, or hours that do not parse: never."""
        m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$", self.hours or "")
        if not m:
            return False
        a = int(m.group(1)) * 60 + int(m.group(2))
        b = int(m.group(3)) * 60 + int(m.group(4))
        t = when.hour * 60 + when.minute
        return a <= t < b if a <= b else (t >= a or t < b)


class Policies:
    """The `default` policy and one per app, looked up by an install's full
    or short name. Lodestar and the Observatory always come back unmanaged."""

    def __init__(self, default: Optional[RunPolicy] = None, apps: Optional[dict[str, RunPolicy]] = None) -> None:
        self.default = default or RunPolicy()
        self.apps = dict(apps or {})

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "Policies":
        d = d or {}
        default = RunPolicy.from_dict(d.get("default"))
        apps = {str(k): RunPolicy.from_dict(v, default)
                for k, v in (d.get("apps") or {}).items()}
        return cls(default, apps)

    def to_dict(self) -> dict[str, Any]:
        return {"default": self.default.to_dict(), "apps": {k: v.to_dict() for k, v in sorted(self.apps.items())}}

    def for_service(self, service: str) -> RunPolicy:
        if short_name(service) in NEVER_MANAGED:
            return RunPolicy("unmanaged", 0.0)
        exact = [p for k, p in self.apps.items() if _norm(k) == _norm(service)]
        if exact:
            return exact[0]
        short = short_name(service)
        for k, p in self.apps.items():
            if short and _norm(k) == _norm(short):
                return p
        return self.default


@dataclass
class Decision:
    node: str
    service: str
    action: str          # "start" | "stop"
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def decide(entries: dict[tuple[str, str], dict[str, Any]], running: dict[tuple[str, str], bool],
           policies: Policies, now: Optional[float] = None, local: Optional[datetime] = None) -> list[Decision]:
    """What to start and what to stop, from the book (Leases.entries()), what
    is running (per node and service, for every node that answered) and the
    policies. Pure: the caller does it and tells the book."""
    now = time.time() if now is None else now
    local = local or datetime.fromtimestamp(now)
    out: list[Decision] = []
    for (node, svc), up in sorted(running.items()):
        if short_name(svc) in NEVER_MANAGED:
            continue
        pol = policies.for_service(svc)
        e = entries.get((node, svc)) or {"holders": [], "started": False, "idle_since": None}
        if pol.mode == "scheduled" and pol.in_hours(local):
            if not up:
                out.append(Decision(node, svc, "start", f"inside its hours ({pol.hours})"))
            continue
        if pol.mode == "always" or not up or e["holders"]:
            continue
        if e.get("idle_since") is None:
            continue
        idle = now - e["idle_since"]
        if idle < pol.idle_minutes * 60.0:
            continue
        if pol.mode in ("on_demand", "scheduled"):
            why = "outside its hours, " if pol.mode == "scheduled" else ""
            out.append(Decision(node, svc, "stop", f"{why}nobody has held it for {idle / 60:.0f} min"))
        elif e.get("started"):
            out.append(Decision(node, svc, "stop", f"an ensure started it and nobody has held it for {idle / 60:.0f} min"))
    return out


__all__ = ["ENSURE_PATH", "RELEASE_PATH", "DEFAULT_WAIT_SECONDS", "MAX_WAIT_SECONDS", "DEFAULT_TTL_SECONDS",
           "EnsureRequest", "EnsureResult", "ReleaseRequest", "ReleaseResult", "Leases",
           "RUN_MODES", "SEREN_APPS", "RunPolicy", "Policies", "Decision", "decide", "short_name", "matches",
           "resolve"]
