"""
The shapes on the wire between Lodestar and an Observatory.

Each dataclass is one answer (or one request body) of the Observatory's
``/api/v1/...`` API, field for field. Two kinds:

- FIXED shapes (a ping, a version, a health rollup, a lifecycle answer): every
  key the Observatory sends is a field here. A key that is not is DRIFT, and
  ``from_dict(..., strict=True)`` raises ``WireDrift`` naming it. Both sides'
  contract tests parse strictly, so the drift fails a test instead of being
  dropped on the floor the way ``agent_version`` was.
- OPEN shapes (a service's manifest, its status, the node's manifest): a
  file the operator or an installer wrote, or a status whose keys depend on
  the service type (a docker service says ``compose_service``, a systemd one
  ``systemd_unit``). These carry an ``extra`` dict, and an unknown key lands
  there instead of being lost, so the viewer can still show it and a round
  trip through ``to_dict`` is faithful.

``from_dict`` is LENIENT by default, as the old Lodestar helper was: a
required field the payload lacks is filled with the empty value of its type.
The strict version once turned ONE manifest with no ``service`` key into
"agent returned null" for the whole node.

History: ported from SerenCluster/Models/AgentDtos.cs into Lodestar's
dtos.py, moved here 28 Oct 2026 (punch list sinew-cluster) so the
Observatory's routes and tests can import the same shapes.
"""
from __future__ import annotations

import dataclasses
import typing
from dataclasses import dataclass, field
from typing import Any, Optional


# ── drift ──────────────────────────────────────────────────────────────────

class WireDrift(ValueError):
    """The payload and the dataclass disagree: keys the shape does not know,
    or required fields the payload lacks. Raised only by a strict parse."""

    def __init__(self, dto: type, unknown: list[str], missing: list[str], path: str = ""):
        self.dto, self.unknown, self.missing, self.path = dto, unknown, missing, path
        where = f" at {path}" if path else ""
        parts = []
        if unknown:
            parts.append(f"keys {dto.__name__} does not know: {', '.join(unknown)}")
        if missing:
            parts.append(f"required fields missing: {', '.join(missing)}")
        super().__init__(f"wire drift{where}: " + "; ".join(parts))


#: Keys an Observatory sends under a different name than the field. The
#: Observatory's /system/version has said ``observatory_version`` since the
#: service was renamed from seren-agent; an Observatory from before says
#: ``agent_version``. Both land in ``observatory_version``.
ALIASES: dict[str, str] = {"agent_version": "observatory_version"}

#: The catch-all field name on an OPEN shape.
EXTRA = "extra"


# ── System endpoints ───────────────────────────────────────────────────────

@dataclass
class PingResponse:
    """GET /api/v1/system/ping"""
    ok: bool
    ts: int  # unix timestamp


@dataclass
class VersionResponse:
    """GET /api/v1/system/version. ``updates`` is seren_meninges.updates'
    payload (status, installed, latest, update_available...), passed through
    as the dict it is."""
    observatory_version: str = ""
    manifest_schema: int = 0
    updates: Optional[dict[str, Any]] = None

    @property
    def agent_version(self) -> str:
        """The field's old name; what Lodestar's callers still read."""
        return self.observatory_version


@dataclass
class NodeManifest:
    """~/.seren/node.json as the node wrote it (or what the Observatory
    discovers when there is none). OPEN: installers add keys (``hardware``,
    ``jetpack_release``, ``source``); they ride in ``extra``."""
    hostname: Optional[str] = None
    ip_addresses: Optional[list[str]] = None
    platform: Optional[str] = None
    jetpack_release: Optional[str] = None
    cuda_arch: Optional[str] = None
    cuda_version: Optional[str] = None
    unified_memory_gb: Optional[int] = None
    cpu_cores: Optional[int] = None
    installed_at: Optional[str] = None
    schema_version: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class NodeRuntime:
    load_avg: Optional[list[float]] = None
    memory_mb_total: Optional[int] = None
    memory_mb_available: Optional[int] = None
    memory_pct_used: Optional[float] = None
    uptime_seconds: Optional[int] = None


