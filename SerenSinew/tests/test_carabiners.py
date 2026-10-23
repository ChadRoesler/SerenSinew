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


# ── upgrades (23 Oct 2026) ───────────────────────────────────────────

FAKE_MAIN = textwrap.dedent('''
    import json, os, re, shutil, sys, zipfile
    args = sys.argv[1:]
    def version(p):
        return re.search(r'"(.+)"', zipfile.ZipFile(p).read("kbh/__init__.py").decode()).group(1)
    if args[:1] == ["upgrade"]:
        into, frm = args[args.index("--into") + 1], args[args.index("--from") + 1]
        was, now = version(into), version(frm)
        with open(os.path.join(os.path.dirname(into), "ran-by.txt"), "w") as f:
            f.write(os.path.basename(sys.argv[0]))
        shutil.copyfile(frm, into)
        print("kbh: a warning first", file=sys.stderr)
        print(json.dumps({"ok": True, "was": was, "now": now, "changed": True, "sha256_checked": "--sha256" in args,
                          "belay": {"claude": {"lost": [], "after_ok": True}}, "from_the_future": 1}))
''')


def _zip_kbh(path, version, build="", upgrade=True) -> bytes:
    import zipfile
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("__main__.py", FAKE_MAIN)
        zf.writestr("kbh/__init__.py", f'__version__ = "{version}"\n')
        if build:
            zf.writestr("kbh/_build.py", f"BUILD = {build!r}\n")
        if upgrade:
            zf.writestr("kbh/upgrade.py", "")
    return path.read_bytes()


@pytest.fixture
def zbox(tmp_path):
    folder = tmp_path / "zkbh"
    folder.mkdir()
    _zip_kbh(folder / "claude.kbh", "5.1.3", "1f116ed")
    (folder / "claude.yaml").write_text("carabiner: claude\n", encoding="utf-8")
    new = _zip_kbh(tmp_path / "new.kbh", "5.1.4", "abc1234")
    return KbhRunner(folder=str(folder), python=sys.executable, node="desktop", timeout_seconds=5), folder, new


def test_the_upgrade_messages_carry_the_file_and_its_hash():
    import hashlib
    from seren_sinew.carabiners import UpgradeRequest, UpgradeResult
    req = UpgradeRequest.carrying(b"PK-ish bytes", node="spark", version="5.1.4", source="release v5.1.4")
    assert req.sha256 == hashlib.sha256(b"PK-ish bytes").hexdigest() and req.data() == b"PK-ish bytes"
    back = UpgradeRequest.from_dict({**req.to_dict(), "sha256": req.sha256.upper(), "extra": 1})
    assert back == req, "the hash is compared lower-case; unknown fields are shrugged off"
    with pytest.raises(ValueError):
        UpgradeRequest(kbh="not base64!!").data()
    with pytest.raises(ValueError):
        UpgradeRequest(kbh="").data()
    res = UpgradeResult.from_dict({"ok": True, "belay": {"claude": {"lost": ["x"]}}, "belay_extra": 2})
    assert res.belay == {"claude": {"lost": ["x"]}} and UpgradeResult.from_dict(res.to_dict()) == res


def test_list_says_each_kbh_version_and_hash_without_running_it(zbox):
    import hashlib
    runner, folder, _ = zbox
    (e,) = runner.list()
    assert e.version == "5.1.3+1f116ed"
    assert e.sha256 == hashlib.sha256((folder / "claude.kbh").read_bytes()).hexdigest()
    assert not (folder / "ran-by.txt").exists()


def test_upgrade_checks_the_hash_then_the_installed_kbh_swaps_it_in(zbox):
    from seren_sinew.carabiners import UpgradeRequest
    runner, folder, new = zbox
    bad = UpgradeRequest.carrying(new)
    bad.sha256 = "0" * 64
    r = runner.upgrade("claude", bad)
    assert not r.ok and "damaged on the way" in r.error and not (folder / "ran-by.txt").exists()
    r = runner.upgrade("claude", UpgradeRequest(kbh=UpgradeRequest.carrying(new).kbh))
    assert not r.ok and "sha256" in r.error, "no hash, no upgrade"
    r = runner.upgrade("claude", UpgradeRequest.carrying(new))
    assert r.ok and r.was == "5.1.3" and r.now == "5.1.4" and r.sha256_checked and r.node == "desktop", r.error
    assert (folder / "ran-by.txt").read_text() == "claude.kbh", "the file already trusted runs it"
    assert (folder / "claude.kbh").read_bytes() == new and not r.bootstrap
    assert [p.name for p in folder.iterdir() if "incoming" in p.name] == [], "the incoming file is cleaned up"
    assert runner.upgrade("toaster", UpgradeRequest.carrying(new)).error.startswith("LookupError")


def test_a_kbh_from_before_the_upgrade_verb_is_upgraded_by_the_new_one(zbox):
    from seren_sinew.carabiners import UpgradeRequest
    runner, folder, new = zbox
    _zip_kbh(folder / "claude.kbh", "5.1.2", upgrade=False)
    r = runner.upgrade("claude", UpgradeRequest.carrying(new))
    assert r.ok and r.bootstrap and r.was == "5.1.2", r.error
    assert (folder / "ran-by.txt").read_text().startswith(".claude.kbh.incoming-")
