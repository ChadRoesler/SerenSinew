"""
seren_sinew.runas
════════════════════════════════════════════════════════════════════════

Start a command AS a person, from a service that runs as someone else.

WHY: a service on a Seren box usually runs as root (a node's systemd unit) or
LocalSystem (an NSSM service on Windows), and the thing it needs to start
lives in a person's account - `claude` and its login in their profile, their
PATH, their home. The hippocampus's ripple wakes the main model this way, and
the Observatory runs a ripple on the box the model lives on. Design note:
2026: "a localsystem can call it AS me as needed". One copy of that here, so
every service that needs it does it the same way. It lives in Sinew, not
Meninges: Sinew is the connective RUNTIME code the services share, Meninges
the membrane (contracts, config, auth) - Design note:.

How, by platform:

- Linux, running as root: `runuser -u <user> --` with that user's HOME, USER
  and LOGNAME. Root may do this without a password.
- Linux, running as someone else: `sudo -n -u <user> -H --`. Works only when
  a sudoers rule allows it - a narrow grant, like seren-systemctl's.
- Windows, running as LocalSystem: borrow the user's LOGGED-ON session
  (WTSQueryUserToken) and CreateProcessAsUser with that session's own
  environment block, so USERPROFILE, APPDATA and PATH are the person's. No
  password is stored anywhere. It needs the person to be logged on; when they
  are not, it says so - the thing that wanted them waits for next time.
- Anything else wanting another account is refused, with the reason.

THE SAFETY RULE: a privileged service (root, LocalSystem) with no `run_as`
refuses. Otherwise whoever can edit the service's config gets a root shell
for free. Dropping to a person is always the safe direction; running as
root/SYSTEM has to be said on purpose: run_as "root" (or "SYSTEM").

The program is found on the TARGET user's PATH (the session's PATH on
Windows; ~/.local/bin and ~/bin ahead of the service's PATH on Linux), and an
absolute path always works. Arguments are a list and never pass through a
shell.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import IO, Any, Optional, Union

IS_WINDOWS = os.name == "nt"
PRIVILEGED_NAMES = {"root", "system"}


class RunAsRefused(PermissionError):
    """The requested identity cannot (or must not) be used from here."""


@dataclass
class Plan:
    mode: str          # direct | runuser | sudo | windows-session
    user: str          # who the command runs as
    current: str       # who this process is


def _bare(name: str) -> str:
    """'DOMAIN\\alice', '.\\alice', 'alice@host' -> 'alice', lowercased."""
    n = (name or "").strip()
    n = n.split("\\")[-1].split("@")[0]
    return n.lower()


def whoami() -> tuple[str, bool]:
    """(this process's account name, privileged?) - root or LocalSystem."""
    if IS_WINDOWS:
        import ctypes
        buf = ctypes.create_unicode_buffer(257)
        size = ctypes.c_ulong(len(buf))
        name = buf.value if ctypes.windll.advapi32.GetUserNameW(buf, ctypes.byref(size)) else ""
        name = name or os.environ.get("USERNAME", "")
        return name, name.upper() == "SYSTEM"
    import pwd
    euid = os.geteuid()
    try:
        name = pwd.getpwuid(euid).pw_name
    except KeyError:
        name = str(euid)
    return name, euid == 0


def plan(run_as: str = "", *, current: Optional[tuple[str, bool]] = None,
         windows: Optional[bool] = None) -> Plan:
    """Decide how to run as `run_as`. Pure given `current` and `windows`, so
    the policy is testable anywhere; raises RunAsRefused with the reason."""
    name, privileged = current if current is not None else whoami()
    win = IS_WINDOWS if windows is None else windows
    want = _bare(run_as)
    if not want:
        if privileged:
            raise RunAsRefused(
                f"this service runs as {name} and run_as is empty - refusing to run the command as {name}. "
                f"Set run_as to the account whose login the command needs (the Starwright card defaults it to "
                f"whoever installed it), or to '{'SYSTEM' if win else 'root'}' to run it that way on purpose.")
        return Plan("direct", name, name)
    if want == _bare(name) or (privileged and want in PRIVILEGED_NAMES):
        return Plan("direct", name, name)
    if win:
        if privileged:
            return Plan("windows-session", run_as.strip(), name)
        raise RunAsRefused(f"this service runs as {name}; only LocalSystem can start a command as another "
                           f"logged-on user ({run_as}). Run the service as LocalSystem, or as {run_as}.")
    return Plan("runuser" if privileged else "sudo", want, name)


# ── Linux ─────────────────────────────────────────────────────────────

def _linux_home(user: str) -> str:
    import pwd
    try:
        return pwd.getpwnam(user).pw_dir
    except KeyError:
        raise RunAsRefused(f"no such user on this box: {user}") from None


def _find(program: str, path_dirs: list[str], exts: Optional[list[str]] = None) -> Optional[str]:
    """`program` on the given PATH (with PATHEXT on Windows), or None."""
    if os.path.isabs(program) or os.sep in program or (os.altsep and os.altsep in program):
        return program if os.path.isfile(program) else None
    for d in path_dirs:
        if not d:
            continue
        for ext in ([""] + (exts or [])):
            cand = os.path.join(d, program + ext)
            if os.path.isfile(cand):
                return cand
    return None


def linux_argv(argv: list[str], p: Plan, home: str) -> list[str]:
    """The command line for a runuser / sudo plan. Pure: tested anywhere."""
    if p.mode == "runuser":
        return ["runuser", "-u", p.user, "--", "env", f"HOME={home}", f"USER={p.user}",
                f"LOGNAME={p.user}", *argv]
    if p.mode == "sudo":
        return ["sudo", "-n", "-u", p.user, "-H", "--", *argv]
    return list(argv)


# ── Windows: the logged-on user's session ─────────────────────────────

def parse_env_block(raw: str) -> dict[str, str]:
    """'A=1\\0B=2\\0\\0' -> {'A': '1', 'B': '2'}. Entries starting with '='
    (the per-drive cwd) are kept verbatim under their own key."""
    out: dict[str, str] = {}
    for entry in raw.split("\0"):
        if not entry:
            continue
        k, sep, v = entry[1:].partition("=")
        if sep:
            out[entry[0] + k] = v
    return out


def build_env_block(env: dict[str, str]) -> str:
    """The reverse, sorted case-insensitively the way Windows wants it."""
    return "".join(f"{k}={v}\0" for k, v in sorted(env.items(), key=lambda kv: kv[0].upper())) + "\0"


class _SessionProcess:
    """The slice of Popen the callers use: pid, poll, wait, kill."""

    def __init__(self, handle: Any, pid: int) -> None:
        self._h, self.pid, self.returncode = handle, pid, None

    def poll(self) -> Optional[int]:
        return self._check(0)

    def wait(self, timeout: Optional[float] = None) -> int:
        ms = 0xFFFFFFFF if timeout is None else int(timeout * 1000)
        rc = self._check(ms)
        if rc is None:
            raise subprocess.TimeoutExpired("session process", timeout)
        return rc

    def kill(self) -> None:
        import ctypes
        ctypes.windll.kernel32.TerminateProcess(self._h, 1)

    def _check(self, ms: int) -> Optional[int]:
        import ctypes
        if self.returncode is not None:
            return self.returncode
        if ctypes.windll.kernel32.WaitForSingleObject(self._h, ms) != 0:      # WAIT_OBJECT_0
            return None
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(self._h, ctypes.byref(code))
        self.returncode = int(code.value)
        ctypes.windll.kernel32.CloseHandle(self._h)
        return self.returncode


def _spawn_in_session(argv: list[str], user: str, env: dict[str, str], cwd: Optional[str],
                      out: IO[bytes]) -> _SessionProcess:  # pragma: no cover - needs LocalSystem
    import ctypes
    import msvcrt
    from ctypes import wintypes as w

    wtsapi, advapi, userenv, k32 = (ctypes.windll.wtsapi32, ctypes.windll.advapi32,
                                    ctypes.windll.userenv, ctypes.windll.kernel32)

    class SESSION_INFO(ctypes.Structure):
        _fields_ = [("SessionId", w.DWORD), ("pWinStationName", w.LPWSTR), ("State", ctypes.c_int)]

    # 1. the session this user is logged on to
    sessions, count = ctypes.POINTER(SESSION_INFO)(), w.DWORD()
    if not wtsapi.WTSEnumerateSessionsW(None, 0, 1, ctypes.byref(sessions), ctypes.byref(count)):
        raise OSError(ctypes.get_last_error(), "WTSEnumerateSessions failed")
    target = None
    try:
        for i in range(count.value):
            sid = sessions[i].SessionId
            buf, n = w.LPWSTR(), w.DWORD()
            if wtsapi.WTSQuerySessionInformationW(None, sid, 5, ctypes.byref(buf), ctypes.byref(n)):  # WTSUserName
                name = buf.value or ""
                wtsapi.WTSFreeMemory(buf)
                if name and _bare(name) == _bare(user):
                    target = sid
                    break
    finally:
        wtsapi.WTSFreeMemory(sessions)
    if target is None:
        raise RunAsRefused(f"{user} is not logged on to this box, so there is no session to start the "
                           f"command in; it runs next time they are")

    # 2. their token, as a primary token
    tok, prim = w.HANDLE(), w.HANDLE()
    if not wtsapi.WTSQueryUserToken(target, ctypes.byref(tok)):
        raise OSError(ctypes.get_last_error(), "WTSQueryUserToken failed (is this service LocalSystem?)")
    try:
        if not advapi.DuplicateTokenEx(tok, 0x02000000, None, 2, 1, ctypes.byref(prim)):  # MAXIMUM_ALLOWED, Impersonation, Primary
            raise OSError(ctypes.get_last_error(), "DuplicateTokenEx failed")
    finally:
        k32.CloseHandle(tok)

    # 3. their environment, plus ours
    block = ctypes.c_void_p()
    if not userenv.CreateEnvironmentBlock(ctypes.byref(block), prim, False):
        k32.CloseHandle(prim)
        raise OSError(ctypes.get_last_error(), "CreateEnvironmentBlock failed")
    try:
        raw, i = [], 0
        base = ctypes.cast(block, ctypes.POINTER(ctypes.c_wchar))
        while True:
            if base[i] == "\0" and (i == 0 or base[i - 1] == "\0"):
                break
            raw.append(base[i]); i += 1
        user_env = parse_env_block("".join(raw) + "\0")
    finally:
        userenv.DestroyEnvironmentBlock(block)
    user_env.update(env)
    exe = _find(argv[0], user_env.get("PATH", user_env.get("Path", "")).split(";"),
                [e.lower() for e in (user_env.get("PATHEXT") or ".EXE;.CMD;.BAT").split(";") if e])
    if not exe:
        k32.CloseHandle(prim)
        raise FileNotFoundError(f"'{argv[0]}' is not on {user}'s PATH")
    cmdline = ctypes.create_unicode_buffer(subprocess.list2cmdline([exe, *argv[1:]]))
    env_buf = ctypes.create_unicode_buffer(build_env_block(user_env))

    # 4. start it, output to the caller's file, no window
    class STARTUPINFO(ctypes.Structure):
        _fields_ = [("cb", w.DWORD), ("lpReserved", w.LPWSTR), ("lpDesktop", w.LPWSTR), ("lpTitle", w.LPWSTR),
                    ("dwX", w.DWORD), ("dwY", w.DWORD), ("dwXSize", w.DWORD), ("dwYSize", w.DWORD),
                    ("dwXCountChars", w.DWORD), ("dwYCountChars", w.DWORD), ("dwFillAttribute", w.DWORD),
                    ("dwFlags", w.DWORD), ("wShowWindow", w.WORD), ("cbReserved2", w.WORD),
                    ("lpReserved2", ctypes.c_void_p), ("hStdInput", w.HANDLE), ("hStdOutput", w.HANDLE),
                    ("hStdError", w.HANDLE)]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", w.HANDLE), ("hThread", w.HANDLE), ("dwProcessId", w.DWORD),
                    ("dwThreadId", w.DWORD)]

    hout = msvcrt.get_osfhandle(out.fileno())
    os.set_handle_inheritable(hout, True)
    nul = open(os.devnull, "rb")                                     # noqa: SIM115 - closed below
    hin = msvcrt.get_osfhandle(nul.fileno())
    os.set_handle_inheritable(hin, True)
    si = STARTUPINFO(cb=ctypes.sizeof(STARTUPINFO), lpDesktop="winsta0\\default",
                     dwFlags=0x100, hStdInput=hin, hStdOutput=hout, hStdError=hout)   # STARTF_USESTDHANDLES
    pi = PROCESS_INFORMATION()
    flags = 0x00000400 | 0x08000000                                  # CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW
    try:
        ok = advapi.CreateProcessAsUserW(prim, None, cmdline, None, None, True, flags, env_buf,
                                         cwd or user_env.get("USERPROFILE"), ctypes.byref(si), ctypes.byref(pi))
        if not ok:
            raise OSError(ctypes.get_last_error(), "CreateProcessAsUser failed")
    finally:
        k32.CloseHandle(prim)
        nul.close()
    k32.CloseHandle(pi.hThread)
    return _SessionProcess(pi.hProcess, int(pi.dwProcessId))


