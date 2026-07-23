"""
Scaling experiment: runs the same load test (load_test.py's run_load_test())
at several NUM_WORKERS values, changing only the worker-pool size each time,
to find where throughput stops scaling roughly linearly with worker count —
that point reveals the gateway's real bottleneck (Redis connections, event
loop saturation, etc.), not the queue/worker pool itself.

Requires Docker Compose CLI on PATH. Run from the repo root:

    python scaling_test.py

This recreates the llm-gateway container once per worker count (NUM_WORKERS
is read from the environment at process startup in app/main.py, so it can't
be changed on a running container) and flushes Redis between runs so token
buckets / prior run state don't leak into the next measurement.
"""

import asyncio
import subprocess
import sys
import time

import httpx

from load_test import GATEWAY_URL, print_report, run_load_test

WORKER_COUNTS = [4, 8, 16, 32]
COMPOSE_FILES = ["-f", "docker-compose.yml", "-f", "docker-compose.test.yml"]


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def recreate_gateway(num_workers: int) -> None:
    import os

    env = {**os.environ, "NUM_WORKERS": str(num_workers)}
    subprocess.run(
        ["docker", "compose", *COMPOSE_FILES, "up", "-d", "--build", "llm-gateway", "redis"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def flush_redis() -> None:
    run(["docker", "compose", *COMPOSE_FILES, "exec", "redis", "redis-cli", "FLUSHALL"])


async def wait_for_gateway(timeout_seconds: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error = None
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            try:
                response = await client.get(f"{GATEWAY_URL}/healthz", timeout=2.0)
                if response.status_code == 200:
                    return
            except httpx.HTTPError as e:
                last_error = e
            await asyncio.sleep(0.5)
    raise RuntimeError(f"gateway never became ready: {last_error}")


async def main():
    summary = []
    for num_workers in WORKER_COUNTS:
        print(f"\n{'=' * 60}")
        print(f"NUM_WORKERS = {num_workers}")
        print("=" * 60)

        recreate_gateway(num_workers)
        await wait_for_gateway()
        flush_redis()

        results = await run_load_test()
        print_report(results)
        summary.append({"num_workers": num_workers, **results})

    print(f"\n{'=' * 60}")
    print("SCALING SUMMARY")
    print("=" * 60)
    print(f"{'workers':>8}  {'throughput (req/s)':>20}  {'p50 (ms)':>10}  {'p95 (ms)':>10}")
    baseline_throughput = summary[0]["throughput"]
    for row in summary:
        p50 = row["success_latencies"][len(row["success_latencies"]) // 2] * 1000 if row["success_latencies"] else 0
        p95_idx = min(int(len(row["success_latencies"]) * 0.95), len(row["success_latencies"]) - 1)
        p95 = row["success_latencies"][p95_idx] * 1000 if row["success_latencies"] else 0
        scaling_factor = row["throughput"] / baseline_throughput
        print(
            f"{row['num_workers']:>8}  {row['throughput']:>15.1f} ({scaling_factor:.2f}x)  "
            f"{p50:>10.1f}  {p95:>10.1f}"
        )


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())
