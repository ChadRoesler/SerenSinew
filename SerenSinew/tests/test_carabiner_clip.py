"""Clip and unclip (29 Oct 2026): a carabiner's first file on a box, and
taking one off. Against zip stand-ins, never a real kbh.

- a clip checks the hash, writes <name>.kbh and runs nothing of it
- a file that is not a .kbh is refused and nothing is left behind
- one already clipped is an upgrade, not a clip
- an unclip keeps the file beside itself and the yaml where it is, and says
  what it did not undo
"""
from __future__ import annotations

import hashlib
import sys
import zipfile

import pytest

from seren_sinew.carabiners import KbhRunner, UnclipResult, UpgradeRequest


def _zip_kbh(path, version: str) -> bytes:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("__main__.py", "import sys; sys.exit(99)\n")        # running it would be a failure
        zf.writestr("kbh/__init__.py", f'__version__ = "{version}"\n')
        zf.writestr("kbh/upgrade.py", "")
    return path.read_bytes()


@pytest.fixture
def box(tmp_path):
    folder = tmp_path / "kbh"
    folder.mkdir()
    new = _zip_kbh(tmp_path / "codex.kbh", "5.1.4")
    return KbhRunner(folder=str(folder), python=sys.executable, node="desktop", timeout_seconds=5), folder, new


def test_a_clip_checks_the_hash_writes_the_file_and_runs_nothing(box):
    runner, folder, new = box
    bad = UpgradeRequest.carrying(new)
    bad.sha256 = "0" * 64
    r = runner.clip("codex", bad)
    assert not r.ok and "damaged on the way" in r.error and not (folder / "codex.kbh").exists()
    r = runner.clip("codex", UpgradeRequest(kbh=UpgradeRequest.carrying(new).kbh))
    assert not r.ok and "sha256" in r.error, "no hash, no clip"

    r = runner.clip("codex", UpgradeRequest.carrying(new, node="desktop", version="5.1.4", source="shelf"))
    assert r.ok and r.changed and r.was == "" and r.now == "5.1.4" and r.sha256_checked and r.node == "desktop", r.error
    assert r.sha256 == hashlib.sha256(new).hexdigest()
    assert (folder / "codex.kbh").read_bytes() == new
    assert [p.name for p in folder.iterdir()] == ["codex.kbh"], "no incoming file left, no yaml invented"
    (e,) = runner.list()
    assert e.carabiner == "codex" and e.version == "5.1.4" and not e.configured

    r = runner.clip("codex", UpgradeRequest.carrying(new))
    assert not r.ok and "already clipped" in r.error and "upgrade" in r.error


def test_a_file_that_is_not_a_kbh_is_refused_and_nothing_is_left(box, tmp_path):
    runner, folder, _ = box
    r = runner.clip("codex", UpgradeRequest.carrying(b"not a zip at all"))
    assert not r.ok and "not a .kbh" in r.error
    assert list(folder.iterdir()) == []
    assert runner.clip("Bad Name", UpgradeRequest.carrying(b"x")).error.startswith("LookupError")


def test_an_unclip_keeps_the_file_and_the_yaml_and_says_what_stays_registered(box):
    runner, folder, new = box
    assert runner.clip("codex", UpgradeRequest.carrying(new)).ok
    (folder / "codex.yaml").write_text("carabiner: codex\n", encoding="utf-8")

    r = runner.unclip("codex", reason="from the Rack")
    assert r.ok and r.was == "5.1.4" and r.yaml_kept and r.reason == "from the Rack" and r.node == "desktop", r.error
    assert r.kept.endswith("codex.kbh.unclipped-5.1.4") and (folder / "codex.kbh.unclipped-5.1.4").read_bytes() == new
    assert not (folder / "codex.kbh").exists() and (folder / "codex.yaml").exists()
    assert "still registered" in r.note and runner.list() == []
    assert UnclipResult.from_dict({**r.to_dict(), "extra": 1}) == r

    assert runner.unclip("codex").error.startswith("LookupError"), "gone is gone"
    assert runner.clip("codex", UpgradeRequest.carrying(new)).ok, "a re-clip finds the yaml"
    assert runner.list()[0].configured
    r = runner.unclip("codex")
    assert r.ok and r.kept.endswith("codex.kbh.unclipped-5.1.4-2"), "the first kept file is not overwritten"
