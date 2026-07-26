# Chapter 7: Testing — Integration Tests, Mock Providers, and Load Testing

This chapter took the gateway from "the code exists and I've eyeballed it working"
to "there's a real test suite that proves it, plus honest performance numbers instead
of a guess." It has three parts, built in this order because each depends on the
last: a pytest integration-test convention, a mock-provider layer that lets those
tests run fast and free, and a load/scaling test that turned into a real debugging
investigation rather than a clean success story.

---

## 1. Choosing a testing convention — and why it wasn't the obvious one

Before writing a single test, the repo already had two loose scripts at the root —
`test_priority.py` and `test_circuit_breaker.py` — that were plain `asyncio` scripts
hitting `http://localhost:8000` over a real socket against whatever was already
running in `docker compose`, with results printed for a human to read. No `pytest`,
no fixtures, no assertions — a manual-proof style, not an automated one.

The natural instinct might have been to introduce `pytest` with `httpx.AsyncClient` +
`ASGITransport`, running the FastAPI app in-process without a real network socket —
that's the standard, fast way to integration-test a FastAPI app. But that would have
been a first-time shift in how this project verifies itself, and it would have
sidestepped the actual Docker network entirely: no real `redis` hostname resolution,
no real container boundary between the endpoint and the worker, no real
`docker-compose` topology. Given the choice, the decision was **pytest, but still
against the real, running `docker-compose` stack** — matching the existing philosophy
(hit the real thing) while adding the ability to assert instead of eyeball.

### The infrastructure this required

```
tests/
├── conftest.py       # fixtures: redis_client, test_client
├── test_rate_limiting.py
├── test_budget.py
├── test_fallback.py
└── test_streaming.py

Dockerfile.test           # pytest + pytest-asyncio layered on the real app image
docker-compose.test.yml   # override file: adds a `tests` service, sets MOCK_PROVIDERS=1
pytest.ini                # asyncio_mode = auto
```

Two real, deliberate decisions here:

- **A separate `Dockerfile.test`, not baked into the main image.** `pytest`/
  `pytest-asyncio` are test-only dependencies — the real gateway image (built from
  the original `Dockerfile`) never needs them. `requirements.txt` still only lists
  what the running service actually needs (`httpx`, `redis`, etc., which the app
  needs anyway); `Dockerfile.test` layers the test tools on top in its own image.
- **A `docker-compose.test.yml` override, not edits to the main compose file.**
  Compose supports layering multiple `-f` files, where later files override/extend
  earlier ones. This meant the tests service and its `MOCK_PROVIDERS=1` environment
  variable only exist when explicitly requested:

  ```bash
  docker compose -f docker-compose.yml -f docker-compose.test.yml \
      run --rm --build tests pytest tests/ -v
  ```

  A plain `docker compose up` (no `-f docker-compose.test.yml`) is completely
  unaffected — the gateway still talks to real providers by default.

### `conftest.py` — two fixtures, one subtlety

```python
@pytest.fixture
def redis_client():
    client = redis.Redis(host=REDIS_HOST, port=6379, decode_responses=True)
    yield client
    client.flushdb()  # clean slate — don't let one test's tokens bleed into the next
    client.close()


@pytest_asyncio.fixture
async def test_client():
    _wait_for_gateway()
    async with httpx.AsyncClient(base_url=GATEWAY_URL, timeout=30) as client:
        yield client
```

`redis_client` is a plain **sync** `redis.Redis` — this fixture runs in the test
*process*, not inside the gateway container, so it's a separate connection making its
own blocking calls in its own control flow; nothing else is waiting on it. Its
teardown (`flushdb()`) is what keeps tests independent and repeatable: without it,
running the same test twice in a row would start from whatever Redis state the first
run left behind (a partially-drained rate-limit bucket, leftover spend, a stale
mock-failure counter), and a failure on the second run could be blamed on the wrong
thing entirely.

`test_client` hits the *real* running gateway container over the Docker network
(`http://llm-gateway:8000`), not an in-process ASGI transport. That only works if the
gateway is actually up and accepting connections by the time a test starts — and
`depends_on` in Compose only waits for the container to *start*, not for `uvicorn`
inside it to finish booting. So `_wait_for_gateway()` polls `/healthz` with a
timeout before yielding the client, rather than trusting Compose's weak startup
ordering:

