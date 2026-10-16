"""
Files kept by what they are (seren_sinew.blobs).

Pinned here:
- a blob is named by its sha256, under a two-character shard; the same bytes
  twice is one file; nothing that is not a digest ever becomes a path
- put() streams: bytes, a file-like, a path all land the same; one over
  max_bytes or past max_total is refused before anything is kept, and the
  message says the sizes
- what is written is whole or absent (.incoming, then a rename)
- delete() takes one; sweep() takes what no row names, after a grace period
- sniff() tells pictures, video, audio and PDF from their first bytes, and
  says nothing about text
- store() declares the folder as a `blobs` Store with .incoming left out
"""
from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path

import pytest

from seren_sinew.blobs import INCOMING, BlobStore, BlobStoreFull, BlobTooLarge, is_digest, sniff

PNG = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 40


def test_a_blob_is_named_by_its_bytes_and_kept_once(tmp_path):
    bs = BlobStore(tmp_path / "keep")
    a = bs.put(PNG)
    assert a.digest == hashlib.sha256(PNG).hexdigest() and a.bytes == len(PNG) and not a.existed
    assert a.mime == "image/png" and a.ext == ".png"
    assert bs.path(a.digest) == tmp_path / "keep" / a.digest[:2] / a.digest
    assert bs.read(a.digest) == PNG and bs.has(a.digest) and bs.size(a.digest) == len(PNG)
    again = bs.put(io.BytesIO(PNG))
    assert again.digest == a.digest and again.existed
    f = tmp_path / "from-disk.png"
    f.write_bytes(PNG)
    assert bs.put(f).digest == a.digest
    assert bs.count() == 1 and bs.total_bytes() == len(PNG) and list(bs.digests()) == [a.digest]
    assert not list((tmp_path / "keep" / INCOMING).iterdir()), "nothing half-written left behind"


def test_only_a_digest_becomes_a_path(tmp_path):
    bs = BlobStore(tmp_path)
    for bad in ("", "../x", "ABCDEF" + "0" * 58, "0" * 63, "0" * 65, "lantern.png", "0" * 32 + "/" + "0" * 31):
        assert not is_digest(bad)
        with pytest.raises(ValueError, match="not a blob digest"):
            bs.path(bad)
        assert not bs.has(bad)
    assert is_digest("a" * 64)


def test_limits_refuse_before_anything_is_kept(tmp_path):
    bs = BlobStore(tmp_path, max_bytes=100, max_total=150)
    with pytest.raises(BlobTooLarge, match="over this store's limit of one file at 100 B"):
        bs.put(b"x" * 101)
    assert bs.count() == 0 and not list((tmp_path / INCOMING).iterdir())
    bs.put(b"a" * 90)
    with pytest.raises(BlobStoreFull, match=r"would put the store at 180 B, over its limit of 150 B \(it holds 90 B\)"):
        bs.put(b"b" * 90)
    assert bs.count() == 1
    assert bs.put(b"a" * 90).existed, "what is already here costs nothing more"


def test_delete_one_and_sweep_the_unreferenced_after_a_grace(tmp_path):
    bs = BlobStore(tmp_path)
    a, b, c = (bs.put(bytes([i]) * 10).digest for i in range(3))
    assert bs.delete(a) and not bs.delete(a) and not bs.has(a)
    assert not (tmp_path / a[:2]).exists(), "an empty shard folder goes with its last blob"
    import os, time
    old = time.time() - 7200
    os.utime(bs.path(b), (old, old))
    stray = tmp_path / INCOMING / "crashed.partial"
    stray.write_bytes(b"half")
    os.utime(stray, (old, old))
    assert bs.sweep(referenced=[c]) == [b]                    # b: unreferenced and old
    assert bs.has(c) and not stray.exists()
    d = bs.put(b"fresh" * 3).digest
    assert bs.sweep(referenced=[]) == [], "a fresh blob (its row may still be on its way) is kept"
    assert bs.sweep(referenced=[], grace=0) == sorted([c, d])


def test_copy_out_shows_a_blob_under_a_name_and_keeps_its_own(tmp_path):
    bs = BlobStore(tmp_path / "keep")
    d = bs.put(PNG).digest
    out = bs.copy_out(d, tmp_path / "chat" / "files" / "a-bird.png")
    assert out.read_bytes() == PNG and bs.has(d)


