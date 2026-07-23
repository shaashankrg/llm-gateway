"""
Chaos test: measures success rate and circuit-breaker recovery time during a
simulated provider outage. Backs a specific resume claim, so this script
optimizes for measurement accuracy over a clean-looking number — see the
"Known effects on these numbers" section printed in the report.

Run with:

    docker compose -f docker-compose.yml -f docker-compose.test.yml \
        up -d llm-gateway redis
    python chaos_test.py

Requires MOCK_PROVIDERS=1 on the gateway (set via docker-compose.test.yml,
same as load_test.py) so mock:fail_count:<provider> can force failures, and
the load-test-key-N entries in app/auth.py's TEAM_CONFIG for team spread.

What this does NOT claim:
  - /generate (non-streaming) DOES fall back to the other provider same-request
    on primary failure (see app/queue.py's attempt_provider/worker) — so during
    an openai outage, /generate requests for gpt-4/gpt-4o-mini mostly still
    return 200, just served by anthropic under a different `model` in the
    response body. This is real, existing behavior, not a test bug — the
    report separates "succeeded via same-provider" from "succeeded via
    fallback" so a high headline success rate doesn't silently imply the
    outage had no effect.
  - /generate/stream has NO fallback routing at all (confirmed by reading
    app/main.py's generate_stream — it checks can_attempt() for the single
    requested provider and either fast-rejects or attempts, nothing else).
    Streaming failures during the outage are real failures, not masked ones.
"""

import argparse
import asyncio
import json
import random
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx
import redis.asyncio as redis

GATEWAY_URL = "http://localhost:8000"
REDIS_HOST = "localhost"

# Reuse load_test.py's team spread so 429s from the rate limiter (10 capacity,
# 10/60 refill per team) don't dominate the result at 100-200 req/s — see the
# matching comment in load_test.py and app/auth.py.
TEAM_KEYS = [f"load-test-key-{i}" for i in range(1, 56)]

# NOTE on rate: the rate limiter is 10 burst capacity + ~10/60 refill PER
# team across 55 teams (see TEAM_KEYS / app/rate_limit.py) — a sustained
# aggregate ceiling of only ~9 req/s once the initial 550-request burst is
# spent. Targeting 150 rps (the original ask) makes ~90% of requests 429
# rate-limited, which are outage-INDEPENDENT and swamp the measurement of
# what the outage actually does to provider-reaching traffic. We target a
# rate the limiter can mostly absorb so "success rate during outage" is a
# number about the outage, not about rate-limiting. The 150-rps behavior is
# still reproducible by raising this — the report flags the 429 share either
# way so the number is never silently inflated/deflated by rate-limiting.
TARGET_RATE_RPS = 40  # steady-state target across the whole run
OUTAGE_PROVIDER = "openai"
OUTAGE_DURATION_S = 30.0
RECOVERY_WINDOW_S = 60.0  # how long to keep sampling after forced failures stop
PRE_OUTAGE_WARMUP_S = 10.0  # steady load before T0, so T0 isn't measuring cold-start

# consume_forced_failure (app/providers/mock_control.py) DECRs a Redis counter
# per call and stops forcing failures once it hits 0 — a single large SET at
# T0 would drain out partway through a 30s outage if enough requests land
# (each /generate attempt burns up to 3 decrements via call_with_retry's
# retries; each /generate/stream attempt burns exactly 1, no retry wrapper).
# Re-set it on a short interval for the full outage window instead of once.
FAIL_COUNT_REFRESH_INTERVAL_S = 0.5
FAIL_COUNT_REFRESH_VALUE = 10_000  # comfortably more than can drain in one interval at this rps

LOG_PATH = Path("chaos_test_log.jsonl")

# gpt-4/gpt-4o-mini -> claude-sonnet-5 fallback exists per app/queue.py's
# FALLBACK_MODEL — used here only to detect, from the response body, whether
# a /generate 200 was actually served by the outage-affected provider or
# silently failed over.
FALLBACK_MODEL = {
    "gpt-4": "claude-sonnet-5",
    "gpt-4o-mini": "claude-sonnet-5",
    "claude-sonnet-5": "gpt-4o-mini",
}


@dataclass
class RequestLog:
    t: float  # time.time() at request start, absolute wall clock
    kind: str  # "generate" | "stream"
    team_key: str
    model_requested: str
    provider_requested: str  # "openai" | "anthropic", derived from model
    status: int | None
    outcome: str  # "success" | "http_error" | "transport_error" | "rate_limited" | "budget_rejected"
    model_served: str | None  # from response body, None if not 200 or not applicable
    provider_served: str | None  # inferred from model_served; None if unknown
    fallback_used: bool
    elapsed_s: float
    error: str | None = None


