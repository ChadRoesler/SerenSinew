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

**Running apps as needed** (`seren_sinew.orchestration`, `seren_sinew.ondemand`).
"I need this up" and "I'm done with it", said the same way by every Seren app:
`ensure` / `release` messages, and a lease book that knows who holds what, for
how long (a lease runs out unless it is renewed), and how long an app has sat
with nobody holding it. A **run policy** per app says which apps run all the
time and which run as needed:

| mode | |
| --- | --- |
| `unmanaged` | the default: a release stops what an ensure started, nothing else |
| `always` | never stopped |
| `on_demand` | started by an ensure, stopped after `idle_minutes` with nobody holding it |
| `scheduled` | kept up inside `hours` (`"22:00-07:00"`), on demand outside them |

`decide()` turns the book, what's running and the policies into "start this,
stop that"; Lodestar runs it on a timer (its `orchestration:` block). For the
caller, `OnDemandTransport` wraps an app's own httpx client: a refused
connection asks Lodestar to start the other app and retries, and the lease is
renewed while the calls keep coming.

```python
from seren_sinew.ondemand import OnDemand, OnDemandTransport

probe = OnDemand(lodestar_url, "probe", holder="seren-workbench", token=lodestar_token)
client = httpx.AsyncClient(base_url=probe_url, transport=OnDemandTransport(probe))
```

**What a service keeps, and snapshots of it** (`seren_sinew.stores`). A
service declares its stores (`Store(name, kind, path)`: `sqlite`, `chroma`,
`dir`, `file`, `blobs`) and gets the same routes as every other: `GET /stores`
says what is kept, `POST /stores/snapshot` copies it the way its kind needs (a
live SQLite database through the backup API), `GET /stores/snapshots` lists
them, `.../archive` hands one over for Lodestar to stash, `.../rehearse` is a
restore's dry run. Retention is the newest `keep_daily`, then one a week. The
archive is a tar streamed from a file, `gz` by default; `compress="xz"` on the
keeper for a store that is mostly database, `"none"` for one that is mostly
pictures.

**Files kept by what they are** (`seren_sinew.blobs`). A memory, a fact or a
margin note can be about a picture; `BlobStore` keeps the bytes beside the
database that keeps the row, named by their sha256 and nothing else, so they
travel with it - same snapshot, same privacy, pruned when the row goes. As a
`blobs` store, a snapshot hardlinks every file the previous snapshot already
holds: twenty-two kept snapshots of a video cost one video.

```python
from seren_sinew.blobs import BlobStore

keep = BlobStore("~/.seren-margin/keepsakes", max_bytes=512 << 20, max_total=64 << 30)
blob = keep.put(png_bytes)          # Blob(digest, bytes, mime, ext, existed)
stores = [Store("notes", "sqlite", db_path), keep.store("keepsakes", "pictures kept with the notes")]
```

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
