"""
Horizontal scale test: measures throughput + latency for the gateway behind
an nginx round-robin LB with N replicas, versus a single instance, to produce
a defensible horizontally-scaled throughput number (replacing the single-
instance ~240 req/s figure).

Backs a resume claim, so: runs each (instance-count, concurrency) point
multiple times and reports the range, not a one-off best case. Also relies on
the separately-run cross-instance quota-enforcement check (see
scale_correctness_check() below and the manual verification in the report) —
a scale-out that breaks quota enforcement is a regression, reported as such.

Prereqs: the scale stack must be up before running the 3-instance sweep:

    docker compose -f docker-compose.yml -f docker-compose.test.yml \
        -f docker-compose.scale.yml up -d --build

and for the single-instance baseline, the plain mock stack:

    docker compose -f docker-compose.yml -f docker-compose.test.yml \
        up -d --build llm-gateway redis

This script does NOT flip the topology itself (that requires tearing down and
recreating containers, which is slow and error-prone to do mid-process) — run
it once per topology and pass --label to tag the output. See run_scale_test.sh
for the full orchestrated sequence.
"""

import argparse
import asyncio
import collections
import json
import statistics
import time
from pathlib import Path

import httpx
import redis.asyncio as redis

GATEWAY_URL = "http://localhost:8000"
REDIS_HOST = "localhost"
TEAM_KEYS = [f"load-test-key-{i}" for i in range(1, 56)]
RESULTS_PATH = Path("scale_test_results.jsonl")


async def flush_redis(redis_client: redis.Redis) -> None:
    # Portable flush via the redis client — an earlier version shelled out to
    # `docker exec ... redis-cli flushdb` with a bash-style redirect, which
    # silently failed under Windows cmd.exe ("system cannot find the path"),
    # so buckets were never actually reset between runs and every run after
    # the first measured drained-bucket 429 storms instead of real throughput.
    await redis_client.flushdb()


async def hit(client: httpx.AsyncClient, api_key: str, stream: bool) -> dict:
    start = time.perf_counter()
    try:
        if stream:
            async with client.stream(
                "POST", "/generate/stream",
                json={"model": "gpt-4", "prompt": "load", "max_tokens": 5},
                headers={"x-api-key": api_key},
            ) as r:
                status = r.status_code
                async for _ in r.aiter_raw():
                    pass
            return {"status": status, "elapsed": time.perf_counter() - start, "stream": True}
        r = await client.post(
            "/generate",
            json={"model": "gpt-4", "prompt": "load", "max_tokens": 5},
            headers={"x-api-key": api_key},
        )
        return {"status": r.status_code, "elapsed": time.perf_counter() - start, "stream": False}
    except httpx.HTTPError as e:
        return {"status": None, "elapsed": time.perf_counter() - start, "stream": stream, "error": str(e)[:120]}


async def one_load_run(total_requests: int) -> dict:
    # Same httpx.Limits fix as load_test.py: raise max_connections above the
    # request count and explicitly cap keepalive (a partial Limits() resets
    # keepalive to unbounded, which measurably degrades throughput here).
    limits = httpx.Limits(max_connections=total_requests, max_keepalive_connections=200)
    async with httpx.AsyncClient(base_url=GATEWAY_URL, timeout=30, limits=limits) as client:
        wall_start = time.perf_counter()
        # ~30% streaming mix, same as the chaos test, deterministic per index
        # so runs are comparable.
        tasks = [
            hit(client, TEAM_KEYS[i % len(TEAM_KEYS)], stream=(i % 10 < 3))
            for i in range(total_requests)
        ]
        results = await asyncio.gather(*tasks)
        wall_elapsed = time.perf_counter() - wall_start

    statuses = collections.Counter(r["status"] for r in results)
    success_latencies = sorted(r["elapsed"] for r in results if r["status"] == 200)
    all_latencies = sorted(r["elapsed"] for r in results)
    successes = statuses[200]

    def pct(vals, p):
        if not vals:
            return 0.0
        return vals[min(int(len(vals) * p), len(vals) - 1)]

    return {
        "total_requests": total_requests,
        "wall_elapsed": wall_elapsed,
        "throughput_total_rps": total_requests / wall_elapsed,
        "throughput_success_rps": successes / wall_elapsed,
        "successes": successes,
        "rate_limited_429": statuses[429],
        "errors": sum(v for k, v in statuses.items() if k not in (200, 429)),
        "p50_success_ms": pct(success_latencies, 0.50) * 1000,
        "p95_success_ms": pct(success_latencies, 0.95) * 1000,
        "p99_success_ms": pct(success_latencies, 0.99) * 1000,
        "p50_all_ms": pct(all_latencies, 0.50) * 1000,
        "p95_all_ms": pct(all_latencies, 0.95) * 1000,
    }


