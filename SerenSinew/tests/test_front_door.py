"""The front door is standard library only: the body's shapes import on a Python with nothing else
installed. That Python is the LLM module's (Ubuntu 22.04, 3.10, no network to pip from), where the
Ganglion reads the same shapes the core and the voice server do."""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parents[1]

# Run in a fresh interpreter whose import system refuses every third-party web package, so a
# stray top-level `import starlette` anywhere on the path fails here and not on the module.
PROBE = textwrap.dedent("""
    import sys
    BLOCKED = ("starlette", "httpx", "anyio", "fastapi", "yaml")
    class Refuse:
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in BLOCKED:
                raise ImportError(f"{name} is not on the module")
    sys.meta_path.insert(0, Refuse())
    sys.path.insert(0, sys.argv[1])

    import seren_sinew
    from seren_sinew.soma import Appendages, RobotStatus, pack, crc32_hex
    from seren_sinew.cluster.dtos import from_dict, to_dict
    got = from_dict({"appendages": [{"name": "light", "ok": True, "provides": ["light"],
                                     "night": {"default": 28, "holding": 28}}]}, Appendages)
    assert got.providing("light").name == "light", got
    assert to_dict(from_dict({"ok": True, "firmware": "x"}, RobotStatus))["firmware"] == "x"
    loaded = sorted(m for m in sys.modules if m.split(".")[0] in BLOCKED)
    assert not loaded, loaded
    try:
        seren_sinew.RequestLoggingMiddleware
    except ImportError:
        pass   # asked for by name, it does reach for starlette: that's the lazy part working
    else:
        raise AssertionError("the middleware loaded with starlette refused")
    print("ok")
""")


def test_the_shapes_import_with_the_standard_library_alone():
    r = subprocess.run([sys.executable, "-I", "-c", PROBE, str(PKG_ROOT)], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr


def test_the_logging_names_still_answer_from_the_package_root():
    import seren_sinew
    from seren_sinew import RequestLoggingMiddleware, setup_request_logger
    from seren_sinew.request_log import RequestLoggingMiddleware as direct
    assert RequestLoggingMiddleware is direct and callable(setup_request_logger)
    assert set(seren_sinew.__all__) >= {"RequestLoggingMiddleware", "get_logger", "__version__"}
