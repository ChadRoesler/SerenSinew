"""
seren_sinew.ondemand - call another Seren app that may not be running yet.

When an app runs on demand (seren_sinew.orchestration RunPolicy), whoever
calls it has to have it started first and keep holding it while it is in
use. This does both, under the caller's own httpx client:

    from seren_sinew.ondemand import OnDemand, OnDemandTransport

    memory = OnDemand(lodestar_url, "memory", holder="seren-corpus-callosum",
                      token=lodestar_token)
    client = httpx.AsyncClient(base_url=memory_url,
                               transport=OnDemandTransport(memory))

Every request through the client:

  - renews the lease when it is older than half its ttl (an ensure to
    Lodestar; quick when the app is already up). A renewal that fails is
    logged and the request goes ahead: Lodestar being away never breaks a
    call to an app that is running.
  - on a refused connection (the app is down) asks Lodestar to ensure it,
    waits until it is ready, and sends the request once more. If Lodestar
    cannot make it ready, the original connection error is raised with why.

Nothing to release by hand: stop calling and the lease runs out (ttl), the
app goes idle, and its policy decides when it stops. `release()` is there for
a caller that knows it is done.

OnDemandSyncTransport / OnDemand.ensure_sync are the same for a sync
httpx.Client. The caller's token for Lodestar is passed in, never read here,
and never logged.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Callable, Optional

import httpx

from .orchestration import ENSURE_PATH, RELEASE_PATH, EnsureRequest, EnsureResult, ReleaseRequest

log = logging.getLogger("seren_sinew.ondemand")

DEFAULT_TTL_SECONDS = 900.0
DEFAULT_WAIT_SECONDS = 120.0


class OnDemand:
    """One app, held through Lodestar on one caller's behalf."""

    def __init__(self, lodestar_url: str, service: str, holder: str, token: str = "",
                 ttl_seconds: float = DEFAULT_TTL_SECONDS, wait_seconds: float = DEFAULT_WAIT_SECONDS,
                 transport: Optional[httpx.AsyncBaseTransport] = None,
                 sync_transport: Optional[httpx.BaseTransport] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.lodestar_url = lodestar_url.rstrip("/")
        self.service = service
        self.holder = holder
        self.ttl_seconds = float(ttl_seconds)
        self.wait_seconds = float(wait_seconds)
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._transport = transport
        self._sync_transport = sync_transport
        self._clock = clock
        self.last: Optional[EnsureResult] = None
        self._renewed_at: Optional[float] = None
        self._alock: Optional[asyncio.Lock] = None
        self._slock = threading.Lock()

    def _request(self, reason: str) -> dict[str, Any]:
        return EnsureRequest(holder=self.holder, reason=reason, wait_seconds=self.wait_seconds,
                             ttl_seconds=self.ttl_seconds).to_dict()

    def _url(self, path: str) -> str:
        return self.lodestar_url + path.format(service=self.service)

    def due(self) -> bool:
        return self._renewed_at is None or self._clock() - self._renewed_at >= self.ttl_seconds / 2.0

    def _took(self, data: Any) -> EnsureResult:
        res = EnsureResult.from_dict(data if isinstance(data, dict) else {})
        self.last = res
        if res.ok and res.ready:
            self._renewed_at = self._clock()
        return res

    # ── async ─────────────────────────────────────────────────────────────
    async def ensure(self, reason: str = "a call") -> EnsureResult:
        """Ask Lodestar for the app, wait until it is ready. Never raises:
        a failure is an EnsureResult with ok/ready false and the reason."""
        if self._alock is None:
            self._alock = asyncio.Lock()
        async with self._alock:
            try:
                async with httpx.AsyncClient(transport=self._transport, headers=self._headers,
                                             timeout=self.wait_seconds + 60.0) as c:
                    r = await c.post(self._url(ENSURE_PATH), json=self._request(reason))
                return self._took(r.json() if r.content else {})
            except (httpx.HTTPError, ValueError) as e:
                self.last = EnsureResult.failed(self.service, f"Lodestar did not answer: {type(e).__name__}: {e}")
                return self.last

    async def release(self, reason: str = "done") -> bool:
        try:
            async with httpx.AsyncClient(transport=self._transport, headers=self._headers, timeout=60.0) as c:
                r = await c.post(self._url(RELEASE_PATH), json=ReleaseRequest(holder=self.holder, reason=reason).to_dict())
            self._renewed_at = None
            return r.is_success
        except httpx.HTTPError:
            return False

    # ── sync ──────────────────────────────────────────────────────────────
    def ensure_sync(self, reason: str = "a call") -> EnsureResult:
        with self._slock:
            try:
                with httpx.Client(transport=self._sync_transport, headers=self._headers,
                                  timeout=self.wait_seconds + 60.0) as c:
                    r = c.post(self._url(ENSURE_PATH), json=self._request(reason))
                return self._took(r.json() if r.content else {})
            except (httpx.HTTPError, ValueError) as e:
                self.last = EnsureResult.failed(self.service, f"Lodestar did not answer: {type(e).__name__}: {e}")
                return self.last

    def release_sync(self, reason: str = "done") -> bool:
        try:
            with httpx.Client(transport=self._sync_transport, headers=self._headers, timeout=60.0) as c:
                r = c.post(self._url(RELEASE_PATH), json=ReleaseRequest(holder=self.holder, reason=reason).to_dict())
            self._renewed_at = None
            return r.is_success
        except httpx.HTTPError:
            return False


def _down(e: Exception) -> bool:
    """A refused connection is an app that is not running. A timeout is not:
    the app may be busy, and starting it again would not help."""
    return isinstance(e, httpx.ConnectError)


class OnDemandTransport(httpx.AsyncBaseTransport):
    """An httpx transport that keeps `ondemand`'s app up for the requests
    that go through it. `inner` is the real transport (default: a plain
    httpx.AsyncHTTPTransport)."""

    def __init__(self, ondemand: OnDemand, inner: Optional[httpx.AsyncBaseTransport] = None) -> None:
        self.ondemand = ondemand
        self.inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()                       # so the body can be sent a second time
        if self.ondemand.due():
            res = await self.ondemand.ensure("renewing the hold")
            if not (res.ok and res.ready):
                log.info("could not renew %s through Lodestar (%s); calling it anyway",
                         self.ondemand.service, res.error)
        try:
            return await self.inner.handle_async_request(request)
        except httpx.TransportError as e:
            if not _down(e):
                raise
            res = await self.ondemand.ensure("it was not running")
            if not (res.ok and res.ready):
                raise httpx.ConnectError(f"{self.ondemand.service} is not running and Lodestar could not start it: "
                                         f"{res.error or 'not ready'}", request=request) from e
            return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


class OnDemandSyncTransport(httpx.BaseTransport):
    """The same, for a sync httpx.Client."""

    def __init__(self, ondemand: OnDemand, inner: Optional[httpx.BaseTransport] = None) -> None:
        self.ondemand = ondemand
        self.inner = inner or httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        request.read()
        if self.ondemand.due():
            res = self.ondemand.ensure_sync("renewing the hold")
            if not (res.ok and res.ready):
                log.info("could not renew %s through Lodestar (%s); calling it anyway",
                         self.ondemand.service, res.error)
        try:
            return self.inner.handle_request(request)
        except httpx.TransportError as e:
            if not _down(e):
                raise
            res = self.ondemand.ensure_sync("it was not running")
            if not (res.ok and res.ready):
                raise httpx.ConnectError(f"{self.ondemand.service} is not running and Lodestar could not start it: "
                                         f"{res.error or 'not ready'}", request=request) from e
            return self.inner.handle_request(request)

    def close(self) -> None:
        self.inner.close()


__all__ = ["OnDemand", "OnDemandTransport", "OnDemandSyncTransport"]