@dataclass
class NodeResponse:
    """GET /api/v1/system/node"""
    manifest: Optional[NodeManifest] = None
    runtime: Optional[NodeRuntime] = None
    #: The carabiners clipped on the box, Rack or no Rack (24 Oct 2026): rows
    #: of {carabiner, version, sha256, kbh, configured, present, rack,
    #: found_by, install}, as the Observatory's /system/node says them (its
    #: carabiner_inventory). Rows pass through as dicts, like a service's
    #: children. None = an Observatory from before, which does not say.
    carabiners: Optional[list[dict[str, Any]]] = None


@dataclass
class ThermalZone:
    zone: str
    type: str
    temp_c: float


@dataclass
class ThermalResponse:
    """GET /api/v1/system/thermal"""
    available: bool
    zones: Optional[list[ThermalZone]] = None
    max_temp_c: Optional[float] = None


# ── Services ────────────────────────────────────────────────────────────────

@dataclass
class ServiceManifest:
    """One roster file (~/.seren/services/<name>.json). OPEN: a manifest is
    written by an installer and read by the Observatory's manifests module,
    which accepts any key; the ones named here are the ones the head routes
    and draws by. Everything else rides in ``extra`` (health_url,
    config_path, app_dir, systemd_unit, windows_service, description...)."""
    service: str
    implementation: Optional[str] = None
    port: int = 0
    endpoint: Optional[str] = None
    start_script: Optional[str] = None
    stop_script: Optional[str] = None
    pid_path: Optional[str] = None
    log_path: Optional[str] = None
    venv_path: Optional[str] = None
    repo_path: Optional[str] = None
    persistence_dir: Optional[str] = None
    installed_at: Optional[str] = None
    schema_version: int = 0
    service_specific: Optional[dict[str, Any]] = None
    service_type: Optional[str] = None
    #: What it belongs to / what it owns (Observatory manifests, 20 Oct 2026):
    #: `parent` names the owner of a service with a manifest of its own;
    #: `children` says how the owner finds what it starts itself
    #: ({"docker_project": "seren-probe-*"}). The viewer folds the family.
    parent: Optional[str] = None
    children: Optional[dict[str, Any]] = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class PortHealth:
    """GET /api/v1/service/{name}/health, and ``port_health`` on a status."""
    ok: bool
    status_code: Optional[int] = None
    latency_ms: Optional[int] = None
    probed_path: Optional[str] = None
    probed_url: Optional[str] = None
    error: Optional[str] = None
    reason: Optional[str] = None
    #: A library-mode service's /health: {ok, library_mode, service}.
    library_mode: Optional[bool] = None
    service: Optional[str] = None


@dataclass
class ServiceStatus:
    """GET /api/v1/service/{name}/status, and each ``status`` in
    /system/services. OPEN: the universal keys are fields; what one service
    type adds (``compose_service``, ``container_id``, a launcher's ``lock``)
    rides in ``extra``."""
    service: Optional[str] = None
    service_type: Optional[str] = None
    running: bool = False
    pid: Optional[int] = None
    memory_mb: Optional[int] = None
    cpu_percent: Optional[float] = None
    uptime_seconds: Optional[int] = None
    library_mode: Optional[bool] = None
    port_health: Optional[PortHealth] = None
    #: Off on purpose: Lodestar starts it when someone needs it. `idle` is
    #: orchestrated + not running, and the viewer shows it as idle, not down.
    orchestrated: Optional[bool] = None
    idle: Optional[bool] = None
    #: The family (see ServiceManifest.parent / children): the owner's name,
    #: and the rows of what this service owns, each {name, kind, running,
    #: memory_mb, cpu_percent, ...}, as the Observatory found them this poll.
    parent: Optional[str] = None
    children: Optional[list[dict[str, Any]]] = None
    children_error: Optional[str] = None
    #: Why a status could not be read (a manifest with no unit, an unknown
    #: service_type): the Observatory answers 200 with the reason, never 500.
    error: Optional[str] = None
    systemd_unit: Optional[str] = None
    windows_service: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ServiceEntry:
    manifest: ServiceManifest
    status: Optional[ServiceStatus] = None


