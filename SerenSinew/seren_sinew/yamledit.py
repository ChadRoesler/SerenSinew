"""
seren_sinew.yamledit - change one key of a yaml a person reads, and nothing else.

A service's yaml (seren-lodestar.yaml, a carabiner's claude.yaml) is a file
people read: its comments are the manual for the box it sits on. A round trip
through a yaml library would drop every one of them, so a setting is changed
by finding its line and rewriting that line. Two levels, like the files:
`key: value`, and `block:` with `  key: value` under it. A key's own trailing
comment is kept; the lines under a key it replaces (a block list, `    - Read`)
go with it.

`set` and `unset` take the text and give text back; `write` puts it on disk
atomically, the old file kept beside it as <name>.seren-bak, its mode kept
(a yaml holding an inline token is 0600 and stays so).

The same editor as kbh.yamledit in SerenCarabiners (29 Oct 2026, for
Lodestar's Rack changing ripple.target). kbh stays standard-library only and
cannot import this one, so the two are kept the same by hand.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from typing import Any, List, Optional, Tuple

_PLAIN = re.compile(r"[A-Za-z0-9_./:\\~*-]+")
_RESERVED = {"true", "false", "yes", "no", "on", "off", "null", "~", "", ".nan", ".inf"}
_DRIVE = re.compile(r"[A-Za-z]:[\\/][^:]*")


def scalar(value: Any) -> str:
    """A value as the right side of `key: `, readable by PyYAML and by a
    by-hand reader alike. A list is one comma line, quoted."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return json.dumps(", ".join(str(v) for v in value))
    s = "" if value is None else str(value)
    # Plain only when no yaml reader could take it for anything but this
    # string: not a keyword, nothing number-like (0x10, 1e3, 1:30 - YAML 1.1
    # reads that as 90), and a colon only as a Windows drive (C:\...).
    plain = bool(_PLAIN.fullmatch(s)) and s.lower() not in _RESERVED and not s.startswith(("-", "*", "~", "."))
    plain = plain and not s[:1].isdigit() and (":" not in s or bool(_DRIVE.fullmatch(s)))
    return s if plain else json.dumps(s)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _content(line: str) -> bool:
    s = line.strip()
    return bool(s) and not s.startswith("#")


def _name(line: str) -> Optional[str]:
    if not _content(line) or ":" not in line:
        return None
    return line.strip().split(":", 1)[0].strip()


def _value(line: str) -> str:
    """What follows `key:`, without the key's own comment."""
    if ":" not in line:
        return ""
    rest, comment = line.split(":", 1)[1], _comment(line).strip()
    return (rest[:rest.rfind(comment)] if comment else rest).strip()


def _comment(line: str) -> str:
    """The key's own trailing comment (` # ...`), outside quotes, or ""."""
    rest = line.split(":", 1)[1] if ":" in line else ""
    quote = None
    for i, ch in enumerate(rest):
        if ch in "\"'" and quote in (None, ch):
            quote = None if quote else ch
        elif ch == "#" and quote is None and i > 0 and rest[i - 1] in " \t":
            return "  " + rest[i:].rstrip()
    return ""


def _top(lines: List[str], name: str) -> Optional[int]:
    for i, line in enumerate(lines):
        if _indent(line) == 0 and _name(line) == name:
            return i
    return None


def _children(lines: List[str], start: int, indent: int) -> int:
    """The index just past the lines under lines[start] (deeper than
    `indent`); blank lines and comments inside the run belong to it, trailing
    ones do not."""
    end = start + 1
    last = start
    while end < len(lines):
        line = lines[end]
        if _content(line):
            if _indent(line) <= indent:
                break
            last = end
        end += 1
    return last + 1


def _span(lines: List[str], at: int) -> int:
    """The index just past a key's line and everything that is its value:
    the lines deeper than it, and a list written at its own indent
    (`  allowed_tools:` / `  - Read`, which YAML allows)."""
    indent = _indent(lines[at])
    end = _children(lines, at, indent)
    while end < len(lines) and _indent(lines[end]) == indent and lines[end].strip().startswith("- "):
        end = _children(lines, end, indent)
    return end


def _find(lines: List[str], key: str) -> Tuple[Optional[int], Optional[int]]:
    """(the block header's index, the key's own index) for a dotted key; for a
    top-level key (None, its index)."""
    if "." not in key:
        return None, _top(lines, key)
    block, sub = key.split(".", 1)
    head = _top(lines, block)
    if head is None:
        return None, None
    end = _children(lines, head, 0)
    child = None
    for i in range(head + 1, end):
        if _content(lines[i]):
            child = _indent(lines[i]) if child is None else child
            if _indent(lines[i]) == child and _name(lines[i]) == sub:
                return head, i
    return head, None


def set(text: str, key: str, value: Any) -> str:  # noqa: A001 - the verb is the name
    lines = text.split("\n")
    head, at = _find(lines, key)
    leaf = key.split(".", 1)[-1]
    if at is not None:
        line = lines[at]
        pad = line[:_indent(line)]
        if "." not in key and _value(line) == "" and _children(lines, at, 0) > at + 1:
            raise ValueError(f"{key} holds a block in this file; change what is under it instead")
        lines[at:_span(lines, at)] = [f"{pad}{leaf}: {scalar(value)}{_comment(line)}"]
        return "\n".join(lines)
    if "." not in key:
        return _append(lines, [f"{key}: {scalar(value)}"])
    block = key.split(".", 1)[0]
    if head is None:
        return _append(lines, [f"{block}:", f"  {leaf}: {scalar(value)}"])
    if _value(lines[head]) not in ("", "{}"):
        raise ValueError(f"{block} is a single value in this file, not a block")
    if _value(lines[head]) == "{}":
        lines[head] = f"{block}:{_comment(lines[head])}"
    end = _children(lines, head, 0)
    kids = [_indent(lines[i]) for i in range(head + 1, end) if _content(lines[i])]
    pad = " " * (kids[0] if kids else 2)
    lines[end:end] = [f"{pad}{leaf}: {scalar(value)}"]
    return "\n".join(lines)


def unset(text: str, key: str) -> str:
    """Take the key out (and its block header, once the block is empty).
    A key that is not there is not an error."""
    lines = text.split("\n")
    head, at = _find(lines, key)
    if at is None:
        return text
    del lines[at:_span(lines, at)]
    if head is not None and not any(_content(lines[i]) for i in range(head + 1, _children(lines, head, 0))):
        del lines[head]
    return "\n".join(lines)


def _append(lines: List[str], new: List[str]) -> str:
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines + new) + "\n"


def write(path: str, text: str) -> None:
    """Atomically, the old file kept as <path>.seren-bak, its mode kept."""
    tmp = path + ".seren-tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    if os.path.exists(path):
        shutil.copy2(path, path + ".seren-bak")
        try:
            shutil.copymode(path, tmp)
        except OSError:
            pass
    os.replace(tmp, path)
