"""
seren_sinew.cluster.discovery
=======================================================================

Cluster-wide capability router and discovery service.
Ported from SerenCluster/JetsonClusterClient.cs and
JetsonDiscoveryService.cs into Lodestar, moved here 28 Oct 2026 (punch
list sinew-cluster).

Owns one NodeClient per configured node and a capability map.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from .client import NodeClient
from .dtos import (
    ClusterOptions,
    NodeOptions,
    NodeSnapshot,
    RefreshSummary,
    RoutedService,
    ServiceManifest,
)

log = logging.getLogger("seren_sinew.cluster.discovery")


class ClusterClient:
    """Cluster-wide capability router.

    Owns one NodeClient per configured node and a capability map.
    Workers ask get_service_url() "where does X live right now" and connect
    directly to the resolved URL.
    """

    #: How long a node stays out of routing after a connection failure before
    #: it is re-probed on demand. Short on purpose: a refused connection is
    #: usually a service mid-restart, and the fix for a wrong guess is one
    #: cheap probe, not thirty minutes of 503s.
    SUSPECT_BACKOFF_S = 30.0

    def __init__(self, options: ClusterOptions, log_fn=None, default_token: str = ""):
        self._options = options
        self._log = log_fn or (lambda msg: log.info(f"[cluster] {msg}"))
        # node -> (monotonic deadline, reason). See mark_node_suspect.
        self._suspect: dict[str, tuple[float, str]] = {}
        # (node, service) -> why routing must not send a request for that
        # service to that node right now. See hold.
        self._holds: dict[tuple[str, str], str] = {}

        if not options.nodes:
            self._log("no nodes configured - cluster starts empty (zero-config mode)")

        # Sanity: node names must be unique
        names = [n.name for n in options.nodes]
        if len(names) != len(set(names)):
            dupes = {n for n in names if names.count(n) > 1}
            raise ValueError(
                f"Duplicate node name(s) in cluster config: {', '.join(sorted(dupes))}"
            )

        self._agents: dict[str, NodeClient] = {
            n.name: NodeClient(n, log_fn=self._log, default_token=default_token)
            for n in options.nodes
        }

        # Per-node capability snapshots
        self._snapshots: dict[str, NodeSnapshot] = {}

    @property
    def node_names(self) -> list[str]:
        return [n.name for n in self._options.nodes]

    def get_agent(self, node_name: str) -> Optional[NodeClient]:
        return self._agents.get(node_name)

    @property
    def agents(self) -> dict[str, NodeClient]:
        return dict(self._agents)

    # ── discovery ───────────────────────────────────────────────────────────

    async def refresh_async(self) -> RefreshSummary:
        """Re-query every configured node's services in parallel."""
        tasks = [
            self._probe_node(agent)
            for agent in self._agents.values()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        per_node: dict[str, NodeSnapshot] = {}
        online_count = 0
        for name, result in zip(self._agents.keys(), results):
            if isinstance(result, Exception):
                log.warning("refresh failed for %s: %s", name, result)
                snap = NodeSnapshot(
                    online=False,
                    installed_services=[],
                    status={},
                    last_error=str(result),
                    last_probed=datetime.now(timezone.utc).isoformat(),
                )
            else:
                snap = result
            self._snapshots[name] = snap
            per_node[name] = snap
            if snap.online:
                online_count += 1

        self._log(
            f"refresh: {online_count}/{len(self._agents)} nodes online"
        )
        return RefreshSummary(
            total_nodes=len(self._agents),
            online_nodes=online_count,
            per_node=per_node,
        )

    async def refresh_node_async(
        self, node_name: str
    ) -> Optional[NodeSnapshot]:
        """Refresh just one node."""
        agent = self._agents.get(node_name)
        if agent is None:
            return None

        try:
            snap = await self._probe_node(agent)
            self._snapshots[node_name] = snap
            if snap.online:
                self._suspect.pop(node_name, None)
            self._log(
                f"refresh-node {node_name}: online={snap.online} "
                f"services={len(snap.installed_services)}"
            )
            return snap
        except Exception as ex:
            self._log(f"refresh-node {node_name} threw: {ex}")
            return None

    def mark_node_suspect(self, node_name: str, reason: str,
                          backoff_s: Optional[float] = None) -> None:
        """Take a node out of routing for a short backoff, then re-probe it.

        WHAT THIS REPLACED. `mark_node_offline` flipped the snapshot to
        online=False and nothing re-probed until the discovery loop (thirty
        minutes by default) or a manual refresh. One refused connection - a
        llama mid-restart, a reclaim the head itself asked for - cost the
        whole node for half an hour, and because routing needs an online
        snapshot, even "start llama" was refused on the node that needed it.

        Now a failure is a SUSPICION with a deadline. While it stands the node
        is skipped by routing; once it lapses, the next request that needs the
        node probes it once (see get_service_url_async) and either clears the
        suspicion or renews it. The snapshot keeps `online=True` and records
        the reason, so the dashboard shows what happened without the node
        vanishing from the map.
        """
        if node_name not in self._agents:
            return
        until = time.monotonic() + (self.SUSPECT_BACKOFF_S if backoff_s is None else backoff_s)
        self._suspect[node_name] = (until, reason)
        prev = self._snapshots.get(node_name)
        if prev is not None:
            self._snapshots[node_name] = NodeSnapshot(
                online=prev.online,
                installed_services=prev.installed_services,
                status=prev.status,
                last_error=reason,
                last_probed=prev.last_probed,
            )
        self._log(f"suspect: {node_name} for {int(until - time.monotonic())}s ({reason})")

    def mark_node_offline(self, node_name: str, reason: str) -> None:
        """Kept for callers; now means `mark_node_suspect` (see there)."""
        self.mark_node_suspect(node_name, reason)

    def is_suspect(self, node_name: str) -> bool:
        entry = self._suspect.get(node_name)
        return entry is not None and time.monotonic() < entry[0]

    def suspect_reason(self, node_name: str) -> Optional[str]:
        entry = self._suspect.get(node_name)
        return entry[1] if entry is not None and time.monotonic() < entry[0] else None

    async def _reprobe_lapsed_suspects(self) -> None:
        """A node whose suspicion has lapsed gets one probe before routing.

        This is what makes recovery happen on demand instead of on the
        thirty-minute discovery clock.
        """
        lapsed = [n for n, (until, _) in self._suspect.items() if time.monotonic() >= until]
        for name in lapsed:
            self._suspect.pop(name, None)
            await self.refresh_node_async(name)

    async def _probe_node(
        self, agent: NodeClient
    ) -> NodeSnapshot:
        """Probe one node for its services."""
        try:
            services = await asyncio.wait_for(
                agent.get_services_async(),
                timeout=self._options.discovery_timeout_seconds,
            )
            if services is None:
                return NodeSnapshot(
                    online=False,
                    installed_services=[],
                    status={},
                    last_error="agent returned null (unreachable or auth failed)",
                    last_probed=datetime.now(timezone.utc).isoformat(),
                )

            installed = list(services.services.keys())
            status_map = {
                svc: entry.status
                for svc, entry in services.services.items()
            }
            return NodeSnapshot(
                online=True,
                installed_services=installed,
                status=status_map,
                last_error=None,
                last_probed=datetime.now(timezone.utc).isoformat(),
            )
        except asyncio.TimeoutError:
            return NodeSnapshot(
                online=False,
                installed_services=[],
                status={},
                last_error=(
                    f"discovery timed out after "
                    f"{self._options.discovery_timeout_seconds}s"
                ),
                last_probed=datetime.now(timezone.utc).isoformat(),
            )

    # ── routing ─────────────────────────────────────────────────────────────

    async def get_service_url_async(
        self, capability: str
    ) -> Optional[RoutedService]:
        """Resolve 'where is service X right now?'."""
        await self._reprobe_lapsed_suspects()
        node = self._choose_node_for(capability)
        if node is None:
            self._log(
                f"no online node available for capability '{capability}'"
            )
            return None

        snapshot = self._snapshots.get(node.name)
        if snapshot is None or capability not in snapshot.installed_services:
            snapshot = await self.refresh_node_async(node.name)
            if snapshot is None or not snapshot.online or capability not in snapshot.installed_services:
                return None

        agent = self._agents.get(node.name)
        if agent is None:
            return None

        manifest = await agent.get_service_manifest_async(capability)
        if manifest is None:
            return None

        if manifest.port <= 0:
            return RoutedService(
                node_name=node.name,
                capability=capability,
                base_url=None,
                manifest=manifest,
                library_mode=True,
            )

        # Build URL from agent's host + service port
        base = agent.base_url.rstrip("/")
        scheme_host = base.split("://", 1)[-1].split(":")[0]
        # Extract scheme from base
        scheme = "http"
        if "://" in base:
            scheme = base.split("://")[0]
        service_url = f"{scheme}://{scheme_host}:{manifest.port}"

        return RoutedService(
            node_name=node.name,
            capability=capability,
            base_url=service_url,
            manifest=manifest,
            library_mode=False,
        )

    def choose_node_for(self, capability: str) -> Optional[NodeOptions]:
        """Public alias for the preferred-node selection logic."""
        return self._choose_node_for(capability)

    def _choose_node_for(self, capability: str) -> Optional[NodeOptions]:
        """Pick the best node for a capability."""
        # First tier: preferred_for
        for node in self._options.nodes:
            if capability in node.preferred_for and self._is_online_with(node.name, capability):
                self._log(
                    f"routing '{capability}' -> '{node.name}' (preferred)"
                )
                return node

        # Second tier: installed but not preferred
        for node in self._options.nodes:
            if capability not in node.preferred_for and self._is_online_with(node.name, capability):
                self._log(
                    f"routing '{capability}' -> '{node.name}' (fallback)"
                )
                return node

        self._log(
            f"routing '{capability}' -> unavailable (no online node has it)"
        )
        return None

    def nodes_for(self, capability: str) -> list[NodeOptions]:
        """Every online node with a capability, the preferred ones first - for
        a caller that picks among them itself (pictures go to the node whose
        ComfyUI can draw the model asked for)."""
        online = [n for n in self._options.nodes if self._is_online_with(n.name, capability)]
        return sorted(online, key=lambda n: capability not in n.preferred_for)

    # ── holds: a service a node has, that routing must not use right now ──

    def hold(self, node_name: str, service: str, reason: str) -> None:
        """Take ONE service on ONE node out of routing: the box still answers
        and the service is still installed, but a request for it goes to the
        fallback instead. The Transit (28 Oct 2026): while the hippocampus's
        llama has displaced the main one on a box, 'can use?' for the main
        one is no, and Symposium's ask goes elsewhere instead of queueing
        behind a brief. Lifted with unhold; never by time."""
        if node_name in self._agents:
            self._holds[(node_name, service)] = reason
            self._log(f"hold: {node_name}/{service} ({reason})")

    def unhold(self, node_name: str, service: str) -> None:
        if self._holds.pop((node_name, service), None) is not None:
            self._log(f"unhold: {node_name}/{service}")

    def hold_reason(self, node_name: str, service: str) -> Optional[str]:
        return self._holds.get((node_name, service))

    def holds(self) -> dict[str, str]:
        """{"node/service": reason} for every hold standing."""
        return {f"{n}/{s}": why for (n, s), why in sorted(self._holds.items())}

    def _is_online_with(self, node_name: str, capability: str) -> bool:
        if self.is_suspect(node_name):
            return False
        if (node_name, capability) in self._holds:
            return False
        snap = self._snapshots.get(node_name)
        if snap is None:
            return False
        return snap.online and capability in snap.installed_services

    def get_snapshots(self) -> dict[str, NodeSnapshot]:
        return dict(self._snapshots)

    def get_capabilities(self) -> dict[str, list[str]]:
        """Build inverse: capability -> [nodes that have it]."""
        caps: dict[str, list[str]] = {}
        for node_name, snap in self._snapshots.items():
            if not snap.online:
                continue
            for svc in snap.installed_services:
                caps.setdefault(svc, []).append(node_name)
        return caps

    async def aclose(self) -> None:
        """Close every agent's HTTP client. Called on app shutdown."""
        for agent in self._agents.values():
            try:
                await agent.aclose()
            except Exception as ex:
                self._log(f"error closing agent client: {ex}")


# ── Discovery background service ────────────────────────────────────────────

class DiscoveryService:
    """Background service that drives periodic refresh.

    Ported from SerenCluster/JetsonDiscoveryService.cs.
    """

    def __init__(
        self,
        cluster: ClusterClient,
        options: ClusterOptions,
        log_fn=None,
    ):
        self._cluster = cluster
        self._interval = options.refresh_interval_seconds
        self._log = log_fn or (lambda msg: log.info(f"[discovery] {msg}"))
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        """Start the background discovery loop."""
        self._log("startup eager refresh")
        try:
            await self._cluster.refresh_async()
        except Exception as ex:
            self._log(
                f"startup refresh threw: {type(ex).__name__}: {ex}"
            )

        self._log(
            f"periodic refresh every {self._interval / 60:.0f} minutes"
        )

        self._task = asyncio.create_task(self._run_loop())

    async def _run_loop(self):
        while True:
            try:
                await asyncio.sleep(self._interval)
                await self._cluster.refresh_async()
            except asyncio.CancelledError:
                break
            except Exception as ex:
                self._log(
                    f"periodic refresh threw: {type(ex).__name__}: {ex}"
                )

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


#: The names Lodestar gave them; see the package docstring.
JetsonClusterClient = ClusterClient
JetsonDiscoveryService = DiscoveryService

__all__ = ["ClusterClient", "DiscoveryService", "JetsonClusterClient", "JetsonDiscoveryService"]