@dataclass
class ServicesResponse:
    """GET /api/v1/system/services"""
    count: int
    services: dict[str, ServiceEntry]


@dataclass
class HealthResponse:
    """GET /api/v1/system/health. Always 200; ``ok`` is the verdict."""
    ok: bool
    total: int
    healthy: int
    degraded: list[str]
    not_running: list[str]
    #: Off on purpose (orchestrated, waiting to be asked for): not a fault.
    #: An Observatory from before 7 Oct 2026 does not say.
    idle: list[str] = field(default_factory=list)


@dataclass
class ReclaimFailure:
    service: str
    error: Optional[str] = None


@dataclass
class ReclaimResponse:
    """POST /api/v1/system/reclaim"""
    stopped: list[str]
    #: Observatory sends {service, why} rows so a caller can see WHY a service
    #: stayed up; an older Observatory sends bare names. Both pass through.
    kept: list[Any]
    failed: Optional[list[ReclaimFailure]] = None


# ── Service lifecycle ───────────────────────────────────────────────────────

@dataclass
class ServiceLifecycleResponse:
    """POST /api/v1/service/{name}/start | stop | restart. A start or stop
    that RAN and failed is 200 with ok false and the script's words, never a
    500 (a 500 reads as "the node did not answer")."""
    ok: bool
    pid: Optional[int] = None
    already_running: Optional[bool] = None
    was_running: Optional[bool] = None
    exit_code: Optional[int] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    error: Optional[str] = None
    hint: Optional[str] = None
    #: A restart's two halves, each a lifecycle answer of its own.
    stop: Optional[dict[str, Any]] = None
    start: Optional[dict[str, Any]] = None
    port_released: Optional[bool] = None
    #: A library-mode service refusing a lifecycle verb says which type it is.
    service_type: Optional[str] = None
    #: The Observatory restarting itself hands the restart to what started
    #: it ("systemd" / "nssm") and says so, with the service and its exit
    #: policy when NSSM minds it.
    handed_to: Optional[str] = None
    service: Optional[str] = None
    exit_policy: Optional[dict[str, Any]] = None
    #: A launcher that had to be SIGKILLed.
    killed: Optional[bool] = None


@dataclass
class LogsResponse:
    """GET /api/v1/service/{name}/logs"""
    ok: bool
    lines: Optional[list[str]] = None
    log_path: Optional[str] = None
    note: Optional[str] = None
    error: Optional[str] = None
    #: "docker compose logs" when the lines came from the daemon, not a file.
    source: Optional[str] = None


# ── Reboot ──────────────────────────────────────────────────────────────────

@dataclass
class RebootResponse:
    """POST /api/v1/system/reboot"""
    scheduled: bool
    scheduled_at: Optional[str] = None
    delay_minutes: Optional[int] = None
    method: Optional[str] = None
    error: Optional[str] = None
    hint: Optional[str] = None
    command: Optional[str] = None


@dataclass
class RebootCancelResponse:
    """POST /api/v1/system/reboot/cancel"""
    cancelled: bool
    error: Optional[str] = None
    command: Optional[str] = None


# ── Observatory update ──────────────────────────────────────────────────────

@dataclass
class AgentUpdateResponse:
    """POST /api/v1/system/observatory-update"""
    ok: bool
    message: Optional[str] = None
    error: Optional[str] = None


@dataclass
class AgentUpdateNodeResult:
    ok: bool
    node: str
    message: Optional[str] = None
    error: Optional[str] = None


# ── Cluster-internal DTOs (the head's own, never on the node wire) ─────────

@dataclass
class NodeSnapshot:
    online: bool
    installed_services: list[str]
    status: dict[str, Optional[ServiceStatus]]
    last_error: Optional[str] = None
    last_probed: Optional[str] = None  # ISO timestamp string


