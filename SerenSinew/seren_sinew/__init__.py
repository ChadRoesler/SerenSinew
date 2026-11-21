"""seren_sinew - the connective-tissue runtime plumbing of the Seren stack.

Sibling to seren_meninges (the UI / auth / config baseplate). Where Meninges
is the membrane, Sinew is the tendon: cross-cutting *runtime* concerns shared
by every Seren web service. Request logging; `runas` (seren_sinew.runas),
starting a command AS a person from a root / LocalSystem service - the ripple
that wakes the model. `cluster` (seren_sinew.cluster): the head-to-node wire
spelled once - the DTOs, the node client and discovery that Lodestar and the
Observatory used to spell separately (28 Oct 2026).

THE FRONT DOOR IS STANDARD LIBRARY ONLY. Importing this package, or any part of it
that says it is stdlib only (``seren_sinew.soma.dtos``, ``soma.bundle``,
``cluster.dtos``), must not pull in starlette or httpx: the Ganglion, our unit on the
LLM module, has no network to pip from and reads the body's shapes with Python 3.10
and nothing else (20 Nov 2026). So the request-logging names below load only when
someone asks for one; ``from seren_sinew import RequestLoggingMiddleware`` still works.
The 3.10 floor in pyproject is the module's Python: raise it and the Ganglion falls off.
"""
from __future__ import annotations

from typing import Any

_REQUEST_LOG = ("RequestLoggingMiddleware", "current_request_id", "get_logger",
                "request_id_hooks", "request_id_scope", "setup_request_logger")


def __getattr__(name: str) -> Any:
    if name in _REQUEST_LOG:
        from . import request_log
        return getattr(request_log, name)
    raise AttributeError(f"module 'seren_sinew' has no attribute {name!r}")


try:  # the setuptools-scm build artifact (gitignored); present in installed wheels
    from ._version import version as __version__
except Exception:  # source checkout without a build -> metadata, then dev fallback
    try:
        from importlib.metadata import version as _v

        __version__ = _v("seren-sinew")
    except Exception:
        __version__ = "0.0.0.dev0"

__all__ = [
    "RequestLoggingMiddleware",
    "setup_request_logger",
    "get_logger",
    "current_request_id",
    "request_id_scope",
    "request_id_hooks",
    "__version__",
]