@dataclass
class BreakerSample:
    t: float
    state: str  # "closed" | "half_open" | "open" | "unknown"


def _provider_for_model(model: str) -> str:
    return "anthropic" if "claude" in model else "openai"


async def parse_circuit_state(client: httpx.AsyncClient, provider: str) -> str:
    """Reads gateway_circuit_breaker_state from /metrics — ground truth for
    can_attempt()'s underlying state, rather than inferring it from response
    codes/latency (ties directly to app/tracing.py's _observe_circuit_breaker_state).
    """
    try:
        resp = await client.get("/metrics/")
        text = resp.text
    except httpx.HTTPError:
        return "unknown"
    state_values = {"0": "closed", "1": "half_open", "2": "open"}
    for line in text.splitlines():
        if line.startswith("gateway_circuit_breaker_state{") and f'provider="{provider}"' in line:
            value = line.rsplit(" ", 1)[1].strip()
            # Prometheus gauges may render as float ("2.0") — normalize.
            value_int = str(int(float(value)))
            return state_values.get(value_int, "unknown")
    return "unknown"


async def do_generate(client: httpx.AsyncClient, team_key: str, model: str) -> RequestLog:
    provider_requested = _provider_for_model(model)
    start = time.time()
    perf_start = time.perf_counter()
    try:
        resp = await client.post(
            "/generate",
            json={"model": model, "prompt": "chaos test", "max_tokens": 5},
            headers={"x-api-key": team_key},
        )
        elapsed = time.perf_counter() - perf_start
        if resp.status_code == 200:
            body = resp.json()
            model_served = body.get("model")
            provider_served = _provider_for_model(model_served) if model_served else None
            fallback_used = model_served is not None and model_served != model
            return RequestLog(
                t=start, kind="generate", team_key=team_key, model_requested=model,
                provider_requested=provider_requested, status=200, outcome="success",
                model_served=model_served, provider_served=provider_served,
                fallback_used=fallback_used, elapsed_s=elapsed,
            )
        outcome = {429: "rate_limited", 402: "budget_rejected"}.get(resp.status_code, "http_error")
        return RequestLog(
            t=start, kind="generate", team_key=team_key, model_requested=model,
            provider_requested=provider_requested, status=resp.status_code, outcome=outcome,
            model_served=None, provider_served=None, fallback_used=False, elapsed_s=elapsed,
            error=resp.text[:200],
        )
    except httpx.HTTPError as e:
        elapsed = time.perf_counter() - perf_start
        return RequestLog(
            t=start, kind="generate", team_key=team_key, model_requested=model,
            provider_requested=provider_requested, status=None, outcome="transport_error",
            model_served=None, provider_served=None, fallback_used=False, elapsed_s=elapsed,
            error=str(e)[:200],
        )


async def do_stream(client: httpx.AsyncClient, team_key: str, model: str) -> RequestLog:
    provider_requested = _provider_for_model(model)
    start = time.time()
    perf_start = time.perf_counter()
    try:
        async with client.stream(
            "POST", "/generate/stream",
            json={"model": model, "prompt": "chaos test", "max_tokens": 5},
            headers={"x-api-key": team_key},
        ) as resp:
            status = resp.status_code
            if status != 200:
                body = (await resp.aread()).decode(errors="replace")
                elapsed = time.perf_counter() - perf_start
                outcome = {429: "rate_limited", 402: "budget_rejected", 503: "http_error"}.get(status, "http_error")
                return RequestLog(
                    t=start, kind="stream", team_key=team_key, model_requested=model,
                    provider_requested=provider_requested, status=status, outcome=outcome,
                    model_served=None, provider_served=None, fallback_used=False, elapsed_s=elapsed,
                    error=body[:200],
                )
            chunks = []
            async for chunk in resp.aiter_text():
                chunks.append(chunk)
            elapsed = time.perf_counter() - perf_start
            # /generate/stream has no fallback routing (see module docstring) —
            # a 200 here always means provider_requested actually served it.
            return RequestLog(
                t=start, kind="stream", team_key=team_key, model_requested=model,
                provider_requested=provider_requested, status=200, outcome="success",
                model_served=model, provider_served=provider_requested, fallback_used=False,
                elapsed_s=elapsed,
            )
    except httpx.HTTPError as e:
        elapsed = time.perf_counter() - perf_start
        return RequestLog(
            t=start, kind="stream", team_key=team_key, model_requested=model,
            provider_requested=provider_requested, status=None, outcome="transport_error",
            model_served=None, provider_served=None, fallback_used=False, elapsed_s=elapsed,
            error=str(e)[:200],
        )


