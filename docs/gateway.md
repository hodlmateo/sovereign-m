# gateway

One authenticated front door for the services on a machine you own. Instead of
five apps each listening on the network with their own (or no) login, they
listen on loopback only, and the gateway is the single thing that answers
outside requests.

It is a small reverse proxy on the Python standard library:

- **Routes.** `/ollama/...` goes to one upstream, `/m/...` to another.
- **Per-client keys** with route scopes and rate limits.
- **Path allowlist and denylist** per route, plus built-in blocks for model-server admin calls and app docs/debug pages.
- **Audit log** of who called what, with no bodies and no keys.
- **Middleware chain** (for example a cache) loaded from the config.
- **Health endpoints**, one public and one private.

```bash
python -m sovereign_m.gateway check-config                    # validate and print routes
python -m sovereign_m.gateway keys add laptop --scopes ollama --rate 60/min
python -m sovereign_m.gateway serve                           # loopback, default port
python -m sovereign_m gateway serve --port 0                  # same verbs through the root CLI
python examples/gateway/gateway_local_demo.py                 # safe demo with a throwaway backend
```

Start it yourself with `serve`; nothing here installs or restarts a service.
The gateway does not auto-restart anything, and a config change needs a manual
restart of the gateway.

## What it is not

It is **not a firewall**. It only protects services that are reachable through
it. If a backend still listens on every network interface, anyone on that
network can skip the gateway and talk to it directly. The gateway becomes the
only door when you also rebind each backend to loopback (see the migration
guide) and keep a host firewall as a second layer. Use the selftest
(`docs/selftest.md`) to check what still listens widely.

It speaks plain HTTP. Keep it on loopback, or put it behind something that
adds TLS, before sending keys across a network you do not fully control.
WebSocket upgrades are refused in this version.

## Config reference

Default file: `data/gateway/config.yaml`. Pass another with `--config PATH`.
The file is the repo's tiny YAML subset (mappings, lists, scalars, comments).

Values written `${NAME}` come from the environment at start-up, and
`${NAME:-fallback}` uses a fallback when `NAME` is unset. A route whose
upstream still has an unset `${NAME}` is **disabled**, the others run, and
`check-config` says which variable to set. Upstream ports in the example are
all of this form.

### `server`

| Key | Default | Meaning |
|-----|---------|---------|
| `host` | `loopback` | `loopback`, `localhost`, or a single address literal |
| `port` | `8787` | listen port (`--port` overrides; `0` picks a free one) |
| `allow_nonloopback` | `false` | required for any non-loopback host; `--allow-nonloopback` does the same |

Binding the wildcard address or any other non-loopback address is refused
unless `allow_nonloopback` is true, and even then the gateway prints a warning.

### `auth`

| Key | Default | Meaning |
|-----|---------|---------|
| `keys_file` | workspace `gateway/keys.json` | where client keys live (hashes only) |
| `default_rate_per_min` | `120` | per-client limit when a key has no `--rate`; `0` means none |

### `audit`

| Key | Default | Meaning |
|-----|---------|---------|
| `enabled` | `true` | |
| `path` | workspace `gateway/audit.jsonl` | |
| `max_bytes` | `5242880` | rotate when the file would pass this size |
| `keep` | `5` | rotated files kept (`.1` newest) |
| `query` | `strip` | `strip` drops `?query`; `redact` keeps names, hides values |

The workspace is `SOVEREIGN_M_WORKSPACE` if set, else the checkout's `var/`
(gitignored). The gateway never writes anywhere else on its own.

### `middleware` (global) and per-route `middleware`

A list of `module:attr` factories, outermost first. Each is called with the
entry's `config` mapping and must return an object with
`handle(request, call_next) -> response`:

```yaml
middleware:
  - use: sovereign_m.cache.middleware:make_middleware
    config:
      ttl_seconds: 60
```

Global middleware runs first, then the route's own. A route can set
`use_global_middleware: false` to skip the global list. The config file is
trusted code: a `use:` entry imports and runs that module.

The contract is duck-typed, so nothing needs to import the gateway:

- **request**: `method`, `path` (after the route prefix, with query), `route`, `client_id`, `headers` (lower-case keys, the gateway's auth headers already removed), `body` (bytes)
- **response**: `status`, `headers` (dict), `body` (bytes)
- `call_next(request)` runs the next middleware or the upstream. A middleware may return its own response without calling it, and may set headers such as `x-cache` (recorded in the audit log).
- Middleware instances are shared by all request threads, so keep their state thread-safe.
- A route with no middleware streams the upstream body to the client in chunks. A route with middleware hands the middleware a buffered body (up to 64 MiB).

### `routes`

| Key | Default | Meaning |
|-----|---------|---------|
| `name` | required | letters, digits, dot, dash, underscore; used for scopes and logs |
| `prefix` | required | e.g. `/m/`; may not start with `/_gateway` |
| `upstream` | required | `http://host:port` or `https://...`, optional base path |
| `methods` | `GET` | ceiling of methods the route accepts (`HEAD` rides along with `GET`) |
| `allow` | empty | allowed paths; **empty means every path is refused** |
| `deny` | empty | paths refused even if allowed |
| `timeout` | `30` | seconds for connect and for each read |
| `max_body_bytes` | `1048576` | larger request bodies get 413 before being read |
| `strip_prefix` | `true` | `false` sends the prefix on to the upstream |
| `websocket` | `false` | `true` is rejected: not implemented |
| `allow_admin` | `false` | lifts the built-in admin block (see below) |
| `allow_docs` | `false` | lifts the built-in docs/debug block |
| `health_path` | `/` | probed by `/_gateway/routes/health` |
| `rate_per_min` | none | extra per-client limit on this route |
| `upstream_auth` | none | `header`, `scheme`, `key_file`: add the backend's own key |
| `use_global_middleware` | `true` | |

Path patterns (for `allow` and `deny`) apply to the path **after the route
prefix**:

- `/api/tags` is an exact path.
- `/v1/*` is a glob; `*` also matches across slashes.
- `/static/` (ends with a slash) is a prefix.
- `GET /api/tags` restricts one entry to a method.

Requests whose path has `.` or `..` segments, empty segments (`//`), encoded
slashes (`%2f`), backslashes or control characters get 400, so the gateway and
the backend cannot read one path two ways. Built-in and `deny` patterns are
also checked case-insensitively and with a trailing slash removed.

**Built-in blocks.** On every route, unless lifted:

- `allow_admin: false` blocks `/api/pull`, `/api/delete`, `/api/create`, `/api/copy`, `/api/push`, `/api/blobs` and `/api/blobs/*`. These let a caller download or delete models.
- `allow_docs: false` (and `allow_admin: false`) blocks `/docs`, `/redoc`, `/openapi.json` and `/debug` (with their sub-paths), the interactive pages of uvicorn-style apps.

### `upstream_auth`

For a backend that wants its own key (for example a llama-server started with
`--api-key-file`):

```yaml
upstream_auth:
  header: Authorization
  scheme: Bearer
  key_file: $XDG_CONFIG_HOME/sovereign-m/llama-server.key
```

The file holds the key on one line, mode 0600. It is re-read when it changes,
added to the upstream request only, and never logged or echoed. `$VAR` and `~`
in the path are expanded when used. `check-config` warns if the file is missing
or readable by others.

## Key management

```bash
python -m sovereign_m.gateway keys add laptop --scopes ollama,m-chat --rate 60/min
python -m sovereign_m.gateway keys list
python -m sovereign_m.gateway keys revoke laptop
```

- `keys add` makes a random key (`secrets.token_urlsafe(32)`), prints it **once**, and stores only a per-key random salt and the SHA-256 of salt plus key. The key cannot be recovered. To rotate, revoke and add again.
- `--scopes` lists route names the client may use (default `*`, all). `--rate` is requests per minute (a token bucket; burst equals the rate).
- The keys file is written with mode 0600. The gateway refuses to start if it is readable by group or others or owned by another user, and a running gateway that finds it loosened denies everyone until it is fixed.
- Changes are picked up without a restart; a revoked key stops working on the next request.
- Clients send `Authorization: Bearer KEY` or `X-M-Key: KEY`. Keys are compared in constant time. The gateway removes both headers before the request reaches middleware or the upstream.
- Missing or wrong key: 401. Valid key, route not in its scopes: 403. Over the limit: 429 with `Retry-After`. Without a key, every path (including unknown ones) answers 401, so routes are not revealed.

SHA-256 (not a slow password hash) is deliberate here: the keys are 256 random
bits, so there is nothing to guess, and the check runs on every request.

## Audit log

One JSON line per request, in `audit.jsonl` (mode 0600, rotated by size):

```json
{"ts":"2026-01-01T00:00:00.000+00:00","client_id":"laptop","route":"ollama","method":"POST","path":"/ollama/api/chat","status":200,"bytes_in":212,"bytes_out":1840,"duration_ms":412.3,"cache":"MISS"}
```

Only those fields are written. Never a request or response body, never a
header, never a key, and the query string is dropped (or its values hidden).
Failed logins are logged with `client_id` of `-`.

## Health

- `GET /_gateway/health`: no login; returns `{"ok":true,"version":"..."}` and nothing else.
- `GET /_gateway/routes/health`: needs a key; probes each route's `health_path` on its upstream (2 second cap) and returns `{"routes":{"name":{"up":true,"status":200,"latency_ms":3.1}}}` for the routes in that client's scope. Any answer below 500 counts as up. Upstream addresses are not shown.

## Security notes

- The gateway is one layer. Rebind backends to loopback and keep a host firewall.
- Keys travel in clear text over HTTP. Do not expose the gateway beyond loopback without TLS in front of it or a private network you trust.
- Failed logins are not rate limited. Keys are long and random, but if the gateway faces a network, limit who can connect.
- An allowlist is stronger than a denylist. Keep `allow` lists narrow; the built-in blocks are a backstop, not a policy.
- Request bodies are read into memory up to `max_body_bytes`. Keep it small on routes that do not need large uploads.
- Upstreams that are not loopback are flagged by `check-config`; the gateway will still proxy to them, and `--strict` turns the flag into a failing exit code.
- Logs and error responses never contain keys, bodies or upstream error text.

## Migration guide: make the gateway the only door

Repeat this for each service. The steps use placeholders: `<SERVICE_PORT>` is
the port the service already uses, `<ROUTE>` is a name you choose, `<GATEWAY_PORT>`
is the gateway's port. Do one service at a time, and verify before moving on.

For each one:

1. **Rebind** the service to loopback (the setting is in the table below).
2. **Add a route** in the gateway config with `upstream: http://localhost:<SERVICE_PORT>` and a narrow `allow` list.
3. **Verify** from this machine (loopback) through the gateway, then from a second machine you own that the service port no longer answers directly.
4. **Roll back** if anything breaks: restore the old setting and restart the service yourself, and remove or disable the route.

| Service | Rebind to loopback by | Typical route |
|---------|----------------------|---------------|
| Ollama | environment `OLLAMA_HOST=localhost:<SERVICE_PORT>` for the service (a systemd override sets `Environment=`) | `allow` the few `/api` and `/v1` calls you use; leave `allow_admin` off |
| uvicorn / FastAPI app | start with `--host localhost` (or `host="localhost"` in `uvicorn.run`) | `allow` your API paths; `/docs`, `/redoc`, `/openapi.json` stay blocked |
| Streamlit | `server.address = "localhost"` in its config, or `--server.address localhost` | `allow` `/`, `/_stcore/*`, `/static/` (GET only) |
| ttyd | `-i lo` / `--interface lo` (an interface name or a socket path) **and** `-c user:password` (or `--credential`) | think twice: a shell is the riskiest thing to proxy. Prefer SSH. If you must, allow only `GET`/`POST` on the paths it needs and keep its own credential on |
| open-webui | environment `HOST=localhost` (and `PORT=<SERVICE_PORT>`) | `allow` `/` and `/api/*`; it has its own logins, so keep those on |

Notes per service:

- **Ollama.** After rebinding, any client that used the old address must use the gateway. Native clients set `OLLAMA_HOST` to the gateway URL plus the prefix (`http://localhost:<GATEWAY_PORT>/ollama`) and send the key through whatever header support the client has; clients that cannot add a header need a small local helper instead. Verification: with a key, `GET /ollama/api/tags` returns 200 and `POST /ollama/api/pull` returns 403.
- **uvicorn.** Behind a prefix, apps that build absolute links may need a root path setting (`--root-path /<ROUTE>`). Verify `/<ROUTE>/docs` returns 403 and your API path returns 200.
- **Streamlit.** It uses WebSockets for the live page, which this version of the gateway refuses, so a proxied Streamlit app will load but not update. Keep it on loopback and reach it by an SSH tunnel for now.
- **ttyd.** Also a WebSocket service, so the gateway cannot proxy it today. Rebinding to loopback and using an SSH tunnel is the safe answer.
- **open-webui.** Uses WebSockets for streaming in some versions; test chat in a browser before relying on it.

Verification recipe for any service:

```bash
python -m sovereign_m.gateway check-config                 # route listed, upstream is loopback
python -m sovereign_m.gateway keys add check --scopes <ROUTE>
curl -s -H "Authorization: Bearer $KEY" http://localhost:<GATEWAY_PORT>/<ROUTE>/<an allowed path>
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:<GATEWAY_PORT>/<ROUTE>/<an allowed path>   # expect 401
python -m sovereign_m.selftest scan --check ports          # the service port is no longer wide
python -m sovereign_m.gateway keys revoke check
```

Rollback recipe: put the old bind setting back, restart the service by hand,
disable the route (delete it, or leave its `${NAME}` unset so it is disabled),
and restart the gateway by hand. Your clients can go back to the old address
immediately.

## Files

- `sovereign_m/gateway/`: `config.py`, `keys.py`, `policy.py`, `ratelimit.py`, `audit.py`, `core.py` (routing, middleware, upstream calls), `server.py`, `cli.py`
- `data/gateway/config.yaml`: example config with placeholder routes
- `examples/gateway/gateway_local_demo.py`: runs the gateway against a throwaway backend
- `tests/test_gateway.py`