```python
def _wait_for_gateway(timeout_seconds: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{GATEWAY_URL}/healthz", timeout=2.0)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError(...)
```

---

## 2. Mock providers — the discipline of mocking only what has to be mocked

### Why the provider calls need mocking but Redis doesn't

Every test in this suite hits the real `/generate` endpoint, which means every test
also exercises real auth, real rate limiting, real budget tracking, real circuit
breaker state, and real Redis — none of that is mocked. The **only** thing mocked is
the actual outbound HTTP call to Anthropic/OpenAI, for two reasons: it costs real
money, and it depends on a third party being up. Everything else is either free
(Redis, running locally) or *is* the thing being tested (the Lua script's atomicity
can't be proven against a mocked Redis — a mock is just a Python dict with no real
concurrency semantics, so the test would pass even if the actual script were broken).

This is the same reasoning as `MockProvider` from earlier in the project, applied
consistently: mock the boundary that's expensive/unreliable, never mock the thing
you're actually trying to verify.

### `MOCK_PROVIDERS=1` — an env-gated bypass at the real call site

```python
# app/providers/openai_provider.py
async def call_openai(payload: dict) -> dict:
    if os.environ.get("MOCK_PROVIDERS") == "1":
        if await consume_forced_failure("openai"):
            request = httpx.Request("POST", OPENAI_URL)
            response = httpx.Response(503, request=request, text="mock forced failure")
            raise httpx.HTTPStatusError("mock forced failure", request=request, response=response)

        return {
            "model": payload["model"],
            "choices": [{"message": {"content": "mock openai response"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    headers = {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}
    async with httpx.AsyncClient() as client:
        response = await client.post(OPENAI_URL, json=payload, headers=headers, timeout=60.0)
        response.raise_for_status()
        return response.json()
```

The mock returns a payload shaped exactly like the real API's response — `raw["choices"][0]["message"]["content"]`,
`raw["usage"]["prompt_tokens"]` — so `from_openai_response` (the real parsing function)
runs completely unchanged against fake input. The mock never bypasses parsing logic;
it only replaces the network call.

### The mock's first bug: a fail-counter that fires on every call, not just forced ones

The first version of the failure-forcing mechanism used a plain Redis `DECR`:

```python
remaining = _mock_control.decr("mock:fail_count:openai")
if remaining >= 0:
    raise ...  # forced failure
```

This looked fine in isolation, but had a real, latent problem: `DECR` on a
nonexistent key auto-creates it at `0` and decrements to `-1` — meaning **every**
mock call touches this counter, whether or not any test intended to force a failure.
Running the full suite together left stray keys (`mock:fail_count:anthropic`,
`mock:fail_count:openai`) sitting at arbitrary negative values in Redis afterward.
Harmless *today*, since any negative value still correctly falls through to success —
but it's exactly the kind of drift that would silently break a future test if it ever
needed the counter to mean "untouched" versus "consumed."

The fix, `app/providers/mock_control.py`:

```python
_DECR_IF_EXISTS = _client.register_script(
    """
    if redis.call("EXISTS", KEYS[1]) == 1 then
        return redis.call("DECR", KEYS[1])
    end
    return nil
    """
)

async def consume_forced_failure(provider: str) -> bool:
    remaining = await _DECR_IF_EXISTS(keys=[f"mock:fail_count:{provider}"])
    return remaining is not None and remaining >= 0
```

