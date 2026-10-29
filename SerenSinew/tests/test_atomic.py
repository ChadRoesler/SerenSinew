"""
Small files written so a reader never sees half of one (seren_sinew.atomic).
"""
from __future__ import annotations

import os
import stat
import sys

import pytest

from seren_sinew import atomic


def _leftovers(d):
    return [p.name for p in d.iterdir() if p.name.endswith(".tmp")]


def test_json_round_trips_and_leaves_no_temp_file(tmp_path):
    p = atomic.write_json(tmp_path / "deep" / "state.json", {"last_sleep": 1.5, "name": "é"})
    assert atomic.read_json(p) == {"last_sleep": 1.5, "name": "é"}
    assert _leftovers(p.parent) == []


def test_options_shape_the_text(tmp_path):
    p = atomic.write_json(tmp_path / "r.json", {"b": 1, "a": "é"}, indent=None, sort_keys=True,
                          ensure_ascii=False, newline=True)
    assert p.read_text(encoding="utf-8") == '{"a": "é", "b": 1}\n'


def test_a_rewrite_replaces_the_whole_file(tmp_path):
    p = tmp_path / "s.json"
    atomic.write_text(p, "x" * 1000)
    atomic.write_text(p, "short")
    assert p.read_text(encoding="utf-8") == "short"


def test_a_failed_swap_leaves_the_last_good_file_and_no_temp(tmp_path, monkeypatch):
    p = atomic.write_json(tmp_path / "s.json", {"good": True})

    def refuse(src, dst):
        raise OSError("disk went away")
    monkeypatch.setattr(atomic.os, "replace", refuse)
    with pytest.raises(OSError):
        atomic.write_json(p, {"good": False})
    assert atomic.read_json(p) == {"good": True} and _leftovers(tmp_path) == []


def test_a_swap_refused_for_a_moment_is_tried_again(tmp_path, monkeypatch):
    real, calls = os.replace, []

    def busy_then_free(src, dst):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError("a reader holds it open")
        real(src, dst)
    monkeypatch.setattr(atomic.os, "replace", busy_then_free)
    monkeypatch.setattr(atomic, "REPLACE_PAUSE", 0)
    p = atomic.write_text(tmp_path / "s.txt", "in")
    assert p.read_text(encoding="utf-8") == "in" and len(calls) == 3


def test_a_swap_refused_for_good_raises(tmp_path, monkeypatch):
    def never(src, dst):
        raise PermissionError("held")
    monkeypatch.setattr(atomic.os, "replace", never)
    monkeypatch.setattr(atomic, "REPLACE_PAUSE", 0)
    with pytest.raises(PermissionError):
        atomic.write_text(tmp_path / "s.txt", "in")
    assert _leftovers(tmp_path) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_a_mode_is_the_files_from_the_start(tmp_path):
    p = atomic.write_text(tmp_path / "token", "hf_x", mode=0o600)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_read_json_falls_back_on_missing_or_damaged(tmp_path):
    assert atomic.read_json(tmp_path / "nope.json", {}) == {}
    (tmp_path / "bad.json").write_text("{half", encoding="utf-8")
    assert atomic.read_json(tmp_path / "bad.json", "fallback") == "fallback"
