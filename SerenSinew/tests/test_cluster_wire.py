"""
seren_sinew.cluster: the shapes on the head-to-node wire, spelled once.

What these pin:
- a fixed shape parsed strictly refuses a key it does not know (that is the
  drift the package exists to catch) and a required field the wire lacks;
- an open shape (a manifest, a status, the node's own manifest) keeps an
  unknown key in `extra` instead of dropping it, and `to_dict` puts it back;
- the lenient default fills a missing required field with its empty value,
  since one manifest with no `service` once took a whole node off the map;
- the Observatory's old and new spelling of its version both land;
- `drift` reports every disagreement, nested ones with their path, without
  raising.
"""
from __future__ import annotations

import pytest

from seren_sinew.cluster import (
    HealthResponse,
    NodeResponse,
    PingResponse,
    ServiceEntry,
    ServiceManifest,
    ServiceStatus,
    ServicesResponse,
    VersionResponse,
    WireDrift,
    drift,
    from_dict,
    to_dict,
)


def test_a_fixed_shape_parsed_strictly_refuses_a_key_it_does_not_know():
    with pytest.raises(WireDrift) as e:
        from_dict({"ok": True, "ts": 1, "uptime": 9}, PingResponse, strict=True)
    assert e.value.unknown == ["uptime"] and e.value.missing == []
    assert "PingResponse does not know: uptime" in str(e.value)


def test_a_fixed_shape_parsed_strictly_refuses_a_missing_required_field():
    with pytest.raises(WireDrift) as e:
        from_dict({"ok": True, "total": 1, "healthy": 1, "degraded": []}, HealthResponse, strict=True)
    assert e.value.missing == ["not_running"]


def test_the_lenient_default_fills_what_the_wire_left_out():
    """One manifest with no `service` key once made the whole node read as
    'agent returned null'. Lenient fills the hole; the caller fixes the name."""
    m = from_dict({"port": 8080}, ServiceManifest)
    assert m.service == "" and m.port == 8080
    h = from_dict({"ok": True, "total": 0, "healthy": 0, "degraded": [], "not_running": []}, HealthResponse)
    assert h.idle == [], "an Observatory from before idle existed still parses"


def test_an_open_shape_keeps_an_unknown_key_in_extra_and_gives_it_back():
    raw = {"service": "seren-memory-wren", "service_type": "systemd", "port": 7250,
           "health_url": "http://127.0.0.1:7250/health", "config_path": "/x/seren-memory.yaml"}
    m = from_dict(raw, ServiceManifest, strict=True)       # strict, and still fine: it is open
    assert m.extra == {"health_url": "http://127.0.0.1:7250/health", "config_path": "/x/seren-memory.yaml"}
    assert to_dict(m, drop_none=True) == {**raw, "schema_version": 0}


def test_a_status_keeps_what_its_service_type_adds():
    raw = {"service": "probe", "service_type": "docker_compose", "running": True, "compose_service": "probe",
           "container_id": "abc", "port_health": {"ok": True, "status_code": 200, "latency_ms": 3,
                                                  "probed_path": "/health", "probed_url": "http://127.0.0.1:1/health"}}
    st = from_dict(raw, ServiceStatus, strict=True)
    assert st.extra == {"compose_service": "probe", "container_id": "abc"}
    assert st.port_health.probed_url.endswith("/health")
    assert to_dict(st, drop_none=True)["container_id"] == "abc"


def test_the_version_lands_under_either_spelling():
    new = from_dict({"observatory_version": "5.6.0", "manifest_schema": 2, "updates": {"status": "ok"}},
                    VersionResponse, strict=True)
    old = from_dict({"agent_version": "2.0.0", "manifest_schema": 1}, VersionResponse, strict=True)
    assert new.observatory_version == new.agent_version == "5.6.0" and new.updates == {"status": "ok"}
    assert old.observatory_version == "2.0.0"


def test_drift_reports_every_disagreement_with_its_path_and_raises_nothing():
    raw = {"count": 1, "services": {"llama": {"manifest": {"service": "llama", "port": 8080},
                                               "status": {"running": True,
                                                          "port_health": {"ok": True, "jitter_ms": 2}}}},
           "took_ms": 12}
    found = drift(raw, ServicesResponse)
    assert [(d.path, d.unknown, d.missing) for d in found] == [
        ("", ["took_ms"], []),
        ("services['llama'].status.port_health", ["jitter_ms"], []),
    ]
    assert from_dict(raw, ServicesResponse).services["llama"].status.port_health.ok is True, "lenient still parses"


def test_a_nested_strict_parse_names_where_it_went_wrong():
    with pytest.raises(WireDrift) as e:
        from_dict({"manifest": {"hostname": "node-a"}, "runtime": {"load_avg": [0.1], "cores": 4}},
                  NodeResponse, strict=True)
    assert e.value.path == "runtime" and e.value.unknown == ["cores"]


def test_the_shapes_round_trip():
    raw = {"count": 1, "services": {"coral": {"manifest": {"service": "coral", "port": 0, "service_type": "library",
                                                            "schema_version": 2},
                                               "status": {"service": "coral", "service_type": "library",
                                                          "running": True, "library_mode": True}}}}
    parsed = from_dict(raw, ServicesResponse, strict=True)
    assert isinstance(parsed.services["coral"], ServiceEntry)
    assert to_dict(parsed, drop_none=True) == raw
