"""
What a service keeps, and snapshots of it (seren_sinew.stores).

Pinned here:
- every kind is copied the way it needs: a live SQLite database through the
  backup API (WAL included), a chroma directory with its sqlite the same way,
  a directory minus its excluded patterns, a single file
- a snapshot is whole or absent: written as .partial, renamed at the end, and
  a failure leaves nothing behind
- the manifest names every file with its size and sha256, and carries what the
  service adds
- the plain export is written as JSON lines
- retention: the newest N, then one a week
- due() is how a standalone service schedules itself
- one at a time
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

import pytest

from seren_sinew.stores import SnapshotBusy, Store, StoreKeeper


def _db(path: Path, rows: int = 3) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    con.executemany("INSERT INTO t VALUES (?)", [(f"row {i}",) for i in range(rows)])
    con.commit()
    return con


def _keeper(tmp_path: Path, stores, **kw) -> StoreKeeper:
    return StoreKeeper("seren-test", lambda: stores, tmp_path / "backups", **kw)


def test_a_live_sqlite_database_is_copied_whole_wal_and_all(tmp_path):
    con = _db(tmp_path / "data" / "loci.db")
    con.execute("INSERT INTO t VALUES ('written after the last checkpoint')")
    con.commit()                                             # sits in the -wal file; the db is still open
    k = _keeper(tmp_path, [Store("facts", "sqlite", str(tmp_path / "data" / "loci.db"), "the facts")])
    snap = k.snapshot("test")
    copy = Path(snap["path"]) / "raw" / "facts" / "loci.db"
    assert [p.name for p in copy.parent.iterdir()] == ["loci.db"], "one file: no -wal or -shm to go missing"
    check = sqlite3.connect(copy)
    got = [r[0] for r in check.execute("SELECT v FROM t")]
    check.close()
    assert "written after the last checkpoint" in got and len(got) == 4
    con.close()


def test_a_chroma_directory_keeps_its_segments_and_copies_its_sqlite_safely(tmp_path):
    d = tmp_path / "chroma"
    con = _db(d / "chroma.sqlite3")
    (d / "seg-1").mkdir()
    (d / "seg-1" / "data_level0.bin").write_bytes(b"vectors")
    k = _keeper(tmp_path, [Store("memory", "chroma", str(d))])
    snap = Path(k.snapshot()["path"]) / "raw" / "memory"
    assert (snap / "seg-1" / "data_level0.bin").read_bytes() == b"vectors"
    assert sqlite3.connect(snap / "chroma.sqlite3").execute("SELECT count(*) FROM t").fetchone()[0] == 3
    con.close()


def test_a_directory_leaves_out_what_it_was_told_to_and_a_file_is_a_file(tmp_path):
    d = tmp_path / "state"
    (d / "models" / "blobs").mkdir(parents=True)
    (d / "models" / "blobs" / "big.bin").write_bytes(b"x" * 100)
    (d / "voice.json").write_text("{}")
    (d / "ripple.log").write_text("noise")
    f = tmp_path / "state.json"
    f.write_text('{"a": 1}')
    k = _keeper(tmp_path, [Store("state", "dir", str(d), exclude=("models", "*.log")), Store("one", "file", str(f)),
                           Store("cache", "dir", str(d / "models"), "a download", backed_up=False),
                           Store("later", "file", str(tmp_path / "not-yet.json"))])
    snap = k.snapshot()
    root = Path(snap["path"])
    assert sorted(p.relative_to(root / "raw").as_posix() for p in (root / "raw").rglob("*") if p.is_file()) == \
        ["one/state.json", "state/voice.json"]
    man = json.loads((root / "manifest.json").read_text())
    assert man["stores"] == ["state", "one"]
    assert {s["name"]: s["why"] for s in man["skipped"]} == {"cache": "declared, not backed up", "later": "nothing on disk yet"}


def test_the_manifest_names_every_file_and_carries_what_the_service_adds(tmp_path):
    f = tmp_path / "voice.json"
    f.write_text("the card")
    k = _keeper(tmp_path, [Store("voice", "file", str(f))],
                export=lambda: {"notes.jsonl": iter([{"id": 1, "text": "café"}, {"id": 2, "text": "b"}])},
                extra=lambda: {"version": "9.9", "embedder": "all-MiniLM-L6-v2", "counts": {"notes": 2}})
    snap = k.snapshot("before the migration")
    root = Path(snap["path"])
    man = json.loads((root / "manifest.json").read_text())
    assert man["service"] == "seren-test" and man["reason"] == "before the migration" and man["format"] == 1
    assert man["version"] == "9.9" and man["embedder"] == "all-MiniLM-L6-v2" and man["exports"] == {"notes.jsonl": 2}
    by = {x["path"]: x for x in man["files"]}
    assert set(by) == {"raw/voice/voice.json", "export/notes.jsonl"}
    assert by["raw/voice/voice.json"]["sha256"] == hashlib.sha256(b"the card").hexdigest()
    lines = (root / "export" / "notes.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["text"] for x in lines] == ["café", "b"] and "café" in lines[0], "readable, not escaped"
    assert snap["embedder"] == "all-MiniLM-L6-v2" and snap["files"] == 2 and snap["bytes"] == man["bytes"]


def test_a_snapshot_is_whole_or_absent(tmp_path):
    f = tmp_path / "a.json"
    f.write_text("x")

    def boom():
        raise RuntimeError("the export failed half way")
    k = _keeper(tmp_path, [Store("a", "file", str(f))], export=boom)
    with pytest.raises(RuntimeError):
        k.snapshot()
    assert k.list() == [] and not list((tmp_path / "backups").rglob("*.partial"))
    k.export = None
    assert k.snapshot()["id"] and len(k.list()) == 1


def test_retention_keeps_the_newest_then_one_a_week(tmp_path):
    f = tmp_path / "a.json"
    f.write_text("x")
    k = _keeper(tmp_path, [Store("a", "file", str(f))], keep_daily=3, keep_weekly=2)
    base = time.mktime((2026, 10, 30, 3, 0, 0, 0, 0, -1))        # a Friday
    for days_ago in range(30, -1, -1):                           # 31 nightly snapshots, oldest first
        k.snapshot(now=base - days_ago * 86400)
    ids = [s["id"] for s in k.list()]
    assert ids[:3] == ["20261030-030000", "20261029-030000", "20261028-030000"], "the newest three"
    assert len(ids) == 5, f"then one for each of two earlier weeks: {ids}"
    weeks = {time.strftime("%G-%V", time.strptime(i, "%Y%m%d-%H%M%S")) for i in ids[3:]}
    assert len(weeks) == 2


def test_due_is_how_a_standalone_service_schedules_itself(tmp_path):
    f = tmp_path / "a.json"
    f.write_text("x")
    k = _keeper(tmp_path, [Store("a", "file", str(f))])
    assert k.due(24) is True, "never snapshotted"
    k.snapshot(now=1_000_000)
    assert k.due(24, now=1_000_000 + 23 * 3600) is False
    assert k.due(24, now=1_000_000 + 24 * 3600) is True
    assert k.due(0, now=9e9) is False, "0 = this service does not schedule its own"


def test_one_at_a_time_and_describe_says_what_is_kept(tmp_path):
    f = tmp_path / "a.json"
    f.write_text("xyz")
    k = _keeper(tmp_path, [Store("a", "file", str(f), "the one file")])
    k._lock.acquire()
    try:
        with pytest.raises(SnapshotBusy):
            k.snapshot()
    finally:
        k._lock.release()
    k.snapshot()
    d = k.describe()
    assert d["service"] == "seren-test" and d["stores"][0] == {
        "name": "a", "kind": "file", "path": str(f), "what": "the one file", "backed_up": True, "exists": True, "bytes": 3}
    assert d["snapshots"]["count"] == 1 and d["snapshots"]["latest"]["id"]


def test_two_in_the_same_second_are_both_kept(tmp_path):
    f = tmp_path / "a.json"
    f.write_text("x")
    k = _keeper(tmp_path, [Store("a", "file", str(f))])
    a, b = k.snapshot(now=1_000_000), k.snapshot(now=1_000_000)
    assert a["id"] != b["id"] and len(k.list()) == 2


def test_the_three_routes(tmp_path):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    from seren_sinew.stores import add_store_routes
    f = tmp_path / "a.json"
    f.write_text("x")
    k = _keeper(tmp_path, [Store("a", "file", str(f), "the one file")])
    holder = {"k": None}
    app = Starlette()
    add_store_routes(app, lambda: holder["k"])
    with TestClient(app) as c:
        assert c.get("/stores").status_code == 404, "switched off: said, not hidden"
        assert c.post("/stores/snapshot").status_code == 404
        holder["k"] = k
        d = c.get("/stores").json()
        assert d["ok"] and d["stores"][0]["name"] == "a" and d["snapshots"]["count"] == 0
        r = c.post("/stores/snapshot", json={"reason": "before the cut-over"}).json()
        assert r["ok"] and r["snapshot"]["reason"] == "before the cut-over"
        assert c.post("/stores/snapshot").json()["snapshot"]["reason"] == "by hand"
        got = c.get("/stores/snapshots").json()
        assert got["count"] == 2 and got["snapshots"][0]["id"] >= got["snapshots"][1]["id"]
        k._lock.acquire()
        try:
            assert c.post("/stores/snapshot").status_code == 409
        finally:
            k._lock.release()
        for path in ("/stores/restore", "/stores/snapshots/x"):
            assert c.post(path).status_code in (404, 405) and c.delete(path).status_code in (404, 405), "no restore, no delete"


def test_the_loop_snapshots_when_one_is_due_and_survives_a_failure(tmp_path):
    import asyncio
    from seren_sinew.stores import snapshot_loop
    f = tmp_path / "a.json"
    f.write_text("x")
    said = []
    k = _keeper(tmp_path, [Store("a", "file", str(f))], log=said.append)

    async def run(keeper, seconds=0.5):
        task = asyncio.create_task(snapshot_loop(lambda: keeper, 24, check_seconds=0.05, first_after=0.0))
        await asyncio.sleep(seconds)
        task.cancel()
    asyncio.run(run(k))
    assert len(k.list()) == 1, "one was due; the later checks found it fresh"

    def boom():
        raise RuntimeError("disk full")
    bad = _keeper(tmp_path / "b", [Store("a", "file", str(f))], export=boom, log=said.append)
    asyncio.run(run(bad, 0.3))
    assert bad.list() == [] and any("scheduled snapshot failed: RuntimeError: disk full" in m for m in said)


def test_snapshots_kept_inside_the_store_are_not_copied_into_themselves(tmp_path):
    """backup.dir pointed into the store's own folder: each snapshot would
    hold every earlier one, and itself while it was being written."""
    d = tmp_path / "store"
    d.mkdir()
    (d / "voice.json").write_text("the card")
    k = StoreKeeper("seren-test", lambda: [Store("all", "dir", str(d))], d / "backups")
    k.snapshot(now=1_000_000)
    second = Path(k.snapshot(now=1_000_100)["path"])
    files = sorted(p.relative_to(second).as_posix() for p in second.rglob("*") if p.is_file())
    assert files == ["manifest.json", "raw/all/voice.json"]


def test_a_snapshot_travels_as_an_archive_and_is_verified_on_arrival(tmp_path):
    """Lodestar's half: pull the archive, verify every sha256, stash it."""
    import io, tarfile
    from seren_sinew.stores import unpack_snapshot, verify_snapshot
    f = tmp_path / "voice.json"
    f.write_text("the card")
    k = _keeper(tmp_path, [Store("voice", "file", str(f))], export=lambda: {"n.jsonl": [{"a": 1}]})
    sid = k.snapshot("nightly")["id"]
    data = k.archive(sid)
    assert data[:2] == b"\x1f\x8b" and k.archive("nope") is None and k.archive("../x") is None
    got = unpack_snapshot(data, tmp_path / "stash")
    assert got == tmp_path / "stash" / sid and verify_snapshot(got) == []
    assert (got / "raw" / "voice" / "voice.json").read_text() == "the card"
    assert json.loads((got / "manifest.json").read_text())["reason"] == "nightly"
    with pytest.raises(ValueError, match="not zzz"):
        unpack_snapshot(data, tmp_path / "stash2", expect_id="zzz")
    # a damaged copy is seen, and a forged archive is not stashed
    (got / "raw" / "voice" / "voice.json").write_text("tampered")
    assert verify_snapshot(got) == ["changed: raw/voice/voice.json"]
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as out, tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as src:
        for m in src.getmembers():
            fh = src.extractfile(m)
            payload = fh.read() if fh else b""
            if m.name.endswith("voice.json"):
                payload = b"forged"
            m.size = len(payload)
            out.addfile(m, io.BytesIO(payload))
    with pytest.raises(ValueError, match="did not verify"):
        unpack_snapshot(buf.getvalue(), tmp_path / "stash3")
    assert not (tmp_path / "stash3" / sid).exists() and not list((tmp_path / "stash3").glob("*.partial"))


