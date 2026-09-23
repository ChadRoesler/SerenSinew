"""
Two things the middleware promises beyond the log line.

A request id on every request, kept when the caller sent one and minted when
they did not, on the line and on the response. And a service that boots with
no home directory: Path.home() raises RuntimeError there, not OSError, and it
used to sit outside the try - so a container run as `--user 1000` on a Jetson
crashed at middleware construction on a feature that only writes a log.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from seren_sinew.request_log import (
    REQUEST_ID_HEADER, RequestLoggingMiddleware, _default_log_filename,
    _request_id, setup_request_logger,
)


@pytest.fixture(autouse=True)
def _clean_request_loggers():
    yield
    for name in list(logging.root.manager.loggerDict):
        if name.endswith(".requests"):
            lg = logging.getLogger(name)
            for h in list(lg.handlers):
                h.close()
                lg.removeHandler(h)


def _app(service, log_dir):
    async def ok(request):
        return PlainTextResponse(request.state.request_id)
    app = Starlette(routes=[Route("/ok", ok)])
    app.add_middleware(RequestLoggingMiddleware, service_name=service, log_dir=str(log_dir))
    return app


def _log(log_dir, service):
    return (log_dir / _default_log_filename(service)).read_text(encoding="utf-8")


def test_an_inbound_request_id_is_kept_logged_and_echoed(tmp_path):
    c = TestClient(_app("t-rid-in", tmp_path))
    r = c.get("/ok", headers={REQUEST_ID_HEADER: "lodestar-abc123"})
    assert r.headers[REQUEST_ID_HEADER] == "lodestar-abc123"
    assert r.text == "lodestar-abc123", "routes read it off request.state"
    assert "[rid=lodestar-abc123]" in _log(tmp_path, "t-rid-in")


def test_a_missing_request_id_is_minted(tmp_path):
    c = TestClient(_app("t-rid-mint", tmp_path))
    r = c.get("/ok")
    rid = r.headers[REQUEST_ID_HEADER]
    assert rid and len(rid) == 12
    assert f"[rid={rid}]" in _log(tmp_path, "t-rid-mint")


def test_two_requests_get_two_ids(tmp_path):
    c = TestClient(_app("t-rid-two", tmp_path))
    assert c.get("/ok").headers[REQUEST_ID_HEADER] != c.get("/ok").headers[REQUEST_ID_HEADER]


@pytest.mark.parametrize("bad", ["", "   ", "x" * 65, "has space", "tab\there", "é"])
def test_a_junk_inbound_id_is_replaced_not_carried(bad):
    """An id is a short printable token. Anything else is somebody's payload
    trying to land in a log file, and it gets a fresh one instead."""
    rid = _request_id(bad)
    assert rid != bad.strip() and len(rid) == 12


def test_no_home_directory_degrades_to_stderr_instead_of_crashing(monkeypatch, caplog):
    def no_home():
        raise RuntimeError("Could not determine home directory.")
    monkeypatch.setattr(Path, "home", staticmethod(no_home))
    lg = setup_request_logger("t-nohome")          # must not raise
    assert any(isinstance(h, logging.StreamHandler) for h in lg.handlers)
    assert not any(isinstance(h, logging.FileHandler) for h in lg.handlers)
