"""
seren_sinew.blobs
════════════════════════════════════════════════════════════════════════

Files a service keeps because they ARE the thing kept - a picture it drew, a
screenshot, a clip - named by what they are, not by what they were called.

WHY: a memory, a fact or a margin note can be about a picture, and the
picture used to be a path on some box's gallery: not in the store, not in
its snapshots, not under its privacy, and gone when the gallery was tidied.
This puts the bytes beside the database that keeps the row about them, so
they travel together: same snapshot, same rules about who reads them, pruned
when the row goes. Each store owns its own; the same picture kept in Memory
and in Margin is two intentions, and losing one must never touch the other.

LAYOUT
    <root>/<aa>/<sha256 hex>      no extension, no name
    <root>/.incoming/             what is being written (never in a snapshot)

    Everything with meaning - what it shows, why it was kept, its type, where
    it came from - is a row in the service's own database. The file is the
    bytes. So a listing of the folder, or of a snapshot's manifest, says
    nothing about what was kept; that is what lets Margin snapshot its
    keepsakes under the rule that a listing never says what a note says.

IMMUTABLE. A blob's name is its sha256, so it never changes once written: it
is only ever added or unlinked. That is what makes snapshots cheap: the
"blobs" store kind (seren_sinew.stores) hardlinks each file from the previous
snapshot instead of copying it again, so twenty-two kept snapshots of a video
cost one video.

    put()         bytes, a file-like or a path -> Blob (digest, size, type).
                  Written to .incoming and renamed: a blob that exists is
                  whole. The same bytes twice is one file.
    path / open / read   by digest. A digest that is not 64 hex characters is
                  refused before it can become a path.
    delete()      one blob. sweep(referenced): every blob no row names any
                  more, with a grace period - a blob younger than `grace` is
                  kept, so a put() whose row is not written yet survives a
                  sweep that runs in between.
    LIMITS        max_bytes per blob and max_total for the folder, each
                  refused with a message that says both sizes (BlobTooLarge,
                  BlobStoreFull). One stray 40 GB file must not fill the NVMe.
    sniff()       (mime, extension) from the first bytes: the pictures every
                  webview draws, video, audio, PDF. The type a service writes
                  in its row and hands back over MCP; nothing here trusts a
                  filename.

WHERE A BLOB MAY BE TAKEN FROM (resolve_source, fetch, keep_each). A tool
that keeps "the file at this path" is a tool that reads any file on the box,
and one that fetches "this URL" is a hand on every service on the LAN. So a
service's config names the shelves it may reach - folders (a path must
resolve, symlinks followed, to a file under one) and URL prefixes
("https://", or one host) - and nothing listed means nothing is kept, with a
refusal that says so. kind_kept() decides what is kept by its first bytes: a
picture, a clip, a sound, a PDF; a text file is a note and an SVG is a script
wearing a picture's extension.

SHOWN OVER MCP (choose_shown). A model API draws png, jpeg, webp and gif,
one image block up to about 5 MB of base64, so a look at a row's pictures
sends the ones that fit and lists the rest with the reason.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable, Iterator, Optional, Union

from .stores import Store

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
INCOMING = ".incoming"
CHUNK = 1 << 20

# (magic at offset 0, mime, extension), checked in order
_MAGIC: tuple[tuple[bytes, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
    (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
    (b"GIF87a", "image/gif", ".gif"),
    (b"GIF89a", "image/gif", ".gif"),
    (b"BM", "image/bmp", ".bmp"),
    (b"%PDF", "application/pdf", ".pdf"),
    (b"OggS", "audio/ogg", ".ogg"),
    (b"fLaC", "audio/flac", ".flac"),
    (b"ID3", "audio/mpeg", ".mp3"),
)
# ISO base media (ftyp box at offset 4): the brand says which
_FTYP_VIDEO = (b"isom", b"iso2", b"mp41", b"mp42", b"avc1", b"M4V ", b"dash")
_FTYP_IMAGE = {b"avif": ("image/avif", ".avif"), b"avis": ("image/avif", ".avif"),
               b"heic": ("image/heic", ".heic"), b"heix": ("image/heic", ".heic"), b"mif1": ("image/heif", ".heif")}


def sniff(head: bytes) -> Optional[tuple[str, str]]:
    """(mime, extension) when the first bytes say what the file is. None for
    anything else - text, an archive, nothing - and the service decides."""
    for magic, mime, ext in _MAGIC:
        if head.startswith(magic):
            return mime, ext
    if head[:4] == b"RIFF":
        if head[8:12] == b"WEBP":
            return "image/webp", ".webp"
        if head[8:12] == b"WAVE":
            return "audio/wav", ".wav"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in _FTYP_IMAGE:
            return _FTYP_IMAGE[brand]
        if brand == b"qt  ":
            return "video/quicktime", ".mov"
        if brand in _FTYP_VIDEO or brand.startswith(b"mp4") or brand.startswith(b"iso"):
            return "video/mp4", ".mp4"
        if brand == b"M4A ":
            return "audio/mp4", ".m4a"
    if head[:4] == b"\x1a\x45\xdf\xa3":                      # EBML: webm or matroska, the DocType says which
        return ("video/webm", ".webm") if b"webm" in head[:64] else ("video/x-matroska", ".mkv")
    if len(head) >= 2 and head[0] == 0xFF and head[1] in (0xFB, 0xF3, 0xF2):
        return "audio/mpeg", ".mp3"
    return None


EXT_FOR_MIME: dict[str, str] = {mime: ext for _, mime, ext in _MAGIC} | {
    "image/webp": ".webp", "audio/wav": ".wav", "video/mp4": ".mp4", "video/quicktime": ".mov",
    "audio/mp4": ".m4a", "video/webm": ".webm", "video/x-matroska": ".mkv",
    **{v[0]: v[1] for v in _FTYP_IMAGE.values()}}


def file_name(prefix: str, digest: str, mime: Optional[str]) -> str:
    """A name to show a blob under - "<prefix>-<digest start><ext>" - for a
    harness that keeps a tool's pictures in a chat under a name."""
    return f"{prefix}-{digest[:8]}{EXT_FOR_MIME.get(mime or '', '')}"


