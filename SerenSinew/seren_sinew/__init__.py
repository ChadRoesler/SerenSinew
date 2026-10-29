"""seren_sinew - the connective-tissue runtime plumbing of the Seren stack.

Sibling to seren_meninges (the UI / auth / config baseplate). Where Meninges
is the membrane, Sinew is the tendon: cross-cutting *runtime* concerns shared
by every Seren web service. Request logging; `runas` (seren_sinew.runas),
starting a command AS a person from a root / LocalSystem service - the ripple
that wakes the model. `cluster` (seren_sinew.cluster): the head-to-node wire
spelled once - the DTOs, the node client and discovery that Lodestar and the
Observatory used to spell separately (28 Oct 2026).
"""
from __future__ import annotations

from .request_log import (
    RequestLoggingMiddleware,
    current_request_id,
    get_logger,
    request_id_hooks,
    request_id_scope,
    setup_request_logger,
)

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
