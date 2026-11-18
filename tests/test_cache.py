import json
import stat
import threading
from types import SimpleNamespace

import pytest

from sovereign_m.cache.cli import main as cache_main, run_demo
from sovereign_m.cache.config import load_config
from sovereign_m.cache.middleware import make_middleware
from sovereign_m.cache.policy import Policy, should_cache
from sovereign_m.cache.store import ResponseCache, make_key
from sovereign_m.cli import main as root_main


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def chat(text="Explain hash maps.", **extra):
    body = {"model": "m1", "temperature": 0, "messages": [{"role": "user", "content": text}]}
    body.update(extra)
    return body


# ---- keys --------------------------------------------------------------

def test_key_normalization_equal():
    a = make_key(chat("Explain   hash\n maps. "))
    b = make_key(chat("  Explain hash maps."))
    assert a == b
    # NFC: composed and decomposed forms match
    assert make_key(chat("café")) == make_key(chat("café"))
    # stream flag and key order are ignored
    one = {"model": "m1", "prompt": "hi", "temperature": 0, "top_p": 1}
    two = {"top_p": 1, "temperature": 0, "prompt": "hi", "model": "m1", "stream": False}
    assert make_key(one) == make_key(two)


def test_key_inequality():
    base = make_key(chat())
    assert base != make_key(chat("Explain queues."))
    assert base != make_key(chat(temperature=0.1))
    assert base != make_key(chat(seed=3))
    assert base != make_key(chat(), route="other")
    assert base != make_key({**chat(), "model": "m2"})
    assert base != make_key(chat(stop=["x"]))
    two = {"model": "m1", "temperature": 0, "messages": [
        {"role": "system", "content": "a"}, {"role": "user", "content": "b"}]}
    swapped = {"model": "m1", "temperature": 0, "messages": [
        {"role": "user", "content": "b"}, {"role": "system", "content": "a"}]}
    assert make_key(two) != make_key(swapped)


# ---- store -------------------------------------------------------------

def test_ttl_expiry_with_injected_clock(tmp_path):
    clock = Clock()
    with ResponseCache(tmp_path / "c" / "db.sqlite", ttl_seconds=10, clock=clock) as cache:
        cache.put("k", b"{}")
        assert cache.get("k") is not None
        clock.t += 11
        assert cache.get("k") is None
        assert cache.stats()["expired"] == 1
        cache.put("k2", b"{}")
        clock.t += 11
        assert cache.purge_expired() == 1
        assert cache.stats()["entries"] == 0


def test_lru_eviction_by_entries(tmp_path):
    clock = Clock()
    with ResponseCache(tmp_path / "db.sqlite", max_entries=2, clock=clock) as cache:
        cache.put("a", b"1")
        clock.t += 1
        cache.put("b", b"2")
        clock.t += 1
        assert cache.get("a") is not None  # a is now newer than b
        clock.t += 1
        cache.put("c", b"3")
        assert cache.get("b") is None
        assert cache.get("a") is not None and cache.get("c") is not None
        assert cache.stats()["evictions"] == 1


def test_lru_eviction_by_bytes(tmp_path):
    clock = Clock()
    with ResponseCache(tmp_path / "db.sqlite", max_bytes=10, clock=clock) as cache:
        for i, key in enumerate("abc"):
            clock.t += 1
            cache.put(key, b"xxxx")
        stats = cache.stats()
        assert stats["bytes"] <= 10 and stats["entries"] == 2
        assert cache.get("a") is None
        assert cache.put("huge", b"x" * 11) is False


def test_invalidate_and_clear(tmp_path):
    with ResponseCache(tmp_path / "db.sqlite") as cache:
        cache.put("a", b"1")
        cache.put("b", b"2")
        assert cache.invalidate("a") and not cache.invalidate("a")
        assert cache.clear() == 1


def test_stats_persist_across_reopen(tmp_path):
    path = tmp_path / "db.sqlite"
    with ResponseCache(path) as cache:
        cache.put("a", b"1")
        cache.get("a")
        cache.get("nope")
        cache.record_bypass("temperature")
    with ResponseCache(path) as cache:
        s = cache.stats()
    assert (s["hits"], s["misses"], s["entries"]) == (1, 1, 1)
    assert s["bypasses"] == {"temperature": 1}
    assert s["hit_rate"] == 0.5