def is_digest(s: str) -> bool:
    """A sha256 as this module names things: 64 lowercase hex characters."""
    return bool(s) and _HEX64.match(s) is not None


# ── where a blob may be taken from ────────────────────────────────────────
KEPT_PREFIXES = ("image/", "video/", "audio/", "application/pdf")


class SourceRefused(ValueError):
    """The service may not take a blob from there, and says why."""


# A source is a folder, a URL prefix, or a dict for a URL prefix that wants
# a bearer: {"url": "http://head:6361/api/v1/gallery/", "bearer_token": "..."}
# or {"url": ..., "bearer_token_env": "SEREN_LODESTAR_TOKEN"} (the NAME of an
# env var). A service's config holds the pointer, not the secret.
Source = Union[str, dict[str, Any]]


def _url_prefixes(sources: Iterable[Source]) -> list[tuple[str, str]]:
    """[(prefix, bearer or "")] for the URL sources."""
    out = []
    for s in sources:
        if isinstance(s, dict):
            url = str(s.get("url") or s.get("prefix") or "")
            if not url.lower().startswith(("http://", "https://")):
                continue
            tok = str(s.get("bearer_token") or "") or os.environ.get(str(s.get("bearer_token_env") or ""), "")
            out.append((url, tok))
        elif str(s).lower().startswith(("http://", "https://")):
            out.append((str(s), ""))
    return out


def _folders(sources: Iterable[Source]) -> list[Path]:
    out = []
    for s in sources:
        if isinstance(s, dict) or str(s).lower().startswith(("http://", "https://")):
            continue
        try:
            out.append(Path(str(s)).expanduser().resolve())
        except OSError:
            continue
    return out


def bearer_for(url: str, sources: Iterable[Source]) -> str:
    """The bearer the allowing prefix carries for this URL, or ""."""
    for prefix, tok in _url_prefixes(sources):
        if url.startswith(prefix):
            return tok
    return ""


