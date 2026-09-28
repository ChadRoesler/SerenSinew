"""
The shared ripple runner (seren_sinew.ripple): the hippocampus, the
Observatory and Lodestar all run a ripple through it (28 Sept 2026).

- {message} / {event} / {draft_id} fill per argument; the event rides in the
  environment; a caller cannot change the command
- stdin: true puts the message on stdin instead - quotes, $, backticks and
  newlines arrive intact, which is what lets it cross `ssh host claude -p`
  with no remote shell parsing it
- one at a time per event; an empty or oversized message is refused
- a privileged runner with no run_as refuses (seren_sinew.runas)
- output lands in the log file
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from seren_sinew import runas
from seren_sinew.ripple import MAX_MESSAGE, RippleRunner

RECORDER = """
import json, os, sys, time
out = sys.argv[1]
if "slow" in sys.argv[2:]:
    time.sleep(3)
data = sys.stdin.read() if "stdin" in sys.argv[2:] else None
with open(out, "w", encoding="utf-8") as f:
    json.dump({"argv": sys.argv[2:], "stdin": data, "event": os.environ.get("SEREN_RIPPLE_EVENT"),
               "payload": json.loads(os.environ.get("SEREN_RIPPLE_JSON") or "{}")}, f)
print("recorded")
"""


@pytest.fixture
def rec(tmp_path, monkeypatch):
    monkeypatch.setattr(runas, "whoami", lambda: ("alice", False))
    script = tmp_path / "recorder.py"
    script.write_text(RECORDER, encoding="utf-8")
    return script, tmp_path / "out.json"


def _wait_for(path: Path, seconds=15.0):
    end = time.time() + seconds
    while time.time() < end:
        if path.exists() and path.stat().st_size:
            return json.loads(path.read_text(encoding="utf-8"))
        time.sleep(0.05)
    raise AssertionError(f"the ripple never wrote {path}")


def test_placeholders_fill_and_the_event_rides_along(rec, tmp_path):
    script, out = rec
    r = RippleRunner(command=[sys.executable, str(script), str(out), "{message}", "{event}", "{draft_id}"],
                     log_path=tmp_path / "logs" / "ripple.log")
    status, answer = r.run("draft_submitted", "review d1", draft_id="d1", payload={"operations": 3})
    assert status == 200 and answer["ok"] is True, answer
    got = _wait_for(out)
    assert got["argv"] == ["review d1", "draft_submitted", "d1"]
    assert got["event"] == "draft_submitted" and got["payload"]["operations"] == 3
    r.wait()
    assert "recorded" in (tmp_path / "logs" / "ripple.log").read_text(), "output lands in the log"


def test_stdin_carries_a_message_no_shell_should_parse(rec):
    script, out = rec
    nasty = "it's bedtime; echo $HOME `whoami` \"quoted\"\nsecond line & | > <"
    r = RippleRunner(command=[sys.executable, str(script), str(out), "stdin"], stdin=True)
    status, answer = r.run("brief_requested", nasty)
    assert answer["ok"] is True, answer
    got = _wait_for(out)
    assert got["stdin"] == nasty, "the message arrives byte for byte"
    assert got["argv"] == ["stdin"], "and it is not in the argument list"
    r.wait()


def test_one_ripple_at_a_time_per_event(rec):
    script, out = rec
    r = RippleRunner(command=[sys.executable, str(script), str(out), "slow"])
    assert r.run("brief_requested", "bedtime")[1]["ok"] is True
    second = r.run("brief_requested", "bedtime")[1]
    assert second["ok"] is False and "still running" in second["skipped"]
    assert r.run("draft_submitted", "review")[1]["ok"] is True, "another event is not blocked"
    r.wait()


@pytest.mark.parametrize("message,status", [("", 400), ("   ", 400), ("x" * (MAX_MESSAGE + 1), 413)])
def test_an_empty_or_oversized_message_is_refused(rec, message, status):
    script, out = rec
    assert RippleRunner(command=[sys.executable, str(script), str(out)]).run("e", message)[0] == status
    assert not out.exists()


def test_a_privileged_runner_with_no_run_as_refuses(rec, monkeypatch):
    script, out = rec
    monkeypatch.setattr(runas, "whoami", lambda: ("root", True))
    status, answer = RippleRunner(command=[sys.executable, str(script), str(out)]).run("e", "bedtime")
    assert status == 409 and "run_as is empty" in answer["error"]
    assert not out.exists()


def test_a_missing_program_is_an_answer_not_an_exception(rec):
    status, answer = RippleRunner(command=["definitely-not-a-real-cli-4f2a", "{message}"]).run("e", "hi")
    assert status == 200 and answer["ok"] is False and "not on the PATH" in answer["error"]