Same idea as the rate limiter's own Lua script — use Redis's atomicity to make
"check-then-act" a single indivisible operation — but here the point isn't
concurrency safety, it's **scope**: a test that never calls `SET
mock:fail_count:anthropic` leaves that key completely untouched, forever, so
unrelated tests' normal traffic can never accidentally consume or create it. Verified
by running the full suite and confirming `redis-cli KEYS '*'` came back empty
afterward — not just "the tests passed," but "nothing leaked."

### Mocking the streaming path without bypassing the SSE parser

The harder design question: `stream_anthropic`/`stream_openai` don't just return a
dict — they parse a live Server-Sent-Events stream line by line, dispatching on event
type, and populate a `usage_holder` side-channel used later to bill the request. A
naive mock (`if MOCK_PROVIDERS: yield ["fake", "chunks"]; usage_holder["usage"] = {...}`)
would skip that parsing entirely — proving the endpoint plumbing works, but never
touching the actual thing most likely to have a bug (Anthropic's two-event usage
split, OpenAI's `choices[0]` guard against an `IndexError` on the empty-choices usage
chunk).

The real fix (`app/providers/mock_stream.py`) mocks one layer lower — it fakes the
*transport*, not the *parser*:

```python
class FakeStreamedResponse:
    """An async context manager whose aiter_lines() yields canned SSE 'data: ...'
    lines. Lets the real parsing loop (json.loads, event dispatch, usage_holder
    population) run unchanged against fake input."""

    def __init__(self, lines: list[str]):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def raise_for_status(self):
        pass

    async def aiter_lines(self):
        for line in self._lines:
            yield line
```

`anthropic_sse_lines(...)`/`openai_sse_lines(...)` build lists of real-shaped
`"data: {...}"` JSON lines (including OpenAI's genuinely tricky final usage chunk,
which arrives with an *empty* `choices` list). The provider files were refactored to
extract the actual parsing loop into a shared helper (`_consume_anthropic_stream`,
`_consume_openai_stream`) called identically from both the mock path and the real
path:

```python
async def stream_openai(payload: dict, usage_holder: dict):
    if os.environ.get("MOCK_PROVIDERS") == "1":
        lines = openai_sse_lines(["mock ", "openai ", "stream"], input_tokens=1, output_tokens=1)
        async with FakeStreamedResponse(lines) as response:
            response.raise_for_status()
            async for chunk in _consume_openai_stream(response, usage_holder):
                yield chunk
        return

    headers = {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}
    async with httpx.AsyncClient() as client:
        async with client.stream("POST", OPENAI_URL, json=payload, headers=headers, timeout=60.0) as response:
            response.raise_for_status()
            async for chunk in _consume_openai_stream(response, usage_holder):
                yield chunk
```

**A real bug caught mid-edit, not after:** the first draft of this refactor
constructed `httpx.AsyncClient()` outside of any `async with` in the real branch,
because the branching logic got flattened into a single shared `async with stream_cm`
block. That would have silently leaked a connection on every real (non-mock)
streaming call — a genuine resource leak that never would have shown up in a quick
manual test, only under sustained real traffic. Caught by re-reading the diff before
moving on, and fixed by keeping the real path's `async with httpx.AsyncClient() as client:`
wrapping intact, structured as two separate branches that both funnel into the same
shared parsing helper — rather than one clever unified code path that quietly broke
resource cleanup.

---

## 3. The four integration tests

### Rate limiting — proving atomicity, not just "limiting happens"

```python
async def test_rate_limit_allows_exactly_bucket_capacity_under_concurrency(test_client, redis_client):
    concurrent_requests = BUCKET_CAPACITY * 5  # 50

    results = await asyncio.gather(*[make_request() for _ in range(concurrent_requests)])

    successes = sum(1 for status in results if status == 200)
    rejections = sum(1 for status in results if status == 429)

    assert successes == BUCKET_CAPACITY  # exactly 10, not <= 10
    assert rejections == concurrent_requests - BUCKET_CAPACITY
```

Two details that separate this from a shallow test:

- **`asyncio.gather`, not a loop.** Sequential requests can never overlap inside the
  Lua script, so they could never expose a race condition even if the script's
  `HMGET`/compute/`HMSET` sequence weren't atomic. Only genuine concurrency tests what
  this test claims to test.
- **`== BUCKET_CAPACITY` exactly, not `<=`.** A looser assertion would pass even if the
  limiter were *overly* strict and rejected everything. The exact match is what
  actually proves the bucket honors its capacity precisely.

### Budget enforcement — both directions, not just the rejection

The first version of this test only checked the over-budget `402` case. That's a real
gap: a test that only checks rejection could pass even if the threshold comparison
were backwards (e.g., always rejecting regardless of spend). The fix was a companion
test seeding spend comfortably under both the 80% warning line and the 100% rejection
line:

```python
async def test_request_succeeds_when_under_budget(test_client, redis_client):
    seeded_spend = DAILY_BUDGET * 0.79
    redis_client.set(f"spend:{TEAM_ID}:daily", seeded_spend)
    response = await test_client.post("/generate", ...)
    assert response.status_code == 200