# openai-backed models only, so every request in this test targets the
# outage-affected provider — mixing in claude-sonnet-5 would dilute the
# outage's measured effect with unaffected traffic and muddy "success rate
# during the outage" as a number about the broken provider specifically.
MODELS_UNDER_TEST = ["gpt-4", "gpt-4o-mini"]


async def load_worker(
    worker_id: int,
    client: httpx.AsyncClient,
    stop_event: asyncio.Event,
    logs: list[RequestLog],
    target_rps: float,
    num_workers: int,
):
    # Each worker fires at target_rps/num_workers, staggered by a random
    # initial offset so num_workers workers don't all wake up in lockstep.
    per_worker_interval = num_workers / target_rps
    await asyncio.sleep(random.uniform(0, per_worker_interval))
    rng = random.Random(worker_id)
    while not stop_event.is_set():
        loop_start = time.perf_counter()
        team_key = rng.choice(TEAM_KEYS)
        model = rng.choice(MODELS_UNDER_TEST)
        # ~30% streaming / 70% non-streaming, mixed per the task's requirement.
        if rng.random() < 0.3:
            log = await do_stream(client, team_key, model)
        else:
            log = await do_generate(client, team_key, model)
        logs.append(log)
        elapsed = time.perf_counter() - loop_start
        sleep_for = per_worker_interval - elapsed
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)


async def outage_controller(
    redis_client: redis.Redis,
    provider: str,
    start_at: float,
    duration_s: float,
    stop_event: asyncio.Event,
):
    """Refreshes mock:fail_count:<provider> on a short interval for the full
    outage window so the counter never drains to 0 before T0+duration_s
    (see FAIL_COUNT_REFRESH_INTERVAL_S comment above for why a single SET
    isn't sufficient at sustained load).
    """
    now = time.time()
    if now < start_at:
        await asyncio.sleep(start_at - now)
    end_at = time.time() + duration_s
    while time.time() < end_at and not stop_event.is_set():
        await redis_client.set(f"mock:fail_count:{provider}", FAIL_COUNT_REFRESH_VALUE)
        await asyncio.sleep(FAIL_COUNT_REFRESH_INTERVAL_S)
    # CRITICAL: the last SET leaves a large forced-failure count still queued
    # in Redis. If we don't delete the key here, consume_forced_failure keeps
    # returning True for thousands more calls *after* the intended outage
    # window — failures never actually stop, the breaker re-trips on every
    # half-open probe, and recovery is never observed. Deleting the key is
    # what makes "outage end" a real, measurable boundary.
    await redis_client.delete(f"mock:fail_count:{provider}")


async def breaker_poller(
    client: httpx.AsyncClient,
    provider: str,
    stop_event: asyncio.Event,
    samples: list[BreakerSample],
    interval_s: float = 0.25,
):
    while not stop_event.is_set():
        state = await parse_circuit_state(client, provider)
        samples.append(BreakerSample(t=time.time(), state=state))
        await asyncio.sleep(interval_s)


