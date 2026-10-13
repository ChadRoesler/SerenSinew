"""
Running the main Seren apps as needed (seren_sinew.orchestration RunPolicy,
Leases with clocks, decide; seren_sinew.ondemand).

Pinned here:

- names: an install's name comes down to its app ('seren-corpus-callosum-wren'
  -> 'corpus-callosum'); a short name resolves to every install of it
- policies parse forgivingly, look up by full or short name, and Lodestar
  and the Observatory are never managed
- leases run out, renew, linger when asked, keep idle clocks, and survive a
  save and a load
- decide(): on_demand stops after its idle minutes whoever started it;
  unmanaged stops only what an ensure started; always never; scheduled is
  started inside its hours and on_demand outside them; nobody holding it is
  the condition, never just the clock
- the transport: a refused connection is an ensure, then one retry; a
  running app is renewed at half its ttl; Lodestar being away never breaks a
  call to an app that is up
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime

import httpx
import pytest

from seren_sinew.ondemand import OnDemand, OnDemandSyncTransport, OnDemandTransport
from seren_sinew.orchestration import (EnsureRequest, Leases, Policies, RunPolicy, decide, matches, resolve,
                                       short_name)

H = 3600.0


def test_names():
    assert short_name("seren-corpus-callosum-wren") == "corpus-callosum"
    assert short_name("SerenCorpusCallosum-wren") == "corpus-callosum"
    assert short_name("seren-memory") == "memory" and short_name("seren-margin-wren") == "margin"
    assert short_name("llama") is None and short_name("searxng") is None
    assert matches("seren-probe-wren", "probe") and matches("seren-probe-wren", "seren-probe-wren")
    assert not matches("seren-probe-wren", "memory")
    installed = ["seren-memory-wren", "seren-memory-alice", "seren-loci-wren", "llama"]
    assert resolve("memory", installed) == ["seren-memory-wren", "seren-memory-alice"], "two instances, two answers"
    assert resolve("llama", installed) == ["llama"] and resolve("seren-loci-wren", installed) == ["seren-loci-wren"]
    assert resolve("theatre", installed) == []


def test_policies():
    p = Policies.from_dict({"default": {"mode": "unmanaged", "idle_minutes": 5},
                            "apps": {"probe": "on_demand", "theatre": {"mode": "on-demand", "idle_minutes": 45},
                                     "hippocampus": {"mode": "scheduled", "hours": "22:00-06:00"},
                                     "memory": {"mode": "always"}, "loci": {"mode": "nonsense"},
                                     "lodestar": "on_demand"}})
    assert p.for_service("seren-probe-wren") == RunPolicy("on_demand", 15.0), "on_demand with no minutes: 15"
    assert p.for_service("SerenTheatre-wren").idle_minutes == 45
    assert p.for_service("seren-memory-wren").mode == "always"
    assert p.for_service("seren-loci-wren") == RunPolicy("unmanaged", 5.0), "a mode that is not one falls back"
    assert p.for_service("llama") == RunPolicy("unmanaged", 5.0)
    assert p.for_service("seren-lodestar-wren").mode == "unmanaged", "never managed, whatever the yaml says"
    night = p.for_service("seren-hippocampus-wren")
    assert night.in_hours(datetime(2026, 10, 12, 23, 0)) and night.in_hours(datetime(2026, 10, 13, 5, 59))
    assert not night.in_hours(datetime(2026, 10, 12, 12, 0)) and not RunPolicy("scheduled", hours="soon").in_hours(datetime.now())
    assert Policies.from_dict(p.to_dict()).to_dict() == p.to_dict()


def test_leases_run_out_renew_linger_and_keep_idle_clocks():
    book = Leases(default_ttl=H)
    book.acquire("nuc", "seren-probe-wren", "seren-workbench", started=True, ttl=600, now=0)
    book.acquire("nuc", "seren-probe-wren", "seren-workbench", started=False, ttl=600, now=500)   # renewed
    assert book.expire(now=1000) == [] and book.entries()[("nuc", "seren-probe-wren")]["holders"] == ["seren-workbench"]
    assert book.expire(now=1101) == [{"node": "nuc", "service": "seren-probe-wren", "holder": "seren-workbench"}]
    e = book.entries()[("nuc", "seren-probe-wren")]
    assert e["holders"] == [] and e["idle_since"] == 1101 and e["started"], "ran out: idle from then, still lease-started"
    # linger: released, kept, idle
    book.acquire("nuc", "seren-theatre-wren", "a", started=True, now=0)
    assert book.release("nuc", "seren-theatre-wren", "a", linger=True, now=50) == ([], True)
    assert book.entries()[("nuc", "seren-theatre-wren")]["idle_since"] == 50
    # tick: a running service nobody holds gets a clock; a stopped one is forgotten
    book.tick({("nuc", "seren-symposium-wren"): True, ("nuc", "seren-theatre-wren"): False}, now=60)
    assert book.entries()[("nuc", "seren-symposium-wren")]["idle_since"] == 60
    assert ("nuc", "seren-theatre-wren") not in book.entries()
    # holding it again stops the clock
    book.acquire("nuc", "seren-symposium-wren", "b", started=False, now=70)
    assert book.entries()[("nuc", "seren-symposium-wren")]["idle_since"] is None


def test_the_book_survives_a_restart(tmp_path):
    book = Leases(default_ttl=H)
    book.acquire("nuc", "llama", "seren-hippocampus", started=True, now=100)
    book.tick({("nuc", "seren-probe-wren"): True}, now=200)
    path = tmp_path / "orchestration" / "leases.json"
    book.save(path)
    again = Leases.load(path)
    assert again.entries() == book.entries() and again.default_ttl == H
    assert again.node_of("llama", "seren-hippocampus") == "nuc"
    path.write_text("{ not json")
    assert Leases.load(path).entries() == {}, "a book that does not parse is an empty one, not a crash"
    assert Leases.load(tmp_path / "missing.json").entries() == {}
    assert Leases.from_dict({"leases": [{"node": 1}]}).entries() == {}, "a bad row is skipped"


def test_decide():
    pol = Policies.from_dict({"apps": {"probe": {"mode": "on_demand", "idle_minutes": 10}, "memory": "always",
                                       "hippocampus": {"mode": "scheduled", "hours": "22:00-06:00", "idle_minutes": 5}},
                              "default": {"idle_minutes": 30}})
    noon, night = datetime(2026, 10, 12, 12, 0), datetime(2026, 10, 12, 23, 0)
    book = Leases()
    book.tick({("nuc", "seren-probe-wren"): True, ("nuc", "seren-memory-wren"): True,
               ("nuc", "seren-hippocampus-wren"): True, ("nuc", "seren-observatory-wren"): True,
               ("nuc", "seren-loci-wren"): True}, now=0)
    running = {k: True for k in book.entries()}
    running[("nuc", "seren-theatre-wren")] = False
    at = lambda t, local: [(d.service, d.action) for d in decide(book.entries(), running, pol, now=t, local=local)]
    assert at(5 * 60, noon) == [("seren-hippocampus-wren", "stop")], "outside its hours: on_demand, 5 min"
    assert at(5 * 60, night) == [], "inside its hours it stays"
    assert sorted(at(11 * 60, noon)) == [("seren-hippocampus-wren", "stop"), ("seren-probe-wren", "stop")]
    # never: memory (always), the Observatory, loci (unmanaged and nobody's ensure started it)
    assert all(s not in ("seren-memory-wren", "seren-observatory-wren", "seren-loci-wren") for s, _ in at(99 * H, noon))
    # held: not stopped, however long
    book.acquire("nuc", "seren-probe-wren", "seren-workbench", started=False, now=0)
    assert ("seren-probe-wren", "stop") not in at(99 * H, noon)
    # unmanaged + an ensure started it: stops after the default idle
    book.acquire("nuc", "seren-loci-wren", "x", started=True, now=0)
    book.release("nuc", "seren-loci-wren", "x", linger=True, now=0)
    assert ("seren-loci-wren", "stop") not in at(29 * 60, noon) and ("seren-loci-wren", "stop") in at(31 * 60, noon)
    # scheduled and down inside its hours: started
    running[("nuc", "seren-hippocampus-wren")] = False
    assert ("seren-hippocampus-wren", "start") in at(0, night)


def test_ensure_request_carries_a_ttl():
    assert EnsureRequest.from_dict({"ttl_seconds": 600}).ttl_seconds == 600
    assert EnsureRequest.from_dict({"ttl_seconds": "x"}).ttl_seconds == 0.0
    assert EnsureRequest.from_dict({"ttl_seconds": 10 ** 9}).ttl_seconds == 7 * 86400


class Lodestar:
    """A fake Lodestar: ensure starts the app (after `refuse` failures)."""

    def __init__(self, app, refuse=0):
        self.app, self.refuse, self.calls = app, refuse, []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.calls.append((request.url.path, body, request.headers.get("authorization")))
        if request.url.path.endswith("/release"):
            return httpx.Response(200, json={"ok": True})
        if self.refuse:
            self.refuse -= 1
            return httpx.Response(200, json={"ok": False, "error": "no online node has it"})
        started = not self.app["up"]
        self.app["up"] = True
        return httpx.Response(200, json={"ok": True, "ready": True, "started": started, "service": "seren-probe-wren"})


def _app_transport(app):
    def handler(request):
        if not app["up"]:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json={"echo": json.loads(request.content or b"null")})
    return handler


def test_the_transport_starts_a_stopped_app_and_retries_once():
    app = {"up": False}
    lode = Lodestar(app)
    clock = {"t": 0.0}
    od = OnDemand("http://lodestar:6361", "probe", holder="seren-workbench", token="lode-secret", ttl_seconds=600,
                  transport=httpx.MockTransport(lode.handler), clock=lambda: clock["t"])

    async def go():
        t = OnDemandTransport(od, inner=httpx.MockTransport(_app_transport(app)))
        async with httpx.AsyncClient(base_url="http://nuc:7400", transport=t) as c:
            r = await c.post("/api/v1/eval", json={"a": 1})
            assert r.status_code == 200 and r.json() == {"echo": {"a": 1}}, "the body went the second time too"
            n = len(lode.calls)
            clock["t"] = 200.0
            await c.get("/x")
            assert len(lode.calls) == n, "inside half the ttl: no renewal"
            clock["t"] = 301.0
            await c.get("/x")
            assert len(lode.calls) == n + 1, "past half the ttl: renewed"
    asyncio.run(go())
    path, body, auth = lode.calls[0]
    assert path == "/api/v1/service/probe/ensure" and body["holder"] == "seren-workbench" and body["ttl_seconds"] == 600
    assert auth == "Bearer lode-secret"


def test_lodestar_away_never_breaks_a_call_to_a_running_app_and_a_down_one_says_why():
    app = {"up": True}

    def gone(request):
        raise httpx.ConnectError("no lodestar", request=request)
    od = OnDemand("http://lodestar:6361", "probe", holder="h", transport=httpx.MockTransport(gone))

    async def go():
        t = OnDemandTransport(od, inner=httpx.MockTransport(_app_transport(app)))
        async with httpx.AsyncClient(base_url="http://nuc:7400", transport=t) as c:
            assert (await c.get("/x")).status_code == 200
            app["up"] = False
            with pytest.raises(httpx.ConnectError, match="Lodestar could not start it"):
                await c.get("/x")
    asyncio.run(go())


def test_the_sync_transport():
    app = {"up": False}
    lode = Lodestar(app)
    od = OnDemand("http://lodestar:6361", "probe", holder="seren-hippocampus",
                  sync_transport=httpx.MockTransport(lode.handler))
    with httpx.Client(base_url="http://nuc:7400",
                      transport=OnDemandSyncTransport(od, inner=httpx.MockTransport(_app_transport(app)))) as c:
        assert c.get("/x").status_code == 200
    assert app["up"] and od.last.started
    assert od.release_sync() and lode.calls[-1][0] == "/api/v1/service/probe/release"
