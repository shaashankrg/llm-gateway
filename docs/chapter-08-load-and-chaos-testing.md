# Chapter 8 — Load Testing, Chaos Testing & Horizontal Scaling

> **Who this is for:** You (future you), or anyone picking up this project cold.
> This chapter assumes you know basic Python but *not* the systems/testing
> concepts. Every term is explained the first time it appears. If something
> feels over-explained, skip ahead — it's written so a beginner can follow the
> whole story end to end.

---

## Table of contents

1. [What this chapter covers](#1-what-this-chapter-covers)
2. [Background concepts you need first](#2-background-concepts-you-need-first)
3. [The streaming work that set this up](#3-the-streaming-work-that-set-this-up)
4. [Integration testing: what we have and why](#4-integration-testing-what-we-have-and-why)
5. [The chaos test (simulated provider outage)](#5-the-chaos-test-simulated-provider-outage)
6. [The horizontal scale test](#6-the-horizontal-scale-test)
7. [Bugs found along the way](#7-bugs-found-along-the-way)
8. [Every file we created or changed](#8-every-file-we-created-or-changed)
9. [How to reproduce all of it](#9-how-to-reproduce-all-of-it)
10. [Honest summary of results (what you can actually claim)](#10-honest-summary-of-results-what-you-can-actually-claim)
11. [Glossary](#11-glossary)

---

## 1. What this chapter covers

This project is an **LLM gateway** — a server that sits between your
applications and large-language-model providers (OpenAI, Anthropic). Instead of
your app calling OpenAI directly, it calls the gateway, and the gateway handles
the messy production concerns: authentication, rate limiting, budgets, retries,
failover between providers, metrics, and so on.

This chapter is about **proving the gateway behaves correctly and measuring how
it performs** — three distinct activities:

- **Integration testing** — automated tests that spin up the *real* gateway in
  a container and hit it over HTTP to confirm features work end to end.
- **Chaos testing** — deliberately breaking a provider mid-run to measure how
  the gateway copes (does it fail fast? does it recover? does it corrupt any
  accounting?).
- **Load & scale testing** — throwing lots of traffic at the gateway to measure
  throughput and latency, and checking whether running multiple copies of the
  gateway ("horizontal scaling") makes it faster.

The most important theme: **we optimized for honest, defensible numbers, not
good-looking ones.** Several results came out *worse* or more nuanced than
hoped, and this report says so plainly, because these numbers are meant to back
real claims (e.g. on a resume) where "did you actually verify that?" is a fair
question.

---

## 2. Background concepts you need first

If you already know these, skip to section 3. Otherwise, read this once and the
rest of the chapter will make sense.

### 2.1 Request/response vs. streaming

A normal HTTP request is **request → wait → full response**. You ask, the
server thinks, you get the complete answer in one lump.

**Streaming** is different: the server sends the answer in **chunks** over time,
as it's generated. This is how ChatGPT "types" its answer word by word instead
of freezing for 10 seconds and dumping everything at once. Technically this uses
**SSE (Server-Sent Events)** — a format where the server sends lines like
`data: {...}` repeatedly over a single long-lived connection.

Why it matters here: streaming breaks a lot of assumptions. With a normal
request you know the full cost (how many tokens the model used) *before* you
respond. With streaming, you're already sending data to the user before you know
the final token count — so budgeting has to work differently (more on this in
section 3).

### 2.2 Tokens and cost

LLM providers charge by **tokens** — roughly, pieces of words (~4 characters
each). A request has *input tokens* (your prompt) and *output tokens* (the
model's reply). Each has a price. The gateway tracks spend per team so a team
can't run up an unlimited bill.

### 2.3 Rate limiting (the "token bucket")

**Rate limiting** caps how many requests a team can make per unit time, so one
team can't overwhelm the system. This project uses a **token bucket**:

- Each team has a bucket that holds up to `CAPACITY = 10` tokens.
- The bucket refills at `10 tokens / 60 seconds`.
- Each request costs 1 token. No token available → request rejected with HTTP
  **429 ("Too Many Requests")**.

The "burst" of 10 lets a team make 10 quick requests, then they're throttled to
the slow refill rate. **Important:** "token" here means a rate-limit token,
totally unrelated to the LLM tokens in 2.2. Same word, different thing.

The bucket state lives in **Redis** (an in-memory database) so it's shared and
fast. The refill math is done in a **Lua script** that Redis runs atomically —
"atomically" meaning the whole read-refill-decrement calculation happens as one
indivisible step, so two simultaneous requests can't both read the same stale
count and both think there's a token left. (`app/rate_limiter.lua`.)

### 2.4 The circuit breaker

A **circuit breaker** is a safety pattern borrowed from electrical circuits.
When a provider (say OpenAI) starts failing, you don't want to keep hammering it
with requests that will just time out — that wastes time and makes things worse.
So the breaker "trips open" after enough failures and starts **fast-rejecting**
requests immediately (without even trying the dead provider), then periodically
tests whether the provider has recovered.

The three states (`app/circuit_breaker.py`):

- **CLOSED** — normal. Requests flow through. (Confusingly, "closed" = working,
  like a closed electrical circuit that lets current flow.)
- **OPEN** — tripped. Requests are fast-rejected without calling the provider.
  Happens after `failure_count >= 5`.
- **HALF_OPEN** — testing recovery. After a 30-second cooldown, the breaker
  lets *one* request through as a probe. If it succeeds → back to CLOSED. If it
  fails → back to OPEN for another 30s.

This is central to the chaos test: the whole point is to trip the breaker on
purpose and measure how fast it protects the system and how fast it recovers.

### 2.5 Fallback (failover) routing

When one provider fails, the gateway can **fall back** to another provider for
the same request. In this project, `/generate` (the non-streaming endpoint)
does this: if OpenAI fails, it retries the request against Anthropic
automatically, and the user still gets a successful answer (just from a
different model). `/generate/stream` (the streaming endpoint) does **not** have
fallback — this asymmetry matters a lot in the chaos test.

### 2.6 The worker pool and queue

Requests don't call the provider directly. They're put on a **priority queue**,
and a fixed pool of **workers** (`NUM_WORKERS`, default 4) pull requests off the
queue and process them. This lets the gateway prioritize "realtime" over "batch"
traffic and control concurrency. It also means only `NUM_WORKERS` requests are
truly in-flight at once — a detail that becomes important in the scale test.

### 2.7 Mock providers

Calling the real OpenAI/Anthropic APIs in tests would be slow, costly, and
flaky. So the gateway has a **mock mode** (`MOCK_PROVIDERS=1`): instead of real
API calls, it returns instant canned responses. This isolates the gateway's own
overhead (queueing, auth, rate limiting) from real network latency.

Crucially, the mocks support a **forced-failure mechanism**: a test can set a
Redis key `mock:fail_count:openai` to N, and the next N calls to that provider
will fail with a fake "503 Service Unavailable" error. This is how we simulate
an outage without unplugging anything real. The counter *decrements* on each
call — set it to 3, and exactly the next 3 calls fail, then normal responses
resume. (`app/providers/mock_control.py`.)

### 2.8 Metrics, Prometheus, and the `/metrics` endpoint

The gateway exposes a `/metrics` HTTP endpoint in **Prometheus** format (plain
text, one metric per line). Prometheus is a monitoring system that scrapes these
numbers over time. Relevant here: a metric called
`gateway_circuit_breaker_state` that reports each provider's breaker state as a
number (0 = closed, 1 = half_open, 2 = open). We read this directly to know the
breaker's true state, rather than guessing from response codes.

### 2.9 Docker and docker-compose

**Docker** packages the app + its dependencies into a **container** (an isolated
mini-environment). **docker-compose** describes a *set* of containers that run
together (the gateway, Redis, Prometheus, Grafana) in a YAML file, so `docker
compose up` starts the whole stack. We use **overlay** compose files
(`-f base.yml -f extra.yml`) to layer changes on top of the base setup without
editing it.

---

## 3. The streaming work that set this up

Before the testing work, we finished and hardened **streaming budget
enforcement**, because the chaos test depends on it behaving correctly under
failure. Here's what that involved and why.

### 3.1 The problem

With normal requests, the flow is: check budget → call provider → know exact
cost → record spend. Clean.

With streaming you *can't* know the exact cost up front, because tokens are
counted as the stream flows. But you also can't let a team that's already over
budget start an expensive stream. The industry-standard solution is **optimistic
reservation**:

1. **Estimate** the maximum possible cost before starting (based on the
   requested `max_tokens` and the prompt length).
2. **Reserve** that estimate against the team's budget atomically — if it
   wouldn't fit, reject with HTTP **402 ("Payment Required")** *before* streaming
   a single byte.
3. **Stream** the response.
4. **Reconcile** when the stream ends: release the reservation and record the
   *actual* cost (refunding the difference between estimate and reality).

### 3.2 What we built

- **`app/budget_reserve.lua`** — a Redis Lua script that atomically checks
  `current_spend + already_reserved + this_estimate <= daily_budget`, and if so,
  reserves the estimate. Atomic so two concurrent streams from the same team
  can't both pass the check against stale numbers.
- **`app/budget_reconcile.lua`** — atomically releases a reservation and records
  the real cost. Used both on clean completion *and* on failure/cancellation.
- **Python wrappers** `reserve_budget()` / `reconcile_budget()` in
  `app/budget.py`.
- Wired into `/generate/stream` in `app/main.py`.

### 3.3 The subtle bug we fixed (important for the chaos test)

The original code reconciled cost using Starlette's `BackgroundTask` — a hook
that runs *after* the response finishes sending. But we discovered by reading
Starlette's source that **`BackgroundTask` only runs if the response generator
finishes normally.** If the provider dies *mid-stream* (an exception is raised
partway through), the background task is silently skipped — meaning the budget
reservation would be stuck, never released.

The fix: wrap the streaming generator in a helper (`_tracked_stream` in
`app/main.py`) that uses a `try/except/finally` block. The `finally` clause runs
**no matter how the stream ends** — clean finish, exception, or client
disconnect — so reconciliation always happens. The same wrapper also signals the
circuit breaker (`record_failure()` on error, `record_success()` on success),
which is how streaming failures now correctly trip the breaker.

**Why this matters for the chaos test:** the chaos test deliberately fails
streams mid-flight. If reservations leaked on failure, a team's budget would
slowly get eaten by "phantom" reservations for requests that never actually cost
anything. The chaos test explicitly verifies this doesn't happen (section 5).

---

## 4. Integration testing: what we have and why

**Integration tests** (as opposed to *unit* tests) start the real gateway in a
container and talk to it over HTTP, exercising the whole stack — auth, Redis,
queue, workers — the way a real client would. They live in `tests/` and run via
`docker-compose.test.yml`, which sets `MOCK_PROVIDERS=1` so no real API calls
happen.

The suite (8 tests) as it stands:

| Test file | What it proves |
|---|---|
| `tests/test_budget.py` | A team is rejected with **402** once its daily spend crosses the budget, and succeeds when comfortably under it. |
| `tests/test_rate_limiting.py` | Firing 50 concurrent requests at a fresh bucket, **exactly 10** succeed (the capacity) and 40 get 429 — proving the Lua rate-limit script is truly atomic under concurrency. |
| `tests/test_fallback.py` | When OpenAI is forced to fail all 3 retry attempts, the *same request* falls back to Anthropic and still returns 200, and the fallback metric increments. |
| `tests/test_streaming.py` (4 tests) | Streamed chunks reconstruct the full text in order; usage data is captured and charged; an over-budget team is rejected with 402 *before* streaming; and — critically — a stream that fails mid-flight **releases its budget reservation and records no spend**. |

### Why the "concurrency" framing keeps appearing

Several tests deliberately fire requests **concurrently** (all at once) rather
than one-at-a-time. The reason: a bug in atomic/shared-state logic (like the
rate limiter or budget reservation) can only show up when two requests race each
other. Sequential requests can never overlap, so they'd pass even if the code
had a race condition. Testing under concurrency is what actually exercises the
guarantee.

**Result:** all 8 integration tests pass, including after all the streaming and
scaling changes in this session (verified by re-running the suite at the end).

---

## 5. The chaos test (simulated provider outage)

**Goal:** produce a measured, reproducible answer to *"what's the success rate
and recovery time during a provider outage?"* — the kind of number you'd put on
a resume, so accuracy matters more than a pretty figure.

**File:** `chaos_test.py`

### 5.1 What the test actually does

1. **Sustained load** — many concurrent workers send a mix of `/generate` (70%)
   and `/generate/stream` (30%) requests, spread across 55 team keys, at a
   steady target rate, for the whole run.
2. **Warmup (10s)** — steady load before the outage, so the outage moment (T0)
   isn't measuring cold-start noise.
3. **Outage (30s)** — at a known timestamp T0, force OpenAI into failure by
   repeatedly setting `mock:fail_count:openai` to a large number.
4. **Recovery window (60s)** — stop forcing failures, keep sending load, and
   watch how fast the breaker closes again.
5. **Log every request** — timestamp, which provider was requested, which
   actually served it (to detect fallback), the outcome, and whether it was a
   fast-reject (breaker open, no provider call) or a real attempt that failed.
6. **Poll the breaker gauge** — separately, sample `/metrics`'s
   `gateway_circuit_breaker_state` every 0.25s so we have *ground truth* on when
   the breaker opened and closed, independent of guessing from responses.

### 5.2 Two non-obvious design decisions

**Refreshing the failure counter.** `mock:fail_count:openai` is a *decrementing*
counter, not a boolean "provider is down" flag. Under sustained load it would
drain to zero partway through the 30s outage (each `/generate` attempt can burn
up to 3 decrements via its retries). So the test **re-sets it every 0.5s** for
the whole outage window. And — this was a bug we hit and fixed — it **deletes
the key at the end of the window**, otherwise the last large value leaves
thousands of forced failures still queued, the outage never actually ends, and
recovery is never observed.

**Measuring recovery two independent ways.** We report recovery time both by
*traffic* (first real successful request after the outage) and by *gauge*
(first moment `/metrics` reports the breaker CLOSED). If those two disagree by
more than 2 seconds, the test flags it — because a number you can't
cross-validate isn't one you should trust.

### 5.3 The honest caveat about fallback

`/generate` has automatic fallback, so during an OpenAI outage most `/generate`
requests **still succeed** — served by Anthropic under a different model. If we
just reported one big "success rate" number, it would look like the outage
barely mattered, which would be misleading. So the report **separates**:

- `/generate` successes *via fallback* (outage was handled by rerouting), vs.
- `/generate` successes *directly* (provider actually worked), vs.
- `/generate/stream` — which has **no fallback**, so its failures are honest,
  un-masked failures.

This way a high headline number can't hide the outage's real effect.

### 5.4 What we verified before trusting any number

Before running the full test, we manually confirmed each mechanism worked:

- Forcing OpenAI failure → `/generate` falls back to Anthropic (response model
  becomes `claude-sonnet-5`). ✓
- Enough stream failures → breaker gauge flips from `0.0` (closed) to `2.0`
  (open). ✓
- Once open → subsequent stream requests get instant **HTTP 503** (fast-reject,
  no provider call). ✓
- After the 30s cooldown → first request probes (HALF_OPEN) → succeeds → breaker
  returns to CLOSED (`0.0`). ✓

We also discovered and fixed a real gap: the *streaming* mock path didn't
support the forced-failure mechanism at all (only the non-streaming path did),
so mid-stream failures weren't testable. We added it to both
`stream_openai`/`stream_anthropic`.

### 5.5 Results (3 runs, reported as ranges — no cherry-picking)

| Metric | Range across 3 runs | Verdict |
|---|---|---|
| Overall success rate (incl. rate-limits) | 20.97% – 32.18% (mean 24.72%) | **Unstable — flagged.** Dominated by rate-limit 429s, not a claimable number. |
| **Success rate excluding rate-limits** | **89.37% – 89.78% (mean ~89.5%)** | **Stable and defensible.** This is the real "success rate during outage." |
| Time to first fast-reject (breaker opens) | 0.39s – 3.77s (mean 2.58s) | Flagged as inherently variable (depends when the 5th failure lands). |
| Recovery time (by traffic) | 4.47s – 5.05s (mean 4.78s) | Stable. |
| Recovery time (by breaker gauge) | 4.72s – 5.20s (mean 4.96s) | Stable, **agrees with the traffic number within 0.3s**. |
| Budget hygiene (no stuck reservations) | PASS every run | Failed streams released reservations cleanly. |

**The defensible claim:** *"~89% request success rate during a 30-second
provider outage, with automatic circuit-breaker recovery in ~5 seconds, verified
two independent ways."*

**Two things you must be able to explain if asked:**

1. **Why is recovery ~5s, not the 30s cooldown?** Because the breaker's cooldown
   timer resets every time it re-trips on a failed probe during the sustained
   outage. So the last trip often happens well before the outage ends, meaning
   the 30s cooldown elapses *during* the outage — leaving the breaker ready
   (HALF_OPEN) right at outage-end, so the first success closes it almost
   immediately. The 30s cooldown is real; the *observed recovery from outage-end*
   is shorter because of when the last trip lands.

2. **Why exclude rate-limits from the success rate?** Because at the tested
   request rate, the rate limiter rejects a large share of requests regardless
   of any outage — that's a property of the workload, not the failure. Including
   them would deflate the number for a reason unrelated to what we're measuring.
   Both numbers are reported so nothing is hidden.

**Output artifacts:** a raw per-request log (`chaos_test_log.jsonl`, one JSON
object per request plus breaker samples) and the printed summary above.

---

## 6. The horizontal scale test

**Goal:** measure throughput and latency with **multiple gateway instances**
behind a load balancer, and check whether scaling out horizontally increases
capacity — while confirming that quota enforcement stays correct across
instances.

**Files:** `scale_test.py`, `docker-compose.scale.yml`, `nginx.scale.conf`

### 6.1 "Horizontal scaling" explained

- **Vertical scaling** = make one machine bigger (more CPU/RAM).
- **Horizontal scaling** = run more copies of the app and spread traffic across
  them.

For horizontal scaling to work, the copies must **share state** correctly. Our
gateway keeps rate-limit and budget state in Redis, and all instances point at
the *same* Redis. So in principle, three gateway instances should enforce one
team's quota globally — not let the team get 3× its limit by spreading requests
across the three.

### 6.2 The setup

- **`docker-compose.scale.yml`** — an overlay that runs **3 replicas** of the
  gateway (all sharing the one Redis) and adds an **nginx** container as a
  load balancer.
- **`nginx.scale.conf`** — configures nginx to **round-robin** (distribute
  requests evenly) across the 3 replicas. nginx becomes the single entry point
  on port 8000; the replicas no longer publish their own ports.

### 6.3 Correctness first (the most important check)

Per good practice, we verified **correctness before performance** — a scale-out
that breaks quota enforcement is a regression, not a win.

**Rate-limit check:** we fired 40 requests for a *single team* through the load
balancer (so they were spread across all 3 instances) and counted successes.
Result: **exactly 10 succeeded, 30 got 429** — identical to a single instance.
The team was capped at its global bucket capacity of 10, *not* 30. **PASS.**

**Budget check:** we confirmed the spend counter (`spend:{team}:daily`) is a
single shared Redis key that every instance increments — so a team can't get 3×
its budget across instances either. **PASS.**

This is the strongest result of the scale test: *"verified rate-limit and budget
quota enforcement remain correct across horizontally scaled instances sharing
Redis state."*

### 6.4 The throughput result — and why it's nuanced

Here's where the honest reporting matters. We measured throughput (requests per
second) for single-instance vs. 3-instance, under identical conditions:

| Topology (warmed, tracing-noise removed, 5 runs) | Throughput @ 500 concurrent requests |
|---|---|
| Single instance | ~172 – 195 req/s |
| 3 instances + nginx | ~157 – 200 req/s |

**These ranges overlap. Three instances was NOT faster than one.** We did not
dress this up as a scaling win, because it wasn't one.

### 6.5 Why scaling didn't help — the diagnosis (verified, not guessed)

We investigated instead of hand-waving:

1. **Every container sat below 1% CPU** during load, yet latency was 2.5–5
   seconds. Nothing was resource-bound — requests were *waiting in line*, not
   doing work. That rules out "the gateways are maxed out."

2. **The `ConsoleSpanExporter` was flooding logs.** The tracing setup
   (`app/tracing.py`) printed every internal tracing "span" as multi-line JSON
   to standard output — **~90,000 log lines for 500 requests**. Those synchronous
   writes serialize the event loop and throttle throughput. We added an env flag
   `DISABLE_CONSOLE_SPANS=1` (default keeps the old behavior) to turn it off for
   load runs. This helped but wasn't the whole story.

3. **The load-test client is the real bottleneck.** When we ran *two* client
   processes in parallel against a *single* instance, aggregate throughput
   (~185 req/s) exceeded what one client process could generate alone
   (~158 req/s). In other words, **one gateway instance can already serve more
   traffic than a single-threaded Python test client can produce.** Adding more
   server instances can't show gains until the *client* can push harder.

**Conclusion (honest):** the scale-out infrastructure is correct (round-robin
verified, shared-state correctness verified), but our current load generator
saturates client-side before the server does. Demonstrating linear horizontal
scaling would require a **distributed/multi-process load generator**, which we
did not build. We report "correctness verified; throughput is client-bound"
rather than inventing a "3× throughput" number the method can't support.

### 6.6 A note on the throughput ceiling and the queue

Even isolated from tracing noise, single-instance throughput plateaued around
~180 req/s with multi-second latency while CPU sat idle. This points at the
**worker pool + queue** design (section 2.6): with a small fixed number of
workers pulling from a queue, effective concurrency is capped regardless of how
many requests arrive at once. Raising `NUM_WORKERS` from 4 to 32 barely moved
the number, which suggests the serialization is a mix of the queue design and
the single-event-loop client — not simply "too few workers." This is a good
candidate for future investigation if a higher single-instance number is needed.

---

## 7. Bugs found along the way

Finding real bugs while measuring is a feature, not a distraction — each of
these would have quietly corrupted a result or leaked resources.

1. **`BackgroundTask` skipped on stream failure** (streaming budget work). Fixed
   by moving reconciliation into a `try/finally` wrapper (`_tracked_stream`) so
   it always runs. Without this, failed streams leaked budget reservations.

2. **Streaming mocks couldn't simulate failure.** Only the non-streaming mock
   path honored `mock:fail_count`. Added it to the streaming path so mid-stream
   outages became testable.

3. **`docker exec ... redis-cli flushdb >/dev/null` silently failed on Windows.**
   The first draft of the scale test used a bash-style shell redirect that
   `cmd.exe` couldn't parse ("the system cannot find the path specified"), so
   Redis was never actually flushed between runs — every run after the first
   measured a rate-limit "429 storm" from drained buckets instead of real
   throughput. Fixed by flushing via the Python Redis client.

4. **Chaos outage never ended.** The outage controller left a large
   `mock:fail_count` value queued after the window, so failures kept happening
   and recovery was never observed. Fixed by deleting the key at outage-end.

5. **`ConsoleSpanExporter` throttled throughput.** Per-span synchronous stdout
   logging (~90k lines / 500 requests) capped request throughput with idle CPU.
   Gated behind `DISABLE_CONSOLE_SPANS` so load runs can measure real capacity.

6. **Env var didn't propagate to scale replicas.** An early scale comparison had
   tracing *on* for the 3-instance run but *off* for the single-instance
   baseline — an invalid apples-to-oranges comparison. Fixed by explicitly
   passing `DISABLE_CONSOLE_SPANS` through both compose files, then re-running
   both topologies under identical conditions.

---

## 8. Every file we created or changed

### Created

| File | Purpose |
|---|---|
| `chaos_test.py` | The chaos/outage test: sustained load, forced outage, breaker + budget + recovery measurement. |
| `scale_test.py` | The horizontal scale test: throughput/latency sweep + cross-instance quota check. |
| `docker-compose.scale.yml` | Overlay: 3 gateway replicas + nginx load balancer, shared Redis. |
| `nginx.scale.conf` | nginx round-robin config in front of the replicas. |
| `app/budget_reserve.lua` | Atomic budget reservation for streaming (check + reserve). |
| `app/budget_reconcile.lua` | Atomic reservation release + real-cost recording. |
| `docs/chapter-08-load-and-chaos-testing.md` | This report. |

### Changed

| File | Change |
|---|---|
| `app/main.py` | Added streaming budget reservation, `_tracked_stream` wrapper (reliable reconcile + breaker signaling), pre-flight breaker check on `/generate/stream`. |
| `app/budget.py` | Added `reserve_budget()` / `reconcile_budget()` wrappers loading the two new Lua scripts. |
| `app/providers/openai_provider.py`, `app/providers/anthropic_provider.py` | Added forced-failure support to the *streaming* mock paths (previously only non-streaming had it). |
| `app/tracing.py` | Gated `ConsoleSpanExporter` behind `DISABLE_CONSOLE_SPANS` (default = original behavior). |
| `load_test.py` | Made request count overridable via `LOAD_TEST_TOTAL_REQUESTS` env var so the scale test can sweep concurrency levels. |
| `docker-compose.yml` | Pass `DISABLE_CONSOLE_SPANS` through to the gateway service. |
| `tests/test_streaming.py` | Updated comments to match the new reconcile flow; added two tests (pre-flight 402 rejection, reservation release on mid-stream failure). |

### Output artifacts (generated by runs, not source)

- `chaos_test_log.jsonl` — raw per-request log from the last chaos run.
- `scale_test_results.jsonl` — raw per-run scale results.

---

## 9. How to reproduce all of it

All commands run from the repo root. Requires Docker + Docker Compose and a
local Python with `httpx` and `redis` installed (`pip install httpx redis`).

### Integration tests

```bash
docker compose -f docker-compose.yml -f docker-compose.test.yml \
    up --build --abort-on-container-exit tests
```
Expected: `8 passed`.

### Chaos test

```bash
# Start the gateway in MOCK mode (required for forced failures)
docker compose -f docker-compose.yml -f docker-compose.test.yml \
    up -d --build llm-gateway redis

# Run it (single run, or --runs 3 for a variance report)
python chaos_test.py --runs 3
```
Reads the raw log into `chaos_test_log.jsonl` and prints the summary. Because
the circuit breaker's state lives in memory per gateway process, restart the
gateway container between runs if you want each run to start from a fully
CLOSED breaker.

### Scale test

```bash
# --- Single-instance baseline (tracing noise off for a fair number) ---
export DISABLE_CONSOLE_SPANS=1
docker compose -f docker-compose.yml -f docker-compose.test.yml \
    up -d --build llm-gateway redis
python scale_test.py --label single --levels 500 1000 1500 --repeats 3

# --- 3-instance + load balancer ---
docker compose -f docker-compose.yml -f docker-compose.test.yml \
    -f docker-compose.scale.yml up -d --build
python scale_test.py --label 3-instance --levels 500 1000 1500 --repeats 3
```
Results append to `scale_test_results.jsonl`. **Tip:** discard the first run of
each topology as a warmup — throughput ramps for the first run or two as
connection pools and Python internals warm up.

### Restore the normal dev stack

```bash
docker compose -f docker-compose.yml -f docker-compose.test.yml \
    -f docker-compose.scale.yml down --remove-orphans
docker compose up -d
```

---

## 10. Honest summary of results (what you can actually claim)

**Defensible claims:**

- ✅ **~89% request success rate during a 30-second simulated provider outage**,
  with the circuit breaker fast-rejecting within a few seconds and **recovering
  in ~5 seconds**, cross-validated two independent ways (live traffic + the
  Prometheus breaker-state gauge). Stable across 3 runs.
- ✅ **Budget accounting stays correct under failure** — streams that die
  mid-flight release their reservations and record no phantom spend (verified by
  a dedicated integration test *and* re-checked live during every chaos run).
- ✅ **Quota enforcement holds under horizontal scale-out** — one team spread
  across 3 instances behind a load balancer is still capped at its single global
  rate-limit bucket and budget, because all instances share Redis state.
- ✅ **8/8 integration tests pass**, including after all the streaming and scaling
  changes.

**Claims to NOT make (and why):**

- ❌ *"Horizontal scaling gave 3× throughput."* It didn't — 3 instances measured
  the same as 1 because the single-threaded test client saturates before the
  server does. The infrastructure is correct; the measurement is client-bound.
  Proving linear scaling needs a distributed load generator we didn't build.
- ❌ *"X% overall success rate"* without the "excluding rate-limits" qualifier —
  the overall number is dominated by outage-independent 429s and is unstable
  run-to-run.

**The meta-point:** the value of this work is as much in the *bugs found and the
honesty about limits* as in any single number. A reviewer who asks "did you
verify that?" gets a real answer for every claim above, and an honest "here's
why not" for every claim we chose not to make.

---

## 11. Glossary

| Term | Plain-English meaning |
|---|---|
| **Atomic** | An operation that happens all-at-once as one indivisible step, so concurrent operations can't interleave and corrupt shared state. |
| **Circuit breaker** | A safety mechanism that stops sending requests to a failing provider and fast-rejects instead, then periodically tests for recovery. |
| **CLOSED / OPEN / HALF_OPEN** | Breaker states: working / tripped (fast-rejecting) / testing-recovery. |
| **Chaos testing** | Deliberately injecting failures to measure how the system copes. |
| **Concurrency** | Multiple things happening at overlapping times. |
| **Fallback / failover** | Rerouting a failed request to a backup provider. |
| **Fast-reject** | Rejecting a request instantly (breaker open) without calling the dead provider. |
| **Horizontal scaling** | Running more copies of the app and spreading traffic across them. |
| **Integration test** | A test that exercises the real system end-to-end over HTTP, not just one function in isolation. |
| **Latency** | How long a single request takes (often reported as p50 = median, p95 = 95th percentile = "worst 5% start here"). |
| **Load balancer** | A component (here, nginx) that distributes incoming requests across multiple server instances. |
| **Lua script (in Redis)** | A small program Redis runs atomically, used for multi-step logic that must not be interrupted. |
| **Mock provider** | A fake stand-in for the real OpenAI/Anthropic API that returns instant canned responses. |
| **Optimistic reservation** | Reserving an estimated cost up front, then reconciling to the real cost afterward. |
| **Rate limiting** | Capping how many requests a client can make per unit time. |
| **Reconcile** | Settle up after the fact — release the reservation and record the true cost. |
| **Redis** | A fast in-memory database used here for shared rate-limit/budget/mock state. |
| **Round-robin** | Distributing requests evenly across instances in rotation. |
| **SSE (Server-Sent Events)** | A format for streaming a response as a series of `data:` lines over one connection. |
| **Throughput** | How many requests per second the system handles. |
| **Token (LLM)** | A chunk of text (~4 chars) that providers bill by. |
| **Token (rate-limit)** | A unit in the rate-limit bucket; one per request. Unrelated to LLM tokens. |
| **Token bucket** | A rate-limiting algorithm: a bucket of capacity N that refills over time; each request spends one. |
| **Worker pool** | A fixed set of workers that pull queued requests and process them, controlling concurrency. |
| **429 / 402 / 503** | HTTP status codes: rate-limited / payment-required (over budget) / service-unavailable (fast-reject or provider down). |

---

*End of Chapter 8.*
