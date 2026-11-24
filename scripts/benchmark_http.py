"""HTTP load benchmark for a running Lyrica server (#18).

Used by .github/workflows/benchmark.yml against Gunicorn + Redis, but works
against any deployment:

    python scripts/benchmark_http.py --base-url http://127.0.0.1:7860 \
        --requests 2000 --concurrency 32 --json results.json

Each scenario is requested once to warm the cache (that cold request goes to
the live providers and its latency is reported separately), then hit
``--requests`` times with ``--concurrency`` requests in flight. Every request
must return the expected status, otherwise the run fails.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import statistics
import sys
import time

import httpx

# name, path, expected status after warm-up, extra headers (filled at runtime)
SCENARIOS = [
    ("health", "/health", 200),
    ("v1 /lyrics/ cached line", "/lyrics/?artist=Coldplay&song=Yellow&timestamps=true", 200),
    ("v2 /v2/lyrics cached line", "/v2/lyrics?artist=Coldplay&song=Yellow&tier=line", 200),
    ("v2 /v2/lyrics cached word",
     "/v2/lyrics?artist=Rick Astley&song=Never Gonna Give You Up&tier=word&allow_lower=false", 200),
    ("v2 /v2/lyrics 304 (If-None-Match)", "/v2/lyrics?artist=Coldplay&song=Yellow&tier=line", 304),
]


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, max(0, round(pct / 100 * (len(sorted_values) - 1))))
    return sorted_values[index]


async def _warm(client: httpx.AsyncClient, path: str) -> tuple[float, httpx.Response]:
    started = time.perf_counter()
    response = await client.get(path)
    return (time.perf_counter() - started) * 1000, response


async def _load(client, path, headers, expected, total, concurrency) -> dict:
    latencies: list[float] = []
    failures: list[str] = []
    counter = iter(range(total))

    async def worker():
        for _ in counter:
            started = time.perf_counter()
            response = await client.get(path, headers=headers)
            latencies.append((time.perf_counter() - started) * 1000)
            if response.status_code != expected:
                failures.append(f"{response.status_code}: {response.text[:120]}")

    started = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(concurrency)))
    elapsed = time.perf_counter() - started

    latencies.sort()
    return {
        "requests": total,
        "concurrency": concurrency,
        "failures": len(failures),
        "first_failure": failures[0] if failures else None,
        "rps": round(total / elapsed, 1),
        "p50_ms": round(_percentile(latencies, 50), 2),
        "p95_ms": round(_percentile(latencies, 95), 2),
        "p99_ms": round(_percentile(latencies, 99), 2),
        "mean_ms": round(statistics.fmean(latencies), 2),
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:7860")
    parser.add_argument("--requests", type=int, default=2000)
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--json", dest="json_path")
    args = parser.parse_args()

    limits = httpx.Limits(max_connections=args.concurrency, max_keepalive_connections=args.concurrency)
    timeout = httpx.Timeout(120.0)
    results = []
    async with httpx.AsyncClient(base_url=args.base_url, limits=limits, timeout=timeout) as client:
        cache = (await client.get("/cache/stats")).json()
        etags: dict[str, str] = {}

        for name, path, expected in SCENARIOS:
            headers = {}
            if expected == 304:
                headers["If-None-Match"] = etags[path]
                cold_ms = None
            else:
                cold_ms, warm = await _warm(client, path)
                if warm.status_code != 200:
                    print(f"Warm-up failed for {name}: {warm.status_code} {warm.text[:200]}")
                    return 1
                if "/v2/" in path and "tier=word" in path and warm.json().get("tier") != "word":
                    print(f"Warm-up for {name} did not return word timing: {warm.text[:200]}")
                    return 1
                if "ETag" in warm.headers:
                    etags[path] = warm.headers["ETag"]

            stats = await _load(client, path, headers, expected, args.requests, args.concurrency)
            stats.update({"scenario": name, "path": path, "cold_ms": None if cold_ms is None else round(cold_ms)})
            results.append(stats)
            print(f"{name}: {stats['rps']} req/s, p50 {stats['p50_ms']} ms, "
                  f"p95 {stats['p95_ms']} ms, p99 {stats['p99_ms']} ms, failures {stats['failures']}")

    report = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cache_backend": cache.get("backend"),
        "results": results,
    }
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)

    print()
    print(f"Cache backend: {report['cache_backend']}")
    print()
    print("| Scenario | Req/s | p50 ms | p95 ms | p99 ms | Failures | Cold (first) ms |")
    print("|---|---:|---:|---:|---:|---:|---:|")
    for row in results:
        cold = "" if row["cold_ms"] is None else row["cold_ms"]
        print(f"| {row['scenario']} | {row['rps']} | {row['p50_ms']} | {row['p95_ms']} | "
              f"{row['p99_ms']} | {row['failures']} | {cold} |")

    return 1 if any(row["failures"] for row in results) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
