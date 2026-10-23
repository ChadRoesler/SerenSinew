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
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Optional

from . import runas

LIST_PATH = "/api/v1/carabiners"
SETTINGS_PATH = "/api/v1/carabiner/{carabiner}/settings"
BELAY_PATH = "/api/v1/carabiner/{carabiner}/belay"

REQUEST_ENV = "KBH_SETTINGS_REQUEST"
MAX_REQUEST = 30000            # characters: a Windows environment variable holds 32767
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")


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
    <name>.yaml beside it (configured false until there is one)."""
    carabiner: str = ""
    node: str = ""
    kbh: str = ""
    configured: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[dict[str, Any]]) -> "CarabinerEntry":
        k = _known(cls, d)
        return cls(carabiner=str(k.get("carabiner") or ""), node=str(k.get("node") or ""),
                   kbh=str(k.get("kbh") or ""), configured=bool(k.get("configured")))


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
                 node: str = "") -> None:
        self.folder = os.path.abspath(os.path.expanduser(folder)) if folder else ""
        self.python = python
        self.run_as = run_as or ""
        self.timeout_seconds = float(timeout_seconds)
        self.node = node

    def list(self) -> list[CarabinerEntry]:
        if not self.folder or not os.path.isdir(self.folder):
            return []
        out = []
        for p in sorted(Path(self.folder).glob("*.kbh")):
            if NAME.fullmatch(p.stem):
                out.append(CarabinerEntry(carabiner=p.stem, node=self.node, kbh=str(p),
                                          configured=(p.with_suffix(".yaml")).is_file()))
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
        fd, out_path = tempfile.mkstemp(prefix="kbh-", suffix=".out")
        try:
            with os.fdopen(fd, "wb") as out:
                proc = runas.spawn([self.python, kbh, carabiner, *args, "--config", yaml], run_as=self.run_as,
                                   env=env, cwd=self.folder, stdout=out)
                try:
                    rc = proc.wait(timeout=self.timeout_seconds)
                except Exception as e:  # noqa: BLE001 - subprocess.TimeoutExpired, or the session process's own
                    proc.kill()
                    raise TimeoutError(f"kbh {carabiner} {args[0] if args else ''} ran past "
                                       f"{self.timeout_seconds:g}s and was stopped") from e
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
