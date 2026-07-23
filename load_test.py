"""
Load test against MOCK_PROVIDERS — isolates the gateway's own overhead
(queueing, auth, rate limiting, circuit breaker checks) from real provider
latency, which is intentionally reduced to near-zero by the mock. Run with:

    docker compose -f docker-compose.yml -f docker-compose.test.yml \
        up -d llm-gateway redis
    python load_test.py

Requires MOCK_PROVIDERS=1 on the gateway (set via docker-compose.test.yml)
and the load-test-key-N entries in app/auth.py's TEAM_CONFIG.
"""

import asyncio
import statistics
import time

import httpx

import os

GATEWAY_URL = "http://localhost:8000"
# Overridable via env so scale_test.py can sweep concurrency levels without
# editing this file — defaults to 500 for the standalone single-run case.
TOTAL_REQUESTS = int(os.environ.get("LOAD_TEST_TOTAL_REQUESTS", "500"))
# 55 teams * CAPACITY=10 (rate_limit.py) >= TOTAL_REQUESTS, so the rate
# limiter doesn't bottleneck this — see the matching comment in app/auth.py.
LOAD_TEST_KEYS = [f"load-test-key-{i}" for i in range(1, 56)]


async def hit(client: httpx.AsyncClient, api_key: str) -> dict:
    start = time.perf_counter()
    try:
        response = await client.post(
            "/generate",
            json={"model": "gpt-4", "prompt": "load test", "max_tokens": 5},
            headers={"x-api-key": api_key},
        )
        elapsed = time.perf_counter() - start
        return {"status": response.status_code, "elapsed": elapsed}
    except httpx.HTTPError as e:
        elapsed = time.perf_counter() - start
        return {"status": None, "elapsed": elapsed, "error": str(e)}


def percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(int(len(sorted_values) * p), len(sorted_values) - 1)
    return sorted_values[index]


async def run_load_test() -> dict:
    """Runs the load test once and returns the raw results, without printing.
    Used directly by main() for a standalone run, and by scaling_test.py to
    drive the same measurement across several NUM_WORKERS values.
    """
    # httpx.AsyncClient's internal default (httpx._config.DEFAULT_LIMITS) caps
    # the pool at max_connections=100, max_keepalive_connections=20 — with
    # TOTAL_REQUESTS above that, requests queue up client-side waiting for a
    # free connection, which dominates measured latency and has nothing to do
    # with the gateway itself. max_connections must be raised for a load test
    # at this volume.
    #
    # Gotcha: constructing httpx.Limits(max_connections=N) alone resets
    # max_keepalive_connections to None (unbounded) rather than leaving it at
    # DEFAULT_LIMITS' value of 20 — and an unbounded/very large keepalive pool
    # measurably degrades performance here (measured on this setup: keepalive
    # left unbounded via this partial-Limits mistake -> ~7s total for 500
    # requests; keepalive explicitly capped at 100 -> consistently ~1.8-2.0s
    # for the same 500 requests, 3 runs). Both fields must be set explicitly.
    limits = httpx.Limits(max_connections=TOTAL_REQUESTS, max_keepalive_connections=100)
    async with httpx.AsyncClient(base_url=GATEWAY_URL, timeout=30, limits=limits) as client:
        wall_start = time.perf_counter()
        tasks = [
            hit(client, LOAD_TEST_KEYS[i % len(LOAD_TEST_KEYS)])
            for i in range(TOTAL_REQUESTS)
        ]
        results = await asyncio.gather(*tasks)
        wall_elapsed = time.perf_counter() - wall_start

    all_latencies = sorted(r["elapsed"] for r in results)
    success_latencies = sorted(r["elapsed"] for r in results if r["status"] == 200)
    statuses = [r["status"] for r in results]

    return {
        "total_requests": TOTAL_REQUESTS,
        "wall_elapsed": wall_elapsed,
        "throughput": TOTAL_REQUESTS / wall_elapsed,
        "successes": sum(1 for s in statuses if s == 200),
        "rejections_429": sum(1 for s in statuses if s == 429),
        "errors": sum(1 for s in statuses if s not in (200, 429)),
        "all_latencies": all_latencies,
        "success_latencies": success_latencies,
    }


def print_latency_block(label: str, latencies: list[float]) -> None:
    print(f"{label}:")
    if not latencies:
        print("  (no requests in this category)")
        return
    print(f"  min:  {latencies[0]*1000:.1f}ms")
    print(f"  p50:  {percentile(latencies, 0.50)*1000:.1f}ms")
    print(f"  p95:  {percentile(latencies, 0.95)*1000:.1f}ms")
    print(f"  p99:  {percentile(latencies, 0.99)*1000:.1f}ms")
    print(f"  max:  {latencies[-1]*1000:.1f}ms")
    print(f"  mean: {statistics.mean(latencies)*1000:.1f}ms")


def print_report(r: dict) -> None:
    print(f"Total requests:        {r['total_requests']}")
    print(f"Wall-clock duration:   {r['wall_elapsed']:.2f}s")
    print(f"Throughput:            {r['throughput']:.1f} req/s")
    print(f"200 OK:                {r['successes']}")
    print(f"429 rate-limited:      {r['rejections_429']}")
    print(f"Other/errors:          {r['errors']}")
    print()
    # 429s return almost instantly (a single Redis round-trip), so mixing
    # them into "all requests" latency drags percentiles down and understates
    # how long a real, fully-processed request actually takes — report both.
    print_latency_block("Latency — successful (200) requests only", r["success_latencies"])
    print()
    print_latency_block("Latency — all requests, including 429s/errors", r["all_latencies"])


async def main():
    results = await run_load_test()
    print_report(results)


if __name__ == "__main__":
    asyncio.run(main())
