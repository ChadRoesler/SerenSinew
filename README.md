# SerenSinew

The connective tissue of the Seren stack.

If [SerenMeninges](https://github.com/ChadRoesler/SerenMeninges) is the
**membrane** - the UI shell, auth, config, credentials every service wears -
then Sinew is the **tendon**: the cross-cutting *runtime* plumbing every Seren
web service repeats. One copy, so a fix lands everywhere at once.

## What's in it (today)

**Request logging.** Drop-in middleware that logs every HTTP request as
`<client> <METHOD> <path> -> <status> (<ms>ms)` - INFO for 2xx/3xx, WARNING for
4xx (and slow 2xx), ERROR with a full traceback for 5xx. Goes to **both** stderr
(so `journalctl` catches it) **and** a rotating file the running user owns at
`~/seren-logs/<service>-requests.log`, so anyone debugging can `tail` it without
sudo.

It's parameterized, not hardcoded - `service_name` picks the logger name and
log filename, `env_prefix` picks the env-var namespace:

```python
from seren_sinew.request_log import RequestLoggingMiddleware

# Mount OUTERMOST - before auth - so 401s get logged too.
app.add_middleware(BearerAuthMiddleware, expected_token=token)   # inner
app.add_middleware(                                              # outer
    RequestLoggingMiddleware,
    service_name="seren-observatory",
    env_prefix="SEREN_AGENT",   # -> SEREN_AGENT_LOG_LEVEL / SEREN_AGENT_LOG_QUERY
)
```

Knobs (per `env_prefix`):

| env var | effect |
| --- | --- |
| `<PREFIX>_LOG_LEVEL` | `INFO` (default) `\| DEBUG \| WARNING \| ERROR` |
| `<PREFIX>_LOG_QUERY` | `1` to append `?query` to the logged path (off by default - query strings can carry tokens / PII) |

## What's coming

The Python landing pad for the cluster client / discovery / DTOs when Lodestar
(RuntimeHost) and Workbench port over from C#. Sinew is where the connective
runtime code goes; Meninges stays the membrane.

## Install

```
pip install seren-sinew
```

Light by design - depends only on `starlette` (already in every leaf via
FastAPI), so it stays FastAPI-agnostic and adds nothing to a real install.

GPL-3.0-or-later.