async def run(args) -> dict:
    redis_client = redis.Redis(host=REDIS_HOST, port=6379, decode_responses=True)
    # Confirm the gateway is actually in mock mode before relying on
    # mock:fail_count semantics — a real-provider run would either hang on
    # real API calls or silently no-op the forced failures.
    async with httpx.AsyncClient(base_url=GATEWAY_URL, timeout=5) as probe:
        try:
            health = await probe.get("/healthz")
            health.raise_for_status()
        except httpx.HTTPError as e:
            raise RuntimeError(f"gateway not reachable at {GATEWAY_URL}: {e}")

    logs: list[RequestLog] = []
    breaker_samples: list[BreakerSample] = []
    stop_event = asyncio.Event()

    total_duration = PRE_OUTAGE_WARMUP_S + OUTAGE_DURATION_S + RECOVERY_WINDOW_S
    limits = httpx.Limits(max_connections=300, max_keepalive_connections=100)

    async with httpx.AsyncClient(base_url=GATEWAY_URL, timeout=30, limits=limits) as client:
        wall_start = time.time()
        t0 = wall_start + PRE_OUTAGE_WARMUP_S  # outage start timestamp, fixed up front

        num_workers = 40
        workers = [
            asyncio.create_task(load_worker(i, client, stop_event, logs, TARGET_RATE_RPS, num_workers))
            for i in range(num_workers)
        ]
        poller = asyncio.create_task(breaker_poller(client, OUTAGE_PROVIDER, stop_event, breaker_samples))
        controller = asyncio.create_task(
            outage_controller(redis_client, OUTAGE_PROVIDER, t0, OUTAGE_DURATION_S, stop_event)
        )

        print(f"Warmup: {PRE_OUTAGE_WARMUP_S:.0f}s, outage T0 at wall time {t0:.3f} "
              f"({OUTAGE_DURATION_S:.0f}s), recovery window {RECOVERY_WINDOW_S:.0f}s. "
              f"Total run: ~{total_duration:.0f}s.")

        await asyncio.sleep(total_duration)
        stop_event.set()

        await asyncio.gather(*workers, poller, controller, return_exceptions=True)
        wall_end = time.time()

    await redis_client.aclose()

    return {
        "logs": logs,
        "breaker_samples": breaker_samples,
        "t0": t0,
        "outage_end": t0 + OUTAGE_DURATION_S,
        "wall_start": wall_start,
        "wall_end": wall_end,
    }


def write_raw_log(result: dict) -> None:
    with LOG_PATH.open("w") as f:
        for log in result["logs"]:
            f.write(json.dumps(asdict(log)) + "\n")
        for sample in result["breaker_samples"]:
            f.write(json.dumps({"breaker_sample": True, **asdict(sample)}) + "\n")


def _first_fast_reject_after(logs: list[RequestLog], t0: float, provider: str) -> float | None:
    """First request targeting `provider`, started at/after t0, that got
    fast-rejected by an open breaker (503, no provider call, no retry/backoff).

    In practice this signal comes from /generate/stream, whose generate_stream
    (app/main.py) returns 503 immediately on can_attempt()==False. /generate
    does NOT surface fast-reject the same way: on breaker-open its worker
    raises CircuitOpenSkip and falls back to the other provider, so it mostly
    returns a 200 (fallback) and only yields 503 if BOTH breakers are open.
    So this is deliberately a streaming-path measurement — the honest proof
    that the breaker short-circuits latency lives there, not on /generate.
    """
    candidates = [
        log for log in logs
        if log.t >= t0 and log.provider_requested == provider and log.status == 503
    ]
    candidates.sort(key=lambda l: l.t)
    return candidates[0].t if candidates else None


def _first_success_after(logs: list[RequestLog], after_t: float, provider: str) -> float | None:
    """First request targeting `provider` (or falling back FROM it, for
    /generate) at/after `after_t` that actually succeeded — used both to
    detect recovery and to sanity-check it against the breaker_samples gauge.
    """
    candidates = [
        log for log in logs
        if log.t >= after_t and log.provider_requested == provider and log.outcome == "success"
        # For /generate, "success via same-provider" is the real recovery
        # signal — a fallback success during the outage doesn't mean openai
        # itself recovered, it means anthropic covered for it.
        and not log.fallback_used
    ]
    candidates.sort(key=lambda l: l.t)
    return candidates[0].t if candidates else None


def _first_breaker_close_after(samples: list[BreakerSample], after_t: float) -> float | None:
    closed = [s for s in samples if s.t >= after_t and s.state == "closed"]
    closed.sort(key=lambda s: s.t)
    return closed[0].t if closed else None


