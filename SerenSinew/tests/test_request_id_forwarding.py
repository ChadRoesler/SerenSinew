"""
The request id goes on to the next hop.

The middleware minted an id and echoed it, but no service sent it on, so
every hop started a new one and a call could not be followed from one log
into the next (design note, 25 Oct 2026). Now the id is bound for the life of
the request, a client built with request_id_hooks() carries it, and work no
request started binds its own with request_id_scope().
"""
from __future__ import annotations

import logging

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from seren_sinew.request_log import (
    REQUEST_ID_HEADER, RequestLoggingMiddleware, current_request_id, request_id_hooks, request_id_scope,
)


@pytest.fixture(autouse=True)
def _clean_request_loggers():
    yield
    for name in list(logging.root.manager.loggerDict):
        if name.endswith(".requests"):
            lg = logging.getLogger(name)
            for h in list(lg.handlers):
                h.close()
                lg.removeHandler(h)


def _next_hop(seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get(REQUEST_ID_HEADER))
        return httpx.Response(200, text="ok")
    return handler


def _app(tmp_path, seen: list):
    def sync_route(request):
        with httpx.Client(transport=httpx.MockTransport(_next_hop(seen)), event_hooks=request_id_hooks()) as c:
            c.get("http://memory.test/x")
        return PlainTextResponse("sync")

    async def async_route(request):
        async with httpx.AsyncClient(transport=httpx.MockTransport(_next_hop(seen)),
                                     event_hooks=request_id_hooks(asynchronous=True)) as c:
            await c.get("http://memory.test/x")
        return PlainTextResponse("async")

    app = Starlette(routes=[Route("/sync", sync_route), Route("/async", async_route)])
    app.add_middleware(RequestLoggingMiddleware, service_name="t-forward", log_dir=str(tmp_path))
    return app


@pytest.mark.parametrize("path", ["/sync", "/async"])
def test_the_inbound_id_rides_on_the_outbound_call(tmp_path, path):
    seen: list = []
    r = TestClient(_app(tmp_path, seen)).get(path, headers={REQUEST_ID_HEADER: "lodestar-abc123"})
    assert r.status_code == 200 and seen == ["lodestar-abc123"]


@pytest.mark.parametrize("path", ["/sync", "/async"])
def test_a_minted_id_rides_on_too(tmp_path, path):
    seen: list = []
    r = TestClient(_app(tmp_path, seen)).get(path)
    assert seen == [r.headers[REQUEST_ID_HEADER]]


def test_the_binding_ends_with_the_request(tmp_path):
    TestClient(_app(tmp_path, [])).get("/sync", headers={REQUEST_ID_HEADER: "gone-after"})
    assert current_request_id() is None


def test_nothing_bound_sends_nothing_and_the_next_hop_mints():
    seen: list = []
    with httpx.Client(transport=httpx.MockTransport(_next_hop(seen)), event_hooks=request_id_hooks()) as c:
        c.get("http://memory.test/x")
    assert seen == [None]


def test_an_id_the_caller_set_is_left_alone():
    seen: list = []
    with request_id_scope("outer"):
        with httpx.Client(transport=httpx.MockTransport(_next_hop(seen)), event_hooks=request_id_hooks()) as c:
            c.get("http://memory.test/x", headers={REQUEST_ID_HEADER: "mine"})
    assert seen == ["mine"]


def test_a_scope_mints_once_for_everything_inside_it():
    seen: list = []
    with request_id_scope(prefix="sleep-") as rid:
        assert rid.startswith("sleep-") and current_request_id() == rid
        with httpx.Client(transport=httpx.MockTransport(_next_hop(seen)), event_hooks=request_id_hooks()) as c:
            c.get("http://memory.test/a")
            c.get("http://memory.test/b")
    assert seen == [rid, rid]
    assert current_request_id() is None


def test_a_scope_inside_a_request_keeps_the_requests_id():
    with request_id_scope("from-post-sleep"):
        with request_id_scope(prefix="sleep-") as rid:
            assert rid == "from-post-sleep"
        with request_id_scope("explicit") as rid:
            assert rid == "explicit"
        assert current_request_id() == "from-post-sleep"