```

Both tests seed Redis directly via `SET` rather than making dozens of real calls to
organically accumulate spend — the same "isolate the thing being tested from
unrelated setup cost" principle as the mock providers.

### Same-request fallback — the test that required a real architecture fix first

This test almost couldn't be written as intended. The original `queue.py` had four
duplicated if/else blocks (one per provider-is-primary × circuit-is-open/closed
combination); on a primary-provider failure, each branch did `record_failure(); raise`
— re-raising immediately rather than attempting the fallback provider *within the same
request*. Fallback only happened on a **later** request, once a prior request's
failures had already tripped the circuit breaker open. Writing this test is what
surfaced that gap concretely, since there was no way to observe "one request tries
anthropic, fails, and falls back to openai in the same trace."

The fix (a separate, earlier piece of work) extracted a shared `attempt_provider()`
helper in `queue.py` and restructured `worker()` so a primary-provider failure — circuit
already open, *or* retries genuinely exhausted — falls through to the fallback
provider inline:

```python
try:
    result = await attempt_provider(primary, req)
except Exception:
    fallback_model = FALLBACK_MODEL.get(req.model)
    if fallback_model is None or fallback_model not in team["allowed_models"]:
        raise CircuitOpenError(f"{primary} is unavailable and no authorized fallback is available")
    fallback_triggered_total.add(1, {"from_provider": primary, "to_provider": fallback})
    fallback_req = req.model_copy(update={"model": fallback_model})
    try:
        result = await attempt_provider(fallback, fallback_req)
    except CircuitOpenSkip:
        raise CircuitOpenError("Both providers are unavailable")
```

The test itself forces genuine retry exhaustion — not a pre-tripped circuit — via the
Redis-backed fail-counter, letting `call_with_retry`'s real backoff sleeps run for
real (~3-5 seconds for 3 exhausted attempts) rather than mocking away the timing:

```python
redis_client.set("mock:fail_count:anthropic", 3)  # exhausts all 3 call_with_retry attempts
response = await test_client.post("/generate", json={"model": "claude-sonnet-5", ...}, ...)

assert response.status_code == 200
assert response.json()["model"] == "gpt-4o-mini"  # signal 1: response reflects the fallback model

after = await _fallback_triggered_count(anthropic_to_openai=True)
assert after == before + 1  # signal 2: the Prometheus counter agrees
```

**Two independent signals, not one.** `response.json()["model"]` works because
`attempt_provider` swaps `req.model` to the fallback model before calling it — so a
successful fallback response's `model` field literally differs from what was
requested. `gateway_fallback_triggered_total` (read directly from `/metrics/`, parsed
out of Prometheus's text exposition format) is a completely independent measurement.
One signal being coincidentally right for the wrong reason is a real risk; two
independent signals agreeing is the same discipline used for the OPEN→HALF_OPEN→CLOSED
proof and the trace-verification work in earlier chapters.

Also caught here: the fallback target (`gpt-4o-mini` for `claude-sonnet-5`) wasn't in
`team-a-key`'s `allowed_models` at all. That's a genuinely different *kind* of finding
than the others — not "does existing behavior hold under a new condition," but **a
latent production bug**: if team-a's primary provider had ever actually gone down for
real, their own configured fallback would have silently failed too, because they
weren't authorized to use it. Fixed permanently in `app/auth.py`, not routed around in
the test.

### Streaming integrity — chunk reconstruction and the usage/budget side-channel

Two sub-tests, because "integrity" here means two genuinely different things:

```python
async def test_stream_chunks_reconstruct_full_text_in_order(test_client, redis_client):
    async with test_client.stream("POST", "/generate/stream", ...) as response:
        chunks = [chunk async for chunk in response.aiter_text()]
    assert "".join(chunks) == "mock openai stream"
