# Multi-Tier Caching Architecture

Lyrica employs a 3-tier caching system designed to minimize external provider queries, eliminate rate limiting on third-party APIs, and deliver microsecond-level response times.

---

## The 3 Cache Tiers

```mermaid
graph TD
    Query[Incoming Request] --> Key["Generate SHA-256 Key (v6)"]
    Key --> L1{"L1: In-Memory LRU<br/>(Capacity: 5000 in prod)"}

    L1 -- Hit (~0.0003 ms) --> Return[Return Response]
    L1 -- Miss --> L2{"L2: Redis Distributed Cache<br/>(REDIS_URL)"}

    L2 -- Hit (~0.5 ms) --> PromoteL1["Promote into L1<br/>(12h TTL in prod)"]
    PromoteL1 --> Return

    L2 -- Miss --> L3{"L3: Disk JSON Cache<br/>(cache_data/)"}

    L3 -- Hit (~1.5 ms) --> PromoteL2["Promote into L2 & L1"]
    PromoteL2 --> Return

    L3 -- Miss --> Network["Fetch from Providers"]
    Network --> SaveAll["Save into L1 + L2 + L3"]
    SaveAll --> Return
```

---

## Tier Specifications

| Tier | Technology | Latency | Storage Lifetime | Purpose |
|:---:|---|:---:|---|---|
| **L1** | In-Memory LRU Dictionary with threading locks | **~0.0003 ms** (0.3 µs) | 12 hours (production) / 1 hour (dev) | Ultra-hot items, microsecond repeat hits |
| **L2** | Redis (via `redis-py` + `hiredis` C parser) | **~0.5 ms** | Configurable via `CACHE_TTL` (default 7 days in Docker) | Shared across multiple Gunicorn workers & containers |
| **L3** | Filesystem JSON storage (`cache_data/`) | **~1.5 ms** | Persistent across process restarts | Cold fallback when Redis is absent or offline |

---

## Cache Key Formulation (`CACHE_VERSION = "v6"`)

Cache keys are deterministic **SHA-256 hashes** computed over a canonical JSON object. There are two kinds of entry:

| Kind | Key function | Contains | Key ignores |
|---|---|---|---|
| **raw** | `make_lyrics_cache_key()` | The richest result fetched for a song, plus its tier (`plain`, `line`, `word`, `syllable`) | All view flags: one song, one raw entry |
| **view** | `make_view_cache_key()` (v1's `make_cache_key()` is an alias) | A fully rendered response, plus its pre-serialized JSON body | `fast` (it changes latency, not content) |

```python
# view key payload
payload = {
    "v": "v6",
    "kind": "view",
    "artist": "coldplay",   # normalize_identity(): lower-case, whitespace collapsed,
    "song": "yellow",       # edition noise like "(Remastered)" or "- Live" removed
    "sequence": "",         # default provider order normalizes to ""
    "pass": False,
    "timestamps": True,
    "mood": False,
    "metadata": False,
    "translate": False,
    "romanize": False,
    "language": "en",
    "word_level": True,
    "syllabus": False,
}
```

This guarantees:
1. **Collision Immunity**: Changing any content flag (`timestamps`, `word_level`, `syllabus`, `translate`, `language`, …) produces a different view key.
2. **Shared Song Identity**: `"The  Beatles"` / `"the beatles"` and `"Hey Jude"` / `"Hey Jude (Remastered)"` map to the same key.
3. **Filesystem Safety**: Hashes are hex strings, avoiding invalid directory characters across operating systems.
4. **Redis Namespace Isolation**: Keys are stored in Redis under the `lyrica:lyrics:<hash>` namespace.

Bumping `CACHE_VERSION` from `v5` to `v6` changes every key, so entries written by older versions are simply never read again and expire on their own.

### Negative cache

Misses (no provider had the song) can be remembered for `LYRICA_NEGATIVE_TTL` seconds (default 3600) by `src/core/negative_cache.py`, in memory or Redis. `clear_cache()` clears it too, and `cache_stats()` reports it under `negative`.

---

## Translation Cache

LLM inference via Groq is an expensive operation in terms of tokens and latency. Therefore, translation and romanization outputs are cached in an independent, specialized cache tier (`src/translation_cache.py`):
- Redis Namespace: `lyrica:trans:<hash>`
- Key Parameters: `artist`, `song`, `source`, `language`, `translate`, `romanize`, `has_timestamps`

---

## Cache Promotion Logic

When an item misses L1 memory but hits in L2 Redis or L3 Disk, Lyrica automatically **promotes** the item into L1 memory for subsequent requests. In production mode (`LYRICA_ENV=production`), the promotion TTL is set to 12 hours (`43200` seconds) because lyrics are immutable content.

---

## Performance Benchmark

Results measured under high-concurrency stress test (`Test/test_performance.py`):

- **L1 In-Memory LRU Throughput**: **89,182 requests/second**
- **Median (p50) Latency**: **0.0003 ms**
- **99th Percentile (p99) Latency**: **0.0011 ms**
- **Error Rate**: 0.0%
