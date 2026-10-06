"""
"I need this service up" / "I am done with it" (seren_sinew.orchestration).

Design note: hippocampus => Lodestar => Observatory => start llama =>
Observatory waits until llama is up => tells Lodestar => Lodestar tells the
hippocampus it is ready. Pinned here, the shared half:

- the messages round-trip, and a peer's unknown or missing fields do not break them
- wait_seconds is held to a sane range
- the lease book: a service is stopped only when its last holder lets go AND
  an ensure is what started it
"""
from __future__ import annotations

from seren_sinew.orchestration import (ENSURE_PATH, MAX_WAIT_SECONDS, RELEASE_PATH, EnsureRequest, EnsureResult,
                                       Leases, ReleaseRequest, ReleaseResult)


def test_the_messages_round_trip_and_tolerate_a_different_peer():
    req = EnsureRequest(holder="seren-hippocampus", reason="a sleep", wait_seconds=120, node="node-a")
    assert EnsureRequest.from_dict(req.to_dict()) == req
    assert EnsureRequest.from_dict({"holder": "h", "from_the_future": 1}) == EnsureRequest(holder="h")
    assert EnsureRequest.from_dict(None).wait_seconds == 240.0
    assert EnsureRequest.from_dict({"wait_seconds": "soon"}).wait_seconds == 240.0
    assert EnsureRequest.from_dict({"wait_seconds": 99999}).wait_seconds == MAX_WAIT_SECONDS
    assert EnsureRequest.from_dict({"wait_seconds": -5}).wait_seconds == 0.0

    res = EnsureResult(ok=True, service="llama", node="node-a", ready=True, started=True,
                       base_url="http://192.0.2.101:8090", port=8090, health_path="/health",
                       waited_seconds=41.5, holders=["seren-hippocampus"])
    assert EnsureResult.from_dict(res.to_dict()) == res
    assert EnsureResult.from_dict({"ok": True, "ready": True, "port": "8090", "extra": {}}).port == 8090
    bad = EnsureResult.failed("llama", "no online node has it")
    assert (bad.ok, bad.ready, bad.error) == (False, False, "no online node has it")

    rel = ReleaseRequest(holder="seren-hippocampus", reason="the chain landed")
    assert ReleaseRequest.from_dict(rel.to_dict()) == rel
    out = ReleaseResult(ok=True, service="llama", node="node-a", stopped=True)
    assert ReleaseResult.from_dict(out.to_dict()) == out
    assert ENSURE_PATH.format(service="llama") == "/api/v1/service/llama/ensure"
    assert RELEASE_PATH.format(service="llama") == "/api/v1/service/llama/release"


def test_the_last_holder_stops_what_an_ensure_started():
    book = Leases()
    assert book.acquire("node-a", "llama", "seren-hippocampus", started=True) == ["seren-hippocampus"]
    assert book.acquire("node-a", "llama", "symposium", started=False) == ["seren-hippocampus", "symposium"]
    assert book.node_of("llama", "symposium") == "node-a" and book.node_of("llama", "nobody") is None
    assert book.release("node-a", "llama", "seren-hippocampus") == (["symposium"], False), "someone still needs it"
    assert book.release("node-a", "llama", "symposium") == ([], True), "the last one out, and a lease started it"
    assert book.snapshot() == []
    assert book.release("node-a", "llama", "symposium") == ([], False), "nothing held, nothing to stop"


def test_a_service_that_was_already_running_is_never_stopped_by_a_release():
    book = Leases()
    book.acquire("node-b", "kokoro", "a", started=False)        # someone started it by hand
    assert book.snapshot() == [{"node": "node-b", "service": "kokoro", "holders": ["a"], "started_by_lease": False}]
    assert book.release("node-b", "kokoro", "a") == ([], False)
    # but once ANY ensure has started it, the lease owns stopping it
    book.acquire("node-b", "kokoro", "a", started=True)
    book.acquire("node-b", "kokoro", "b", started=False)
    book.release("node-b", "kokoro", "a")
    assert book.release("node-b", "kokoro", "b") == ([], True)
    # the same holder asking twice holds one lease
    book.acquire("n", "llama", "a", started=True)
    book.acquire("n", "llama", "a", started=False)
    assert book.release("n", "llama", "a") == ([], True)
