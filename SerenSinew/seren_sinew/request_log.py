"""
seren_sinew.request_log
========================================================================

The request-logging middleware every Seren web service mounts. The same idea
that lived more than once - the Observatory's request_log.py and the C#
RuntimeHost's SerenRequestLogger (and, soon, Workbench) - collapsed into ONE
parameterized copy so a fix lands everywhere at once. This is the logging twin
of seren_meninges.auth: ops-sensitive, identical-in-spirit across services,
exactly the thing you want a single source of truth for. Meninges is the
membrane; Sinew is the tendon.

WHAT IT DOES
  Logs every HTTP request as: "<client> <METHOD> <path> -> <status> (<ms>ms)".
  Level by outcome: 2xx/3xx INFO, 4xx WARNING, 5xx ERROR (with full
  traceback), and any 2xx slower than 1s gets a WARNING [slow] tag. Output
  goes to BOTH stderr (journalctl picks it up) AND a rotating file the
  running user owns at <log_dir>/<service>-requests.log.

WHY A FILE THE USER OWNS
  'sudo journalctl -u seren-*' wants a password every time. Anyone debugging
  needs the log NOW, not "go set up sudoers." A plain file under ~/seren-logs/
  is the consistent, no-privileges UX across the whole family.

WHY NOT uvicorn's access log
  stderr-only (no file), fixed format (no duration ms, no 5xx traceback).

PARAMETERIZED, NOT HARDCODED
  service_name drives the logger name and the default log filename; env_prefix
  drives the env-var namespace so each service reads its own knobs:
    {env_prefix}_LOG_LEVEL  - INFO (default) | DEBUG | WARNING | ERROR
    {env_prefix}_LOG_QUERY  - "1" to append ?query to the path (off by
                              default: query strings can carry tokens / PII)
  Observatory keeps its EXACT prior contract by passing env_prefix="SEREN_AGENT"
  (-> SEREN_AGENT_LOG_LEVEL / SEREN_AGENT_LOG_QUERY, and "seren-observatory"
  still resolves to observatory-requests.log).

WHY ASCII '->' AND NOT '->' (the unicode arrow)
  The original line used a literal unicode arrow. Sinew is cross-platform - it
  runs on the Windows dev box too, where a non-UTF-8 stderr codepage makes the
  StreamHandler choke on a non-ASCII glyph (we've eaten that crash before in
  the consolidator). ASCII '->' is the safe floor; the file handler is utf-8
  either way, but the console handler is the one that bites.

MIDDLEWARE ORDER (load-bearing)
  Mount this OUTERMOST - before auth - so auth-rejected (401) requests are
  logged too ("is the dashboard 401ing or 500ing?" is half the debug battle).
  Starlette runs middleware LIFO on the way in, so add auth FIRST and this
  SECOND: auth ends up inner, logging outer.

  Depends on starlette only (already in every leaf via FastAPI), staying
  FastAPI-agnostic like the rest of Sinew.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import time
import traceback
from pathlib import Path
from typing import Optional, Union

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


def _default_log_filename(service_name: str) -> str:
    """`seren-observatory` -> `observatory-requests.log`. Strips a leading
    `seren-` so the family's filenames read cleanly; falls back to the raw
    name if stripping would leave nothing."""
    stem = service_name.removeprefix("seren-") or service_name
    return f"{stem}-requests.log"


def setup_request_logger(
    service_name: str,
    *,
    log_dir: Optional[Union[str, Path]] = None,
    log_filename: Optional[str] = None,
    env_prefix: str = "SEREN",
    level: Optional[str] = None,
    backup_count: int = 7,
) -> logging.Logger:
    """Configure (once) and return the request logger for a service.

    Idempotent: if the logger already has handlers it's returned as-is, so
    repeated calls (app reload, several modules wanting the same sink) never
    stack duplicate handlers.

    Two handlers: a stderr StreamHandler (journalctl) and a midnight
    TimedRotatingFileHandler at <log_dir>/<log_filename> keeping backup_count
    days. If the dir isn't writable it falls back to stderr-only rather than
    crashing the service.
    """
    logger = logging.getLogger(f"{service_name}.requests")
    if logger.handlers:
        return logger  # already configured - don't double up handlers

    level_name = (level or os.environ.get(f"{env_prefix}_LOG_LEVEL", "INFO")).upper()
    logger.setLevel(getattr(logging, level_name, logging.INFO))
    logger.propagate = False  # don't double-log via root

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    directory = Path(log_dir) if log_dir is not None else Path.home() / "seren-logs"
    filename = log_filename or _default_log_filename(service_name)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.TimedRotatingFileHandler(
            filename=directory / filename,
            when="midnight",
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    except OSError as e:
        # Don't crash the service if the log dir isn't writable - degrade to
        # stderr-only. Request lines still reach journalctl.
        logger.warning(f"could not open file log at {directory}: {e}")

    return logger


def get_logger(service_name: str, **kwargs) -> logging.Logger:
    """Accessor for other modules that want to log to the same request sink.
    Same keyword args as setup_request_logger; idempotent."""
    return setup_request_logger(service_name, **kwargs)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Logs every request with timing + status; captures 5xx tracebacks.

    Mount OUTERMOST (before auth). Wire it as::

        from seren_sinew.request_log import RequestLoggingMiddleware
        app.add_middleware(BearerAuthMiddleware, expected_token=token)   # inner
        app.add_middleware(                                              # outer
            RequestLoggingMiddleware,
            service_name="seren-observatory",
            env_prefix="SEREN_AGENT",
        )
    """

    def __init__(
        self,
        app,
        service_name: str,
        *,
        log_dir: Optional[Union[str, Path]] = None,
        log_filename: Optional[str] = None,
        env_prefix: str = "SEREN",
        level: Optional[str] = None,
        backup_count: int = 7,
    ):
        super().__init__(app)
        self._env_prefix = env_prefix
        self._log = setup_request_logger(
            service_name,
            log_dir=log_dir,
            log_filename=log_filename,
            env_prefix=env_prefix,
            level=level,
            backup_count=backup_count,
        )

    async def dispatch(self, request: Request, call_next) -> Response:
        start = time.perf_counter()
        method = request.method
        path = request.url.path
        # Query string off by default - it can carry tokens / PII. Opt in with
        # {env_prefix}_LOG_QUERY=1 when you actually need it for debug.
        if os.environ.get(f"{self._env_prefix}_LOG_QUERY") == "1" and request.url.query:
            path = f"{path}?{request.url.query}"
        client = request.client.host if request.client else "?"

        try:
            response = await call_next(request)
            duration_ms = int((time.perf_counter() - start) * 1000)
            status = response.status_code

            line = f"{client} {method} {path} -> {status} ({duration_ms}ms)"
            if status >= 500:
                self._log.error(line)
            elif status >= 400:
                self._log.warning(line)
            elif duration_ms > 1000:
                self._log.warning(f"{line} [slow]")
            else:
                self._log.info(line)

            return response

        except Exception as e:
            # Unhandled exception escaped the route. Log the full traceback then
            # re-raise so the framework's 500 handler still runs.
            duration_ms = int((time.perf_counter() - start) * 1000)
            tb = traceback.format_exc()
            self._log.error(
                f"{client} {method} {path} -> 500 EXCEPTION ({duration_ms}ms)\n"
                f"  {type(e).__name__}: {e}\n{tb}"
            )
            raise
