"""
seren_sinew.cluster.client
=======================================================================

Typed HTTP client for one node's SerenObservatory API at /api/v1/...
Ported from SerenCluster/JetsonAgentClient.cs into Lodestar, moved here
28 Oct 2026 (punch list sinew-cluster).

The node can be a Jetson, a Spark, a NUC or anything else running an
Observatory - the head doesn't care what the hardware is, only that
something answers at /api/v1/system/ping. The Jetson-flavoured names
(JetsonAgentClient, JetsonNodeOptions, agent_url) predate the Spark and are
kept because agent_url and friends are PUBLIC CONFIG KEYS; renaming them
breaks every deployed seren-lodestar.yaml. Do it as one deliberate
migration, not opportunistically.

One instance per node - see discovery.py for routing across many.

Needs httpx (the ``client`` extra); every Seren service has it already.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import httpx

from ..request_log import request_id_hooks
from .dtos import (
    AgentUpdateResponse,
    HealthResponse,
    LogsResponse,
    NodeOptions,
    NodeResponse,
    PingResponse,
    PortHealth,
    RebootCancelResponse,
    RebootResponse,
    ReclaimResponse,
    ServiceLifecycleResponse,
    ServiceManifest,
    ServiceStatus,
    ServicesResponse,
    ThermalResponse,
    VersionResponse,
    from_dict,
)

log = logging.getLogger("seren_sinew.cluster.client")


#: The three reads the dashboard poll makes of every node (node, thermal,
#: services) wait this long, not the client's 35s. A node that drops its
#: packets (the desktop with its service stopped, 20 Oct 2026) cost every
#: status answer 35s, and the dashboard read "unreachable" for the whole
#: cluster while five nodes were fine. 8s is long enough for a services
#: answer with docker on a slow box; the discovery loop has its own clock.
STATUS_READ_TIMEOUT_S = 8.0


class NodeClient:
    """Typed HTTP client for one node's Observatory API."""

    def __init__(self, options: NodeOptions, log_fn=None, default_token: str = ""):
        if not options.name:
            raise ValueError("node name must not be empty")
        if not options.agent_url:
            raise ValueError("agent_url must not be empty")

        self._node_name = options.name
        self._log = log_fn or (lambda msg: log.info(f"[{self._node_name}] {msg}"))
        # The node's own token wins; the head's own bearer fills in when the
        # operator chose runtime.inject_bearer_token (one secret, every node).
        self._agent_token = options.agent_token or default_token
        self._agent_update_path = options.agent_update_path
        self._is_host = options.is_host
        self._nickname = options.nickname or ""

        base = options.agent_url.rstrip("/") + "/"
        headers = {}
        if self._agent_token:
            headers["Authorization"] = f"Bearer {self._agent_token}"
        self._client = httpx.AsyncClient(base_url=base, headers=headers, timeout=35.0,
                                         event_hooks=request_id_hooks(asynchronous=True))

    # ── properties ──────────────────────────────────────────────────────────

    @property
    def node_name(self) -> str:
        return self._node_name

    @property
    def base_url(self) -> str:
        return str(self._client.base_url)

    @property
    def agent_update_path(self) -> str:
        return self._agent_update_path

    @property
    def is_host(self) -> bool:
        return self._is_host

    @property
    def nickname(self) -> str:
        return self._nickname

    # ── system endpoints ────────────────────────────────────────────────────

    async def ping_async(self) -> Optional[PingResponse]:
        return await self._get_json("api/v1/system/ping", PingResponse)

    async def version_async(self) -> Optional[VersionResponse]:
        return await self._get_json("api/v1/system/version", VersionResponse)

    async def get_node_async(self) -> Optional[NodeResponse]:
        return await self._get_json("api/v1/system/node", NodeResponse, timeout=STATUS_READ_TIMEOUT_S)

    async def get_thermal_async(self) -> Optional[ThermalResponse]:
        return await self._get_json("api/v1/system/thermal", ThermalResponse, timeout=STATUS_READ_TIMEOUT_S)

    async def get_services_async(self) -> Optional[ServicesResponse]:
        resp = await self._get_json("api/v1/system/services", ServicesResponse, timeout=STATUS_READ_TIMEOUT_S)
        if resp is not None and isinstance(resp.services, dict):
            # Observatory allows a manifest with no `service` key (the name
            # falls back to the filename stem). The key we were handed IS the
            # name; fill it in rather than fail the whole node over one file.
            for key, entry in resp.services.items():
                manifest = getattr(entry, "manifest", None)
                if manifest is not None and not getattr(manifest, "service", None):
                    manifest.service = key
        return resp

    async def get_health_async(self) -> Optional[HealthResponse]:
        return await self._get_json("api/v1/system/health", HealthResponse)

    async def reclaim_async(
        self, exclude: Optional[list[str]] = None, *,
        include: Optional[list[str]] = None, everything: bool = False,
    ) -> Optional[ReclaimResponse]:
        body = {"exclude": exclude or [], "include": include or [], "all": bool(everything)}
        return await self._post_json("api/v1/system/reclaim", body, ReclaimResponse)

    async def reboot_async(
        self, delay_minutes: int = 1
    ) -> Optional[RebootResponse]:
        body = {"delay_minutes": delay_minutes}
        return await self._post_json("api/v1/system/reboot", body, RebootResponse)

    async def ripple_async(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Forward a ripple to this node's Observatory. The Observatory's own
        status and body come back as they are: its reasons ("not set up on this
        node", "alice is not logged on") are the answer, and a None - what
        _post_json makes of any non-2xx - would erase them."""
        try:
            resp = await self._client.post("api/v1/system/ripple", json=body, timeout=30.0)
        except Exception as ex:  # noqa: BLE001
            return 502, {"ok": False, "error": f"the node's Observatory did not answer: {type(ex).__name__}: {ex}"}
        try:
            data = resp.json()
        except ValueError:
            data = {"ok": resp.is_success, "error": resp.text[:400]}
        if not isinstance(data, dict):
            data = {"ok": resp.is_success, "answer": data}
        return resp.status_code, data

    async def carabiner_call_async(self, method: str, path: str, body: Optional[dict[str, Any]] = None,
                                   timeout: float = 90.0) -> tuple[int, dict[str, Any]]:
        """A carabiner route on this node's Observatory (seren_sinew.carabiners).
        Status and body come back as the Observatory gave them, like a
        ripple's: a carabiner's refusals and "not set up on this node" are the
        answer. The timeout covers kbh running belay after a change."""
        try:
            if method == "POST":
                resp = await self._client.post(path.lstrip("/"), json=body or {}, timeout=timeout)
            else:
                resp = await self._client.get(path.lstrip("/"), timeout=timeout)
        except Exception as ex:  # noqa: BLE001
            return 502, {"ok": False, "error": f"the node's Observatory did not answer: {type(ex).__name__}: {ex}"}
        try:
            data = resp.json()
        except ValueError:
            data = {"ok": False, "error": resp.text[:400]}
        if not isinstance(data, dict):
            data = {"ok": resp.is_success, "answer": data}
        return resp.status_code, data

    async def start_upgrade_async(self, service: str, body: dict[str, Any],
                                  timeout: float = 900.0) -> tuple[int, dict[str, Any]]:
        """Ask the node's Observatory to upgrade one service (or, with
        dry_run, to say what it would do). Status and body come back as the
        Observatory gave them: 202 with the job, 409 'not upgradable' or
        'busy', each with its reason. The timeout covers a dry run, which is
        pip resolving the whole tree."""
        path = f"api/v1/service/{quote(service, safe='')}/upgrade"
        try:
            resp = await self._client.post(path, json=body, timeout=timeout)
        except Exception as ex:  # noqa: BLE001
            return 502, {"ok": False, "error": f"the node's Observatory did not answer: {type(ex).__name__}: {ex}"}
        try:
            data = resp.json()
        except ValueError:
            data = {"ok": resp.is_success, "error": resp.text[:400]}
        if not isinstance(data, dict):
            data = {"ok": resp.is_success, "answer": data}
        return resp.status_code, data

    async def set_orchestrated_async(self, service: str, flag: bool = True) -> bool:
        """Mark a service as started on demand on its Observatory, so off
        reads as idle there. True when the Observatory took it."""
        path = f"api/v1/service/{quote(service, safe='')}/orchestrated"
        try:
            resp = await self._client.post(path, json={"orchestrated": bool(flag)}, timeout=15.0)
            return resp.is_success
        except Exception as ex:  # noqa: BLE001
            self._log(f"POST {path} -> {type(ex).__name__}: {ex}")
            return False

    async def get_upgrade_job_async(self, job_id: str) -> Optional[dict[str, Any]]:
        """One upgrade job as the Observatory has it, or None (not found, or
        the Observatory is not answering - it may be the thing restarting)."""
        data = await self._get_json_raw(f"api/v1/upgrades/{quote(job_id, safe='')}")
        return (data or {}).get("job") if isinstance(data, dict) else None

    async def get_upgrade_versions_async(self) -> Optional[dict[str, Any]]:
        """Every service on the node its Observatory can upgrade: {name:
        {package, version, needs}}. None when the Observatory is older than
        the route, or not answering. Slow-ish: it asks each venv."""
        try:
            # Two minutes for the answer (it asks each venv), five seconds to
            # connect: a node whose port drops packets (the desktop with its
            # service down, 20 Oct 2026) held every behind for the full 120s.
            resp = await self._client.get("api/v1/upgrades/versions", timeout=httpx.Timeout(120.0, connect=5.0))
            data = resp.json() if resp.is_success else None
        except Exception as ex:  # noqa: BLE001
            self._log(f"GET api/v1/upgrades/versions -> {type(ex).__name__}: {ex}")
            return None
        svcs = (data or {}).get("services") if isinstance(data, dict) else None
        return svcs if isinstance(svcs, dict) else None

    async def reboot_cancel_async(self) -> Optional[RebootCancelResponse]:
        return await self._post_json(
            "api/v1/system/reboot/cancel", None, RebootCancelResponse
        )

    async def push_agent_update_async(
        self, package_bytes: bytes, filename: str, dest_path: str
    ) -> Optional[AgentUpdateResponse]:
        """Push a seren-observatory.tar.gz package to this node."""
        try:
            files = {
                "package": (filename, package_bytes, "application/octet-stream"),
                "dest_path": (None, dest_path),
            }
            # NOT "agent-update". Observatory serves this as
            # /api/v1/system/observatory-update - the endpoint was renamed
            # with the service and this caller wasn't, so every push 404'd.
            resp = await self._client.post(
                "api/v1/system/observatory-update",
                files=files,
                timeout=120.0,
            )
            if not resp.is_success:
                self._log(
                    f"POST observatory-update -> HTTP {resp.status_code}"
                )
                return AgentUpdateResponse(
                    ok=False, error=f"HTTP {resp.status_code}"
                )
            return from_dict(resp.json(), AgentUpdateResponse)
        except httpx.TimeoutException:
            self._log("POST observatory-update -> timeout")
            return AgentUpdateResponse(ok=False, error="timeout")
        except Exception as ex:
            self._log(
                f"POST observatory-update -> {type(ex).__name__}: {ex}"
            )
            return AgentUpdateResponse(ok=False, error=str(ex))

    # ── per-service lifecycle ───────────────────────────────────────────────

    async def get_service_manifest_async(
        self, service: str
    ) -> Optional[ServiceManifest]:
        path = f"api/v1/service/{quote(service, safe='')}/manifest"
        return await self._get_json(path, ServiceManifest)

    async def get_service_status_async(
        self, service: str
    ) -> Optional[ServiceStatus]:
        path = f"api/v1/service/{quote(service, safe='')}/status"
        return await self._get_json(path, ServiceStatus)

    async def get_service_health_async(
        self, service: str
    ) -> Optional[PortHealth]:
        path = f"api/v1/service/{quote(service, safe='')}/health"
        return await self._get_json(path, PortHealth)

    async def start_service_async(
        self, service: str
    ) -> Optional[ServiceLifecycleResponse]:
        path = f"api/v1/service/{quote(service, safe='')}/start"
        return await self._post_json(path, None, ServiceLifecycleResponse)

    async def ensure_service_async(self, service: str, request: Any) -> Any:
        """Ask the node to make the service READY (start it if it is down and
        wait for its health check): seren_sinew.orchestration, the
        Observatory's POST /service/{name}/ensure. Returns an EnsureResult,
        never None - a node that could not be asked is ok False with the
        reason. The timeout follows the wait that was asked for."""
        from ..orchestration import EnsureResult
        path = f"api/v1/service/{quote(service, safe='')}/ensure"
        try:
            resp = await self._client.post(path, json=request.to_dict(),
                                           timeout=float(request.wait_seconds) + 45.0)
            if resp.status_code == 404:
                return EnsureResult.failed(service, f"'{self._node_name}' has no service named '{service}' "
                                                    "(or its Observatory predates ensure)", self._node_name)
            if not resp.is_success:
                self._log(f"POST {path} -> HTTP {resp.status_code}")
                return EnsureResult.failed(service, f"the Observatory on '{self._node_name}' answered "
                                                    f"{resp.status_code}", self._node_name)
            out = EnsureResult.from_dict(resp.json())
            out.node = self._node_name                # the name the head knows it by
            return out
        except httpx.TimeoutException:
            self._log(f"POST {path} -> timeout")
            return EnsureResult.failed(service, f"the Observatory on '{self._node_name}' did not answer in time",
                                       self._node_name)
        except Exception as ex:  # noqa: BLE001
            self._log(f"POST {path} -> {type(ex).__name__}: {ex}")
            return EnsureResult.failed(service, f"{type(ex).__name__}: {ex}", self._node_name)

    async def stop_service_async(
        self, service: str
    ) -> Optional[ServiceLifecycleResponse]:
        path = f"api/v1/service/{quote(service, safe='')}/stop"
        return await self._post_json(path, None, ServiceLifecycleResponse)

    async def restart_service_async(
        self, service: str
    ) -> Optional[ServiceLifecycleResponse]:
        path = f"api/v1/service/{quote(service, safe='')}/restart"
        return await self._post_json(path, None, ServiceLifecycleResponse)

    async def get_service_logs_async(
        self, service: str, lines: int = 100
    ) -> Optional[LogsResponse]:
        path = f"api/v1/service/{quote(service, safe='')}/logs?lines={lines}"
        return await self._get_json(path, LogsResponse)

    # ── service-specific models (llama, comfy, whisper) ─────────────────────

    async def get_service_models_async(
        self, service: str
    ) -> Optional[Any]:
        """Returns raw JSON element since per-service shapes differ."""
        path = f"api/v1/service/{quote(service, safe='')}/models"
        return await self._get_json_raw(path)

    async def service_models_call_async(self, method: str, service: str, sub: str,
                                        body: Optional[Any] = None) -> tuple[int, Any]:
        """A model-shelf call on the node (/models/fetch, /model, a delete),
        passed through as it is: (status, JSON). The node's own refusals - a
        409 "already here", a 404 "no such file" - are the answer, so they
        are handed back rather than flattened to None. (0, None) when the
        node did not answer."""
        path = f"api/v1/service/{quote(service, safe='')}/{sub}"
        try:
            resp = await self._client.request(method, path, json=body, timeout=60.0)
            try:
                data = resp.json()
            except ValueError:
                data = {"detail": resp.text[:300]}
            return resp.status_code, data
        except httpx.TimeoutException:
            self._log(f"{method} {path} -> timeout")
            return 0, None
        except Exception as ex:  # noqa: BLE001
            self._log(f"{method} {path} -> {type(ex).__name__}: {ex}")
            return 0, None

    # ── lifecycle ───────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        """Close the underlying HTTP client and its connection pool. A method
        on the class: it once sat at column zero in Lodestar, a free function
        that happened to take `self`, and every clean stop raised
        AttributeError with no pool ever closed."""
        await self._client.aclose()

    # ── what a service keeps, through its node (seren_sinew.stores, proxied
    # by the Observatory's stores_routes) ─────────────────────────────────

    async def get_service_stores_async(self, service: str) -> Optional[dict[str, Any]]:
        """What the service keeps and its snapshots; None when it keeps none
        (404), is not installed, or did not answer."""
        return await self._get_json_raw(f"api/v1/service/{quote(service, safe='')}/stores")

    async def list_service_snapshots_async(self, service: str) -> Optional[dict[str, Any]]:
        return await self._get_json_raw(f"api/v1/service/{quote(service, safe='')}/stores/snapshots")

    async def take_service_snapshot_async(self, service: str, reason: str = "Lodestar asked") -> Optional[dict[str, Any]]:
        """Ask the service for a fresh snapshot (a mutating call: the
        Observatory's interlock applies). The listing row, or None."""
        path = f"api/v1/service/{quote(service, safe='')}/stores/snapshot"
        try:
            resp = await self._client.post(path, json={"reason": reason}, timeout=180.0)
            if not resp.is_success:
                self._log(f"POST {path} -> HTTP {resp.status_code}")
                return None
            return (resp.json() or {}).get("snapshot")
        except httpx.TimeoutException:
            self._log(f"POST {path} -> timeout")
            return None
        except Exception as ex:  # noqa: BLE001
            self._log(f"POST {path} -> {type(ex).__name__}: {ex}")
            return None

    async def rehearse_service_archive_async(self, service: str, data: bytes) -> dict[str, Any]:
        """Send one snapshot (a tar.gz) down to the service for a restore's
        dry run; the service's report. Never None: when the node or the
        service could not be asked, the answer says so with ok False."""
        path = f"api/v1/service/{quote(service, safe='')}/stores/rehearse"
        try:
            resp = await self._client.post(path, content=data, headers={"Content-Type": "application/gzip"},
                                           timeout=900.0)
            try:
                body = resp.json()
            except ValueError:
                body = None
            if resp.is_success and isinstance(body, dict):
                return body
            why = (body or {}).get("detail") or (body or {}).get("error") if isinstance(body, dict) else None
            self._log(f"POST {path} -> HTTP {resp.status_code}")
            return {"ok": False, "problems": [f"the node answered {resp.status_code}: {why or resp.text[:200]}"]}
        except httpx.TimeoutException:
            self._log(f"POST {path} -> timeout")
            return {"ok": False, "problems": ["the rehearsal did not answer in time"]}
        except Exception as ex:  # noqa: BLE001
            self._log(f"POST {path} -> {type(ex).__name__}: {ex}")
            return {"ok": False, "problems": [f"{type(ex).__name__}: {ex}"]}

    async def fetch_service_snapshot_archive_async(self, service: str, snapshot_id: str, dest: "Path",
                                                   since: Optional[str] = None) -> Optional[str]:
        """Stream one snapshot's archive into the file `dest` (never held in
        memory: a store of pictures is gigabytes). `since`: a snapshot the
        stash already holds; the service then sends only what changed and
        names it in X-Seren-Since. Returns that name ("" for a whole
        archive), or None when nothing could be fetched. 15 Oct 2026."""
        path = f"api/v1/service/{quote(service, safe='')}/stores/snapshots/{quote(snapshot_id, safe='')}/archive"
        dest = Path(dest)
        tmp = dest.with_name(dest.name + ".partial")
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            async with self._client.stream("GET", path, params={"since": since} if since else None,
                                           timeout=1800.0) as resp:
                if not resp.is_success:
                    self._log(f"GET {path} -> HTTP {resp.status_code}")
                    return None
                with open(tmp, "wb") as f:
                    async for chunk in resp.aiter_bytes():
                        f.write(chunk)
                os.replace(tmp, dest)
                return resp.headers.get("x-seren-since", "") or ""
        except httpx.TimeoutException:
            self._log(f"GET {path} -> timeout")
            return None
        except Exception as ex:  # noqa: BLE001
            self._log(f"GET {path} -> {type(ex).__name__}: {ex}")
            return None
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    async def get_service_snapshot_archive_async(self, service: str, snapshot_id: str) -> Optional[bytes]:
        """One snapshot as a tar.gz, or None. The older, in-memory fetch;
        fetch_service_snapshot_archive_async streams to a file."""
        path = f"api/v1/service/{quote(service, safe='')}/stores/snapshots/{quote(snapshot_id, safe='')}/archive"
        try:
            resp = await self._client.get(path, timeout=300.0)
            if not resp.is_success:
                self._log(f"GET {path} -> HTTP {resp.status_code}")
                return None
            return resp.content
        except httpx.TimeoutException:
            self._log(f"GET {path} -> timeout")
            return None
        except Exception as ex:  # noqa: BLE001
            self._log(f"GET {path} -> {type(ex).__name__}: {ex}")
            return None

    # ── the node's own senses, relayed ──────────────────────────────────────

    async def comfy_async(self, method: str, path: str, body: Optional[Any] = None,
                          timeout: float = 35.0) -> tuple[int, Any]:
        """The node's comfy routes (/api/v1/service/comfy/...): (status, JSON).
        Never raises; 0 means the node did not answer, with why in the body.
        Generating takes minutes, so the caller says how long to wait."""
        try:
            resp = await self._client.request(method, f"api/v1/service/comfy/{path.lstrip('/')}",
                                              json=body, timeout=timeout)
            try:
                data = resp.json()
            except ValueError:
                data = {"error": resp.text[:500]}
            return resp.status_code, data
        except httpx.TimeoutException:
            return 0, {"error": f"{self._node_name} did not answer in {timeout:.0f}s"}
        except Exception as ex:  # noqa: BLE001 - a node that cannot be reached is an answer, not a crash
            return 0, {"error": f"{self._node_name}: {type(ex).__name__}: {ex}"}

    async def transcribe_async(self, data: bytes, filename: str, language: str = "",
                               timeout: float = 120.0) -> tuple[int, Any]:
        """The node's whisper (/api/v1/service/whisper/transcribe): the clip
        as multipart, (status, JSON) back. Never raises; 0 means the node did
        not answer, with why in the body."""
        try:
            fields = {"timeout_seconds": str(timeout)}
            if language:
                fields["language"] = language
            resp = await self._client.post("api/v1/service/whisper/transcribe",
                                           files={"file": (filename, data, "audio/wav")}, data=fields,
                                           timeout=timeout + 15)
            try:
                body = resp.json()
            except ValueError:
                body = {"error": resp.text[:500]}
            return resp.status_code, body
        except httpx.TimeoutException:
            return 0, {"error": f"{self._node_name} did not answer in {timeout:.0f}s"}
        except Exception as ex:  # noqa: BLE001 - a node that cannot be reached is an answer, not a crash
            return 0, {"error": f"{self._node_name}: {type(ex).__name__}: {ex}"}

    async def listen_async(self, files: dict[str, tuple[str, bytes, str]], prompt: str = "", mode: str = "listen",
                           max_new_tokens: int = 400, timeout: float = 240.0) -> tuple[int, Any]:
        """The node's listener (/api/v1/service/listener/{listen|compare}):
        the clip(s) as multipart and the question, (status, JSON) back.
        Never raises; 0 means the node did not answer, with why in the body."""
        try:
            fields = {"prompt": prompt or "", "max_new_tokens": str(int(max_new_tokens)), "timeout_seconds": str(timeout)}
            resp = await self._client.post(f"api/v1/service/listener/{'compare' if mode == 'compare' else 'listen'}",
                                           files=files, data=fields, timeout=timeout + 15)
            try:
                body = resp.json()
            except ValueError:
                body = {"error": resp.text[:500]}
            return resp.status_code, body
        except httpx.TimeoutException:
            return 0, {"error": f"{self._node_name} did not answer in {timeout:.0f}s"}
        except Exception as ex:  # noqa: BLE001 - a node that cannot be reached is an answer, not a crash
            return 0, {"error": f"{self._node_name}: {type(ex).__name__}: {ex}"}

    async def speak_async(self, text: str, voice: str = "wren",
                          timeout: float = 180.0) -> tuple[int, Any, dict[str, str]]:
        """The node's speaker (/api/v1/service/speaker/speak): the words and
        the voice, (status, WAV bytes, headers) back - or (status, JSON, {})
        when it could not. Never raises; 0 means the node did not answer."""
        try:
            resp = await self._client.post("api/v1/service/speaker/speak",
                                           json={"text": text, "voice": voice, "timeout_seconds": timeout},
                                           timeout=timeout + 15)
            if resp.status_code == 200 and resp.content.startswith(b"RIFF"):
                return 200, resp.content, dict(resp.headers)
            try:
                body = resp.json()
            except ValueError:
                body = {"error": resp.text[:500]}
            return resp.status_code, body, {}
        except httpx.TimeoutException:
            return 0, {"error": f"{self._node_name} did not answer in {timeout:.0f}s"}, {}
        except Exception as ex:  # noqa: BLE001 - a node that cannot be reached is an answer, not a crash
            return 0, {"error": f"{self._node_name}: {type(ex).__name__}: {ex}"}, {}

    async def relay_json_async(self, path: str, timeout: float = 35.0) -> tuple[int, Any]:
        """A GET relayed through the node's Observatory: (status, JSON).
        Never raises; 0 means the node did not answer, with why in the
        body. For a route that fronts a node's own route (the gallery)."""
        try:
            resp = await self._client.get(path, timeout=timeout)
            try:
                data = resp.json()
            except ValueError:
                data = {"error": resp.text[:500]}
            return resp.status_code, data
        except httpx.TimeoutException:
            return 0, {"error": f"{self._node_name} did not answer in {timeout:.0f}s"}
        except Exception as ex:  # noqa: BLE001
            return 0, {"error": f"{self._node_name}: {type(ex).__name__}: {ex}"}

    async def open_relay_async(self, path: str, timeout: float = 300.0) -> Optional[httpx.Response]:
        """A GET relayed through the node's Observatory, streamed: the
        httpx.Response, still open, for a route to pass the body through
        (it closes it). None when the node did not answer."""
        try:
            return await self._client.send(self._client.build_request("GET", path, timeout=timeout), stream=True)
        except Exception as ex:  # noqa: BLE001
            self._log(f"GET {path} -> {type(ex).__name__}: {ex}")
            return None

    # ── internal HTTP helpers ───────────────────────────────────────────────

    async def _get_json(self, path: str, dto_class: type, timeout: Optional[float] = None) -> Optional[Any]:
        try:
            resp = await self._client.get(path, timeout=timeout) if timeout else await self._client.get(path)
            if not resp.is_success:
                self._log(f"GET {path} -> HTTP {resp.status_code}")
                return None
            data = resp.json()
            return from_dict(data, dto_class)
        except httpx.TimeoutException:
            self._log(f"GET {path} -> timeout")
            return None
        except Exception as ex:
            self._log(f"GET {path} -> {type(ex).__name__}: {ex}")
            return None

    async def _get_json_raw(self, path: str) -> Optional[Any]:
        """Return raw JSON data (dict/list) without DTO mapping."""
        try:
            resp = await self._client.get(path)
            if not resp.is_success:
                self._log(f"GET {path} -> HTTP {resp.status_code}")
                return None
            return resp.json()
        except httpx.TimeoutException:
            self._log(f"GET {path} -> timeout")
            return None
        except Exception as ex:
            self._log(f"GET {path} -> {type(ex).__name__}: {ex}")
            return None

    async def _post_json(
        self, path: str, body: Optional[Any], dto_class: type
    ) -> Optional[Any]:
        try:
            if body is None:
                resp = await self._client.post(path)
            else:
                resp = await self._client.post(path, json=body)
            if not resp.is_success:
                self._log(f"POST {path} -> HTTP {resp.status_code}")
                return None
            data = resp.json()
            return from_dict(data, dto_class)
        except httpx.TimeoutException:
            self._log(f"POST {path} -> timeout")
            return None
        except Exception as ex:
            self._log(f"POST {path} -> {type(ex).__name__}: {ex}")
            return None


#: The name Lodestar gave it; see the package docstring.
JetsonAgentClient = NodeClient

__all__ = ["NodeClient", "JetsonAgentClient", "STATUS_READ_TIMEOUT_S"]
