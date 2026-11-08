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
as `X-Request-Id` on the response, and it goes on to the next hop by itself:
a client built with `request_id_hooks()` carries it, and work no request
started (a sleep on the tick) binds its own with `request_id_scope()`.

**Small files written whole** (`seren_sinew.atomic`). `write_json`,
`write_text`, `write_bytes`: a temp file beside the target, fsynced and
swapped in, so a crash leaves the last good file or the new one.

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

**The wire to a body** (`seren_sinew.soma`). SerenSoma is a Stack-chan robot
with Seren inside: its firmware's control API and the voice server that is its
nervous system. SerenPsycheion watches it and makes its faces. What passes
between them is spelled here once:

- `dtos`: the robot's status and settings (a master light dimmer, speaker
  volume, screen brightness, a night cap), its touch spots and head touches,
  and the voice server's settings (night hours, work hours, overwhelm) and
  live touch feed. Standard library only.
- `bundle`: a face packed the way the robot unpacks it, and its CRC.
- `client`: `SomaRobot` and `SomaVoice`, async, over httpx (the `client`
  extra).

There is deliberately no camera call: a photo is taken only when the person
in the room says so. The firmware is C++, so its side is pinned by a contract
test that reads the firmware's own source for the keys it sends, whenever a
SerenSoma checkout sits beside this one.

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

AGPL-3.0-or-later.
