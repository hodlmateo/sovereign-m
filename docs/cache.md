# cache

A local response cache for model calls. When the same low-temperature,
non-personal request arrives twice, the second answer comes from a sqlite file
instead of the model. Standard library only.

```bash
python -m sovereign_m.cache demo     # dry run: fake model on an ephemeral loopback port
python -m sovereign_m.cache stats [--db PATH]
python -m sovereign_m.cache purge [--db PATH]   # drop expired entries
python -m sovereign_m.cache clear [--db PATH]   # drop everything
python -m sovereign_m cache stats                # same verbs through the root CLI
python examples/cache/library_demo.py
```

## How it works

1. The middleware sees a POST to a known model endpoint (`endpoints` in
   `data/cache/config.yaml`: `/api/chat`, `/api/generate`,
   `/v1/chat/completions`, `/v1/completions`, `/ask-text`, `/chat`).
2. `should_cache` decides whether this request is allowed to use the cache.
3. The key is the sha256 of canonical JSON of the route name, model,
   normalized prompt or messages, and a hash of the answer-changing parameters
   (temperature, top_p, top_k, max_tokens, seed, stop, system prompt, response
   format, penalties, `options`). The `stream` flag is ignored and key order
   does not matter.
4. Normalization: Unicode NFC, whitespace collapsed, ends trimmed. Chat
   messages keep role and content order. Different route names never collide,
   so two backends with the same model name stay separate.
5. On a hit the stored body is returned with `x-cache: HIT`. On a miss the
   upstream is called and a 200 JSON reply is stored (`x-cache: MISS`). A
   skipped request is tagged `x-cache: BYPASS(<reason>)`.

Entries expire after `ttl_seconds`. When the file passes `max_mb` or
`max_entries`, the least recently used entries are evicted first.
Hits, misses, bypasses by reason, evictions, and expiries are counted in the
same sqlite file, so they survive restarts.

## What is never cached, and why

| Reason tag | Skipped when | Why |
|------------|--------------|-----|
| `temperature` / `no-temperature` | temperature above `temperature_max` (0.2), or missing | Hot sampling is meant to vary. A missing value is unknown, so it is treated as not low unless you set `default_temperature`. |
| `tools` | `tools`, `functions`, `tool_choice` | Tool calls have side effects and depend on outside state. |
| `stream` | `stream: true` | A stream cannot be replayed as one JSON body. Set `cache_streams: true` only if your proxy buffers the whole body. |
| `images` | image parts or `images` | Large, private, and not covered by the text key. |
| `cache-control` / `header-off` / `field-off` | `cache-control: no-store`, `x-m-cache: off`, or `"cache": false` | The caller said no. |
| `personal-marker` | whole-word match on `markers` (today, now, current, latest, price, weather, my account, my password, address, phone, schedule, remind) | Personal or time-sensitive answers go stale or leak. |
| `personal-pattern` | email address, card-like or phone-like digits | Private data should not sit in a cache file. |
| `route` / `model` | listed in `never_cache_routes` / `never_cache_models` | Operator choice. |
| `upstream-error` / `response-not-json` | upstream status other than 200, or non-JSON body | Errors and partial replies must never be replayed. |

## Library use

```python
from sovereign_m.cache import ResponseCache, make_key
from sovereign_m.cache.policy import should_cache

cache = ResponseCache("<CACHE_DIR>/responses.sqlite", ttl_seconds=3600,
                      max_bytes=64 * 1024 * 1024, max_entries=5000)
request = {"model": "m1", "temperature": 0,
           "messages": [{"role": "user", "content": "Explain hash maps."}]}
ok, reason = should_cache(request, headers={})
if ok:
    key = make_key(request, route="local")
    hit = cache.get(key)               # (body_bytes, content_type) or None
    if hit is None:
        cache.put(key, b'{"reply": "..."}', "application/json")
print(cache.stats())
```

Methods: `get`, `put`, `invalidate`, `clear`, `purge_expired`, `stats`, `close`.
The object is thread-safe. The `clock` argument is injectable for tests.

## Plugging into the gateway

Both sides follow a duck-typed contract and neither imports the other. A
request has `method`, `path`, `route`, `client_id`, `headers` (lower-case keys),
`body`. A response has `status`, `headers`, `body`. A middleware has
`handle(request, call_next)`. In the gateway config:

```yaml
middleware: ["sovereign_m.cache.middleware:make_middleware"]
```

The factory is called with the config dict, so keys such as `ttl_seconds` or
`db` can be passed through.

## Privacy

- The cache file holds model outputs. It is created with mode 0600, and so
  are its WAL sidecar files. Its default home is `<workspace>/cache/`
  (`SOVEREIGN_M_WORKSPACE`, else `var/`, which is gitignored).
- Only the response body and content-type are stored. The request, its
  headers, and any auth header are never written.
- To wipe it: `python -m sovereign_m.cache clear`, or delete the file and its
  `-wal` and `-shm` neighbors. `purge` only drops expired entries.
- Prompts that look personal are skipped, but a pattern list is not a
  guarantee. Add markers to the config for your own situation, or send
  `x-m-cache: off` for a sensitive call.
