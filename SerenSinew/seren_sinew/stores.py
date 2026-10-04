"""
seren_sinew.stores
════════════════════════════════════════════════════════════════════════

What a service keeps on disk, said out loud - and snapshots of it.

WHY (Design note:): every Seren service holds something that would be
lost with a dead disk or a bad migration - a vector store, a SQLite file, a
state file, a voice card - and none of them could say what, or copy it. This
module is the shared answer:

    a service DECLARES its stores      a list of Store(name, kind, path)
    the declaration is a route         GET  /stores            what is kept
    a snapshot is one call             POST /stores/snapshot   copy it now
    and the snapshots are listed       GET  /stores/snapshots

Each service snapshots ITSELF, because only it knows how its store is copied
safely; the code that does the copying, the manifest and the retention are
here so they are the same everywhere. A service schedules its own snapshots
(`due()`), so a standalone install is covered with no Lodestar; Lodestar can
later ask for one, pull it and stash it on another box through the same
routes. The declaration is also the seam for the system managing itself
later - a voice card, a config - without each service inventing its own list.

WHAT A SNAPSHOT IS
    <dest>/<service>/<YYYYmmdd-HHMMSS>/
        manifest.json     service, version, when, why, every file with its
                          size and sha256, and whatever the service adds
                          (the embedder that built the vectors, row counts)
        raw/<store>/...   a copy of the store as it sits on disk: the fast
                          way back, under the same version and embedder
        export/*.jsonl    the service's own plain export (text + metadata):
                          small, readable, independent of any embedder.
                          VECTORS ARE DERIVED - the text is the memory and
                          any embedder can rebuild an index from it.

    It is written to `<stamp>.partial` and renamed when complete, so a
    snapshot that exists is a whole one.

HOW EACH KIND IS COPIED
    sqlite   SQLite's online backup API: a consistent copy of a database
             that is open and being written to (WAL included).
    chroma   a Chroma persist directory: its chroma.sqlite3 by the backup
             API, the segment files beside it by plain copy. The export is
             the safety net if a segment was mid-write.
    dir      copied as it is, minus `exclude` patterns.
    file     copied as it is.

RETENTION
    The newest `keep_daily` snapshots are kept, then one per ISO week for
    `keep_weekly` weeks before those. Every snapshot is a full one: the
    stores are megabytes, and a chain of incrementals is one more thing to
    restore wrongly.

THE REHEARSAL (Design note: "backups are useless if you can't validate
them")
    POST /stores/snapshots/{id}/rehearse   one of the service's own snapshots
    POST /stores/rehearse                  a snapshot sent as a tar.gz body
                                           (Lodestar, from its stash, through
                                           the Observatory)
    A restore's dry run: the snapshot is verified, laid out in a scratch
    folder as the store would sit, every SQLite file is integrity-checked,
    and the service opens the COPY the way it opens its store, counts it
    against the manifest and replays its tombstones on it. Then the scratch
    is removed. The live store is never written to. See rehearse_restore.

NOT HERE, ON PURPOSE: restore. Putting a snapshot back erases everything
since and brings back anything purged since, so it is not a route anyone can
call; it will replay the tombstones and be asked for with a reason, like the
other ways of changing what a memory says. Until it is built, a snapshot is
restored by a person, with the service stopped.
"""
from __future__ import annotations

import fnmatch
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tarfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

KINDS = ("sqlite", "chroma", "dir", "file")
STAMP = "%Y%m%d-%H%M%S"


@dataclass
class Store:
    """One thing a service keeps on disk."""
    name: str
    kind: str                              # sqlite | chroma | dir | file
    path: str
    what: str = ""                         # one line for a person: what is in it
    exclude: tuple[str, ...] = ()          # dir / chroma: glob patterns left out (caches, logs)
    backed_up: bool = True                 # False = declared, not copied (a download, a log)

    def describe(self) -> dict[str, Any]:
        p = Path(self.path)
        exists = p.exists()
        return {"name": self.name, "kind": self.kind, "path": str(p), "what": self.what,
                "backed_up": self.backed_up, "exists": exists,
                "bytes": _size(p, self.exclude) if exists else 0}


class SnapshotBusy(RuntimeError):
    """A snapshot of this service is already being taken."""


def _size(p: Path, exclude: tuple[str, ...] = ()) -> int:
    if p.is_file():
        return p.stat().st_size
    total = 0
    for f in p.rglob("*"):
        if f.is_file() and not _excluded(f.relative_to(p), exclude):
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total