def test_db_file_mode_0600(tmp_path):
    path = tmp_path / "deep" / "dir" / "db.sqlite"
    with ResponseCache(path) as cache:
        cache.put("a", b"1")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        for suffix in ("-wal", "-shm"):
            side = path.with_name(path.name + suffix)
            if side.exists():
                assert stat.S_IMODE(side.stat().st_mode) == 0o600


def test_concurrency_smoke(tmp_path):
    with ResponseCache(tmp_path / "db.sqlite", max_entries=50) as cache:
        errors = []

        def work(n):
            try:
                for i in range(40):
                    key = f"k{(n * 7 + i) % 30}"
                    cache.put(key, b"v" * 10)
                    cache.get(key)
                    cache.stats()
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert cache.stats()["entries"] <= 50


# ---- policy ------------------------------------------------------------

def test_policy_allows_plain_low_temperature():
    assert should_cache(chat(), {}) == (True, "ok")


@pytest.mark.parametrize("req,headers,reason", [
    (chat(temperature=0.7), {}, "temperature"),
    ({"model": "m", "prompt": "hi"}, {}, "no-temperature"),
    (chat(tools=[{"type": "function"}]), {}, "tools"),
    (chat(functions=[{"name": "f"}]), {}, "tools"),
    (chat(tool_choice="auto"), {}, "tools"),
    (chat(stream=True), {}, "stream"),
    (chat(), {"Cache-Control": "no-store"}, "cache-control"),
    (chat(), {"x-m-cache": "off"}, "header-off"),
    (chat(cache=False), {}, "field-off"),
    (chat("what is the weather"), {}, "personal-marker"),
    (chat("remind me to call"), {}, "personal-marker"),
    (chat("write to bob" + "@" + "example.org"), {}, "personal-pattern"),
    (chat("call 555-123-4567 now please"), {}, "personal-marker"),
    (chat("call 555-123-4567"), {}, "personal-pattern"),
    (chat("card 4111 1111 1111 1111"), {}, "personal-pattern"),
    (chat(images=["abc"]), {}, "images"),
    ({"model": "m", "temperature": 0, "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:x"}}]}]}, {}, "images"),
])
def test_policy_opt_outs(req, headers, reason):
    ok, why = should_cache(req, headers)
    assert (ok, why) == (False, reason)


def test_policy_config_knobs():
    assert should_cache({"model": "m", "prompt": "hi"}, {}, {"default_temperature": 0})[0]
    assert not should_cache(chat(temperature=0.3), {})[0]
    assert should_cache(chat(temperature=0.3), {}, {"temperature_max": 0.5})[0]
    assert should_cache(chat(stream=True), {}, {"cache_streams": True})[0]
    assert should_cache(chat(), {}, {"never_cache_models": ["m1"]}) == (False, "model")
    assert Policy({"never_cache_routes": ["r"]}).should_cache(chat(), {}, "r") == (False, "route")


def test_only_200_json_is_cacheable():
    p = Policy()
    assert p.cacheable_response(200, "application/json", b"{}")
    assert not p.cacheable_response(500, "application/json", b"{}")
    assert not p.cacheable_response(200, "text/plain", b"{}")


def test_default_config_file_loads():
    cfg = load_config()
    assert cfg["temperature_max"] == 0.2
    assert "/chat" in cfg["endpoints"]
    assert should_cache(chat("what is the weather"), {}, cfg)[0] is False


# ---- middleware --------------------------------------------------------

def make_request(payload, path="/v1/chat/completions", route="a", headers=None, method="POST", raw=None):
    return SimpleNamespace(
        method=method, path=path, route=route, client_id="c1",
        headers=headers or {}, body=raw if raw is not None else json.dumps(payload).encode(),
    )


