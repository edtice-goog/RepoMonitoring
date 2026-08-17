"""Redis-backed cache for every recreatable external result.

One rule for the whole pipeline: no external endpoint (Black Duck, Claude, GitHub)
is hit twice for the same input. `cached(namespace, key, producer)` returns the
stored value if present, else runs `producer()` once and stores it. A warm cache
therefore recreates a project with ZERO external calls; a KB-added component is a
cache miss that fetches exactly once.

Redis persists to disk (AOF + RDB, see infra/redis.conf), so the cache survives
restarts — losing the process never loses the cache. Entries are a durable store,
not evictable cache (maxmemory-policy noeviction).

`STATS` counts producer runs (= real external calls) per namespace, so tests can
assert "0 external calls on a warm recreate".
"""

import hashlib
import json
import os

import redis

REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6380/0")
KEY_PREFIX = "repomon"

_client = None

# producer runs (real external calls) and cache hits, per namespace
STATS = {"miss": {}, "hit": {}}


def client() -> "redis.Redis":
    global _client
    if _client is None:
        _client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    return _client


def reset_stats() -> None:
    STATS["miss"].clear()
    STATS["hit"].clear()


def external_calls() -> int:
    """Total producer runs since the last reset (= external calls made)."""
    return sum(STATS["miss"].values())


def _key(namespace: str, key_obj) -> str:
    raw = json.dumps(key_obj, sort_keys=True, default=str)
    return f"{KEY_PREFIX}:{namespace}:{hashlib.sha256(raw.encode()).hexdigest()[:32]}"


def cached(namespace: str, key_obj, producer, ttl=None, refresh=False):
    """Return cached value for (namespace, key_obj), else run producer() once.

    producer() must return a JSON-serializable value.
    refresh=True forces a re-run (used for the SBoM and commit-list, which move).
    """
    r = client()
    k = _key(namespace, key_obj)
    if not refresh:
        hit = r.get(k)
        if hit is not None:
            STATS["hit"][namespace] = STATS["hit"].get(namespace, 0) + 1
            return json.loads(hit)
    value = producer()
    STATS["miss"][namespace] = STATS["miss"].get(namespace, 0) + 1
    r.set(k, json.dumps(value), ex=ttl)
    return value


def get_if_present(namespace: str, key_obj):
    """Peek without running a producer (None if absent). Used by cache-only paths."""
    hit = client().get(_key(namespace, key_obj))
    return json.loads(hit) if hit is not None else None


def put(namespace: str, key_obj, value, ttl=None) -> None:
    client().set(_key(namespace, key_obj), json.dumps(value), ex=ttl)


def cached_many(namespace: str, keys, batch_producer, refresh=False):
    """Per-key cache with a BATCHED producer — the key to per-component granularity.

    Each key is looked up individually (a cache hit per known key); the misses are
    handed to `batch_producer(missing_keys) -> {key: value}` in ONE call. So when a
    KB adds a component, only that component's key misses and is fetched — the rest
    stay warm. Counts as a single external call for the batch.

    keys must be JSON-serializable (tuples/strings). Returns {key: value}.
    """
    r = client()
    out, missing = {}, []
    for key in keys:
        hit = None if refresh else r.get(_key(namespace, key))
        if hit is not None:
            STATS["hit"][namespace] = STATS["hit"].get(namespace, 0) + 1
            out[key] = json.loads(hit)
        else:
            missing.append(key)
    if missing:
        produced = batch_producer(missing)
        STATS["miss"][namespace] = STATS["miss"].get(namespace, 0) + 1
        for key, value in produced.items():
            r.set(_key(namespace, key), json.dumps(value))
            out[key] = value
    return out