@dataclass
class RoutedService:
    node_name: str
    capability: str
    manifest: ServiceManifest
    base_url: Optional[str] = None
    library_mode: bool = False


@dataclass
class RefreshSummary:
    total_nodes: int
    online_nodes: int
    per_node: dict[str, NodeSnapshot]


# ── Request bodies ─────────────────────────────────────────────────────────

@dataclass
class ReclaimRequest:
    exclude: Optional[list[str]] = None
    nodes: Optional[list[str]] = None


@dataclass
class RebootRequest:
    delay_minutes: Optional[int] = None


# ── Cluster options (from the head's YAML) ─────────────────────────────────

@dataclass
class NodeOptions:
    """One node as the head's config names it. The field names (agent_url,
    agent_token, agent_update_path) are PUBLIC CONFIG KEYS in every deployed
    seren-lodestar.yaml, which is why they keep the agent-era spelling."""
    name: str
    agent_url: str
    agent_token: str = ""
    preferred_for: list[str] = field(default_factory=list)
    agent_update_path: str = ""
    is_host: bool = False
    nickname: str = ""


#: The name Lodestar's config and tests use.
JetsonNodeOptions = NodeOptions


@dataclass
class ClusterOptions:
    nodes: list[NodeOptions] = field(default_factory=list)
    refresh_interval_seconds: int = 1800  # 30 minutes
    discovery_timeout_seconds: float = 2.0
    health_strict_mode: bool = False


# ── dict <-> dataclass ─────────────────────────────────────────────────────

def _fields(dto_class: type) -> dict[str, dataclasses.Field]:
    return {f.name: f for f in dataclasses.fields(dto_class)}


def _required(f: dataclasses.Field) -> bool:
    return f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING


def _key_name(k: str) -> str:
    name = k.replace("-", "_").replace(".", "_")
    return ALIASES.get(name, name)


def drift(data: Any, dto_class: type, path: str = "") -> list[WireDrift]:
    """Every place the payload and the shape disagree, without raising:
    unknown keys on a FIXED shape (an OPEN one keeps them in ``extra``) and
    required fields the payload lacks, recursing into nested shapes. Empty
    means the wire and the dataclass agree."""
    out: list[WireDrift] = []
    if data is None or not dataclasses.is_dataclass(dto_class) or not isinstance(data, dict):
        return out
    fields = _fields(dto_class)
    hints = typing.get_type_hints(dto_class)
    names = {_key_name(k): k for k in data}
    unknown = [k for n, k in names.items() if n not in fields]
    if EXTRA in fields:
        unknown = []
    missing = [n for n, f in fields.items() if _required(f) and n not in names and n != EXTRA]
    if unknown or missing:
        out.append(WireDrift(dto_class, unknown, missing, path))
    for n, k in names.items():
        if n in fields and n != EXTRA:
            out.extend(_drift_nested(data[k], hints.get(n), f"{path}.{k}" if path else k))
    return out


def _drift_nested(value: Any, hint: Any, path: str) -> list[WireDrift]:
    if value is None or hint is None:
        return []
    origin, args = typing.get_origin(hint), typing.get_args(hint)
    if origin is typing.Union:
        non_none = [a for a in args if a is not type(None)]
        return _drift_nested(value, non_none[0], path) if len(non_none) == 1 else []
    if origin is list and args and isinstance(value, list):
        out: list[WireDrift] = []
        for i, item in enumerate(value):
            out.extend(_drift_nested(item, args[0], f"{path}[{i}]"))
        return out
    if origin is dict and len(args) == 2 and isinstance(value, dict):
        out = []
        for k, item in value.items():
            out.extend(_drift_nested(item, args[1], f"{path}[{k!r}]"))
        return out
    if dataclasses.is_dataclass(hint):
        return drift(value, hint, path)
    return []