```

```python
async def test_stream_usage_holder_populates_and_charges_budget(test_client, redis_client):
    redis_client.delete(f"spend:{TEAM_ID}:daily")
    async with test_client.stream("POST", "/generate/stream", ...) as response:
        async for _ in response.aiter_text():
            pass  # drain fully — the background task fires after this completes

    spend = await _poll_spend(redis_client)
    assert spend == pytest.approx(MOCK_STREAM_COST)
```

The second one is the trickier mechanism. `_finalize_stream_cost` runs as a Starlette
`BackgroundTask` on the `StreamingResponse`, which Starlette only executes **after**
the full response body has been sent to the client — there's a small, real window
between "client finished reading the last byte" and "server finished running the
background task." Reading Redis exactly once, immediately after the stream closes,
would be a flaky test — sometimes racing ahead of the background task. `_poll_spend`
polls with a timeout instead of trusting a single read to land at the right moment.

This test also proves something the non-streaming budget test cannot: that
`usage_holder` actually gets populated by the real SSE parsing logic, and that the
streaming code path reaches the *same* `record_spend_and_check_budget`/`INCRBYFLOAT`
path as the non-streaming endpoint — i.e., that these two genuinely separate code
paths don't silently diverge on whether spend gets recorded at all.

---

## 4. Load testing — a different question, a different tool

Everything above proves *correctness*: does the right thing happen. A load test
answers a different question: how much overhead does the gateway itself add, and does
it hold up under real concurrent volume? `load_test.py` matches the existing
plain-`asyncio` convention (no `locust`, no new DSL) — `httpx.AsyncClient` +
`asyncio.gather` at higher volume than any correctness test above, measuring wall-clock
latency per request and computing percentiles directly, rather than asserting
pass/fail.

### Why it runs against `MOCK_PROVIDERS`, not real providers

Real providers would conflate two different numbers: "how slow is my gateway" and
"how slow is Anthropic today." Mocking the provider call reduces it to a fixed,
near-zero delay, isolating the gateway's own contribution — queueing, auth, rate
limiting, circuit breaker checks — which is the more honest, reproducible number to
report, and it costs nothing to run repeatedly.

### The rate limiter had to be defeated first, deliberately

With only `team-a-key` (capacity 10, one bucket), 500 concurrent requests would mostly
measure "how fast does the rate limiter reject things," not gateway processing
overhead. The fix was adding **55 separate load-test team keys** — each with its own
independent bucket, keyed by `team_id` in Redis — so aggregate burst capacity (550)
comfortably exceeds the test's 500-request volume:

```python
LOAD_TEST_TEAM_COUNT = 55
TEAM_CONFIG.update({
    f"load-test-key-{i}": {"team_id": f"load-test-{i}", "allowed_models": [...]}
    for i in range(1, LOAD_TEST_TEAM_COUNT + 1)
})
```

This mirrors how a real multi-tenant gateway would actually be loaded — spread across
many tenants — rather than gaming one team's config to make a bigger number possible.

### Bug #1 the load test surfaced: blocking Redis calls inside async handlers

The very first real run produced absurd numbers — 4-9 **seconds** per mocked request,
which should return near-instantly. `check_rate_limit` and
`record_spend_and_check_budget` were both using the **synchronous** `redis` client
(`import redis`, not `redis.asyncio`) called directly inside `async def` request
handlers. A blocking network call made inside a coroutine blocks the *entire event
loop* for its duration, not just that one request — with 500 requests all hitting
this same blocking call, they serialize almost entirely on it.

Fixed by converting `rate_limit.py`, `budget.py`, and `mock_control.py` to
`redis.asyncio`, and threading `await` through every call site (`check_rate_limit`,
`record_spend_and_check_budget`, `_finalize_stream_cost`, `consume_forced_failure` all
became `async def`). Verified in isolation — reverting just this fix via `git stash`,
re-running the corrected-connection-pool load test three times, then restoring the
fix and re-running three more times — gave a precise, honest attribution: **the async
fix is real, worth ~15-25% throughput** (sync: ~185-215 req/s; async: ~235-255 req/s),
smaller than initially assumed once a second, larger bug (below) was isolated out.

### Bug #2 the load test surfaced: the load-test script's own connection pool

This one turned out to be the dominant factor, and it wasn't a gateway bug at all —
it was in `load_test.py` itself. `httpx.AsyncClient()` with no explicit `limits=`
doesn't default to unbounded; it uses `httpx._config.DEFAULT_LIMITS`, which caps
`max_connections=100`. With 500 concurrent requests, 400 of them queue up **client-side**
waiting for a free connection — which dominates measured latency and has nothing to
do with the gateway.

The first fix attempt was itself subtly wrong:

```python
limits = httpx.Limits(max_connections=TOTAL_REQUESTS)  # looked right, wasn't
```

Constructing `httpx.Limits(max_connections=N)` **alone** silently resets
`max_keepalive_connections` to `None` (unbounded) rather than leaving it at
`DEFAULT_LIMITS`'s sane value of 20 — and an unbounded/oversized keepalive pool
measurably *degrades* performance in this `httpx`/`httpcore` version. Measured directly,
holding everything else constant, varying only `max_keepalive_connections`:

| `max_keepalive_connections` | Total wall time (500 requests) |
|---|---|
| 20 | ~1.87s |
| 50 | ~1.80s |
| 100 | ~1.84s |
| 200 | ~3.38s |
| 500 / unbounded | ~7s |

The final fix sets **both** fields explicitly:

```python
limits = httpx.Limits(max_connections=TOTAL_REQUESTS, max_keepalive_connections=100)
```

This was the dominant contributor to the initial 12-second/4-9-second-per-request
numbers — not a server bug, a test-tooling bug. Worth documenting explicitly rather
than quietly fixing, since a reviewer benefits from knowing the load-testing client
itself required tuning, and a future person modifying this script needs to know not
to "simplify" it back to a partial `Limits()` call.

### The final, honest baseline number

With both bugs fixed, run repeatedly for stability: **~2.0s wall time, ~240 req/s
throughput, P50 ~1.5s / P95 ~1.7s for successful requests** (500 concurrent requests,
5 team keys, mocked providers, async Redis). This is what the gateway's own
queueing/auth/rate-limit/circuit-breaker overhead actually costs, with provider
latency genuinely removed from the number.

---

## 5. The scaling experiment — a falsified hypothesis, honestly reported

### The hypothesis

`start_workers(4)` was hardcoded in `main.py`. The working assumption going in: P50
latency was dominated by requests queueing up waiting for one of only 4 `asyncio`
worker tasks to free up — so throughput should scale roughly linearly as workers
increase, until something else becomes the real bottleneck.

### Making it testable

`NUM_WORKERS` was parameterized as an environment variable (default `4`, preserving
existing behavior for a plain `docker compose up`):

```python
NUM_WORKERS = int(os.environ.get("NUM_WORKERS", "4"))
...
for coro in (start_workers(NUM_WORKERS), health_check_loop()):
```

`scaling_test.py` drives the same `load_test.py` measurement across several worker
counts, changing exactly one variable each time — recreating the gateway container
with a new `NUM_WORKERS`, waiting for real readiness, flushing Redis (so one run's
token-bucket/spend state can't leak into the next), then running the identical
500-request load test:

```python
WORKER_COUNTS = [4, 8, 16, 32]

