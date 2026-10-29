"""
The Transit's two Sinew halves (28 Oct 2026): a run policy may name the
apps it `displaces` on the same box, and the cluster router can `hold` one
service on one node out of routing so a request for it goes to the fallback.
"""
from __future__ import annotations

from seren_sinew.cluster.discovery import ClusterClient
from seren_sinew.cluster.dtos import ClusterOptions, NodeOptions, NodeSnapshot
from seren_sinew.orchestration import Policies, RunPolicy, displaced_by


def test_displaces_parses_from_a_list_or_one_name_and_is_not_inherited_from_the_default():
    p = Policies.from_dict({"default": {"mode": "on_demand", "idle_minutes": 5},
                            "apps": {"hippocampus-llama": {"mode": "on_demand", "idle_minutes": 1,
                                                           "displaces": ["llama", "symposium-llama"]},
                                     "whisper": {"displaces": "kokoro"},
                                     "probe": "on_demand"}})
    assert p.for_service("hippocampus-llama").displaces == ["llama", "symposium-llama"]
    assert p.for_service("whisper").displaces == ["kokoro"]
    assert p.for_service("probe").displaces == [] and p.default.displaces == []
    assert RunPolicy.from_dict({"displaces": [" llama ", ""]}).displaces == ["llama"]
    assert "displaces" in p.for_service("whisper").to_dict()


def test_displaced_by_matches_full_or_short_names_and_never_itself():
    pol = RunPolicy(mode="on_demand", displaces=["llama", "memory"])
    running = ["llama", "hippocampus-llama", "seren-memory-wren", "kokoro"]
    assert displaced_by(pol, "hippocampus-llama", running) == ["llama", "seren-memory-wren"]
    assert displaced_by(pol, "llama", running) == ["seren-memory-wren"], "a service never displaces itself"
    assert displaced_by(RunPolicy(), "x", running) == []


def _cluster():
    opts = ClusterOptions(nodes=[NodeOptions(name="nano", agent_url="http://192.0.2.1:7777", preferred_for=["llama"]),
                                 NodeOptions(name="spark", agent_url="http://192.0.2.2:7777")])
    c = ClusterClient(opts, log_fn=lambda m: None)
    for n in ("nano", "spark"):
        c._snapshots[n] = NodeSnapshot(online=True, installed_services=["llama"], status={})
    return c


def test_a_hold_sends_a_request_to_the_fallback_and_an_unhold_brings_the_node_back():
    c = _cluster()
    assert c.choose_node_for("llama").name == "nano", "preferred"
    c.hold("nano", "llama", "in transit for hippocampus-llama")
    assert c.choose_node_for("llama").name == "spark", "the held one is skipped, not queued on"
    assert c.holds() == {"nano/llama": "in transit for hippocampus-llama"}
    assert c.hold_reason("nano", "llama").startswith("in transit")
    assert [n.name for n in c.nodes_for("llama")] == ["spark"]
    c.unhold("nano", "llama")
    assert c.choose_node_for("llama").name == "nano" and c.holds() == {}
    c.hold("nowhere", "llama", "x")
    assert c.holds() == {}, "a node not in the config cannot be held"