# ── the one entry point ───────────────────────────────────────────────

def spawn(argv: list[str], *, run_as: str = "", env: Optional[dict[str, str]] = None,
          cwd: Optional[str] = None, stdout: Union[IO[bytes], int, None] = None,
          stdin_data: Optional[bytes] = None) -> Any:
    """Start `argv` as `run_as` (see the module docstring for how and when it
    refuses). `env` is ADDED to the target's environment. `stdin_data`, when
    given, is written to the command's stdin and closed - how a message crosses
    `ssh host claude -p` without a remote shell ever parsing it. Returns an
    object with pid, poll(), wait(timeout) and kill(). Raises RunAsRefused,
    FileNotFoundError, or OSError - never starts a command as the wrong
    account."""
    if not argv:
        raise ValueError("empty command")
    p = plan(run_as)
    extra = dict(env or {})
    if p.mode == "windows-session":  # pragma: no cover - needs LocalSystem and a logged-on user
        if stdin_data is not None:
            raise RunAsRefused("a stdin ripple cannot be started in another user's session yet; "
                               "put {message} in the command instead")
        out = stdout if hasattr(stdout, "fileno") else open(os.devnull, "wb")   # noqa: SIM115
        return _spawn_in_session(list(argv), p.user, extra, cwd, out)

    full_env = dict(os.environ)
    full_env.update(extra)
    if p.mode in ("runuser", "sudo"):
        home = _linux_home(p.user)
        dirs = [os.path.join(home, ".local", "bin"), os.path.join(home, "bin")] + full_env.get("PATH", "").split(os.pathsep)
        exe = _find(argv[0], dirs)
        if not exe:
            raise FileNotFoundError(f"'{argv[0]}' is not on {p.user}'s PATH (looked in ~/.local/bin, ~/bin, then the service's PATH)")
        cmd = linux_argv([exe, *argv[1:]], p, home)
        if p.mode == "sudo" and extra:
            cmd[cmd.index("--"):cmd.index("--")] = [f"--preserve-env={','.join(sorted(extra))}"]
    else:
        exts = [e.lower() for e in os.environ.get("PATHEXT", "").split(";") if e] if IS_WINDOWS else None
        exe = _find(argv[0], full_env.get("PATH", "").split(os.pathsep), exts) or shutil.which(argv[0])
        if not exe:
            raise FileNotFoundError(f"'{argv[0]}' is not on the PATH of {p.current}, the account this service runs as")
        cmd = [exe, *argv[1:]]
    kw: dict[str, Any] = {"stdout": stdout if stdout is not None else subprocess.DEVNULL,
                          "stderr": subprocess.STDOUT, "cwd": cwd, "env": full_env,
                          "stdin": subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL}
    if IS_WINDOWS:
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(cmd, **kw)
    if stdin_data is not None:
        # Written from a thread: a child that has not started reading must not
        # block the caller (a service's request or its tick).
        def _feed() -> None:
            try:
                proc.stdin.write(stdin_data)
            except OSError:
                pass
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
        threading.Thread(target=_feed, daemon=True).start()
    return proc