def test_sniff_tells_what_the_bytes_are():
    assert sniff(PNG) == ("image/png", ".png")
    assert sniff(b"\xff\xd8\xff\xe0" + b"\0" * 8) == ("image/jpeg", ".jpg")
    assert sniff(b"GIF89a" + b"\0" * 8) == ("image/gif", ".gif")
    assert sniff(b"RIFF\0\0\0\0WEBPVP8 ") == ("image/webp", ".webp")
    assert sniff(b"RIFF\0\0\0\0WAVEfmt ") == ("audio/wav", ".wav")
    assert sniff(b"\0\0\0\x18ftypisom\0\0\x02\0") == ("video/mp4", ".mp4")
    assert sniff(b"\0\0\0\x18ftypmp42\0\0\x02\0") == ("video/mp4", ".mp4")
    assert sniff(b"\0\0\0\x14ftypqt  \0\0\0\0") == ("video/quicktime", ".mov")
    assert sniff(b"\0\0\0\x1cftypavif\0\0\0\0") == ("image/avif", ".avif")
    assert sniff(b"\0\0\0\x1cftypM4A \0\0\0\0") == ("audio/mp4", ".m4a")
    assert sniff(b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\xf7\x81\x01\x42\x82\x84webm") == ("video/webm", ".webm")
    assert sniff(b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\xf7\x81\x01\x42\x82\x88matroska") == ("video/x-matroska", ".mkv")
    assert sniff(b"OggS\0\x02") == ("audio/ogg", ".ogg")
    assert sniff(b"fLaC\0\0\0\x22") == ("audio/flac", ".flac")
    assert sniff(b"ID3\x04\0") == ("audio/mpeg", ".mp3") and sniff(b"\xff\xfb\x90\x00") == ("audio/mpeg", ".mp3")
    assert sniff(b"%PDF-1.7\n") == ("application/pdf", ".pdf")
    assert sniff(b"hello, a note") is None and sniff(b"") is None
    assert sniff(b"<svg xmlns=") is None, "text that can carry script is not a picture here"


# ── where a blob may be taken from ──────────────────────────────────────────

def test_only_the_shelves_the_sources_name(tmp_path):
    from seren_sinew.blobs import SourceRefused, kind_kept, resolve_source
    gallery = tmp_path / "gallery"
    gallery.mkdir()
    (gallery / "a.png").write_bytes(PNG)
    (gallery / "notes.txt").write_text("a text is a note")
    outside = tmp_path / "elsewhere.png"
    outside.write_bytes(PNG)
    sources = [str(gallery), "https://pictures.test/"]
    assert resolve_source(str(gallery / "a.png"), sources) == (gallery / "a.png").resolve()
    assert resolve_source("https://pictures.test/x/y.png", sources) == "https://pictures.test/x/y.png"
    for ref, says in ((str(outside), "not under a folder"), ("https://other.test/y.png", "not under a prefix"),
                      ("http://pictures.test/y.png", "not under a prefix"), (str(gallery / "nope.png"), "no file at"),
                      (str(gallery), "is not a file"), ("  ", "say where")):
        with pytest.raises(SourceRefused, match=says):
            resolve_source(ref, sources)
    with pytest.raises(SourceRefused, match=r"no folders are configured"):
        resolve_source(str(outside), [])
    with pytest.raises(SourceRefused, match=r"no URL prefixes are configured"):
        resolve_source("https://pictures.test/a.png", [str(gallery)])
    try:
        os.symlink(outside, gallery / "sneaky.png")
    except (OSError, NotImplementedError):
        pass
    else:
        with pytest.raises(SourceRefused, match="not under a folder"):
            resolve_source(str(gallery / "sneaky.png"), sources), "a symlink resolves to where it points"
    assert kind_kept(PNG) == "image/png" and kind_kept(b"\0\0\0\x18ftypisom\0\0") == "video/mp4"
    assert kind_kept(b"a text is a note") is None and kind_kept(b"<svg xmlns=") is None


def test_a_url_is_fetched_no_further_than_the_limit(tmp_path):
    import httpx
    from seren_sinew.blobs import SourceRefused, fetch
    sources = ["https://pictures.test/"]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/big.png":
            return httpx.Response(200, content=PNG * 3)
        if req.url.path == "/gone.png":
            return httpx.Response(404)
        return httpx.Response(200, content=PNG)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert fetch("https://pictures.test/a.png", sources, 100, client=client) == PNG
    with pytest.raises(BlobTooLarge, match="over the limit"):
        fetch("https://pictures.test/big.png", sources, 100, client=client)
    with pytest.raises(SourceRefused, match="answered 404"):
        fetch("https://pictures.test/gone.png", sources, 100, client=client)
    f = tmp_path / "a.png"
    f.write_bytes(PNG)
    assert fetch(str(f), [str(tmp_path)]) == f.resolve(), "a path is handed back as the path: the store streams it"


def test_a_url_source_may_carry_a_bearer(monkeypatch):
    """A store on another box reaches the head's gallery with the head's
    token: the source names the prefix and where the token is (the config
    holds a pointer, or the secret inline), and the fetch presents it."""
    import httpx
    from seren_sinew.blobs import SourceRefused, bearer_for, fetch, resolve_source
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers.get("authorization"))
        return httpx.Response(200, content=PNG) if req.headers.get("authorization") == "Bearer head-secret" \
            else httpx.Response(401, json={"detail": "no"})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setenv("HEAD_TOKEN", "head-secret")
    sources = ["/a/folder", {"url": "http://head:6361/api/v1/gallery/", "bearer_token_env": "HEAD_TOKEN"},
               {"url": "https://inline.test/", "bearer_token": "x"}, "https://open.test/"]
    assert resolve_source("http://head:6361/api/v1/gallery/nuc/2026-10-15/a.png", sources).startswith("http://head")
    assert bearer_for("http://head:6361/api/v1/gallery/x.png", sources) == "head-secret"
    assert bearer_for("https://inline.test/x.png", sources) == "x" and bearer_for("https://open.test/x.png", sources) == ""
    assert fetch("http://head:6361/api/v1/gallery/nuc/2026-10-15/a.png", sources, client=client) == PNG
    assert seen == ["Bearer head-secret"]
    with pytest.raises(SourceRefused, match="answered 401"):
        fetch("https://open.test/a.png", sources, client=client)
    with pytest.raises(SourceRefused, match="not under a prefix"):
        resolve_source("http://head:6361/api/v1/other", sources)
    monkeypatch.delenv("HEAD_TOKEN")
    assert bearer_for("http://head:6361/api/v1/gallery/x.png", sources) == "", "no env, no token - never a guess"


def test_keep_each_lands_what_it_can_and_says_why_not(tmp_path):
    from seren_sinew.blobs import keep_each
    (tmp_path / "a.png").write_bytes(PNG)
    (tmp_path / "t.txt").write_text("text")
    bs = BlobStore(tmp_path / "keep", max_bytes=10_000)

    def keep_one(data, ref):
        from seren_sinew.blobs import head_of, kind_kept
        if kind_kept(head_of(data)) is None:
            raise ValueError("a text is a note")
        if ref.endswith("a.png") and "owner-gone" in str(tmp_path):
            return None
        return {"digest": bs.put(data).digest, "source": ref}
    kept, refused = keep_each([str(tmp_path / "a.png"), str(tmp_path / "t.txt"), str(tmp_path / "nope.png")],
                              keep_one, [str(tmp_path)], 10_000)
    assert [k["source"].endswith("a.png") for k in kept] == [True]
    assert [r["why"] for r in refused] == ["a text is a note", f"no file at {tmp_path / 'nope.png'}: FileNotFoundError"]
    kept, refused = keep_each([str(tmp_path / "a.png")], lambda d, r: None, [str(tmp_path)])
    assert kept == [] and refused[0]["why"] == "nothing with that id to keep it with"


def test_choose_shown_sends_what_fits_and_lists_the_rest():
    from seren_sinew.blobs import MAX_IMAGE_BYTES, choose_shown, file_name
    rows = [{"file": "a.png", "mime": "image/png", "bytes": 10},
            {"file": "b.mp4", "mime": "video/mp4", "bytes": 10},
            {"file": "c.jpg", "mime": "image/jpeg", "bytes": MAX_IMAGE_BYTES + 1},
            {"file": "d.webp", "mime": "image/webp", "bytes": 10},
            {"file": "e.gif", "mime": "image/gif", "bytes": 10}]
    shown, not_shown = choose_shown(rows, lambda r: None if r["file"] == "d.webp" else b"x" * r["bytes"])
    assert [f for _, f in shown] == ["png", "gif"]
    assert [(r["file"], r["why"]) for r in not_shown] == [
        ("b.mp4", "not a still picture the model APIs show"), ("c.jpg", "over the size one look can carry"),
        ("d.webp", "its bytes are missing from the folder")]
    assert file_name("keepsake", "b115d89570619bfd" + "0" * 48, "image/png") == "keepsake-b115d895.png"
    assert file_name("figure", "a" * 64, None) == "figure-aaaaaaaa"


def test_the_folder_as_a_store(tmp_path):
    bs = BlobStore(tmp_path / "keep")
    s = bs.store("keepsakes", "pictures kept with the notes")
    assert (s.name, s.kind, s.path, s.what) == ("keepsakes", "blobs", str(tmp_path / "keep"), "pictures kept with the notes")
    assert INCOMING in s.exclude and s.optional, "a snapshot from before there were blobs still restores"
    bs.put(PNG)
    (tmp_path / "keep" / INCOMING / "x.partial").write_bytes(b"half")
    assert s.describe()["bytes"] == len(PNG), "what is being written is not what is kept"
