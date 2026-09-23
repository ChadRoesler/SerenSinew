# SerenSinew

The connective tissue of the Seren stack.

If [SerenMeninges](https://github.com/ChadRoesler/SerenMeninges) is the
**membrane** - the UI shell, auth, config, credentials every service wears -
then Sinew is the **tendon**: the cross-cutting *runtime* plumbing every Seren
web service repeats. One copy, so a fix lands everywhere at once.

## What's in it (today)

**Request logging.** Drop-in middleware that logs every HTTP request as
`<client> <METHOD> <path> -> <status> (<ms>ms) [rid=<id>]` - INFO for 2xx/3xx,
WARNING for 4xx (and slow 2xx), ERROR for 5xx, with the full traceback when an
exception escaped the route. Goes to **both** stderr (so `journalctl` catches
it) **and** a rotating file the running user owns at
`~/seren-logs/<service>-requests.log`, so anyone debugging can `tail` it without
sudo. If there is no home directory to put the file in, it logs to stderr only
and says so, rather than refusing to start.

**Request ids.** Every request gets one: an inbound `X-Request-Id` is kept, so
one string follows a call from Lodestar through Observatory into a service;
otherwise a short random one is minted. It is on the log line and echoed back
as `X-Request-Id` on the response, and routes can read it from
`request.state.request_id` to pass along to the next hop.

It's parameterized, not hardcoded - `service_name` picks the logger name and
log filename, `env_prefix` picks the env-var namespace:

```python
from seren_meninges.auth import bearer_auth_middleware
from seren_sinew.request_log import RequestLoggingMiddleware

# Mount OUTERMOST - before auth - so 401s get logged too.
app.add_middleware(bearer_auth_middleware(token))                # inner
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

The cluster client, discovery and the DTOs that Lodestar and Observatory
currently spell separately. Sinew is where the connective runtime code goes;
Meninges stays the membrane.

## Install

```
pip install seren-sinew
```

Light by design - depends only on `starlette` (already in every leaf via
FastAPI), so it stays FastAPI-agnostic and adds nothing to a real install.

GPL-3.0-or-later.