def resolve_source(ref: str, sources: Iterable[Source]) -> Union[Path, str]:
    """The path a reference names, under one of the allowed folders, or the
    URL itself under one of the allowed prefixes. SourceRefused otherwise."""
    sources = list(sources)
    ref = (ref or "").strip()
    if not ref:
        raise SourceRefused("say where it is: a path, or a URL")
    if ref.lower().startswith(("http://", "https://")):
        prefixes = _url_prefixes(sources)
        if any(ref.startswith(p) for p, _ in prefixes):
            return ref
        raise SourceRefused("that URL is not under a prefix the sources allow"
                            + ("" if prefixes else " (no URL prefixes are configured)"))
    try:
        p = Path(ref).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as e:
        raise SourceRefused(f"no file at {ref}: {type(e).__name__}") from e
    if not p.is_file():
        raise SourceRefused(f"{ref} is not a file")
    folders = _folders(sources)
    for folder in folders:
        if folder == p or folder in p.parents:
            return p
    raise SourceRefused("that path is not under a folder the sources allow"
                        + ("" if folders else " (no folders are configured)"))


def fetch(ref: str, sources: Iterable[Source], max_bytes: Optional[int] = None,
          client: Any = None) -> Union[Path, bytes]:
    """Bring the bytes: the path (BlobStore.put streams it) or, for a URL,
    the body, read no further than max_bytes, with the bearer the allowing
    prefix carries. `client` is an httpx.Client (tests pass one with a
    MockTransport)."""
    sources = list(sources)
    where = resolve_source(ref, sources)
    if isinstance(where, Path):
        return where
    import httpx
    own = client is None
    client = client or httpx.Client(timeout=30.0, follow_redirects=True,
                                   headers={"User-Agent": "Seren (+https://github.com/ChadRoesler)"})
    tok = bearer_for(where, sources)
    headers = {"Authorization": f"Bearer {tok}"} if tok else {}
    try:
        with client.stream("GET", where, headers=headers) as r:
            if r.status_code != 200:
                raise SourceRefused(f"{where} answered {r.status_code}")
            data = bytearray()
            for chunk in r.iter_bytes():
                data += chunk
                if max_bytes and len(data) > max_bytes:
                    raise BlobTooLarge(f"{where} is over the limit of one file at {_human(max_bytes)}")
        return bytes(data)
    except httpx.HTTPError as e:
        raise SourceRefused(f"{where} could not be fetched: {type(e).__name__}: {e}") from e
    finally:
        if own:
            client.close()


def kind_kept(head: bytes) -> Optional[str]:
    """The mime of something a service keeps as a blob, or None for what it
    does not (text, archives, anything the bytes do not name)."""
    k = sniff(head)
    if k and k[0].startswith(KEPT_PREFIXES):
        return k[0]
    return None


def head_of(src: Union[bytes, bytearray, Path, str], n: int = 64) -> bytes:
    if isinstance(src, (bytes, bytearray)):
        return bytes(src[:n])
    with open(src, "rb") as f:
        return f.read(n)


def keep_each(refs: Iterable[str], keep_one: Callable[[Union[Path, bytes], str], Any],
              sources: Iterable[Source], max_bytes: Optional[int] = None) -> tuple[list[Any], list[dict[str, str]]]:
    """Each reference fetched and handed to keep_one(data, ref), which
    returns the row it made (or None: no such owner). What cannot be kept is
    listed with why, and the rest still land. Returns (kept, refused)."""
    sources = list(sources)
    kept, refused = [], []
    for ref in refs:
        try:
            data = fetch(ref, sources, max_bytes)
            row = keep_one(data, ref)
        except (SourceRefused, BlobTooLarge, BlobStoreFull, ValueError) as e:
            refused.append({"source": ref, "why": str(e)})
            continue
        if row is None:
            refused.append({"source": ref, "why": "nothing with that id to keep it with"})
            continue
        kept.append(row)
    return kept, refused


# ── shown over MCP ────────────────────────────────────────────────────────
# One image block is capped by the model APIs at about 5 MB of base64 (3.75
# MB of bytes); the total keeps one answer from carrying an album at full
# size. The formats are the ones every model API draws.
MAX_IMAGE_BYTES = 3_500_000
MAX_TOTAL_IMAGE_BYTES = 8_000_000
SHOWN_FORMATS = {"image/png": "png", "image/jpeg": "jpeg", "image/webp": "webp", "image/gif": "gif"}