for num_workers in WORKER_COUNTS:
    recreate_gateway(num_workers)
    await wait_for_gateway()
    flush_redis()
    results = await run_load_test()
```

But the rate-limiter fix from the load test (55 team keys) turned out to matter here
too, for a different reason: with only enough capacity for 50 requests to ever
succeed, adding workers wouldn't move the number *regardless* of whether the
hypothesis was true — the bottleneck would already be the rate limiter, not the
worker pool. The 55-team setup was necessary groundwork for this experiment
specifically, not just the plain load test.

### The result: flat, not linear

| Workers | Throughput | Scaling factor | P50 | P95 |
|---|---|---|---|---|
| 4 | 154.2 req/s | 1.00x | 2252.5ms | 2941.9ms |
| 8 | 153.5 req/s | 1.00x | 2436.0ms | 2824.2ms |
| 16 | 154.9 req/s | 1.00x | 2440.4ms | 2962.9ms |
| 32 | 144.6 req/s | 0.94x | 2626.5ms | 3169.7ms |

**Completely flat from 4 to 32 workers, with a slight regression at 32.** The
hypothesis was wrong. Adding workers did nothing — confirmed genuinely applied each
time (`docker compose exec llm-gateway env | grep NUM_WORKERS` checked directly after
the 32-worker run) and confirmed no startup errors, so this wasn't a config bug
producing a false flat line.

### Chasing the real bottleneck — a chain of eliminations, not a single answer

This is the part worth being honest about rather than polishing into a clean
narrative. Each layer tested explained *part* of the picture but never resolved to
one clean root cause:

1. **Redis connections**: `redis.asyncio.Redis()` already pools internally
   (`max_connections` defaults to `2^31`, not 1); 247 concurrent connections were
   observed live under load via `redis-cli INFO clients`. Not the bottleneck.
2. **Redis command latency**: `redis-cli INFO commandstats` showed `EVALSHA`
   (both Lua scripts) and `INCRBYFLOAT`/`EXPIRE` executing in **2-27 microseconds**
   per call. Not the bottleneck.
3. **CPU**: `docker stats` during a live run peaked at ~16% — nowhere near
   saturated. Not the bottleneck.
4. **The gap that actually mattered**: querying Prometheus directly —
   `histogram_quantile(0.5, sum(rate(gateway_request_duration_seconds_bucket[1m])) by (le))`
   — showed the gateway's *own* median request-processing time at ~50ms, while the
   client observed P50 of 1.5-2.5 **seconds**. A ~30-50x discrepancy between what the
   gateway's own instrumentation reports and what the client experiences means the time
   isn't being spent inside request processing at all — it's being spent somewhere
   between the client sending bytes and the server actually starting to process them.
5. **Multi-process uvicorn** (`--workers 4`, then `--workers 8`, tested via a one-off
   container attached to the same Docker network, diagnostic only — not committed to
   the Dockerfile): genuinely helped. Throughput rose from the ~150-155 req/s ceiling
   to **~240.8 req/s** (~55-60% improvement) at 4 processes, with only marginal further
   gain at 8. This confirms single-process connection/HTTP-parsing handling was a
   real, measurable contributing factor — but a substantial ~1.3-1.5s P50 baseline
   persisted even there.

### What this chapter concludes, honestly

The app-level `asyncio` worker pool was **never** the bottleneck — that hypothesis is
cleanly falsified by the flat 4→32 data. Multi-process `uvicorn` is a real, partial
answer, worth roughly half the gap. But the full root cause of the remaining
~1.3-1.5-second floor was not isolated within this investigation. That's reported as
a genuine limitation, not papered over with a confident-sounding guess — the concrete
next step identified but not executed is profiling the event loop directly
(`PYTHONASYNCIODEBUG=1` or `py-spy`) to see exactly what it's doing during that
unaccounted-for window.

---

## Summary: the mental model to keep

- **Testing conventions should match what's already there**, not what's theoretically
  best in a vacuum — pytest was worth introducing, but running it against the real
  Docker-networked stack (not an in-process ASGI transport) preserved the project's
  existing "test against the real thing" philosophy rather than replacing it.
- **Mock only the boundary that's expensive or unreliable, never the thing being
  tested.** Redis stays real everywhere (it's free and its atomicity is often
  literally the subject of the test); only the outbound provider HTTP call is mocked,
  and even then only at the network layer — parsing logic runs unchanged against fake
  input.
- **A test that only checks one direction (rejection, or success) can pass for the
  wrong reason.** Both the rate-limit test's exact-count assertion and the budget
  test's under-budget companion exist because of this.
- **Independent signals beat a single signal.** The fallback test's `response.model`
  check and its `/metrics` counter check are deliberately two separate measurements of
  the same event.
- **A load/scaling test is allowed to falsify its own hypothesis** — the worker-pool
  scaling result being flat, not linear, is not a failed experiment; it's the
  experiment doing exactly its job, and reporting it plainly (including the parts of
  the follow-up investigation that didn't fully resolve) is more valuable than forcing
  a tidier conclusion.
