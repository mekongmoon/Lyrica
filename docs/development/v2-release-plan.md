# v2 Stable Release Plan

**Status:** Draft for review · **Target:** `v2.0.0` · **Based on:** `v2-beta-rollout` (`2.0.0b1`, 2026-11-04)

This page decides which v2-beta features ship in the stable `v2.0.0` release, what has to happen first, and how the work is tracked.

---

## 1. Where v2-beta stands

v2-beta lives on the upstream branch [`v2-beta-rollout`](https://github.com/Wilooper/Lyrica/tree/v2-beta-rollout) as a single commit (`2053754`, about 3,600 lines). It adds a lean `/v2` API next to the unchanged v1 `/lyrics/` endpoint, plus a new `src/core/` layer for verification, caching and provider safety. Its own [release notes](https://github.com/Wilooper/Lyrica/blob/v2-beta-rollout/docs/release-notes/v2-beta-rollout.md) report 46 tests passing.

Two facts shape this plan.

**The beta was built on the 1.5 code, before the current `main`.** Since then `main` has moved a long way:

| Change on `main` | Effect on v2 |
|---|---|
| Routes split into blueprints (`src/routes/`), `run_async` moved to `src/async_bridge.py` | The beta's 446-line change to `src/router.py` cannot be merged as-is. It must be ported. |
| CI, integration workflow, route-map and rate-limit tests | v2 gets CI coverage once ported. |
| Dependencies updated (Gunicorn 27, Flask-Limiter 4, limits 5, …) | The beta's code was tested against the old versions. |
| Dead fetchers deleted | The beta's "disabled" list is partly obsolete. |

**Both word/syllable providers were broken in the beta.** v2 routes word and syllable requests only to `lrcmux` and `apple_music`, and both had bugs that are fixed on `main` but still present on the beta branch:

- **lrcmux**: every request sent `sources=musixmatch`, a provider lrcmux no longer offers, so every call failed with HTTP 400. Fixed on `main`.
- **Apple Music**: `lxml` was missing from `requirements.txt` (the fetcher could not load), and word/syllable conversion raised `KeyError: 'leading_space'`. Fixed on `main`.

So the beta's main [known limitation](https://github.com/Wilooper/Lyrica/blob/v2-beta-rollout/docs/release-notes/v2-beta-rollout.md#known-limitations), that word requests fall back to line level and strict word requests return `tier_unavailable`, was most likely caused by these bugs rather than by the providers. **The word and syllable tiers, v2's headline feature, have not yet been verified working.**

---

## 2. Feature decisions for v2.0.0

| v2-beta feature | Decision | Notes |
|---|---|---|
| `GET /v2/lyrics`: lean `[start_ms, text]` lines, `tier`, `allow_lower`, `lite`, `fields` | **Ship** | Contract must be frozen first (blocker 3). |
| `ETag` / `304 Not Modified` | **Ship** | |
| `GET /v2/capabilities` | **Ship** | |
| `GET /v2/health` (breakers, quotas, latency, cache stats) | **Ship, exposure to decide** | Currently public. See blocker 4. |
| Response verifier (rejects wrong song, HTML, boilerplate) | **Ship** | |
| Canonical raw cache + derived tier views, `CACHE_VERSION=v6` | **Ship** | Verify with Redis under Gunicorn (blocker 7). |
| Single-flight request coalescing | **Ship** | |
| Negative cache for misses | **Ship** | |
| Provider controls: token bucket, circuit breaker, bulkhead, metrics, `LYRICA_DISABLED_PROVIDERS` | **Ship** | |
| Word/syllable routing restricted to `lrcmux` + `apple_music` | **Ship after verification** | Blocker 2. |
| Musixmatch only loaded when `MUSIXMATCH_TOKEN` is set | **Ship** | Matches current docs; avoids a fetcher that skips every request. |
| `lrclib_fetcher.py` rewrite | **Ship** | Comes with the port; covered by the beta's tests. |
| Translation / romanization on `/v2/lyrics` | **Ship** | Optional; needs a Groq key. |
| YouTube Music disabled | **Decide** | `main` still enables it, with ytmusicapi updated. See blocker 5. |
| Compression opt-in only (`LYRICA_COMPRESS=true`) | **Decide** | Also changes v1 behaviour: `main` always compresses. See blocker 6. |
| `rapidfuzz` optional acceleration | **Defer to 2.x** | Falls back to `difflib`. Not needed for correctness. |

v1 `/lyrics/` stays available and unchanged in v2.0.0.

---

## 3. Release blockers

Each blocker becomes a GitHub issue in the `v2.0.0` milestone. `v2.0.0` ships when all are closed.

### Engineering

1. **Port v2-beta onto current `main`.** Move the beta's `src/core/` modules, `/v2/*` routes (as a new `src/routes/v2.py` blueprint), cache, fetch-controller and lrclib changes onto `main`. Keep every fix already on `main`. Bring over `Test/test_v2_core.py` and get it passing in CI. Update `Test/test_route_map.py` with the new routes.
2. **Verify word and syllable tiers end to end.** With the fixed lrcmux and Apple Music, confirm `/v2/lyrics?tier=word` returns word timing for songs that have it, and `allow_lower=false` only returns `tier_unavailable` when no provider has the tier. Add a live integration test for word tier (lrcmux) and, when a token is available, for syllable tier (Apple Music).
3. **Freeze and document the v2 response contract.** Final field names, nesting for word/syllable lines, error envelope (`{"err": ...}`), status codes and `ETag` rules. Add contract tests that fail on any change. After `v2.0.0`, breaking changes require `v3`.
4. **Settle the `/v2` namespace and what `/v2/health` exposes.** The admin endpoints `/v2/proxy/*` already use the `/v2` prefix, and `/v2/health` publicly reports provider breaker state and quotas. Decide whether to move the proxy endpoints (for example to `/admin/proxy/*`, keeping aliases) and whether `/v2/health` should be trimmed or admin-key protected.
5. **Decide YouTube Music's status for v2.0.0.** Either keep it enabled (as on `main`, with updated ytmusicapi) and verify it, or ship disabled as in the beta and track the rebuild for 2.x.
6. **Decide the compression default.** The beta makes compression opt-in, which also changes v1 responses. Pick one default for both versions and document it.
7. **Benchmark under production settings.** The beta's HTTP numbers come from Flask's development server. Measure `/v2/lyrics` and `/lyrics/` under Gunicorn `gthread` with Redis, using `gunicorn_production.py`, and record the results in the release notes.

### Release

8. **Release mechanics.** Set the version to `2.0.0` (`pyproject.toml`, `src/__init__.py`), write release notes and an upgrade guide (cache `v6` rebuild, new env vars), publish the Docker image with `latest` moved, and state the v1 support policy (for example, v1 kept for at least one more major version).

---

## 4. Exit criteria for v2.0.0

- All eight blocker issues closed.
- CI green, including the ported v2 tests and the contract tests.
- Integration workflow green for line and word tiers.
- Production benchmark recorded.
- v1 `/lyrics/` passes its existing tests unchanged.

---

## 5. Tracking

After this plan is merged:

- Create the GitHub milestone **`v2.0.0`**.
- Open one issue per blocker above, labelled `release-blocker` (and `decision` for blockers 4–6), each with its acceptance criteria.
- Link the issues from this page.