def choose_shown(rows: Iterable[dict[str, Any]], read: Callable[[dict[str, Any]], Optional[bytes]]
                 ) -> tuple[list[tuple[bytes, str]], list[dict[str, Any]]]:
    """Which of a row's pictures to send as image content, and which to list
    instead. Each row has `mime` and `bytes` (and whatever names it, which
    rides into the not_shown line). read(row) gives the bytes, or None when
    they are missing. Returns ([(bytes, format)], [not_shown rows with why])."""
    shown: list[tuple[bytes, str]] = []
    not_shown: list[dict[str, Any]] = []
    total = 0
    for row in rows:
        fmt = SHOWN_FORMATS.get(row.get("mime") or "")
        size = int(row.get("bytes") or 0)
        if fmt is None:
            not_shown.append({**row, "why": "not a still picture the model APIs show"})
            continue
        if size > MAX_IMAGE_BYTES or total + size > MAX_TOTAL_IMAGE_BYTES:
            not_shown.append({**row, "why": "over the size one look can carry"})
            continue
        data = read(row)
        if data is None:
            not_shown.append({**row, "why": "its bytes are missing from the folder"})
            continue
        total += len(data)
        shown.append((data, fmt))
    return shown, not_shown


class BlobTooLarge(ValueError):
    """One file over max_bytes."""


class BlobStoreFull(ValueError):
    """The folder would pass max_total."""


@dataclass(frozen=True)
class Blob:
    """What put() hands back: enough for the row that keeps it."""
    digest: str
    bytes: int
    mime: Optional[str]                    # from sniff(); None when the bytes said nothing
    ext: Optional[str]
    existed: bool                          # the same bytes were already here


