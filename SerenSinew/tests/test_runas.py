"""
Run a command as a person, from a service that runs as someone else (28 Sept
2026: the hippocampus's ripple and the Observatory's, "a localsystem can call
it AS me as needed").

The policy is pure, so every row of it is checked here on any box:

- a privileged service (root, LocalSystem) with no run_as REFUSES - the config
  must not be a free root shell - and says how to fix it
- the same account, or root/SYSTEM asked for on purpose, runs directly
- root on Linux drops with runuser; anyone else there needs sudo
- LocalSystem on Windows borrows the person's logged-on session; any other
  Windows account asking for someone else is refused with the reason
- names compare without the domain and case (".\\Alice" is "alice")

and the mechanics that don't need a privileged process: the runuser / sudo
command lines, the Windows environment block round trip, finding a program on
a given PATH, and a real direct spawn.
"""
from __future__ import annotations

import os
import sys

import pytest

from seren_sinew import runas
from seren_sinew.runas import Plan, RunAsRefused, plan


@pytest.mark.parametrize("current,windows", [(("root", True), False), (("SYSTEM", True), True)])
def test_a_privileged_service_with_no_run_as_refuses(current, windows):
    with pytest.raises(RunAsRefused) as e:
        plan("", current=current, windows=windows)
    assert "run_as is empty" in str(e.value) and ("root" in str(e.value) or "SYSTEM" in str(e.value))


def test_an_ordinary_account_with_no_run_as_runs_as_itself():
    assert plan("", current=("alice", False), windows=False) == Plan("direct", "alice", "alice")


@pytest.mark.parametrize("run_as", ["alice", "Alice", ".\\alice", "BOX\\alice", "alice@box"])
def test_the_same_account_runs_directly_whatever_the_spelling(run_as):
    assert plan(run_as, current=("alice", False), windows=True).mode == "direct"


@pytest.mark.parametrize("current,run_as,windows", [(("root", True), "root", False), (("SYSTEM", True), "SYSTEM", True)])
def test_root_or_system_on_purpose_runs_directly(current, run_as, windows):
    assert plan(run_as, current=current, windows=windows).mode == "direct"


def test_root_on_linux_drops_with_runuser():
    assert plan("alice", current=("root", True), windows=False) == Plan("runuser", "alice", "root")


def test_another_linux_account_needs_sudo():
    assert plan("alice", current=("seren", False), windows=False).mode == "sudo"


def test_localsystem_borrows_the_logged_on_session():
    p = plan(".\\Alice", current=("SYSTEM", True), windows=True)
    assert p.mode == "windows-session" and p.user == ".\\Alice"


def test_another_windows_account_cannot_switch():
    with pytest.raises(RunAsRefused) as e:
        plan("alice", current=("svc-seren", False), windows=True)
    assert "only LocalSystem" in str(e.value)


def test_the_runuser_line_sets_the_persons_home():
    cmd = runas.linux_argv(["/home/alice/.local/bin/claude", "-p", "hi"], Plan("runuser", "alice", "root"), "/home/alice")
    assert cmd == ["runuser", "-u", "alice", "--", "env", "HOME=/home/alice", "USER=alice", "LOGNAME=alice",
                   "/home/alice/.local/bin/claude", "-p", "hi"]


def test_the_sudo_line_is_non_interactive():
    cmd = runas.linux_argv(["claude"], Plan("sudo", "alice", "seren"), "/home/alice")
    assert cmd[:6] == ["sudo", "-n", "-u", "alice", "-H", "--"]


def test_the_environment_block_round_trips():
    env = {"PATH": "C:\\a;C:\\b", "USERPROFILE": "C:\\Users\\alice", "=C:": "C:\\x", "empty": ""}
    block = runas.build_env_block(env)
    assert block.endswith("\0\0")
    assert runas.parse_env_block(block) == env


def test_a_program_is_found_on_the_given_path(tmp_path):
    d = tmp_path / "bin"; d.mkdir()
    (d / "claude.cmd").write_text("@echo off\n")
    assert runas._find("claude", [str(tmp_path), str(d)], [".exe", ".cmd"]) == str(d / "claude.cmd")
    assert runas._find("nope", [str(d)], [".exe"]) is None
    assert runas._find(str(d / "claude.cmd"), []) == str(d / "claude.cmd"), "an absolute path always works"


def test_a_direct_spawn_runs_and_passes_the_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(runas, "whoami", lambda: ("alice", False))
    out = tmp_path / "out.txt"
    script = f"import os; open(r'{out}', 'w').write(os.environ['SEREN_TEST_VALUE'])"
    p = runas.spawn([sys.executable, "-c", script], env={"SEREN_TEST_VALUE": "rippled"})
    assert p.wait(timeout=30) == 0
    assert out.read_text() == "rippled"


def test_spawn_refuses_before_starting_anything(monkeypatch):
    monkeypatch.setattr(runas, "whoami", lambda: ("SYSTEM" if os.name == "nt" else "root", True))
    with pytest.raises(RunAsRefused):
        runas.spawn([sys.executable, "-c", "raise SystemExit(9)"])


def test_a_missing_program_names_the_account(monkeypatch):
    monkeypatch.setattr(runas, "whoami", lambda: ("alice", False))
    with pytest.raises(FileNotFoundError) as e:
        runas.spawn(["definitely-not-a-real-cli-4f2a"])
    assert "alice" in str(e.value)
