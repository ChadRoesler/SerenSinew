"""
seren_sinew.ripple
════════════════════════════════════════════════════════════════════════

Running a ripple: the one runner every service that receives one uses.

A ripple is the hippocampus reaching the main model - at bedtime for a brief,
when a draft waits for review - named for the sharp-wave ripples a sleeping
hippocampus fires to reach the cortex (Design note:). Where it is run
depends on the setup, and every place runs it the same way, through this:

- the hippocampus itself, when the model is on its box (`ripple.type: script`)
- the model box's Observatory (POST /api/v1/system/ripple)
- Lodestar, forwarding to a node or running it on its own box (`target: local`)

What the runner guarantees, wherever it runs:

- the COMMAND is the runner's own config; a caller supplies only the event,
  the message and a draft id - a bearer buys a message into a known program,
  not a remote shell
- the message fills {message} in one argument, or - with `stdin: true` - is
  written to the command's stdin, which is how it crosses
  `ssh desktop claude -p` for a setup with neither Lodestar nor an
  Observatory: no remote shell ever parses it. {event} and {draft_id} fill
  too; the event also rides in SEREN_RIPPLE_EVENT / _MESSAGE / _JSON
- it runs AS `run_as` (seren_sinew.runas), and a root / LocalSystem service
  with no run_as refuses
- one at a time per event, in the background, killed after timeout_seconds,
  output appended to the log file; an empty or oversized message is refused

`run()` never raises: it answers (http status, body) so a route can return it
as is and a caller in-process can read the same shape.
"""
from __future__ import annotations

import json
import shlex
import subprocess
import threading
from pathlib import Path
from typing import Any, Optional, Union

from . import runas

MAX_MESSAGE = 8000
DEFAULT_COMMAND = ["claude", "-p", "{message}"]


def fill(text: str, values: dict[str, str]) -> str:
    """Replace the known placeholders only, so braces anywhere else (JSON in a
    message, say) are left alone rather than raising KeyError."""
    for k, v in values.items():
        text = text.replace("{" + k + "}", v)
    return text


class RippleRunner:
    def __init__(self, *, command: Union[list[str], str, None] = None, run_as: str = "",
                 cwd: str = "", timeout_seconds: float = 900.0, stdin: bool = False,
                 log_path: Optional[Path] = None) -> None:
        self.command = DEFAULT_COMMAND if command is None else command
        self.run_as = run_as or ""
        self.cwd = cwd or ""
        self.timeout_seconds = float(timeout_seconds)
        self.stdin = bool(stdin)
        self.log_path = log_path
        self._running: dict[str, Any] = {}
        self._lock = threading.Lock()

    def argv(self, event: str, message: str, draft_id: str = "") -> list[str]:
        cmd = self.command
        parts = shlex.split(cmd, posix=not runas.IS_WINDOWS) if isinstance(cmd, str) else [str(p) for p in cmd]
        if not parts:
            raise ValueError("the ripple command is empty")
        values = {"message": message, "event": event, "draft_id": draft_id}
        return [fill(p, values) for p in parts]

    def run(self, event: str, message: str, *, draft_id: str = "",
            payload: Optional[dict[str, Any]] = None) -> tuple[int, dict[str, Any]]:
        event = (event or "ripple")[:64]
        message = message or ""
        if not message.strip():
            return 400, {"ok": False, "error": "a ripple needs a message"}
        if len(message) > MAX_MESSAGE:
            return 413, {"ok": False, "error": f"message longer than {MAX_MESSAGE} characters"}
        with self._lock:
            prev = self._running.get(event)
            if prev is not None and prev.poll() is None:
                return 200, {"ok": False, "skipped": "the last ripple for this event is still running"}
        try:
            argv = self.argv(event, message, draft_id)
        except ValueError as e:
            return 409, {"ok": False, "error": str(e)}
        body = dict(payload or {})
        body.setdefault("event", event)
        env = {"SEREN_RIPPLE_EVENT": event, "SEREN_RIPPLE_MESSAGE": message,
               "SEREN_RIPPLE_JSON": json.dumps(body, default=str)[:65536]}
        out: Any = subprocess.DEVNULL
        try:
            if self.log_path is not None:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                out = open(self.log_path, "ab")                         # noqa: SIM115 - handed to the child
            proc = runas.spawn(argv, run_as=self.run_as, env=env, cwd=self.cwd or None, stdout=out,
                               stdin_data=message.encode("utf-8") if self.stdin else None)
        except runas.RunAsRefused as e:
            return 409, {"ok": False, "error": str(e)}
        except Exception as e:  # noqa: BLE001 - missing program, OS refusal: an answer, not a 500
            return 200, {"ok": False, "error": f"{type(e).__name__}: {e}"}
        with self._lock:
            self._running[event] = proc
        threading.Thread(target=self._reap, args=(proc,), daemon=True).start()
        return 200, {"ok": True, "pid": proc.pid, "event": event, "run_as": self.run_as or None}

    def _reap(self, proc: Any) -> None:
        try:
            proc.wait(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            proc.kill()

    def wait(self, timeout: float = 30.0) -> None:
        """Tests and shutdown: wait for ripples still running."""
        with self._lock:
            procs = list(self._running.values())
        for p in procs:
            try:
                p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                p.kill()
