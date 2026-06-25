"""seren_sinew - the connective-tissue runtime plumbing of the Seren stack.

Sibling to seren_meninges (the UI / auth / config baseplate). Where Meninges
is the membrane, Sinew is the tendon: cross-cutting *runtime* concerns shared
by every Seren web service. First brick: request logging. Future home for the
cluster client / discovery / DTOs once Lodestar ports from C# to Python.
"""
from __future__ import annotations

from .request_log import (
    RequestLoggingMiddleware,
    get_logger,
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
    "__version__",
]