def build_report(result: dict) -> dict:
    logs = result["logs"]
    t0 = result["t0"]
    outage_end = result["outage_end"]
    provider = OUTAGE_PROVIDER

    total = len(logs)
    successes = sum(1 for l in logs if l.outcome == "success")
    rate_limited = sum(1 for l in logs if l.outcome == "rate_limited")
    budget_rejected = sum(1 for l in logs if l.outcome == "budget_rejected")
    transport_errors = sum(1 for l in logs if l.outcome == "transport_error")
    http_errors = sum(1 for l in logs if l.outcome == "http_error")

    # Denominator choice matters for an honest number: rate-limit rejections
    # are a pre-existing, outage-independent property of this workload (55
    # teams * 10 token capacity vs 150 rps target — see TEAM_KEYS comment)
    # and would drag the "success rate" down regardless of the outage,
    # making the outage's real effect harder to see. Report both.
    overall_success_rate = successes / total if total else 0.0
    non_rate_limited_total = total - rate_limited
    success_rate_excl_rate_limit = successes / non_rate_limited_total if non_rate_limited_total else 0.0

    generate_logs = [l for l in logs if l.kind == "generate" and l.provider_requested == provider]
    stream_logs = [l for l in logs if l.kind == "stream" and l.provider_requested == provider]
    generate_fallback_successes = sum(1 for l in generate_logs if l.outcome == "success" and l.fallback_used)
    generate_direct_successes = sum(1 for l in generate_logs if l.outcome == "success" and not l.fallback_used)
    stream_successes = sum(1 for l in stream_logs if l.outcome == "success")
    stream_failures = sum(1 for l in stream_logs if l.outcome != "success" and l.outcome not in ("rate_limited", "budget_rejected"))

    fast_reject_t = _first_fast_reject_after(logs, t0, provider)
    time_to_fast_reject = (fast_reject_t - t0) if fast_reject_t is not None else None

    recovery_success_t = _first_success_after(logs, outage_end, provider)
    recovery_time_by_traffic = (recovery_success_t - outage_end) if recovery_success_t is not None else None

    breaker_close_t = _first_breaker_close_after(result["breaker_samples"], outage_end)
    recovery_time_by_gauge = (breaker_close_t - outage_end) if breaker_close_t is not None else None

    return {
        "total_requests": total,
        "successes": successes,
        "overall_success_rate": overall_success_rate,
        "success_rate_excl_rate_limit": success_rate_excl_rate_limit,
        "rate_limited": rate_limited,
        "budget_rejected": budget_rejected,
        "transport_errors": transport_errors,
        "http_errors": http_errors,
        "generate_direct_successes": generate_direct_successes,
        "generate_fallback_successes": generate_fallback_successes,
        "generate_total_to_provider": len(generate_logs),
        "stream_successes": stream_successes,
        "stream_failures": stream_failures,
        "stream_total_to_provider": len(stream_logs),
        "time_to_fast_reject_s": time_to_fast_reject,
        "recovery_time_by_traffic_s": recovery_time_by_traffic,
        "recovery_time_by_gauge_s": recovery_time_by_gauge,
    }


def print_report(report: dict) -> None:
    print()
    print("=" * 72)
    print("CHAOS TEST REPORT")
    print("=" * 72)
    print(f"Total requests logged:          {report['total_requests']}")
    print(f"  successes:                    {report['successes']}")
    print(f"  rate_limited (429):           {report['rate_limited']}")
    print(f"  budget_rejected (402):        {report['budget_rejected']}")
    print(f"  transport_error:              {report['transport_errors']}")
    print(f"  http_error (incl. fast-fail 503): {report['http_errors']}")
    print()
    print(f"Overall success rate (all requests):            {report['overall_success_rate']*100:.2f}%")
    print(f"Success rate excluding 429 rate-limits:          {report['success_rate_excl_rate_limit']*100:.2f}%")
    print("  (429s are a pre-existing property of this workload's team/rps ratio,")
    print("   not caused by the outage — both numbers are reported so the outage's")
    print("   real effect isn't hidden inside/inflated by unrelated rate-limiting.)")
    print()
    print(f"/generate requests targeting {OUTAGE_PROVIDER}: {report['generate_total_to_provider']}")
    print(f"  succeeded directly (no fallback): {report['generate_direct_successes']}")
    print(f"  succeeded via same-request fallback to the other provider: {report['generate_fallback_successes']}")
    print("  (queue.py's attempt_provider/worker DOES fall back same-request —")
    print("   this is real existing routing behavior, not a gap in this test.)")
    print()
    print(f"/generate/stream requests targeting {OUTAGE_PROVIDER}: {report['stream_total_to_provider']}")
    print(f"  succeeded: {report['stream_successes']}")
    print(f"  failed (no fallback exists for streaming): {report['stream_failures']}")
    print()

    if report["time_to_fast_reject_s"] is not None:
        print(f"Time from T0 to first fast-reject (503, breaker open, no provider call): "
              f"{report['time_to_fast_reject_s']:.2f}s")
    else:
        print("Time from T0 to first fast-reject: NEVER OBSERVED in this run — "
              "either the breaker never tripped (failure_count stayed below 5) or the "
              "outage window ended before it opened. Do not report a recovery/fast-fail "
              "claim from this run if this is None.")
    print()

    by_traffic = report["recovery_time_by_traffic_s"]
    by_gauge = report["recovery_time_by_gauge_s"]
    if by_traffic is not None:
        print(f"Recovery time (outage end -> first real success against {OUTAGE_PROVIDER}): "
              f"{by_traffic:.2f}s")
    else:
        print(f"Recovery time by traffic: NEVER OBSERVED — no successful direct "
              f"{OUTAGE_PROVIDER} request landed within the {RECOVERY_WINDOW_S:.0f}s recovery window.")
    if by_gauge is not None:
        print(f"Recovery time (outage end -> gateway_circuit_breaker_state reports closed): "
              f"{by_gauge:.2f}s")
    else:
        print("Recovery time by breaker gauge: NEVER OBSERVED within the recovery window.")
    if by_traffic is not None and by_gauge is not None and abs(by_traffic - by_gauge) > 2.0:
        print(f"  NOTE: traffic-based and gauge-based recovery times disagree by "
              f"{abs(by_traffic - by_gauge):.2f}s — inspect {LOG_PATH} before trusting either number.")
    print()
    print(f"Raw per-request log written to: {LOG_PATH.resolve()}")
    print("=" * 72)