def _excluded(rel: Path, patterns: tuple[str, ...]) -> bool:
    s = rel.as_posix()
    return any(fnmatch.fnmatch(s, pat) or fnmatch.fnmatch(rel.name, pat)
               or any(fnmatch.fnmatch(part, pat) for part in rel.parts) for pat in patterns)


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy_sqlite(src: Path, dst: Path) -> None:
    """A consistent copy of a live database (SQLite's online backup)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        out = sqlite3.connect(str(dst))
        try:
            con.backup(out)
        finally:
            out.close()
    finally:
        con.close()


def _copy_tree(src: Path, dst: Path, exclude: tuple[str, ...], sqlite_names: tuple[str, ...] = (),
               skip: Optional[Path] = None) -> None:
    # skip: the snapshots folder itself, when it sits inside the store it is
    # copying (backup.dir pointed into the store, or a store that is a whole
    # directory): without this a snapshot copies every earlier snapshot, and
    # itself while it is being written.
    skip = skip.resolve() if skip is not None else None
    for f in src.rglob("*"):
        if not f.is_file():
            continue
        if skip is not None and skip in f.resolve().parents:
            continue
        rel = f.relative_to(src)
        if _excluded(rel, exclude) or f.name.endswith(("-wal", "-shm", "-journal")):
            continue
        out = dst / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        if f.name in sqlite_names:
            _copy_sqlite(f, out)
        else:
            shutil.copy2(f, out)


def copy_store(store: Store, dest: Path, skip: Optional[Path] = None) -> None:
    """Copy one store into dest (a directory), the way its kind needs."""
    src = Path(store.path)
    if store.kind == "sqlite":
        _copy_sqlite(src, dest / src.name)
    elif store.kind == "chroma":
        _copy_tree(src, dest, store.exclude, sqlite_names=("chroma.sqlite3",), skip=skip)
    elif store.kind == "dir":
        _copy_tree(src, dest, store.exclude, skip=skip)
    elif store.kind == "file":
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest / src.name)
    else:
        raise ValueError(f"store '{store.name}': unknown kind '{store.kind}' (one of {', '.join(KINDS)})")


@dataclass
class StoreKeeper:
    """A service's stores and its snapshots of them.

    service      the service's name ("seren-memory")
    stores       a callable returning the Store list (paths can depend on config)
    dest         where snapshots go: <dest>/<service>/<stamp>/
    export       optional: () -> {"<name>.jsonl": iterable of dict rows} - the
                 plain export, written beside the raw copy
    extra        optional: () -> dict merged into the manifest (version,
                 embedder, counts)
    """
    service: str
    stores: Callable[[], list[Store]]
    dest: Path
    export: Optional[Callable[[], dict[str, Iterable[dict[str, Any]]]]] = None
    extra: Optional[Callable[[], dict[str, Any]]] = None
    keep_daily: int = 14
    keep_weekly: int = 8
    log: Callable[[str], None] = lambda m: None
    # optional: (restored {store name: folder}, manifest, snapshot folder) ->
    # dict - the service's own look at a restored COPY during a rehearsal
    # (rehearse_restore, step 4)
    check: Optional[Callable[[dict[str, Path], dict[str, Any], Path], dict[str, Any]]] = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _rehearsing: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ── what is kept ──────────────────────────────────────────────────────
    @property
    def root(self) -> Path:
        return Path(self.dest) / self.service

    def describe(self) -> dict[str, Any]:
        snaps = self.list()
        stores = []
        dest = Path(self.dest).resolve()
        for s in self.stores():
            d = s.describe()
            # snapshots kept inside the store they copy are not part of it
            if d["exists"] and Path(s.path).is_dir() and Path(s.path).resolve() in dest.parents and dest.is_dir():
                d["bytes"] = max(0, d["bytes"] - _size(dest))
            stores.append(d)
        return {"service": self.service, "stores": stores,
                "snapshots": {"dir": str(self.root), "count": len(snaps),
                              "latest": snaps[0] if snaps else None,
                              "keep_daily": self.keep_daily, "keep_weekly": self.keep_weekly}}

    # ── snapshots ─────────────────────────────────────────────────────────
    def list(self) -> list[dict[str, Any]]:
        """Whole snapshots, newest first."""
        out = []
        if not self.root.is_dir():
            return out
        for d in sorted(self.root.iterdir(), reverse=True):
            m = d / "manifest.json"
            if not d.is_dir() or d.name.endswith(".partial") or not m.is_file():
                continue
            try:
                man = json.loads(m.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out.append({"id": d.name, "created_at": man.get("created_at"), "reason": man.get("reason"),
                        "bytes": man.get("bytes"), "files": len(man.get("files") or []),
                        "path": str(d), **{k: man[k] for k in ("version", "embedder", "counts") if k in man}})
        return out

    def due(self, every_hours: float, now: Optional[float] = None) -> bool:
        """Has it been every_hours since the last whole snapshot?"""
        if every_hours <= 0:
            return False
        snaps = self.list()
        if not snaps:
            return True
        now = time.time() if now is None else now
        return now - float(snaps[0].get("created_at") or 0) >= every_hours * 3600

    def snapshot(self, reason: str = "scheduled", now: Optional[float] = None) -> dict[str, Any]:
        """Take one now. Returns its listing row. SnapshotBusy if one is
        already running; any other failure leaves no half snapshot behind."""
        if not self._lock.acquire(blocking=False):
            raise SnapshotBusy(f"a snapshot of {self.service} is already being taken")
        now = time.time() if now is None else now
        stamp = datetime.fromtimestamp(now).strftime(STAMP)
        final = self.root / stamp
        n = 1
        while final.exists():                              # two in one second: keep both
            n += 1
            final = self.root / f"{stamp}-{n}"
        work = final.with_name(final.name + ".partial")
        try:
            if work.exists():
                shutil.rmtree(work)
            work.mkdir(parents=True)
            copied, skipped = [], []
            for s in self.stores():
                if not s.backed_up:
                    skipped.append({"name": s.name, "why": "declared, not backed up"})
                    continue
                if not Path(s.path).exists():
                    skipped.append({"name": s.name, "why": "nothing on disk yet"})
                    continue
                copy_store(s, work / "raw" / s.name, skip=Path(self.dest))
                copied.append(s.name)
            exports: dict[str, int] = {}
            if self.export is not None:
                for fname, rows in (self.export() or {}).items():
                    out = work / "export" / fname
                    out.parent.mkdir(parents=True, exist_ok=True)
                    count = 0
                    with open(out, "w", encoding="utf-8", newline="\n") as f:
                        for row in rows:
                            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                            count += 1
                    exports[fname] = count
            files = [{"path": f.relative_to(work).as_posix(), "bytes": f.stat().st_size, "sha256": _sha256(f)}
                     for f in sorted(work.rglob("*")) if f.is_file()]
            manifest: dict[str, Any] = {
                "service": self.service, "created_at": now,
                "created": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                "reason": reason, "stores": copied, "skipped": skipped, "exports": exports,
                "bytes": sum(f["bytes"] for f in files), "files": files, "format": 1}
            if self.extra is not None:
                manifest.update(self.extra() or {})
            (work / "manifest.json").write_text(json.dumps(manifest, indent=1, default=str), encoding="utf-8")
            os.replace(work, final)
            self.log(f"snapshot {final.name}: {len(files)} file(s), {manifest['bytes']} bytes ({reason})")
        except Exception:
            shutil.rmtree(work, ignore_errors=True)
            raise
        finally:
            self._lock.release()
        removed = self.prune(now)
        row = next((s for s in self.list() if s["id"] == final.name), {"id": final.name})
        return {**row, "pruned": removed}

    def snapshot_dir(self, snapshot_id: str) -> Optional[Path]:
        """The folder of one whole snapshot, by id, or None. The id is a stamp
        this keeper made (no path parts, no .partial)."""
        if not snapshot_id or "/" in snapshot_id or "\\" in snapshot_id or snapshot_id.endswith(".partial"):
            return None
        d = self.root / snapshot_id
        return d if d.is_dir() and (d / "manifest.json").is_file() else None

    def archive(self, snapshot_id: str) -> Optional[bytes]:
        """One snapshot as a tar.gz, for a puller (Lodestar) to stash
        elsewhere: the files exactly as they sit, manifest first, with the
        snapshot's id as the top folder. None when there is no such snapshot."""
        d = self.snapshot_dir(snapshot_id)
        if d is None:
            return None
        return pack_snapshot(d)

    # ── the rehearsal ─────────────────────────────────────────────────────
    @property
    def _scratch_root(self) -> Path:
        return self.root / ".rehearsal"                    # no manifest in it: list() and prune() pass it by

    def rehearse(self, snapshot_id: Optional[str] = None, keep: bool = False) -> Optional[dict[str, Any]]:
        """Rehearse restoring one of this keeper's snapshots (the newest when
        no id is given). None when there is no such snapshot."""
        if not snapshot_id:
            rows = self.list()
            snapshot_id = rows[0]["id"] if rows else ""
        d = self.snapshot_dir(snapshot_id or "")
        if d is None:
            return None
        return self._rehearse(d, "own", keep)

    def rehearse_archive(self, data: bytes, keep: bool = False) -> dict[str, Any]:
        """Rehearse restoring a snapshot that arrived as a tar.gz (from a
        stash on another box). It is unpacked into scratch, never into the
        snapshots folder, and removed afterwards."""
        incoming = self._scratch_root / f"incoming-{os.getpid()}-{time.time_ns()}"
        try:
            try:
                d = unpack_snapshot(data, incoming)
            except (ValueError, tarfile.TarError, OSError, EOFError) as e:
                return {"ok": False, "dry_run": True, "snapshot": None, "service": self.service, "verified": False,
                        "source": "sent", "problems": [f"the archive could not be unpacked: {e}"],
                        "live_store_touched": False}
            man_service = None
            try:
                man_service = json.loads((d / "manifest.json").read_text(encoding="utf-8")).get("service")
            except (OSError, ValueError):
                pass
            if man_service != self.service:
                return {"ok": False, "dry_run": True, "snapshot": d.name, "service": man_service, "verified": True,
                        "source": "sent", "live_store_touched": False,
                        "problems": [f"this is a snapshot of {man_service}, and this service is {self.service}"]}
            return self._rehearse(d, "sent", keep)
        finally:
            _remove_tree(incoming)

    def _rehearse(self, d: Path, source: str, keep: bool) -> dict[str, Any]:
        if not self._rehearsing.acquire(blocking=False):
            raise RehearsalBusy(f"a rehearsal of {self.service} is already running")
        try:
            root = self._scratch_root
            if root.is_dir():                              # what an earlier one could not remove
                for old in root.iterdir():
                    if old.is_dir() and old.name.startswith("run-"):
                        _remove_tree(old)
            scratch = root / f"run-{d.name}-{time.time_ns()}"
            rep = rehearse_restore(d, scratch, self.check, keep)
            rep["source"] = source
            self.log(f"rehearsal of {d.name} ({source}): " + ("ok" if rep["ok"] else "FAILED: " + "; ".join(rep["problems"][:3])))
            return rep
        finally:
            self._rehearsing.release()

    def prune(self, now: Optional[float] = None) -> list[str]:
        """Keep the newest keep_daily, then one per ISO week for keep_weekly
        weeks before those. Leftover .partial folders from a crash go too."""
        removed: list[str] = []
        if not self.root.is_dir():
            return removed
        for d in self.root.iterdir():
            if d.is_dir() and d.name.endswith(".partial") and not self._lock.locked():
                shutil.rmtree(d, ignore_errors=True)
        snaps = self.list()
        keep = {s["id"] for s in snaps[: max(1, self.keep_daily)]}
        weeks: list[tuple[int, int]] = []
        for s in snaps[max(1, self.keep_daily):]:
            wk = tuple(datetime.fromtimestamp(float(s.get("created_at") or 0)).isocalendar()[:2])
            if wk not in weeks and len(weeks) < self.keep_weekly:
                weeks.append(wk)                           # newest first: the newest of that week is kept
                keep.add(s["id"])
        for s in snaps:
            if s["id"] not in keep:
                shutil.rmtree(s["path"], ignore_errors=True)
                removed.append(s["id"])
        if removed:
            self.log(f"pruned {len(removed)} old snapshot(s)")
        return removed