class Upstream:
    def __init__(self, status=200, ctype="application/json"):
        self.calls = 0
        self.status = status
        self.ctype = ctype

    def __call__(self, request):
        self.calls += 1
        body = json.dumps({"n": self.calls}).encode()
        return SimpleNamespace(status=self.status, headers={"content-type": self.ctype}, body=body)


@pytest.fixture
def mw(tmp_path):
    m = make_middleware({"db": str(tmp_path / "mw.sqlite")})
    yield m
    m.cache.close()


def test_middleware_miss_then_hit(mw):
    up = Upstream()
    first = mw.handle(make_request(chat()), up)
    second = mw.handle(make_request(chat("Explain  hash maps.")), up)
    assert first.headers["x-cache"] == "MISS"
    assert second.headers["x-cache"] == "HIT"
    assert second.body == first.body and second.status == 200
    assert up.calls == 1


def test_middleware_routes_do_not_collide(mw):
    up = Upstream()
    mw.handle(make_request(chat(), route="a"), up)
    other = mw.handle(make_request(chat(), route="b"), up)
    assert other.headers["x-cache"] == "MISS" and up.calls == 2


def test_middleware_bypass_reasons(mw):
    up = Upstream()
    r = mw.handle(make_request(chat(temperature=1)), up)
    assert r.headers["x-cache"] == "BYPASS(temperature)"
    r = mw.handle(make_request(chat(), headers={"x-m-cache": "off"}), up)
    assert r.headers["x-cache"] == "BYPASS(header-off)"
    r = mw.handle(make_request(None, raw=b"not json"), up)
    assert r.headers["x-cache"] == "BYPASS(bad-json)"
    assert up.calls == 3
    assert mw.cache.stats()["bypass_total"] == 3
    assert mw.cache.stats()["entries"] == 0


def test_middleware_ignores_other_paths_and_methods(mw):
    up = Upstream()
    r = mw.handle(make_request(chat(), path="/health"), up)
    assert "x-cache" not in r.headers
    r = mw.handle(make_request(chat(), method="GET"), up)
    assert "x-cache" not in r.headers
    r = mw.handle(make_request(chat(), path="/chat?x=1"), up)
    assert r.headers["x-cache"] == "MISS"


def test_middleware_never_caches_errors_or_non_json(mw):
    for status, ctype in ((500, "application/json"), (200, "text/plain")):
        up = Upstream(status=status, ctype=ctype)
        mw.handle(make_request(chat()), up)
        again = mw.handle(make_request(chat()), up)
        assert again.headers["x-cache"] == "MISS"
        assert up.calls == 2
    assert mw.cache.stats()["entries"] == 0


def test_middleware_stores_only_response(mw, tmp_path):
    secret = "bearer-" + "placeholder-token"
    up = Upstream()
    mw.handle(make_request(chat("unique prompt text"), headers={"authorization": secret}), up)
    mw.cache.close()
    raw = b"".join(p.read_bytes() for p in tmp_path.glob("mw.sqlite*"))
    assert secret.encode() not in raw
    assert b"unique prompt text" not in raw


# ---- cli ---------------------------------------------------------------

def test_cli_stats_clear_purge_demo(tmp_path, capsys):
    db = str(tmp_path / "cli.sqlite")
    assert cache_main(["stats", "--db", db]) == 0
    with ResponseCache(db) as cache:
        cache.put("a", b"1")
    assert cache_main(["stats", "--db", db]) == 0
    assert "entries     1" in capsys.readouterr().out
    assert cache_main(["purge", "--db", db]) == 0
    assert cache_main(["clear", "--db", db]) == 0
    assert "cleared 1" in capsys.readouterr().out
    assert cache_main(["bogus"]) == 2


def test_demo_prints_hit_rate_table(capsys):
    assert run_demo() == 0
    out = capsys.readouterr().out
    assert "HIT" in out and "MISS" in out and "BYPASS(temperature)" in out
    assert "hit rate" in out


def test_root_cli_registers_cache(tmp_path, capsys):
    assert root_main(["cache", "stats", "--db", str(tmp_path / "none.sqlite")]) == 0
    assert root_main(["--help"]) == 0
    assert "ask-m cache" in capsys.readouterr().out
