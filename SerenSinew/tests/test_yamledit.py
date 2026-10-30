"""seren_sinew.yamledit (29 Oct 2026): one key changed, the comments kept."""
from __future__ import annotations

import os
import stat
import sys

import pytest

from seren_sinew import yamledit

# PyYAML is not a Sinew dependency: CI's [dev] venv has none. The string
# assertions carry the test there; a real parser's read is checked where one
# is installed. (The first thing ci-shape.py caught, 29 Oct 2026: this file.)
try:
    import yaml
except ImportError:                          # pragma: no cover - CI's shape
    yaml = None


def _parsed(text: str) -> dict | None:
    return yaml.safe_load(text) if yaml else None

TEXT = """# seren-lodestar.yaml - the cluster head
server:
  port: 6361   # the family's
ripple:
  # where a ripple goes
  target: desktop  # the box the model lives on
  command: claude
carabiners:
  enabled: true
"""


def test_set_rewrites_one_line_and_keeps_every_comment():
    out = yamledit.set(TEXT, "ripple.target", "nuc")
    assert "  target: nuc  # the box the model lives on\n" in out
    assert out.count("#") == TEXT.count("#") and "  command: claude\n" in out
    assert _parsed(out) is None or _parsed(out)["ripple"] == {"target": "nuc", "command": "claude"}
    off = yamledit.set(TEXT, "ripple.target", "")
    assert '  target: ""  # the box the model lives on\n' in off
    assert _parsed(off) is None or _parsed(off)["ripple"]["target"] == ""


def test_set_adds_a_missing_key_or_block_and_unset_takes_one_out():
    out = yamledit.set(TEXT, "ripple.run_as", "alice")
    assert "  run_as: alice\n" in out and (_parsed(out) is None or _parsed(out)["ripple"]["run_as"] == "alice")
    out = yamledit.set(TEXT, "updates.enabled", False)
    assert out.endswith("updates:\n  enabled: false\n")
    out = yamledit.unset(TEXT, "carabiners.enabled")
    assert "carabiners" not in out, "an emptied block goes with its last key"
    assert yamledit.unset(TEXT, "nothing.here") == TEXT
    with pytest.raises(ValueError):
        yamledit.set(TEXT, "ripple", "x")


def test_scalar_quotes_what_a_yaml_reader_would_misread():
    assert yamledit.scalar("1:30") == '"1:30"' and yamledit.scalar("yes") == '"yes"' and yamledit.scalar(7) == "7"
    assert yamledit.scalar("C:\\seren\\wren") == "C:\\seren\\wren" and yamledit.scalar(["a", "b"]) == '"a, b"'


def test_write_is_atomic_keeps_a_backup_and_the_mode(tmp_path):
    p = tmp_path / "seren-lodestar.yaml"
    p.write_text(TEXT, encoding="utf-8")
    if sys.platform != "win32":
        os.chmod(p, 0o600)
    yamledit.write(str(p), yamledit.set(TEXT, "ripple.target", "nuc"))
    assert "target: nuc" in p.read_text(encoding="utf-8")
    assert (tmp_path / "seren-lodestar.yaml.seren-bak").read_text(encoding="utf-8") == TEXT
    assert not (tmp_path / "seren-lodestar.yaml.seren-tmp").exists()
    if sys.platform != "win32":
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