async def real_quota_check() -> dict:
    codes = collections.Counter()
    async with httpx.AsyncClient(base_url=GATEWAY_URL, timeout=10) as client:
        for _ in range(40):
            try:
                r = await client.post(
                    "/generate",
                    json={"model": "gpt-4", "prompt": "quota", "max_tokens": 5},
                    headers={"x-api-key": "load-test-key-1"},
                )
                codes[r.status_code] += 1
            except httpx.HTTPError:
                codes[None] += 1
    successes = codes[200]
    passed = successes <= 12  # capacity 10 + small refill slack
    return {
        "burst_status_counts": dict(codes),
        "successes_out_of_40": successes,
        "single_instance_capacity": 10,
        "passed": passed,
        "interpretation": (
            "PASS: one team capped at ~single-instance capacity across all instances (quota shared)"
            if passed else
            "FAIL: successes exceed single-instance capacity — per-instance quota bypass"
        ),
    }


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True, help="Topology label, e.g. 'single' or '3-instance'")
    parser.add_argument("--levels", type=int, nargs="+", default=[500, 1000, 1500])
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()

    async with httpx.AsyncClient(base_url=GATEWAY_URL, timeout=5) as probe:
        try:
            (await probe.get("/healthz")).raise_for_status()
        except httpx.HTTPError as e:
            raise RuntimeError(f"gateway/LB not reachable at {GATEWAY_URL}: {e}")

    redis_client = redis.Redis(host=REDIS_HOST, port=6379, decode_responses=True)

    # Quota correctness first, before buckets are spent by load runs.
    await flush_redis(redis_client)
    quota = await real_quota_check()
    print(f"\n[{args.label}] Cross-instance quota check: {quota['interpretation']} "
          f"({quota['successes_out_of_40']}/40 succeeded)")

    all_rows = []
    for level in args.levels:
        runs = []
        for rep in range(args.repeats):
            await flush_redis(redis_client)
            await asyncio.sleep(0.5)  # let buckets settle after flush
            result = await one_load_run(level)
            runs.append(result)
            print(f"[{args.label}] level={level} rep={rep+1}/{args.repeats}: "
                  f"{result['throughput_total_rps']:.0f} req/s total, "
                  f"{result['throughput_success_rps']:.0f} req/s success, "
                  f"p95(success)={result['p95_success_ms']:.0f}ms, "
                  f"429s={result['rate_limited_429']}")
        # Aggregate the range across repeats
        total_rps = [r["throughput_total_rps"] for r in runs]
        success_rps = [r["throughput_success_rps"] for r in runs]
        p95 = [r["p95_success_ms"] for r in runs]
        row = {
            "label": args.label,
            "concurrency": level,
            "repeats": args.repeats,
            "throughput_total_rps_min": min(total_rps),
            "throughput_total_rps_max": max(total_rps),
            "throughput_total_rps_mean": statistics.mean(total_rps),
            "throughput_success_rps_min": min(success_rps),
            "throughput_success_rps_max": max(success_rps),
            "throughput_success_rps_mean": statistics.mean(success_rps),
            "p95_success_ms_min": min(p95),
            "p95_success_ms_max": max(p95),
            "runs": runs,
            "quota_check": quota,
        }
        all_rows.append(row)

    with RESULTS_PATH.open("a") as f:
        for row in all_rows:
            f.write(json.dumps(row) + "\n")

    print(f"\n{'='*72}")
    print(f"SCALE TEST SUMMARY — topology: {args.label}")
    print(f"{'='*72}")
    print(f"Cross-instance quota enforcement: "
          f"{'PASS' if quota['passed'] else 'FAIL'} "
          f"({quota['successes_out_of_40']}/40 one team's burst succeeded; "
          f"single-instance capacity is {quota['single_instance_capacity']})")
    print()
    print(f"{'concurrency':>11}  {'total req/s (range)':>26}  {'success req/s (range)':>26}  {'p95 success ms':>16}")
    for row in all_rows:
        print(f"{row['concurrency']:>11}  "
              f"{row['throughput_total_rps_min']:>10.0f}-{row['throughput_total_rps_max']:<10.0f} "
              f"({row['throughput_total_rps_mean']:>5.0f})  "
              f"{row['throughput_success_rps_min']:>10.0f}-{row['throughput_success_rps_max']:<10.0f} "
              f"({row['throughput_success_rps_mean']:>5.0f})  "
              f"{row['p95_success_ms_min']:>6.0f}-{row['p95_success_ms_max']:<6.0f}")
    print()
    print("NOTE: success req/s plateaus at the rate-limiter ceiling (55 teams x")
    print("CAPACITY=10 burst = 550), NOT at the gateway's processing capacity —")
    print("total req/s (which counts 429s, each a full auth + Redis round-trip)")
    print("is the better measure of raw request-handling throughput here.")
    print(f"\nRaw per-run results appended to: {RESULTS_PATH.resolve()}")

    await redis_client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
