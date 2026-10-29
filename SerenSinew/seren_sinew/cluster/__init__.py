"""seren_sinew.cluster - the wire between a head and its nodes, spelled once.

Lodestar (the head) and the Observatory (one per node) talk over
``/api/v1/...``. Until 28 Oct 2026 each side spelled the shapes on that wire
itself: Lodestar in its ``dtos.py`` / ``agent_client.py`` / ``cluster.py``,
the Observatory in the dicts its routes build. Nothing held the two to each
other, and they drifted: the Observatory renamed ``agent_version`` to
``observatory_version`` and every ``version_async`` quietly returned None.

Now the shapes live here, both sides import them, and a contract test on
each side parses the Observatory's real answers STRICTLY with these
dataclasses, so a key one side adds and the other does not know is a failing
test instead of a silent drop.

Three modules:

- ``dtos``       the dataclasses, ``from_dict`` (lenient by default, strict
                 on request) and ``to_dict``. Needs nothing beyond the
                 standard library, so a service can import the shapes
                 without pulling an HTTP client in.
- ``client``     ``NodeClient``: the typed HTTP client for one node's
                 Observatory. Needs httpx (the ``client`` extra).
- ``discovery``  ``ClusterClient`` (one NodeClient per node, routing by
                 capability, suspicion with a backoff) and
                 ``DiscoveryService`` (the periodic refresh).

The old Jetson-flavoured names (``JetsonAgentClient``, ``JetsonClusterClient``,
``JetsonDiscoveryService``, ``JetsonNodeOptions``) remain as aliases: they
are public in Lodestar's config and tests, and a rename is a migration of
its own.
"""
from __future__ import annotations

from .dtos import (  # noqa: F401 - the shapes are the package's public face
    ALIASES,
    AgentUpdateNodeResult,
    AgentUpdateResponse,
    ClusterOptions,
    HealthResponse,
    JetsonNodeOptions,
    LogsResponse,
    NodeManifest,
    NodeOptions,
    NodeResponse,
    NodeRuntime,
    NodeSnapshot,
    PingResponse,
    PortHealth,
    RebootCancelResponse,
    RebootRequest,
    RebootResponse,
    ReclaimFailure,
    ReclaimRequest,
    ReclaimResponse,
    RefreshSummary,
    RoutedService,
    ServiceEntry,
    ServiceLifecycleResponse,
    ServiceManifest,
    ServiceStatus,
    ServicesResponse,
    ThermalResponse,
    ThermalZone,
    VersionResponse,
    WireDrift,
    drift,
    from_dict,
    to_dict,
)

__all__ = [
    "ALIASES", "AgentUpdateNodeResult", "AgentUpdateResponse", "ClusterOptions", "HealthResponse",
    "JetsonNodeOptions", "LogsResponse", "NodeManifest", "NodeOptions", "NodeResponse", "NodeRuntime",
    "NodeSnapshot", "PingResponse", "PortHealth", "RebootCancelResponse", "RebootRequest", "RebootResponse",
    "ReclaimFailure", "ReclaimRequest", "ReclaimResponse", "RefreshSummary", "RoutedService", "ServiceEntry",
    "ServiceLifecycleResponse", "ServiceManifest", "ServiceStatus", "ServicesResponse", "ThermalResponse",
    "ThermalZone", "VersionResponse", "WireDrift", "drift", "from_dict", "to_dict",
]
