"""
seren_sinew.stores
════════════════════════════════════════════════════════════════════════

What a service keeps on disk, said out loud - and snapshots of it.

WHY (Chad, 2 Oct 2026): every Seren service holds something that would be
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

NOT HERE, ON PURPOSE: restore. Putting a snapshot back erases everything
since and brings back anything purged since, so it is not a route anyone can
call; it will replay the tombstones and be asked for with a reason, like the
other ways of changing what a memory says. Until it is built, a snapshot is
restored by a person, with the service stopped.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import sqlite3
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
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ── what is kept ──────────────────────────────────────────────────────
    @property
    def root(self) -> Path:
        return Path(self.dest) / self.service

    def describe(self) -> dict[str, Any]:
        snaps = self.list()
        return {"service": self.service, "stores": [s.describe() for s in self.stores()],
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


# ── the same three routes on every service ───────────────────────────────────
def add_store_routes(app: Any, get_keeper: Callable[[], Optional[StoreKeeper]]) -> None:
    """GET /stores, POST /stores/snapshot, GET /stores/snapshots on a
    Starlette / FastAPI app. They sit behind whatever auth middleware the
    service already has. get_keeper returns None while the service has no
    keeper (snapshots switched off): the routes then say so, 404.

    There is no restore route, and no route that deletes a snapshot: see the
    module docstring."""
    import asyncio

    from starlette.responses import JSONResponse

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

    app.add_route("/stores", stores, methods=["GET"])
    app.add_route("/stores/snapshots", snapshots, methods=["GET"])
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


__all__ = ["Store", "StoreKeeper", "SnapshotBusy", "copy_store", "KINDS", "add_store_routes", "snapshot_loop"]
