"""Tests for seren_sinew.request_log.

Runs the middleware against a real (tiny) Starlette app via TestClient and
asserts on the actual rotating-file output - the realest shakedown without a
live server. Each test uses its own service_name so the module-level logger
registry doesn't bleed handlers across cases; an autouse fixture tears the
test loggers down regardless.
"""
from __future__ import annotations

import asyncio
import logging

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from seren_sinew.request_log import (
    RequestLoggingMiddleware,
    _default_log_filename,
    get_logger,
    setup_request_logger,
)


@pytest.fixture(autouse=True)
def _clean_request_loggers():
    """Close + detach any '*.requests' loggers after each test so file
    handlers release tmp_path and the next test starts clean."""
    yield
    for name in list(logging.root.manager.loggerDict):
        if name.endswith(".requests"):
            lg = logging.getLogger(name)
            for h in list(lg.handlers):
                h.close()
                lg.removeHandler(h)


def _build_app(service_name, log_dir, **mw_kwargs):
    async def ok(request):
        return PlainTextResponse("ok")

    async def boom(request):
        raise RuntimeError("kaboom")

    async def slow(request):
        await asyncio.sleep(1.05)
        return PlainTextResponse("slow")

    app = Starlette(routes=[
        Route("/ok", ok),
        Route("/boom", boom),
        Route("/slow", slow),
    ])
    app.add_middleware(
        RequestLoggingMiddleware,
        service_name=service_name,
        log_dir=str(log_dir),
        **mw_kwargs,
    )
    return app


def _read_log(log_dir, service_name):
    f = log_dir / _default_log_filename(service_name)
    return f.read_text(encoding="utf-8") if f.exists() else ""


def test_default_filename_strips_seren_prefix():
    assert _default_log_filename("seren-observatory") == "observatory-requests.log"
    assert _default_log_filename("seren-lodestar") == "lodestar-requests.log"
    assert _default_log_filename("workbench") == "workbench-requests.log"


def test_setup_is_idempotent(tmp_path):
    a = setup_request_logger("test-idem", log_dir=tmp_path)
    n_handlers = len(a.handlers)
    b = setup_request_logger("test-idem", log_dir=tmp_path)
    assert a is b
    assert len(b.handlers) == n_handlers  # no duplicate stacking


def test_get_logger_alias(tmp_path):
    lg = get_logger("test-alias", log_dir=tmp_path)
    assert lg.name == "test-alias.requests"
    assert lg.handlers  # configured


def test_2xx_logged_as_info(tmp_path):
    client = TestClient(_build_app("test-ok", tmp_path))
    assert client.get("/ok").status_code == 200
    log = _read_log(tmp_path, "test-ok")
    assert "GET /ok -> 200" in log
    assert "[INFO]" in log


def test_4xx_logged_as_warning(tmp_path):
    client = TestClient(_build_app("test-404", tmp_path))
    assert client.get("/nope").status_code == 404
    log = _read_log(tmp_path, "test-404")
    assert "-> 404" in log
    assert "[WARNING]" in log


def test_5xx_logged_as_error_with_traceback(tmp_path):
    client = TestClient(_build_app("test-500", tmp_path), raise_server_exceptions=False)
    assert client.get("/boom").status_code == 500
    log = _read_log(tmp_path, "test-500")
    assert "500 EXCEPTION" in log
    assert "RuntimeError: kaboom" in log
    assert "[ERROR]" in log


def test_slow_2xx_gets_slow_warning(tmp_path):
    client = TestClient(_build_app("test-slow", tmp_path))
    assert client.get("/slow").status_code == 200
    log = _read_log(tmp_path, "test-slow")
    assert "[slow]" in log
    assert "[WARNING]" in log


def test_query_omitted_by_default(tmp_path):
    client = TestClient(_build_app("test-q", tmp_path))
    client.get("/ok?secret=shhh")
    log = _read_log(tmp_path, "test-q")
    assert "secret=shhh" not in log
    assert "GET /ok -> 200" in log


def test_query_included_when_env_set(tmp_path, monkeypatch):
    monkeypatch.setenv("SEREN_TEST_LOG_QUERY", "1")
    client = TestClient(_build_app("test-q2", tmp_path, env_prefix="SEREN_TEST"))
    client.get("/ok?secret=shhh")
    log = _read_log(tmp_path, "test-q2")
    assert "secret=shhh" in log


def test_uses_ascii_arrow_not_unicode(tmp_path):
    # Guard the cross-platform-stderr lesson: the log line must use ASCII '->'.
    client = TestClient(_build_app("test-ascii", tmp_path))
    client.get("/ok")
    log = _read_log(tmp_path, "test-ascii")
    assert "->" in log
    assert "\u2192" not in log