# ── carrying a snapshot to another box ────────────────────────────────────────
def pack_snapshot(d: Path) -> bytes:
    """A snapshot folder as a tar.gz with the folder's name on top."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(d / "manifest.json", arcname=f"{d.name}/manifest.json")
        for f in sorted(d.rglob("*")):
            if f.is_file() and f.name != "manifest.json":
                tar.add(f, arcname=f"{d.name}/{f.relative_to(d).as_posix()}")
    return buf.getvalue()


def unpack_snapshot(data: bytes, dest_root: Path, expect_id: Optional[str] = None) -> Path:
    """Put an archived snapshot under dest_root/<id>/, verified against its
    manifest (every file present, every sha256 right) before it is kept: a
    snapshot that does not verify is not stashed. Written to <id>.partial and
    renamed, like a snapshot being taken. Returns the folder."""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        names = tar.getnames()
        tops = {n.split("/", 1)[0] for n in names}
        if len(tops) != 1:
            raise ValueError(f"an archive holds one snapshot, this one holds {sorted(tops)}")
        sid = tops.pop()
        if expect_id and sid != expect_id:
            raise ValueError(f"the archive is snapshot {sid}, not {expect_id}")
        if sid.endswith(".partial") or "/" in sid or ".." in sid:
            raise ValueError(f"bad snapshot id in archive: {sid!r}")
        for m in tar.getmembers():
            if not m.isfile() or m.name.startswith(("/", "..")) or "/../" in m.name:
                raise ValueError(f"bad member in archive: {m.name!r}")
        work = dest_root / f"{sid}.partial"
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        for m in tar.getmembers():
            rel = m.name.split("/", 1)[1] if "/" in m.name else ""
            if not rel:
                continue
            out = work / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(m)
            with open(out, "wb") as f:
                shutil.copyfileobj(src, f)
    problems = verify_snapshot(work)
    if problems:
        shutil.rmtree(work, ignore_errors=True)
        raise ValueError("the snapshot did not verify: " + "; ".join(problems[:5]))
    final = dest_root / sid
    if final.exists():
        shutil.rmtree(final)
    os.replace(work, final)
    return final


def verify_snapshot(d: Path) -> list[str]:
    """Every file the manifest names, present, with the right sha256; and
    nothing the manifest does not name. Empty list = whole."""
    try:
        man = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return [f"manifest.json: {e}"]
    problems = []
    named = set()
    for f in man.get("files") or []:
        rel = f.get("path") or ""
        named.add(rel)
        p = d / rel
        if not p.is_file():
            problems.append(f"missing: {rel}")
        elif _sha256(p) != f.get("sha256"):
            problems.append(f"changed: {rel}")
    for p in d.rglob("*"):
        if p.is_file() and p.name != "manifest.json" and p.relative_to(d).as_posix() not in named:
            problems.append(f"not in the manifest: {p.relative_to(d).as_posix()}")
    return problems


# ── the rehearsal: a restore's dry run ────────────────────────────────────────
class RehearsalBusy(RuntimeError):
    """One rehearsal at a time per service."""


def _is_sqlite(p: Path) -> bool:
    try:
        with open(p, "rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _remove_tree(d: Path) -> bool:
    """Remove a scratch folder; True when it is gone. A handle a library has
    not let go of yet (Windows) gets a few tries."""
    import gc
    for _ in range(4):
        shutil.rmtree(d, ignore_errors=True)
        if not d.exists():
            return True
        gc.collect()
        time.sleep(0.4)
    return not d.exists()


def rehearse_restore(snapshot: Path, scratch: Path,
                     check: Optional[Callable[[dict[str, Path], dict[str, Any], Path], dict[str, Any]]] = None,
                     keep: bool = False) -> dict[str, Any]:
    """Prove a snapshot can be put back, without putting it back.

        1. verify      every file the manifest names, present, sha256 right
        2. lay out     raw/<store>/ copied to <scratch>/<store>/ - the store
                       as it would sit after a restore
        3. open        every SQLite file in the copy: integrity_check
        4. check       the service's own check(restored, manifest, snapshot):
                       open the copy the way the service opens its store,
                       count what is in it, replay its tombstones ON THE
                       COPY. Returns a dict; "counts" in it is compared with
                       the counts the manifest recorded, "problems" (a list)
                       fails the rehearsal.
        5. clean up    the scratch copy is removed (keep=True leaves it)

    `scratch` must not exist or must be empty: a rehearsal writes nowhere
    else, and the live store is never opened for writing by anything here.
    Never raises for a bad snapshot: the report says what failed."""
    t0 = time.time()
    snapshot, scratch = Path(snapshot), Path(scratch)
    rep: dict[str, Any] = {"ok": False, "dry_run": True, "snapshot": snapshot.name, "service": None,
                           "created_at": None, "verified": False, "stores": [], "sqlite": [], "check": None,
                           "problems": [], "live_store_touched": False}
    problems: list[str] = rep["problems"]
    if scratch.exists() and any(scratch.iterdir()):
        problems.append(f"the scratch folder is not empty: {scratch}")
        rep["seconds"] = round(time.time() - t0, 2)
        return rep
    bad = verify_snapshot(snapshot)
    if bad:
        problems.extend(bad[:20])
        rep["seconds"] = round(time.time() - t0, 2)
        return rep
    rep["verified"] = True
    man = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    rep["service"], rep["created_at"] = man.get("service"), man.get("created_at")
    rep["version"], rep["embedder"] = man.get("version"), man.get("embedder")
    try:
        restored: dict[str, Path] = {}
        for name in man.get("stores") or []:
            src = snapshot / "raw" / str(name)
            if not src.is_dir():
                problems.append(f"the manifest names store '{name}' and raw/{name} is not in the snapshot")
                continue
            dst = scratch / str(name)
            shutil.copytree(src, dst)
            files = [f for f in dst.rglob("*") if f.is_file()]
            restored[str(name)] = dst
            rep["stores"].append({"name": name, "files": len(files), "bytes": sum(f.stat().st_size for f in files)})
            for f in files:
                if not _is_sqlite(f):
                    continue
                try:
                    con = sqlite3.connect(f"file:{f.as_posix()}?mode=ro", uri=True)
                    try:
                        verdict = str(con.execute("PRAGMA integrity_check").fetchone()[0])
                    finally:
                        con.close()
                except sqlite3.Error as e:
                    verdict = f"{type(e).__name__}: {e}"
                rep["sqlite"].append({"file": f.relative_to(scratch).as_posix(), "integrity": verdict})
                if verdict != "ok":
                    problems.append(f"{f.relative_to(scratch).as_posix()}: {verdict}")
        if not restored and not problems:
            problems.append("the snapshot holds no stores")
        if check is not None and restored and not problems:
            try:
                got = check(restored, man, snapshot) or {}
            except Exception as e:                          # noqa: BLE001 - a copy that will not open is the finding
                got = {"problems": [f"the service could not open the restored copy: {type(e).__name__}: {e}"]}
            problems.extend(str(p) for p in got.pop("problems", None) or [])
            want, have = man.get("counts"), got.get("counts")
            if isinstance(want, dict) and isinstance(have, dict):
                for k in sorted(set(want) & set(have)):
                    if want[k] != have[k]:
                        problems.append(f"count '{k}': the manifest says {want[k]}, the restored copy holds {have[k]}")
            rep["check"] = got
    except Exception as e:                                  # noqa: BLE001
        problems.append(f"{type(e).__name__}: {e}")
    finally:
        if keep:
            rep["scratch"] = str(scratch)
        elif not _remove_tree(scratch):
            rep["scratch_left_behind"] = str(scratch)        # swept at the next rehearsal
    rep["ok"] = not problems
    rep["seconds"] = round(time.time() - t0, 2)
    return rep


# ── the same routes on every service ─────────────────────────────────────────
def add_store_routes(app: Any, get_keeper: Callable[[], Optional[StoreKeeper]],
                     archive_allowed: Optional[Callable[[], bool]] = None,
                     archive_refusal: str = "this service does not hand its snapshots over HTTP") -> None:
    """GET /stores, POST /stores/snapshot, GET /stores/snapshots on a
    Starlette / FastAPI app. They sit behind whatever auth middleware the
    service already has. get_keeper returns None while the service has no
    keeper (snapshots switched off): the routes then say so, 404.

    GET /stores/snapshots/{id}/archive hands one snapshot over as a tar.gz,
    for Lodestar to stash on another box.

    archive_allowed, when given, is asked on every archive request; False is
    a 403 carrying archive_refusal.

    POST /stores/snapshots/{id}/rehearse and POST /stores/rehearse (a tar.gz
    body) run a restore's dry run and answer with its report: 200 when the
    rehearsal ran, with "ok" saying whether the snapshot passed.

    There is no restore route, and no route that deletes a snapshot: see the
    module docstring."""
    import asyncio

    from starlette.responses import JSONResponse, Response

    def _off() -> JSONResponse:
        return JSONResponse({"ok": False, "error": "this service keeps no snapshots (backup.enabled is off)"},
                            status_code=404)

    async def stores(request):                              # noqa: ANN001
        k = get_keeper()
        return JSONResponse({"ok": True, **k.describe()}) if k else _off()

    async def snapshots(request):                           # noqa: ANN001
        k = get_keeper()
        if not k:
            return _off()
        rows = k.list()
        return JSONResponse({"ok": True, "count": len(rows), "dir": str(k.root), "snapshots": rows})

    async def snapshot(request):                            # noqa: ANN001
        k = get_keeper()
        if not k:
            return _off()
        reason = "by hand"
        try:
            body = await request.json()
            reason = str((body or {}).get("reason") or reason)[:200]
        except Exception:                                   # noqa: BLE001 - no body is fine
            pass
        try:
            row = await asyncio.to_thread(k.snapshot, reason)
        except SnapshotBusy as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=409)
        except Exception as e:                              # noqa: BLE001 - say why, do not 500 blind
            return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)
        return JSONResponse({"ok": True, "snapshot": row})

    async def archive(request):                             # noqa: ANN001
        k = get_keeper()
        if not k:
            return _off()
        # A service may keep its snapshots to itself: listing them and taking
        # one say nothing of what is inside, the archive IS what is inside.
        # (Margin: a diary's HTTP reads are off by default, and a backup pull
        # is a read of the whole diary.)
        if archive_allowed is not None and not archive_allowed():
            return JSONResponse({"ok": False, "error": archive_refusal}, status_code=403)
        sid = request.path_params.get("snapshot_id", "")
        data = await asyncio.to_thread(k.archive, sid)
        if data is None:
            return JSONResponse({"ok": False, "error": f"no snapshot '{sid}'"}, status_code=404)
        return Response(content=data, media_type="application/gzip",
                        headers={"Content-Disposition": f'attachment; filename="{k.service}-{sid}.tar.gz"',
                                 "X-Seren-Snapshot": sid, "X-Seren-Service": k.service})

    async def rehearse(request):                            # noqa: ANN001
        k = get_keeper()
        if not k:
            return _off()
        sid = request.path_params.get("snapshot_id", "")
        try:
            rep = await asyncio.to_thread(k.rehearse, sid)
        except RehearsalBusy as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=409)
        if rep is None:
            return JSONResponse({"ok": False, "error": f"no snapshot '{sid}'"}, status_code=404)
        return JSONResponse(rep)

    async def rehearse_sent(request):                       # noqa: ANN001
        k = get_keeper()
        if not k:
            return _off()
        data = await request.body()
        if not data:
            return JSONResponse({"ok": False, "error": "send the snapshot's tar.gz as the body"}, status_code=400)
        try:
            rep = await asyncio.to_thread(k.rehearse_archive, data)
        except RehearsalBusy as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=409)
        return JSONResponse(rep)

    app.add_route("/stores/snapshots/{snapshot_id}/rehearse", rehearse, methods=["POST"])
    app.add_route("/stores/rehearse", rehearse_sent, methods=["POST"])
    app.add_route("/stores", stores, methods=["GET"])
    app.add_route("/stores/snapshots", snapshots, methods=["GET"])
    app.add_route("/stores/snapshots/{snapshot_id}/archive", archive, methods=["GET"])
    app.add_route("/stores/snapshot", snapshot, methods=["POST"])


async def snapshot_loop(get_keeper: Callable[[], Optional[StoreKeeper]], every_hours: float,
                        check_seconds: float = 900.0, first_after: float = 60.0) -> None:
    """A service's own schedule, so a standalone install is covered with
    nothing else running: a snapshot whenever the newest one is older than
    every_hours. Checked a minute after start and then every check_seconds;
    a failure is logged by the keeper and tried again at the next check.
    Run it as a task from the service's lifespan and cancel it at shutdown."""
    import asyncio

    await asyncio.sleep(first_after)
    while True:
        k = get_keeper()
        if k is not None:
            try:
                if await asyncio.to_thread(k.due, every_hours):
                    await asyncio.to_thread(k.snapshot, "scheduled")
            except SnapshotBusy:
                pass
            except Exception as e:                          # noqa: BLE001 - a backup never takes the service down
                k.log(f"scheduled snapshot failed: {type(e).__name__}: {e}")
        await asyncio.sleep(check_seconds)


__all__ = ["Store", "StoreKeeper", "SnapshotBusy", "copy_store", "KINDS", "add_store_routes", "snapshot_loop",
           "pack_snapshot", "unpack_snapshot", "verify_snapshot", "rehearse_restore", "RehearsalBusy"]
