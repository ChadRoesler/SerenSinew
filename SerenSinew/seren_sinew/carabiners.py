"""
seren_sinew.carabiners
════════════════════════════════════════════════════════════════════════

A carabiner's settings, changed from Lodestar's Rack, said the same way by
everyone who carries them.

THE CHAIN (the same road ensure takes to start llama, kokoro or whisper):

    Rack tab ──SettingsRequest──▶ Lodestar ──▶ the node's Observatory
                                                    │ runs, AS the person:
                                                    ▼ kbh <c> config apply
    Rack tab ◀──SettingsResult─── Lodestar ◀── the carabiner checked every
                                               value, wrote its yaml, belayed

Nobody on the road knows what a setting means. The carabiner says which
settings it has, what each takes and when a change counts (kbh.schema), and
refuses what it will not take, so a new harness is a new .kbh and nothing
else (22 Oct 2026). Settings are set on each box on their own: a Haiku with a
bare wake note for one box's hippocampus, Opus on another, nothing copied
between them.

    GET  /api/v1/carabiners                          -> [CarabinerEntry]
    GET  /api/v1/carabiner/{carabiner}/settings      -> CarabinerManifest
    POST /api/v1/carabiner/{carabiner}/settings      SettingsRequest -> SettingsResult
    GET  /api/v1/carabiner/{carabiner}/belay         -> BelayReport
    POST /api/v1/carabiner/{carabiner}/upgrade       UpgradeRequest -> UpgradeResult
    POST /api/v1/carabiner/{carabiner}/clip          UpgradeRequest -> UpgradeResult (a first file)
    POST /api/v1/carabiner/{carabiner}/unclip        {"reason"} -> UnclipResult

The Observatory answers for the carabiners on its own box; Lodestar answers
for the cluster, choosing the Observatory by the request's `node` (`?node=`
on a GET) and filling `node` in on the way back.

WHAT IS HERE: the messages (plain dataclasses, to_dict / from_dict, tolerant
of fields a newer or older peer adds or lacks, like seren_sinew.orchestration),
and KbhRunner, which runs a .kbh on this box AS the person who owns it, so a
root or LocalSystem service never writes someone's yaml as itself. The
request reaches kbh in KBH_SETTINGS_REQUEST, not on stdin: a process started
in another person's Windows session gets no stdin (seren_sinew.runas).
Tokens never travel this road.

UPGRADES (23 Oct 2026). Lodestar pulls a .kbh (a GitHub release, or one put
on its shelf by hand) and sends the file itself, base64 with its sha256, so a
harness box never has to reach the internet. The Observatory checks the hash,
writes the file beside the one it replaces, and has the INSTALLED .kbh run
`kbh upgrade` as the person: belay before, swap, belay after, the old one put
back if anything that held lets go. A .kbh from before 5.1.3 has no upgrade
verb; then the new file runs it (`--into` the old one), once.

CLIP AND UNCLIP (29 Oct 2026). A clip is a carabiner's FIRST file on a box:
the same message as an upgrade, checked the same way, written as
<carabiner>.kbh - and nothing of it runs, because there is no installed .kbh
to belay with and what it registers in its harness (hooks, an MCP server, a
bookmark) is `kbh <c> register`, the person's, from the settings sheet or by
hand. An unclip moves the .kbh beside itself as .unclipped-<version>, never
deletes it, leaves the yaml (the settings are the person's; a re-clip finds
them), and says plainly that what the carabiner registered is still there.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
import time
import zipfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Optional

from . import runas

LIST_PATH = "/api/v1/carabiners"
SETTINGS_PATH = "/api/v1/carabiner/{carabiner}/settings"
BELAY_PATH = "/api/v1/carabiner/{carabiner}/belay"
UPGRADE_PATH = "/api/v1/carabiner/{carabiner}/upgrade"
CLIP_PATH = "/api/v1/carabiner/{carabiner}/clip"
UNCLIP_PATH = "/api/v1/carabiner/{carabiner}/unclip"

REQUEST_ENV = "KBH_SETTINGS_REQUEST"
MAX_REQUEST = 30000            # characters: a Windows environment variable holds 32767
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
MAX_KBH = 8 * 1024 * 1024      # bytes: a .kbh is ~160KB, the all-in-one ~210KB
SHA256 = re.compile(r"[0-9a-f]{64}")


def _known(cls, d: Optional[dict[str, Any]]) -> dict[str, Any]:
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in (d or {}).items() if k in names}


def _strs(v: Any) -> list[str]:
    return [str(x) for x in (v or []) if x not in (None, "")] if isinstance(v, (list, tuple)) else []


def _dicts(v: Any) -> list[dict[str, Any]]:
    return [dict(x) for x in (v or []) if isinstance(x, dict)] if isinstance(v, (list, tuple)) else []


@dataclass
class SettingsRequest:
    """Change these settings of one carabiner on one box."""
    node: str = ""                                      # which box; Lodestar routes on it
    values: dict[str, Any] = field(default_factory=dict)    # setting key -> new value
    unset: list[str] = field(default_factory=list)      # back to the carabiner's default
    expert: bool = False                                # a value the carabiner holds back (Never ask) is taken
    holder: str = ""                                    # who asked, for the logs: "lodestar-rack"
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def for_kbh(self) -> str:
        """What `kbh <c> config apply` reads."""
        return json.dumps({"values": self.values, "unset": self.unset, "expert": self.expert})

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "SettingsRequest":
        k = _known(cls, d)
        values = k.get("values") if isinstance(k.get("values"), dict) else {}
        return cls(node=str(k.get("node") or "")[:120], values={str(a): b for a, b in values.items()},
                   unset=_strs(k.get("unset")), expert=bool(k.get("expert")),
                   holder=str(k.get("holder") or "")[:120], reason=str(k.get("reason") or "")[:200])


@dataclass
class SettingsResult:
    """ok = written. Nothing is written when anything is refused (refused:
    key -> the carabiner's reason). applies: key -> when it counts
    (next_session, next_wake, now). belay: the carabiner's lines after the
    write, {check, ok, said}; belay_ok None when it did not run."""
    ok: bool = False
    carabiner: str = ""
    node: str = ""
    applied: list[str] = field(default_factory=list)
    refused: dict[str, str] = field(default_factory=dict)
    applies: dict[str, str] = field(default_factory=dict)
    belay_ok: Optional[bool] = None
    belay: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "SettingsResult":
        k = _known(cls, d)
        refused = k.get("refused") if isinstance(k.get("refused"), dict) else {}
        applies = k.get("applies") if isinstance(k.get("applies"), dict) else {}
        belay_ok = k.get("belay_ok")
        return cls(ok=bool(k.get("ok")), carabiner=str(k.get("carabiner") or ""), node=str(k.get("node") or ""),
                   applied=_strs(k.get("applied")), refused={str(a): str(b) for a, b in refused.items()},
                   applies={str(a): str(b) for a, b in applies.items()},
                   belay_ok=None if belay_ok is None else bool(belay_ok), belay=_dicts(k.get("belay")),
                   error=str(k.get("error") or ""))

    @classmethod
    def failed(cls, carabiner: str, error: str, node: str = "") -> "SettingsResult":
        return cls(ok=False, carabiner=carabiner, node=node, error=error)


@dataclass
class CarabinerManifest:
    """What a form draws: every setting the carabiner says it has, with its
    value now. `fields` are kbh.schema's Field dicts and pass through as the
    carabiner wrote them; `connections` say how each token is kept, never
    the token."""
    ok: bool = False
    carabiner: str = ""
    display: str = ""
    node: str = ""
    version: str = ""
    config: str = ""
    fields: list[dict[str, Any]] = field(default_factory=list)
    connections: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "CarabinerManifest":
        k = _known(cls, d)
        return cls(ok=bool(k.get("ok")), carabiner=str(k.get("carabiner") or ""), display=str(k.get("display") or ""),
                   node=str(k.get("node") or ""), version=str(k.get("version") or ""),
                   config=str(k.get("config") or ""), fields=_dicts(k.get("fields")),
                   connections=_dicts(k.get("connections")), error=str(k.get("error") or ""))

    @classmethod
    def failed(cls, carabiner: str, error: str, node: str = "") -> "CarabinerManifest":
        return cls(ok=False, carabiner=carabiner, node=node, error=error)


@dataclass
class BelayReport:
    """`kbh <c> belay --json`, carried: ok when every check that ran held."""
    ok: bool = False
    carabiner: str = ""
    node: str = ""
    checks: list[dict[str, Any]] = field(default_factory=list)   # {verb, side, name, status, why}
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "BelayReport":
        k = _known(cls, d)
        return cls(ok=bool(k.get("ok")), carabiner=str(k.get("carabiner") or ""), node=str(k.get("node") or ""),
                   checks=_dicts(k.get("checks")), error=str(k.get("error") or ""))

    @classmethod
    def failed(cls, carabiner: str, error: str, node: str = "") -> "BelayReport":
        return cls(ok=False, carabiner=carabiner, node=node, error=error)


@dataclass
class CarabinerEntry:
    """One carabiner clipped on a box: a <name>.kbh in the folder, with its
    <name>.yaml beside it (configured false until there is one). version and
    sha256 are read from the file, not by running it: what Lodestar compares
    with its shelf to say who is behind."""
    carabiner: str = ""
    node: str = ""
    kbh: str = ""
    configured: bool = False
    version: str = ""                                   # 5.1.3+1f116ed; 5.1.2 from before builds were stamped
    sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "CarabinerEntry":
        k = _known(cls, d)
        return cls(carabiner=str(k.get("carabiner") or ""), node=str(k.get("node") or ""),
                   kbh=str(k.get("kbh") or ""), configured=bool(k.get("configured")),
                   version=str(k.get("version") or ""), sha256=str(k.get("sha256") or ""))


@dataclass
class UpgradeRequest:
    """Put this .kbh where the box's <carabiner>.kbh is. `kbh` is the file,
    base64; `sha256` is required and checked on arrival, before anything of
    it runs. `version` and `source` are what the sender says, for the logs:
    the file says its own version once it is there."""
    node: str = ""
    kbh: str = ""
    sha256: str = ""
    version: str = ""
    source: str = ""                                    # "release v5.1.3", "shelf"
    downgrade: bool = False                             # an older version is taken
    holder: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def carrying(cls, data: bytes, **kw: Any) -> "UpgradeRequest":
        return cls(kbh=base64.b64encode(data).decode("ascii"), sha256=hashlib.sha256(data).hexdigest(), **kw)

    def data(self) -> bytes:
        """The file's bytes. ValueError for one too big or not base64."""
        if len(self.kbh) > (MAX_KBH * 4) // 3 + 4:
            raise ValueError(f"the .kbh is bigger than {MAX_KBH} bytes")
        try:
            data = base64.b64decode(self.kbh, validate=True)
        except (binascii.Error, ValueError) as e:
            raise ValueError(f"the .kbh is not base64: {e}") from e
        if not data:
            raise ValueError("no .kbh came with the request")
        return data

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "UpgradeRequest":
        k = _known(cls, d)
        return cls(node=str(k.get("node") or "")[:120], kbh=str(k.get("kbh") or ""),
                   sha256=str(k.get("sha256") or "").strip().lower()[:64], version=str(k.get("version") or "")[:80],
                   source=str(k.get("source") or "")[:200], downgrade=bool(k.get("downgrade")),
                   holder=str(k.get("holder") or "")[:120], reason=str(k.get("reason") or "")[:200])


@dataclass
class UpgradeResult:
    """`kbh upgrade --json`, carried. ok with changed false: it was already
    that build. rolled_back: the new one let go of what the old one held
    (belay: carabiner -> {before_ok, after_ok, lost, new_let_go}) and the old
    one is back. bootstrap: the old .kbh had no upgrade verb, so the new one
    ran it."""
    ok: bool = False
    carabiner: str = ""
    node: str = ""
    was: str = ""
    now: str = ""
    changed: bool = False
    rolled_back: bool = False
    backup: str = ""
    sha256: str = ""
    sha256_checked: bool = False
    bootstrap: bool = False
    belay: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "UpgradeResult":
        k = _known(cls, d)
        belay = k.get("belay") if isinstance(k.get("belay"), dict) else {}
        return cls(ok=bool(k.get("ok")), carabiner=str(k.get("carabiner") or ""), node=str(k.get("node") or ""),
                   was=str(k.get("was") or ""), now=str(k.get("now") or ""), changed=bool(k.get("changed")),
                   rolled_back=bool(k.get("rolled_back")), backup=str(k.get("backup") or ""),
                   sha256=str(k.get("sha256") or ""), sha256_checked=bool(k.get("sha256_checked")),
                   bootstrap=bool(k.get("bootstrap")), belay=dict(belay), error=str(k.get("error") or ""))

    @classmethod
    def failed(cls, carabiner: str, error: str, node: str = "") -> "UpgradeResult":
        return cls(ok=False, carabiner=carabiner, node=node, error=error)


@dataclass
class UnclipResult:
    """A carabiner taken off a box. `kept` is where its .kbh went (beside
    itself, never deleted); `yaml_kept` says its settings file is still
    there for a re-clip. `note` says what this did NOT do: what the carabiner
    registered in its harness is still registered."""
    ok: bool = False
    carabiner: str = ""
    node: str = ""
    was: str = ""                                       # the version that came off
    kept: str = ""
    yaml_kept: bool = False
    reason: str = ""
    note: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "UnclipResult":
        k = _known(cls, d)
        return cls(ok=bool(k.get("ok")), carabiner=str(k.get("carabiner") or ""), node=str(k.get("node") or ""),
                   was=str(k.get("was") or ""), kept=str(k.get("kept") or ""), yaml_kept=bool(k.get("yaml_kept")),
                   reason=str(k.get("reason") or ""), note=str(k.get("note") or ""), error=str(k.get("error") or ""))

    @classmethod
    def failed(cls, carabiner: str, error: str, node: str = "") -> "UnclipResult":
        return cls(ok=False, carabiner=carabiner, node=node, error=error)


_VERSION = re.compile(r"""__version__\s*=\s*["']([^"']+)""")
_BUILD = re.compile(r"""BUILD\s*=\s*["']([^"']*)""")


def kbh_identity(path: str) -> tuple[str, str]:
    """(version, sha256) of a .kbh, read from the zip, never run: the
    version kbh/__init__.py says and the build kbh/_build.py stamps."""
    data = Path(path).read_bytes()
    version = ""
    try:
        with zipfile.ZipFile(path) as zf:
            m = _VERSION.search(zf.read("kbh/__init__.py").decode("utf-8", "replace"))
            version = m.group(1) if m else ""
            if version and "kbh/_build.py" in zf.namelist():
                b = _BUILD.search(zf.read("kbh/_build.py").decode("utf-8", "replace"))
                if b and b.group(1):
                    version += "+" + b.group(1)
    except (zipfile.BadZipFile, KeyError, OSError):
        pass
    return version, hashlib.sha256(data).hexdigest()


def has_upgrade_verb(path: str) -> bool:
    """Whether this .kbh can upgrade itself (5.1.3 on)."""
    try:
        with zipfile.ZipFile(path) as zf:
            return "kbh/upgrade.py" in zf.namelist()
    except (zipfile.BadZipFile, OSError):
        return False


# ── running kbh on this box ──────────────────────────────────────────

def _last_json(text: str) -> Optional[dict[str, Any]]:
    """The JSON object kbh printed. stderr shares the stream (runas merges
    them), so a warning line may come first."""
    try:
        d = json.loads(text)
        return d if isinstance(d, dict) else None
    except ValueError:
        pass
    for i in [m.start() for m in re.finditer(r"(?m)^\{", text)]:
        try:
            d = json.loads(text[i:])
            if isinstance(d, dict):
                return d
        except ValueError:
            continue
    return None


class KbhRunner:
    """The carabiners clipped on this box: every <name>.kbh in `folder`, its
    <name>.yaml beside it, run with `python` AS `run_as`. The folder and the
    python are this box's config, never a caller's; a caller names a
    carabiner, and only one whose .kbh is in the folder is run."""

    def __init__(self, *, folder: str, python: str, run_as: str = "", timeout_seconds: float = 60.0,
                 node: str = "", upgrade_timeout_seconds: float = 600.0) -> None:
        self.folder = os.path.abspath(os.path.expanduser(folder)) if folder else ""
        self.python = python
        self.run_as = run_as or ""
        self.timeout_seconds = float(timeout_seconds)
        # An upgrade belays twice, and a belay waits on every service the yaml names.
        self.upgrade_timeout_seconds = float(upgrade_timeout_seconds)
        self.node = node
        self._upgrading = threading.Lock()

    def list(self) -> list[CarabinerEntry]:
        if not self.folder or not os.path.isdir(self.folder):
            return []
        out = []
        for p in sorted(Path(self.folder).glob("*.kbh")):
            if NAME.fullmatch(p.stem):
                try:
                    version, sha = kbh_identity(str(p))
                except OSError:
                    version, sha = "", ""
                out.append(CarabinerEntry(carabiner=p.stem, node=self.node, kbh=str(p),
                                          configured=(p.with_suffix(".yaml")).is_file(), version=version, sha256=sha))
        return out

    def _paths(self, carabiner: str) -> tuple[str, str]:
        if not NAME.fullmatch(carabiner or ""):
            raise LookupError(f"not a carabiner name: {carabiner!r}")
        kbh = os.path.join(self.folder, f"{carabiner}.kbh")
        if not self.folder or not os.path.isfile(kbh):
            raise LookupError(f"no {carabiner}.kbh clipped on this box")
        return kbh, os.path.join(self.folder, f"{carabiner}.yaml")

    def run(self, carabiner: str, args: list[str], env: Optional[dict[str, str]] = None) -> tuple[int, str]:
        """(exit code, what it printed). Raises LookupError for a carabiner
        that is not here, runas.RunAsRefused / OSError when it cannot start,
        TimeoutError when it ran too long (it is killed)."""
        kbh, yaml = self._paths(carabiner)
        return self._exec([self.python, kbh, carabiner, *args, "--config", yaml], env, self.timeout_seconds,
                          f"kbh {carabiner} {args[0] if args else ''}")

    def _exec(self, argv: list[str], env: Optional[dict[str, str]], timeout: float, what: str) -> tuple[int, str]:
        fd, out_path = tempfile.mkstemp(prefix="kbh-", suffix=".out")
        try:
            with os.fdopen(fd, "wb") as out:
                proc = runas.spawn(argv, run_as=self.run_as, env=env, cwd=self.folder, stdout=out)
                try:
                    rc = proc.wait(timeout=timeout)
                except Exception as e:  # noqa: BLE001 - subprocess.TimeoutExpired, or the session process's own
                    proc.kill()
                    raise TimeoutError(f"{what} ran past {timeout:g}s and was stopped") from e
            with open(out_path, encoding="utf-8", errors="replace") as f:
                return int(rc if rc is not None else 1), f.read()
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass

    def _json(self, carabiner: str, args: list[str], env: Optional[dict[str, str]] = None) -> dict[str, Any]:
        rc, text = self.run(carabiner, args, env)
        d = _last_json(text)
        if d is None:
            raise ValueError(f"kbh {carabiner} answered no JSON (exit {rc}): {text.strip()[-300:]}")
        return d

    def manifest(self, carabiner: str) -> CarabinerManifest:
        try:
            m = CarabinerManifest.from_dict(self._json(carabiner, ["config", "show", "--json"]))
        except Exception as e:  # noqa: BLE001 - an answer, not a 500
            return CarabinerManifest.failed(carabiner, f"{type(e).__name__}: {e}", self.node)
        m.node = self.node
        return m

    def apply(self, carabiner: str, req: SettingsRequest) -> SettingsResult:
        body = req.for_kbh()
        if len(body) > MAX_REQUEST:
            return SettingsResult.failed(carabiner, f"the change is longer than {MAX_REQUEST} characters", self.node)
        try:
            r = SettingsResult.from_dict(self._json(carabiner, ["config", "apply"], {REQUEST_ENV: body}))
        except Exception as e:  # noqa: BLE001
            return SettingsResult.failed(carabiner, f"{type(e).__name__}: {e}", self.node)
        r.carabiner, r.node = carabiner, self.node
        return r

    def belay(self, carabiner: str) -> BelayReport:
        try:
            d = self._json(carabiner, ["belay", "--json"])
        except Exception as e:  # noqa: BLE001
            return BelayReport.failed(carabiner, f"{type(e).__name__}: {e}", self.node)
        return BelayReport(ok=bool(d.get("ok")), carabiner=carabiner, node=self.node, checks=_dicts(d.get("checks")))

    def upgrade(self, carabiner: str, req: UpgradeRequest) -> UpgradeResult:
        """Check the file that came, put it beside <carabiner>.kbh, and have
        kbh swap it in AS the person (kbh.upgrade has the steps). One at a
        time on a box; the incoming file is gone afterwards either way."""
        try:
            kbh, yaml = self._paths(carabiner)
        except LookupError as e:
            return UpgradeResult.failed(carabiner, f"LookupError: {e}", self.node)
        if not SHA256.fullmatch(req.sha256 or ""):
            return UpgradeResult.failed(carabiner, "an upgrade carries the file's sha256; this one did not", self.node)
        try:
            data = req.data()
        except ValueError as e:
            return UpgradeResult.failed(carabiner, str(e), self.node)
        got = hashlib.sha256(data).hexdigest()
        if got != req.sha256:
            return UpgradeResult.failed(carabiner, f"the file that arrived has sha256 {got}, not {req.sha256}: "
                                                   f"damaged on the way; nothing was written", self.node)
        if not self._upgrading.acquire(blocking=False):
            return UpgradeResult.failed(carabiner, "an upgrade is already running on this box", self.node)
        incoming = os.path.join(self.folder, f".{carabiner}.kbh.incoming-{secrets.token_hex(4)}")
        try:
            with open(incoming, "wb") as f:
                f.write(data)
            os.chmod(incoming, 0o644)                    # written by the service, read by the person
            bootstrap = not has_upgrade_verb(kbh)
            argv = [self.python, incoming if bootstrap else kbh, "upgrade", "--into", kbh, "--from", incoming,
                    "--sha256", got, "--json"]
            if os.path.isfile(yaml):
                argv += ["--config", yaml]
            if req.downgrade:
                argv.append("--downgrade")
            rc, text = self._exec(argv, None, self.upgrade_timeout_seconds, f"kbh upgrade of {carabiner}")
            d = _last_json(text)
            if d is None:
                return UpgradeResult.failed(carabiner, f"kbh upgrade answered no JSON (exit {rc}): "
                                                       f"{text.strip()[-300:]}", self.node)
            r = UpgradeResult.from_dict(d)
            r.carabiner, r.node, r.bootstrap = carabiner, self.node, bootstrap
            return r
        except Exception as e:  # noqa: BLE001 - an answer, not a 500
            return UpgradeResult.failed(carabiner, f"{type(e).__name__}: {e}", self.node)
        finally:
            try:
                os.unlink(incoming)
            except OSError:
                pass
            self._upgrading.release()

    def clip(self, carabiner: str, req: UpgradeRequest) -> UpgradeResult:
        """A carabiner's FIRST file on this box: check the hash, put it in the
        folder as <carabiner>.kbh. Nothing of it runs here - there is no
        installed .kbh to belay with - and what it registers in its harness is
        the person's `kbh <c> register`, from the settings sheet or by hand.
        One that is already here is an upgrade, not a clip."""
        if not NAME.fullmatch(carabiner or ""):
            return UpgradeResult.failed(carabiner, f"LookupError: not a carabiner name: {carabiner!r}", self.node)
        if not self.folder:
            return UpgradeResult.failed(carabiner, "carabiners have no folder on this box", self.node)
        kbh = os.path.join(self.folder, f"{carabiner}.kbh")
        if os.path.exists(kbh):
            return UpgradeResult.failed(carabiner, f"{carabiner}.kbh is already clipped on this box: "
                                                   f"an upgrade swaps a new file in", self.node)
        if not SHA256.fullmatch(req.sha256 or ""):
            return UpgradeResult.failed(carabiner, "a clip carries the file's sha256; this one did not", self.node)
        try:
            data = req.data()
        except ValueError as e:
            return UpgradeResult.failed(carabiner, str(e), self.node)
        got = hashlib.sha256(data).hexdigest()
        if got != req.sha256:
            return UpgradeResult.failed(carabiner, f"the file that arrived has sha256 {got}, not {req.sha256}: "
                                                   f"damaged on the way; nothing was written", self.node)
        if not self._upgrading.acquire(blocking=False):
            return UpgradeResult.failed(carabiner, "an upgrade is already running on this box", self.node)
        incoming = os.path.join(self.folder, f".{carabiner}.kbh.incoming-{secrets.token_hex(4)}")
        try:
            os.makedirs(self.folder, exist_ok=True)
            with open(incoming, "wb") as f:
                f.write(data)
            os.chmod(incoming, 0o644)                    # written by the service, read by the person
            version, _ = kbh_identity(incoming)
            if not version:
                os.unlink(incoming)
                return UpgradeResult.failed(carabiner, "not a .kbh: it carries no kbh/__init__.py with a "
                                                       "version; nothing was clipped", self.node)
            os.replace(incoming, kbh)
            return UpgradeResult(ok=True, carabiner=carabiner, node=self.node, was="", now=version, changed=True,
                                 sha256=got, sha256_checked=True)
        except OSError as e:
            try:
                os.unlink(incoming)
            except OSError:
                pass
            return UpgradeResult.failed(carabiner, f"{type(e).__name__}: {e}", self.node)
        finally:
            self._upgrading.release()

    def unclip(self, carabiner: str, reason: str = "") -> UnclipResult:
        """Take a carabiner off this box: <carabiner>.kbh moves beside itself
        as <carabiner>.kbh.unclipped-<version>, never deleted, and its yaml
        stays - the settings are the person's, and a re-clip finds them. What
        the carabiner registered in its harness (hooks, an MCP server, a
        bookmark) is not touched; the answer says so."""
        try:
            kbh, yaml = self._paths(carabiner)
        except LookupError as e:
            return UnclipResult.failed(carabiner, f"LookupError: {e}", self.node)
        if not self._upgrading.acquire(blocking=False):
            return UnclipResult.failed(carabiner, "an upgrade is running on this box; try again when it ends",
                                       self.node)
        try:
            version, _ = kbh_identity(kbh)
            stamp = re.sub(r"[^A-Za-z0-9._+-]", "_", version or time.strftime("%Y%m%d-%H%M%S"))
            kept, n = f"{kbh}.unclipped-{stamp}", 1
            while os.path.exists(kept):
                n += 1
                kept = f"{kbh}.unclipped-{stamp}-{n}"
            os.replace(kbh, kept)
        except OSError as e:
            return UnclipResult.failed(carabiner, f"{type(e).__name__}: {e}", self.node)
        finally:
            self._upgrading.release()
        return UnclipResult(ok=True, carabiner=carabiner, node=self.node, was=version, kept=kept,
                            yaml_kept=os.path.isfile(yaml), reason=(reason or "")[:200],
                            note=f"{carabiner}.kbh is off the Rack and kept beside itself; its yaml stays for a "
                                 f"re-clip. What it registered in its harness (hooks, an MCP server, a bookmark) "
                                 f"is still registered: that is the person's to undo.")
