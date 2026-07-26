# LLM Gateway

A production-shaped API gateway that sits between applications and LLM providers (OpenAI, Anthropic), handling the concerns a direct provider call leaves to you: authentication, per-team rate limiting, cost budgeting, retries, cross-provider failover, streaming, and observability.

The interesting part isn't the feature list — it's that **every reliability claim below is backed by a test that measures it**, including a chaos test that fails a provider mid-flight and a horizontal scale-out test whose headline result was negative and is reported as such.

```
                                    ┌──────────────────────────┐
  client ──▶ nginx (round-robin) ──▶│  FastAPI gateway ×N      │
                                    │                          │
                                    │  auth ─▶ rate limit ─▶   │
                                    │  budget ─▶ priority queue│──▶ worker pool
                                    └──────────────────────────┘        │
                                                 │                      │
                                    ┌────────────▼──────────┐  ┌────────▼─────────┐
                                    │ Redis (shared state)  │  │ circuit breaker  │
                                    │ buckets · spend ·     │  │ retry + backoff  │
                                    │ reservations          │  │ failover routing │
                                    └───────────────────────┘  └────────┬─────────┘
                                                                        │
                                                          OpenAI ◀──────┴──────▶ Anthropic
```

---

## Table of contents

- [What it does](#what-it-does)
- [Measured results](#measured-results)
- [Architecture](#architecture)
- [Engineering decisions worth explaining](#engineering-decisions-worth-explaining)
- [Bugs found by the tests](#bugs-found-by-the-tests)
- [Observability](#observability)
- [Running it](#running-it)
- [Testing](#testing)
- [Project layout](#project-layout)
- [Known limits](#known-limits)

---

## What it does

| Capability | Implementation |
|---|---|
| **Provider abstraction** | One `StandardRequest`/`StandardResponse` schema translated per provider, so callers never touch OpenAI vs. Anthropic payload differences (`app/providers/`) |
| **Authentication & authorization** | API-key → team lookup, with a per-team allow-list of models (403 on an unauthorized model) |
| **Rate limiting** | Token bucket (capacity 10, refill 10/min) computed inside a **Redis Lua script** so the read-refill-decrement sequence is atomic under concurrency |
| **Cost budgeting** | Per-team daily spend tracked in Redis from real token usage, with an 80% warning threshold and a hard 402 at 100% |
| **Streaming budgets** | Optimistic **reserve → stream → reconcile** using two more Lua scripts, so a team can't start a stream it can't afford, and a broken stream refunds its reservation |
| **Priority queue** | `asyncio.PriorityQueue` with `realtime` ahead of `batch`, drained by a configurable worker pool; the HTTP handler awaits a `Future` the worker resolves |
| **Retries** | Exponential backoff with jitter, only on retryable statuses (429/500/502/503) — a 400 fails immediately instead of burning three attempts |
| **Circuit breaker** | CLOSED → OPEN after 5 failures → HALF_OPEN probe after a 30s cooldown, per provider |
| **Cross-provider failover** | When the primary exhausts retries or is circuit-open, **the same request** is re-routed to the other provider — but only if the fallback model is in the team's allow-list |
| **Streaming (SSE)** | Both providers' event formats normalized to plain text chunks, with usage extracted from each provider's differently-shaped final events |
| **Observability** | OpenTelemetry traces spanning the queue boundary, Prometheus metrics, and a provisioned Grafana dashboard |
| **Horizontal scale-out** | 3 replicas behind nginx sharing one Redis, with quota correctness verified across instances |

---

## Measured results

Every number below came from a test in this repo. Ranges are across repeated runs — no single best run is quoted as if it were typical.

### Resilience under a simulated provider outage

`chaos_test.py` drives sustained mixed traffic (70% non-streaming, 30% streaming) across 55 teams, forces OpenAI into failure for a 30-second window, then watches recovery for 60 seconds. Breaker state is sampled from the Prometheus gauge every 0.25s as ground truth, independent of what responses imply.

| Metric | Result (3 runs) |
|---|---|
| **Success rate during outage** (excl. rate-limit 429s) | **89.4% – 89.8%** |
| **Recovery time**, measured by live traffic | **4.47s – 5.05s** |
| **Recovery time**, measured by the breaker gauge | **4.72s – 5.20s** — agrees within 0.3s |
| Time to first fast-reject (breaker trips) | 0.39s – 3.77s (variable by design) |
| Budget hygiene (no leaked reservations) | **PASS, every run** |

Two independent measurements of recovery exist because a number that can't be cross-validated isn't one worth quoting. The report also separates requests that succeeded *via failover* from ones that succeeded *directly* — otherwise automatic failover would mask the outage entirely and make the headline number meaningless.

*Why recovery is ~5s and not the 30s cooldown:* the breaker re-trips on each failed probe during the outage, so the last trip usually lands well before the outage ends — the cooldown elapses *during* the outage, leaving the breaker ready to close almost immediately once real traffic recovers.

### Quota correctness under horizontal scale-out

The failure mode that matters when you run N copies of a gateway is a team getting N× its quota. Firing 40 requests for one team through the load balancer, spread across 3 replicas:

**Exactly 10 succeeded, 30 returned 429** — identical to a single instance, because bucket state lives in shared Redis rather than process memory. Verified at 500, 1000, and 1500 concurrency. The same reasoning applies to budgets, which increment one shared `spend:{team}:daily` key.

### Throughput — a negative result, reported as one

| Topology (warmed, tracing noise disabled) | Throughput @ 500 concurrent |
|---|---|
| Single instance | ~172 – 195 req/s |
| 3 instances + nginx | ~157 – 200 req/s |

**Three instances were not faster than one.** Rather than quietly dropping the experiment, it was diagnosed:

1. Every container sat **below 1% CPU** while latency ran multi-second — requests were queued, not working. Nothing was resource-bound.
2. The `ConsoleSpanExporter` was emitting **~90,000 log lines per 500 requests**; those synchronous stdout writes serialize the event loop. Gated behind `DISABLE_CONSOLE_SPANS=1` so load runs measure routing capacity instead of logging I/O.
3. **The load generator is the bottleneck.** Two client processes against a *single* instance produced ~185 req/s aggregate, more than one client process could generate alone (~158 req/s). One gateway instance already serves more than a single-threaded Python client can push.

The honest conclusion: the scale-out infrastructure is *correct* (round-robin verified, shared-state quota verified), but throughput is **client-bound**, so this setup cannot demonstrate linear scaling. Proving that needs a distributed load generator, which isn't built here. There is no "3× throughput" claim because the method doesn't support one.

---

## Architecture

### Request lifecycle

```
POST /generate
  │
  ├─ auth: X-API-Key → team          401 if unknown
  ├─ rate limit: Redis Lua bucket    429 + Retry-After
  ├─ model allow-list check          403 if not permitted
  ├─ enqueue (realtime | batch) ─────────────┐
  │                                          │  priority queue
  └─ await Future ◀──────────────────────────┤
                                             ▼
                                     worker (1 of NUM_WORKERS)
                                       ├─ circuit breaker check
                                       ├─ provider call + retry/backoff
                                       ├─ on exhaustion → failover to other provider
                                       └─ resolve Future
  │
  └─ record real token spend → 402 once the daily budget is crossed
```

The endpoint never calls a provider itself. It enqueues work and awaits an `asyncio.Future` that a worker resolves, which is what makes priority scheduling and bounded provider concurrency possible without blocking the HTTP layer.

### Streaming lifecycle

Streaming breaks the ordinary budget flow, because bytes go out before the token count is known. The gateway uses optimistic reservation:

```
estimate max cost  →  reserve atomically (402 if it wouldn't fit)
                   →  breaker check (release reservation if open)
                   →  stream chunks to the client
                   →  reconcile: release reservation, record actual cost
```

Reconciliation runs from a `finally` block wrapping the generator, so it fires on **every** exit path — clean completion, mid-stream exception, or client disconnect (see below for why this replaced the obvious approach).

---

## Engineering decisions worth explaining

**Atomic multi-step state lives in Lua, not Python.** The rate-limit refill and the budget reservation are both check-then-act sequences. Done in application code, two concurrent requests read the same stale value and both pass. `app/rate_limiter.lua`, `app/budget_reserve.lua`, and `app/budget_reconcile.lua` run as single indivisible Redis operations instead. `tests/test_rate_limiting.py` fires 50 concurrent requests at a fresh bucket and asserts *exactly* 10 pass — a sequential test would pass even with the race present, so the concurrency is the point of the test.

**`finally`, not Starlette's `BackgroundTask`.** Reading Starlette's source revealed that `BackgroundTask` only runs when the response generator finishes *normally*. A provider dying mid-stream — precisely the case where a reservation must be released — silently skipped it, leaking budget. `_tracked_stream` in `app/main.py` wraps the generator in `try/except/finally` so reconciliation always runs, and the same wrapper signals the circuit breaker on both paths, which is what makes streaming failures able to trip the breaker at all.

**Failover is bounded by authorization.** When the primary fails, the request re-routes to the other provider — but only if the mapped fallback model is in the team's allow-list. Failing over to a model a team isn't permitted to use would quietly turn a reliability feature into an authorization bypass.

**Retryable vs. permanent errors are distinguished.** Only 429/500/502/503 retry. A 400 is a bad request that will still be bad three attempts later, so it raises immediately rather than tripling latency and wasting the breaker's failure budget.

**Anthropic's `content[0]` is not the text block.** With adaptive thinking, a thinking block can precede the text block, so the parser searches for `type == "text"` instead of indexing position 0. The two providers also report streaming usage differently — OpenAI sends one final chunk with an *empty* `choices` array (indexing `choices[0]` there raises `IndexError`), while Anthropic splits input tokens into `message_start` and output tokens into `message_delta`. Both are handled explicitly.

**Mock failure injection that can't corrupt neighboring tests.** `mock:fail_count:<provider>` is decremented by a Lua script that **only touches the key if it already exists**, so ordinary mock traffic from unrelated tests can never create or drain a counter nobody set.

**nginx re-resolves upstreams at request time.** Using a `resolver` plus a variable in `proxy_pass` forces DNS re-resolution, so nginx sees all three replica IPs. Caching a single IP at startup would pin every request to one replica and silently invalidate the entire scale-out experiment.

---

## Bugs found by the tests

Each of these would have quietly corrupted a result or leaked a resource:

1. **`BackgroundTask` skipped on stream failure** — failed streams leaked budget reservations forever. Fixed with the `try/finally` wrapper.
2. **Streaming mocks couldn't simulate failure** — only the non-streaming path honored `mock:fail_count`, so mid-stream outages weren't testable at all.
3. **A shell redirect silently failed on Windows** — `redis-cli flushdb >/dev/null` wasn't parsed by `cmd.exe`, so Redis was never flushed between scale runs. Every run after the first was measuring a 429 storm from drained buckets, not throughput. Fixed by flushing through the Python client.
4. **The chaos outage never ended** — a large `mock:fail_count` value left queued after the window kept failures coming, so recovery was never observed. Fixed by deleting the key at outage end.
5. **`ConsoleSpanExporter` throttled throughput** — ~90k synchronous log lines per 500 requests capped throughput while CPU sat idle.
6. **An env var didn't propagate to scale replicas** — an early comparison had tracing *on* for 3 instances and *off* for the baseline. An invalid comparison, caught and re-run under identical conditions.

---

## Observability

**Tracing** — OpenTelemetry spans cover `auth_check`, `generate_request`, `circuit_breaker.check`, `provider.call`, `retry.attempt`, and `worker_processing`. Because the queue hands work to a separate asyncio task, automatic parent-child nesting breaks at that boundary; the trace context is captured at enqueue time and re-attached inside the worker so one request produces one connected trace instead of two orphans.

**Metrics** — exported at `/metrics` in Prometheus format:

| Metric | Type | Labels |
|---|---|---|
| `gateway_requests_total` | counter | team, model, outcome |
| `gateway_errors_total` | counter | provider, error_type |
| `gateway_request_duration_seconds` | histogram | outcome |
| `gateway_provider_call_duration_seconds` | histogram | provider, outcome |
| `gateway_fallback_triggered_total` | counter | from_provider, to_provider |
| `gateway_circuit_breaker_state` | observable gauge | provider (0=closed, 1=half_open, 2=open) |

**Dashboard** — a provisioned Grafana dashboard (`grafana/provisioning/`) ships with panels for requests/sec, error rate, request latency (P50/P95/P99), provider call latency, fallback events, and circuit breaker state. The breaker gauge isn't decorative: the chaos test reads it as ground truth for when the breaker actually opened and closed.

---

## Running it

**Requirements:** Docker + Docker Compose. For live provider calls, an `.env` with `OPENAI_API_KEY` and `ANTHROPIC_API_KEY`.

```bash
docker compose up --build
```

| Service | URL |
|---|---|
| Gateway | http://localhost:8000 |
| Metrics | http://localhost:8000/metrics |
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:3000 |

### Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/generate` | Non-streaming completion, with retry + cross-provider failover |
| `POST` | `/generate/stream` | Streaming completion with pre-flight budget reservation |
| `GET` | `/healthz` | Liveness check |
| `GET` | `/metrics` | Prometheus metrics |

```bash
curl -X POST http://localhost:8000/generate \
  -H "X-API-Key: team-a-key" \
  -H "X-Priority: realtime" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Explain circuit breakers in one sentence.", "model": "gpt-4", "max_tokens": 100}'
```

Headers: `X-API-Key` (required), `X-Priority` (`realtime` | `batch`, default `realtime`).

Status codes: `401` invalid key · `403` model not allowed for team · `429` rate limited · `402` budget exceeded · `502` upstream error · `503` provider unavailable / circuit open.

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `NUM_WORKERS` | `4` | Worker pool size draining the priority queue |
| `MOCK_PROVIDERS` | unset | `1` returns canned responses — no API keys, no cost |
| `DISABLE_CONSOLE_SPANS` | `0` | `1` silences per-span stdout logging for load runs |

---

## Testing

Integration tests run against the **real containerized stack over HTTP** — real Redis hostname resolution, real container boundary between endpoint and worker — rather than an in-process ASGI transport, so what's tested is the deployed topology.

```bash
docker compose -f docker-compose.yml -f docker-compose.test.yml \
    up --build --abort-on-container-exit tests
```

| Test | What it proves |
|---|---|
| `test_rate_limiting.py` | 50 concurrent requests against a fresh bucket → exactly 10 succeed, 40 get 429 (Lua atomicity under a real race) |
| `test_budget.py` | Requests succeed under budget; 402 once daily spend crosses the limit |
| `test_fallback.py` | With OpenAI forced to fail all 3 retries, the same request completes on Anthropic and the fallback metric increments |
| `test_streaming.py` (4) | Chunks reconstruct the full text in order; usage is captured and charged; an over-budget team is rejected *before* any bytes stream; a mid-stream failure releases its reservation and records no spend |

**8/8 pass**, including after the streaming and scale-out changes.

### Chaos test

```bash
docker compose -f docker-compose.yml -f docker-compose.test.yml up -d --build llm-gateway redis
python chaos_test.py --runs 3
```

Writes a per-request log to `chaos_test_log.jsonl` (every request's timestamp, requested vs. serving provider, outcome, and whether it was a fast-reject) and prints the summary. `--runs 3` reports ranges and flags any metric whose variance makes it unsafe to quote.

### Load & scale test

```bash
export DISABLE_CONSOLE_SPANS=1

# baseline
docker compose -f docker-compose.yml -f docker-compose.test.yml up -d --build llm-gateway redis
python scale_test.py --label single --levels 500 1000 1500 --repeats 3

# 3 replicas behind nginx
docker compose -f docker-compose.yml -f docker-compose.test.yml \
    -f docker-compose.scale.yml up -d --build
python scale_test.py --label 3-instance --levels 500 1000 1500 --repeats 3
```

Discard each topology's first run as warmup. Raw results append to `scale_test_results.jsonl`.

---

## Project layout

```
app/
  main.py                  FastAPI endpoints, streaming reserve/reconcile wrapper
  models.py                Provider-neutral request/response schemas
  auth.py                  API key → team, model allow-list, priority header
  rate_limit.py            Token bucket dependency
  rate_limiter.lua         Atomic refill + decrement
  budget.py                Pricing, spend recording, reserve/reconcile wrappers
  budget_reserve.lua       Atomic "would this stream fit in the budget?"
  budget_reconcile.lua     Atomic reservation release + real-cost recording
  queue.py                 Priority queue, worker pool, failover routing
  retry.py                 Retryable-status classification, backoff with jitter
  circuit_breaker.py       CLOSED/OPEN/HALF_OPEN state machine
  health.py                Background provider probes, error rate + p99 window
  tracing.py               OpenTelemetry tracer, metric instruments
  providers/               OpenAI + Anthropic translation, SSE parsing, mocks

tests/                     Integration suite (8 tests, real stack over HTTP)
chaos_test.py              Outage injection, breaker + recovery + budget measurement
scale_test.py              Throughput sweep + cross-instance quota verification
load_test.py               Concurrency load generator
docs/                      Chapter write-ups: observability, testing, load & chaos
grafana/, prometheus.yml   Provisioned dashboard + scrape config
docker-compose*.yml        Base stack, test overlay, 3-replica scale overlay
```

Detailed engineering write-ups live in [docs/](docs/) — including the full [load and chaos testing report](docs/chapter-08-load-and-chaos-testing.md), which documents the methodology, the bugs found while measuring, and the claims deliberately *not* made.

---

## Known limits

Stated plainly, because knowing where a system stops being trustworthy is part of building it:

- **Circuit breaker state is per-process.** With multiple replicas, each keeps its own breaker, so an outage is detected independently N times instead of once globally. Moving the state to Redis would fix this.
- **Team config is hardcoded** in `app/auth.py` as a stand-in for a real datastore.
- **Throughput measurement is client-bound** — the numbers above reflect a single-threaded Python load generator saturating before the server does, so the gateway's true ceiling is unmeasured.
- **The stream cost estimator uses a ~4 chars/token heuristic**, not a real tokenizer. It only needs to be a sane upper bound for the reservation, but it will over-reserve.
- **Daily budget windows are approximate** — a 24h Redis TTL from first write, not a calendar-aligned reset.
