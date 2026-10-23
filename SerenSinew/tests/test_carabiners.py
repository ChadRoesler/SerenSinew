"""seren_sinew.carabiners: the Rack's messages, and running a .kbh on a box
the way the Observatory does - against a stand-in kbh, never a real one."""
from __future__ import annotations

import json
import sys
import textwrap

import pytest

from seren_sinew.carabiners import (BelayReport, CarabinerManifest, KbhRunner, SettingsRequest, SettingsResult,
                                    REQUEST_ENV)

FAKE_KBH = textwrap.dedent('''
    import json, os, sys, time
    args = sys.argv[1:]
    name, rest = args[0], args[1:]
    config = rest[rest.index("--config") + 1]
    if rest[:2] == ["config", "show"]:
        print("kbh: a custom/ module did not load", file=sys.stderr)
        print(json.dumps({"ok": True, "carabiner": name, "display": "Fake", "version": "9.9", "config": config,
                          "fields": [{"key": "session.model", "kind": "choice", "value": ""}],
                          "connections": [{"name": "bookmark", "token": "inline"}], "new_field": 1}, indent=2))
    elif rest[:2] == ["config", "apply"]:
        req = json.loads(os.environ["KBH_SETTINGS_REQUEST"])
        if "slow" in req["values"]:
            time.sleep(10)
        print(json.dumps({"ok": True, "carabiner": name, "applied": list(req["values"]) + req["unset"],
                          "refused": {}, "applies": {k: "next_session" for k in req["values"]},
                          "belay_ok": True, "belay": [{"check": "config / yaml", "ok": True, "said": ""}],
                          "expert_seen": req["expert"], "cwd": os.getcwd()}))
    elif rest[:1] == ["belay"]:
        print(json.dumps({"carabiner": name, "ok": False, "checks": [
            {"verb": "bookmark", "side": "belay on.", "name": "margin", "status": "LET GO", "why": "no answer"}]}))
        sys.exit(1)
    else:
        print("not json at all")
''')


@pytest.fixture
def box(tmp_path):
    folder = tmp_path / "kbh"
    folder.mkdir()
    (folder / "claude.kbh").write_text(FAKE_KBH, encoding="utf-8")
    (folder / "claude.yaml").write_text("carabiner: claude\n", encoding="utf-8")
    (folder / "codex.kbh").write_text(FAKE_KBH, encoding="utf-8")          # clipped, never configured
    (folder / "Bad Name.kbh").write_text("", encoding="utf-8")
    return KbhRunner(folder=str(folder), python=sys.executable, node="desktop", timeout_seconds=5)


def test_the_messages_round_trip_and_shrug_off_fields_they_do_not_know():
    req = SettingsRequest.from_dict({"node": "spark", "values": {"session.model": "claude-haiku-5-5"},
                                     "unset": ["framing", None], "expert": 1, "from_the_future": True})
    assert req.unset == ["framing"] and req.expert is True
    assert SettingsRequest.from_dict(req.to_dict()) == req
    assert json.loads(req.for_kbh()) == {"values": {"session.model": "claude-haiku-5-5"}, "unset": ["framing"],
                                         "expert": True}, "kbh gets the change, not who asked"
    res = SettingsResult.from_dict({"ok": True, "refused": {"a": "why"}, "belay_ok": None, "extra": 2})
    assert res.belay_ok is None and res.refused == {"a": "why"} and SettingsResult.from_dict(res.to_dict()) == res
    assert SettingsResult.from_dict(None) == SettingsResult()
    assert CarabinerManifest.from_dict({"fields": [{"key": "x"}, "junk"]}).fields == [{"key": "x"}]


def test_list_is_every_kbh_in_the_folder_and_whether_it_has_its_yaml(box):
    entries = {e.carabiner: e for e in box.list()}
    assert set(entries) == {"claude", "codex"}, "a file that is not a carabiner name is not run"
    assert entries["claude"].configured and not entries["codex"].configured
    assert entries["claude"].node == "desktop"
    assert KbhRunner(folder="", python=sys.executable).list() == []


def test_the_manifest_comes_back_whole_past_a_warning_on_the_same_stream(box):
    m = box.manifest("claude")
    assert m.ok and m.version == "9.9" and m.node == "desktop", m.error
    assert m.fields == [{"key": "session.model", "kind": "choice", "value": ""}]
    assert m.config.endswith("claude.yaml")


def test_apply_carries_the_change_in_the_environment_and_answers_for_this_node(box, monkeypatch):
    monkeypatch.delenv(REQUEST_ENV, raising=False)
    r = box.apply("claude", SettingsRequest(node="desktop", values={"session.model": "claude-haiku-5-5"},
                                            unset=["framing"], holder="lodestar-rack"))
    assert r.ok and r.applied == ["session.model", "framing"] and r.node == "desktop", r.error
    assert r.belay_ok and r.belay[0]["check"] == "config / yaml"


def test_a_carabiner_that_is_not_here_or_not_a_name_is_an_answer_not_a_traceback(box):
    for name in ("hermes", "../claude", "Bad Name", ""):
        r = box.apply(name, SettingsRequest(values={"x": 1}))
        assert not r.ok and r.error.startswith("LookupError"), name
    assert not box.manifest("hermes").ok and not box.belay("hermes").ok


def test_a_kbh_that_hangs_is_stopped(box):
    r = box.apply("claude", SettingsRequest(values={"slow": 1}))
    assert not r.ok and "ran past" in r.error


def test_a_change_too_big_for_the_environment_is_refused_before_it_runs(box):
    r = box.apply("claude", SettingsRequest(values={"framing": "x" * 40000}))
    assert not r.ok and "longer than" in r.error


def test_belay_comes_back_with_the_check_that_let_go(box):
    b = box.belay("claude")
    assert isinstance(b, BelayReport) and not b.ok and not b.error
    assert b.checks[0]["status"] == "LET GO" and b.node == "desktop"