class BlobStore:
    """A folder of files named by their sha256. See the module docstring."""

    def __init__(self, root: Union[str, Path], max_bytes: Optional[int] = None, max_total: Optional[int] = None):
        self.root = Path(root).expanduser()
        self.max_bytes = max_bytes
        self.max_total = max_total

    # ── as a Store, for snapshots ─────────────────────────────────────────
    def store(self, name: str, what: str = "") -> Store:
        """This folder declared as a `blobs` store for seren_sinew.stores:
        snapshotted by hardlink against the previous snapshot, .incoming
        left out, and optional on a restore - a snapshot from before the
        service kept blobs holds none, and restores the rest."""
        return Store(name, "blobs", str(self.root), what, exclude=(INCOMING,), optional=True)

    # ── where things are ──────────────────────────────────────────────────
    def path(self, digest: str) -> Path:
        """Where a blob with this digest sits (whether or not it exists).
        ValueError for anything that is not a digest: no name, no path part,
        no uppercase, ever becomes a path under root."""
        if not is_digest(digest):
            raise ValueError(f"not a blob digest: {digest!r}")
        return self.root / digest[:2] / digest

    def has(self, digest: str) -> bool:
        return is_digest(digest) and self.path(digest).is_file()

    def size(self, digest: str) -> int:
        return self.path(digest).stat().st_size

    def open(self, digest: str) -> BinaryIO:
        return open(self.path(digest), "rb")

    def read(self, digest: str) -> bytes:
        return self.path(digest).read_bytes()

    def head(self, digest: str, n: int = 64) -> bytes:
        with self.open(digest) as f:
            return f.read(n)

    def kind(self, digest: str) -> Optional[tuple[str, str]]:
        """sniff() of a kept blob."""
        return sniff(self.head(digest))

    # ── in ────────────────────────────────────────────────────────────────
    def put(self, src: Union[bytes, bytearray, memoryview, BinaryIO, str, Path]) -> Blob:
        """Keep these bytes. Streams through sha256 into .incoming, then
        renames into place, so a blob that exists is whole, and the same
        bytes a second time is the same one file. BlobTooLarge / BlobStoreFull
        before anything is kept."""
        incoming = self.root / INCOMING
        incoming.mkdir(parents=True, exist_ok=True)
        tmp = incoming / f"{uuid.uuid4().hex}.partial"
        h = hashlib.sha256()
        total = 0
        first = b""
        try:
            with open(tmp, "wb") as out:
                for chunk in _chunks(src):
                    if not first:
                        first = bytes(chunk[:64])
                    total += len(chunk)
                    if self.max_bytes is not None and total > self.max_bytes:
                        raise BlobTooLarge(f"{_human(total)}+ is over this store's limit of one file at {_human(self.max_bytes)}")
                    h.update(chunk)
                    out.write(chunk)
            digest = h.hexdigest()
            final = self.path(digest)
            kind = sniff(first)
            if final.is_file():
                return Blob(digest, total, kind[0] if kind else None, kind[1] if kind else None, existed=True)
            if self.max_total is not None:
                have = self.total_bytes()
                if have + total > self.max_total:
                    raise BlobStoreFull(f"{_human(total)} would put the store at {_human(have + total)}, "
                                        f"over its limit of {_human(self.max_total)} (it holds {_human(have)})")
            final.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp, final)
            return Blob(digest, total, kind[0] if kind else None, kind[1] if kind else None, existed=False)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    # ── out ───────────────────────────────────────────────────────────────
    def delete(self, digest: str) -> bool:
        """Unlink one. True when it was there."""
        p = self.path(digest)
        try:
            p.unlink()
        except FileNotFoundError:
            return False
        try:
            p.parent.rmdir()                                 # an empty shard folder goes with it
        except OSError:
            pass
        return True

    def sweep(self, referenced: Iterable[str], grace: float = 3600.0, now: Optional[float] = None) -> list[str]:
        """Remove every blob not in `referenced` that is older than `grace`
        seconds, and any .incoming leftovers that old. Returns the digests
        removed. The grace keeps a blob whose row is still being written."""
        now = time.time() if now is None else now
        keep = set(referenced)
        removed = []
        for digest in list(self.digests()):
            if digest in keep:
                continue
            p = self.path(digest)
            try:
                if now - p.stat().st_mtime < grace:
                    continue
            except OSError:
                continue
            if self.delete(digest):
                removed.append(digest)
        incoming = self.root / INCOMING
        if incoming.is_dir():
            for f in incoming.iterdir():
                try:
                    if f.is_file() and now - f.stat().st_mtime >= grace:
                        f.unlink()
                except OSError:
                    pass
        return removed

    # ── what is here ──────────────────────────────────────────────────────
    def digests(self) -> Iterator[str]:
        if not self.root.is_dir():
            return
        for shard in sorted(self.root.iterdir()):
            if shard.is_dir() and len(shard.name) == 2 and not shard.name.startswith("."):
                for f in sorted(shard.iterdir()):
                    if f.is_file() and is_digest(f.name) and f.name[:2] == shard.name:
                        yield f.name

    def count(self) -> int:
        return sum(1 for _ in self.digests())

    def total_bytes(self) -> int:
        total = 0
        for d in self.digests():
            try:
                total += self.path(d).stat().st_size
            except OSError:
                pass
        return total

    def copy_out(self, digest: str, dest: Union[str, Path]) -> Path:
        """A copy of one blob under a name of the caller's choosing: how a
        kept picture is shown somewhere (a chat's files, a folder a person
        looks at). The store keeps its own."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.path(digest), dest)
        return dest


def _chunks(src) -> Iterator[bytes]:
    if isinstance(src, (bytes, bytearray, memoryview)):
        mv = memoryview(src)
        for i in range(0, len(mv), CHUNK):
            yield bytes(mv[i:i + CHUNK])
        return
    if isinstance(src, (str, Path)):
        with open(src, "rb") as f:
            yield from iter(lambda: f.read(CHUNK), b"")
        return
    yield from iter(lambda: src.read(CHUNK), b"")


def _human(n: int) -> str:
    x = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if x < 1024 or unit == "TB":
            return f"{x:.0f} {unit}" if unit == "B" else f"{x:.1f} {unit}"
        x /= 1024
    return f"{n} B"


__all__ = ["BlobStore", "Blob", "BlobTooLarge", "BlobStoreFull", "sniff", "is_digest", "INCOMING",
           "EXT_FOR_MIME", "file_name", "KEPT_PREFIXES", "Source", "SourceRefused", "resolve_source", "bearer_for",
           "fetch", "kind_kept",
           "head_of", "keep_each", "MAX_IMAGE_BYTES", "MAX_TOTAL_IMAGE_BYTES", "SHOWN_FORMATS", "choose_shown"]
