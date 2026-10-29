"""
seren_sinew.atomic
========================================================================

Writing a small file so a reader never sees half of it: a state file, a
ledger record, a job's status. The same few lines lived in more than a dozen
places across the family (write a .tmp beside it, os.replace it in), each a
little different, and one service skipped them and wrote its state file in
place (25 Oct 2026), where a crash mid-write leaves truncated JSON.

WHAT EVERY WRITE DOES
  - the parent folder is made if it is missing
  - the bytes go to a temp file BESIDE the target (same filesystem, so the
    swap is a rename), named for this writer: the process id and a random
    part. One shared 'x.json.tmp' let two writers of the same folder (a
    service and its trainer) trample each other's half-written file.
  - the temp file is flushed and fsynced, then swapped in with os.replace:
    a crash leaves the last good file or the new one, never a mix
  - on Windows a swap over a file a reader holds open is refused
    (PermissionError); it is tried again for about a second before giving up
  - `mode` sets the new file's permissions from the start (0o600 for a
    token), so it is never readable by others for a moment
  - a failed write removes its temp file and raises; the target is untouched

Depends on the standard library only.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Optional, Union

PathLike = Union[str, "os.PathLike[str]"]

#: How often, and how far apart, a swap refused on Windows is tried again.
REPLACE_TRIES = 50
REPLACE_PAUSE = 0.02


def write_bytes(path: PathLike, data: bytes, *, mode: Optional[int] = None) -> Path:
    """Write data to path atomically. Returns the path."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                 0o666 if mode is None else mode)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        for i in range(REPLACE_TRIES):
            try:
                os.replace(tmp, p)
                break
            except PermissionError:
                if i == REPLACE_TRIES - 1:
                    raise
                time.sleep(REPLACE_PAUSE)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return p


def write_text(path: PathLike, text: str, *, encoding: str = "utf-8", mode: Optional[int] = None) -> Path:
    """Write text to path atomically. Returns the path."""
    return write_bytes(path, text.encode(encoding), mode=mode)


def write_json(path: PathLike, data: Any, *, indent: Optional[int] = 2, sort_keys: bool = False,
               ensure_ascii: bool = True, newline: bool = False, mode: Optional[int] = None) -> Path:
    """Write data as JSON to path atomically; newline=True ends the file with
    one. Returns the path."""
    text = json.dumps(data, indent=indent, sort_keys=sort_keys, ensure_ascii=ensure_ascii)
    return write_text(path, text + ("\n" if newline else ""), mode=mode)


def read_json(path: PathLike, default: Any = None) -> Any:
    """The JSON at path, or default when it is missing, unreadable or not
    JSON. For a file that is a convenience; a caller that must not overwrite
    a damaged file reads it itself and decides."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