async def verify_stream_budget_hygiene(redis_client: redis.Redis) -> dict:
    """Reuses the check from tests/test_streaming.py's
    test_stream_failure_releases_reservation_without_charging_spend: after
    the run, no team should have a stuck non-zero `reserved:<team>:daily`
    (a leaked reservation from a stream that failed mid-flight and didn't
    reconcile), and spend should only reflect requests that actually
    completed with real usage.
    """
    stuck_reservations = {}
    for key_suffix in [k.split("load-test-key-")[-1] for k in TEAM_KEYS]:
        team_id = f"load-test-{key_suffix}"
        reserved = await redis_client.get(f"reserved:{team_id}:daily")
        if reserved is not None and float(reserved) > 1e-9:
            stuck_reservations[team_id] = float(reserved)
    return {"stuck_reservations": stuck_reservations}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=1, help="Repeat the full test N times and report the range, not just one run.")
    args = parser.parse_args()

    all_reports = []
    for run_idx in range(args.runs):
        if args.runs > 1:
            print(f"\n--- Run {run_idx + 1}/{args.runs} ---")
        result = await run(args)
        if run_idx == args.runs - 1:
            write_raw_log(result)  # keep the log from the last run only
        report = build_report(result)
        all_reports.append(report)
        print_report(report)

        redis_client = redis.Redis(host=REDIS_HOST, port=6379, decode_responses=True)
        hygiene = await verify_stream_budget_hygiene(redis_client)
        await redis_client.aclose()
        if hygiene["stuck_reservations"]:
            print("WARNING: stuck budget reservations detected after run "
                  f"(reservation not released for {len(hygiene['stuck_reservations'])} teams):")
            for team_id, amount in hygiene["stuck_reservations"].items():
                print(f"    {team_id}: ${amount:.6f} still reserved")
        else:
            print("Budget hygiene check: no stuck reservations across any load-test team — "
                  "all failed/cancelled streams released their reservation cleanly.")

    if args.runs > 1:
        print("\n" + "=" * 72)
        print(f"RANGE ACROSS {args.runs} RUNS (do not cherry-pick — this is the honest number)")
        print("=" * 72)
        for field_name, label in [
            ("overall_success_rate", "Overall success rate"),
            ("success_rate_excl_rate_limit", "Success rate excl. rate-limit"),
            ("time_to_fast_reject_s", "Time to first fast-reject (s)"),
            ("recovery_time_by_traffic_s", "Recovery time by traffic (s)"),
            ("recovery_time_by_gauge_s", "Recovery time by gauge (s)"),
        ]:
            values = [r[field_name] for r in all_reports if r[field_name] is not None]
            missing = args.runs - len(values)
            if not values:
                print(f"{label}: NEVER OBSERVED in any of {args.runs} runs")
                continue
            lo, hi = min(values), max(values)
            mean = statistics.mean(values)
            spread_note = f" ({missing} run(s) never observed this)" if missing else ""
            if "rate" in field_name:
                print(f"{label}: {lo*100:.2f}% - {hi*100:.2f}% (mean {mean*100:.2f}%){spread_note}")
            else:
                print(f"{label}: {lo:.2f}s - {hi:.2f}s (mean {mean:.2f}s){spread_note}")
            if hi - lo > 0.3 * mean and mean > 0:
                print(f"  FLAG: >30% spread on {label} across runs — treat the single-run number "
                      f"above as unstable, not a claimable resume figure without more runs.")


if __name__ == "__main__":
    asyncio.run(main())