def from_dict(data: Any, dto_class: type, *, strict: bool = False, _path: str = "") -> Any:
    """dict -> dataclass, recursing into nested dataclasses, list[...] and
    dict[str, ...].

    LENIENT ON PURPOSE (the default). A required field the payload lacks is
    filled with an empty value of its type instead of raising TypeError. The
    strict version turned ONE manifest with no `service` key into "agent
    returned null" for the whole node - every service on it unroutable
    because of a file the Observatory itself accepts. An unknown key lands in
    ``extra`` when the shape has one and is dropped when it does not.

    ``strict=True`` is for the contract tests: an unknown key on a fixed
    shape or a missing required field raises WireDrift naming it.
    """
    if data is None:
        return None
    if not dataclasses.is_dataclass(dto_class):
        return data
    if not isinstance(data, dict):
        if strict:
            raise WireDrift(dto_class, [f"<{type(data).__name__}, not an object>"], [], _path)
        data = {}
    hints = typing.get_type_hints(dto_class)
    fields = _fields(dto_class)
    kwargs: dict[str, Any] = {}
    extra: dict[str, Any] = {}
    for k, v in data.items():
        name = _key_name(k)
        if name in fields and name != EXTRA:
            kwargs[name] = coerce(v, hints.get(name), strict=strict, _path=f"{_path}.{k}" if _path else k)
        else:
            extra[k] = v
    missing = [n for n, f in fields.items() if n not in kwargs and _required(f) and n != EXTRA]
    if strict and (missing or (extra and EXTRA not in fields)):
        raise WireDrift(dto_class, list(extra) if EXTRA not in fields else [], missing, _path)
    for name in missing:
        kwargs[name] = empty_for(hints.get(name))
    if EXTRA in fields:
        kwargs[EXTRA] = extra
    return dto_class(**kwargs)


def empty_for(hint: Any) -> Any:
    """The zero value of a type hint, for a required field the wire omitted."""
    origin = typing.get_origin(hint)
    if origin is typing.Union:
        return None
    if hint is str:
        return ""
    if hint is int:
        return 0
    if hint is float:
        return 0.0
    if hint is bool:
        return False
    if origin is list or hint is list:
        return []
    if origin is dict or hint is dict:
        return {}
    return None


def coerce(value: Any, hint: Any, *, strict: bool = False, _path: str = "") -> Any:
    if hint is None or value is None:
        return value
    origin, args = typing.get_origin(hint), typing.get_args(hint)
    if origin is typing.Union:                       # Optional[X]
        non_none = [a for a in args if a is not type(None)]
        return coerce(value, non_none[0], strict=strict, _path=_path) if len(non_none) == 1 else value
    if origin is list and args:
        if not isinstance(value, list):
            return value
        return [coerce(i, args[0], strict=strict, _path=f"{_path}[{n}]") for n, i in enumerate(value)]
    if origin is dict and len(args) == 2:
        if not isinstance(value, dict):
            return value
        return {k: coerce(val, args[1], strict=strict, _path=f"{_path}[{k!r}]") for k, val in value.items()}
    if dataclasses.is_dataclass(hint):
        return from_dict(value, hint, strict=strict, _path=_path)
    return value


def to_dict(obj: Any, *, drop_none: bool = False) -> Any:
    """dataclass -> the dict it came from: ``extra`` is folded back to the
    top level, nested shapes recurse. With drop_none, keys whose value is
    None are left out (an Observatory never sends them either)."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        out: dict[str, Any] = {}
        for f in dataclasses.fields(obj):
            v = getattr(obj, f.name)
            if f.name == EXTRA:
                out.update(to_dict(v, drop_none=drop_none))
                continue
            if drop_none and v is None:
                continue
            out[f.name] = to_dict(v, drop_none=drop_none)
        return out
    if isinstance(obj, list):
        return [to_dict(i, drop_none=drop_none) for i in obj]
    if isinstance(obj, dict):
        return {k: to_dict(v, drop_none=drop_none) for k, v in obj.items()}
    return obj


#: The names the helper had in Lodestar's agent_client; its shim re-exports them.
_from_dict = from_dict
_coerce = coerce
_empty_for = empty_for
_ALIASES = ALIASES