def test_the_archive_route(tmp_path):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    from seren_sinew.stores import add_store_routes, unpack_snapshot
    f = tmp_path / "a.json"
    f.write_text("x")
    k = _keeper(tmp_path, [Store("a", "file", str(f))])
    app = Starlette()
    add_store_routes(app, lambda: k)
    with TestClient(app) as c:
        sid = c.post("/stores/snapshot").json()["snapshot"]["id"]
        r = c.get(f"/stores/snapshots/{sid}/archive")
        assert r.status_code == 200 and r.headers["content-type"] == "application/gzip"
        assert r.headers["x-seren-snapshot"] == sid and r.headers["x-seren-service"] == "seren-test"
        assert unpack_snapshot(r.content, tmp_path / "stash", expect_id=sid).name == sid
        assert c.get("/stores/snapshots/nope/archive").status_code == 404


def test_a_service_can_keep_its_archives_to_itself(tmp_path):
    """Margin's case: snapshots are taken and listed, and not handed over."""
    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    from seren_sinew.stores import add_store_routes
    f = tmp_path / "notes.db"
    f.write_text("a diary")
    k = _keeper(tmp_path, [Store("notes", "file", str(f))])
    allow = {"v": False}
    app = Starlette()
    add_store_routes(app, lambda: k, archive_allowed=lambda: allow["v"], archive_refusal="the diary stays here")
    with TestClient(app) as c:
        sid = c.post("/stores/snapshot").json()["snapshot"]["id"]
        assert c.get("/stores/snapshots").json()["count"] == 1, "listing says nothing of what is inside"
        r = c.get(f"/stores/snapshots/{sid}/archive")
        assert r.status_code == 403 and r.json()["error"] == "the diary stays here"
        allow["v"] = True
        assert c.get(f"/stores/snapshots/{sid}/archive").status_code == 200
