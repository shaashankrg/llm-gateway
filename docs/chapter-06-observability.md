# Chapter 6: Observability — Tracing, Metrics, and Dashboards

This is a walkthrough of everything built in this chapter, written the way a senior
engineer would hand off a feature to someone new to the codebase: what problem each
piece solves, why it's built the way it is, and what actually happened when we tested
it (including the things that didn't work on the first try).

The chapter has three layers, built in this order because each one depends on the
last actually working:

1. **Tracing** — answers "what happened, in what order, for *this one request*?"
2. **Metrics** — answers "what's the aggregate picture, across *all* requests, over time?"
3. **Dashboards** — answers "can a human see the answer to (1) and (2) without reading code or logs?"

---

## 1. Tracing

### The core problem tracing solves

A single `/generate` request in this gateway crosses several boundaries:

```
HTTP request  →  FastAPI endpoint  →  asyncio.Queue  →  background worker  →  provider API
```

Once the endpoint hands the request to a worker via the queue, they're running in
different `asyncio` tasks. If something is slow or fails, "the request was slow"
doesn't tell you *where* — was it queue wait time? The circuit breaker check? The
actual HTTP call to Anthropic? A retry backoff sleep? Tracing exists to answer that,
by recording a tree of timestamped **spans**, each one a "this operation started here
and ended there" record, nested to show what contained what.

### Setting up the tracer — `app/tracing.py`

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

trace.set_tracer_provider(TracerProvider())
tracer = trace.get_tracer(__name__)

trace.get_tracer_provider().add_span_processor(
    BatchSpanProcessor(ConsoleSpanExporter())
)
```

Three concepts, each with a reason it's shaped this way:

- **`TracerProvider`** is the factory that hands out tracers. There's normally exactly
  one per process — it's set globally via `trace.set_tracer_provider(...)`, and every
  module that does `from app.tracing import tracer` gets the same tracer, which is why
  spans created in `main.py`, `queue.py`, and `retry.py` all end up correctly nested
  under one another instead of being disconnected.
- **`ConsoleSpanExporter`** just prints spans as JSON. This is a deliberate, temporary
  choice — it's the tracing equivalent of `print()` debugging. It lets you *see* the
  data shape before wiring up a real backend (Jaeger, Honeycomb, etc.), the same way
  you'd `console.log` before building a UI around a value.
- **`BatchSpanProcessor`** buffers finished spans and exports them periodically or when
  the buffer fills, rather than synchronously the instant each span ends
  (`SimpleSpanProcessor` does that). This matters practically: it means console output
  can lag a few requests behind real time — we saw this directly, when a span from an
  earlier request appeared in a `docker compose logs` tail alongside unrelated later
  output. `Batch` is also what you'd use against a real backend in production, since a
  network call to export every single span synchronously would slow down every request.

### The shape of a span

Every span in this codebase is created the same way:

```python
with tracer.start_as_current_span("some_name") as span:
    span.set_attribute("key", "value")
    ...  # the code being measured
```

`start_as_current_span` is a context manager: entering it starts the clock and marks
this span as the "current" one for anything created inside the `with` block; exiting
it (however that happens — a `return`, an uncaught exception, falling off the end)
stops the clock and ships the span to the exporter. **Nesting is automatic** — if you
open a new span while another one is active, OpenTelemetry sets the new span's parent
to whatever span was active when it started. You never manually wire up parent-child
relationships within a single Python call stack; the library tracks "what's currently
active" for you.

`set_attribute` attaches a searchable key-value fact to the span — e.g.
`span.set_attribute("team_id", team["team_id"])` on the endpoint's top-level span
means a trace viewer could filter "show me every request from team-a" without needing
a separate log line.

### The hard part: propagating context across the queue boundary

The automatic parent-child nesting described above only works within a single
in-process call stack. This gateway's architecture deliberately breaks that stack: the
FastAPI endpoint creates a request, drops it onto an `asyncio.Queue`, and returns —
the *worker* that eventually processes it runs in a completely separate `asyncio` task,
started independently in the app's `lifespan`. Without extra work, a span opened in the
worker would have no idea it's related to the span that was open in the endpoint;
you'd get two disconnected traces instead of one tree.

The fix, in `main.py`:

```python
from opentelemetry import context as otel_context

@app.post("/generate", response_model=StandardResponse)
async def generate(req: StandardRequest, team: dict = Depends(check_rate_limit), priority: str = Depends(get_priority)):
    with tracer.start_as_current_span("generate_request") as span:
        span.set_attribute("team_id", team["team_id"])
        span.set_attribute("model", req.model)
        span.set_attribute("priority", priority)

        if req.model not in team["allowed_models"]:
            raise HTTPException(status_code=403, detail=f"Model {req.model} not allowed for this team")

        trace_context = otel_context.get_current()
        future = asyncio.get_running_loop().create_future()
        await enqueue_request(
            priority, {"req": req, "team": team, "future": future, "trace_context": trace_context}
        )
        ...
```

`otel_context.get_current()` captures "whatever span is active right now" as a plain
object. Note where this line sits: **inside** the `with tracer.start_as_current_span(...)`
block, *after* it has opened. If you captured the context before opening the span, you'd
capture an empty/parentless context — the ordering isn't incidental, it's the whole
point. That captured context then rides along inside `request_data`, the same plain
dict this codebase already uses to carry `req`/`team`/`future` across the queue boundary.
No new mechanism was invented — `trace_context` is just one more key in a dict that
already existed for exactly this purpose (getting information from the endpoint to the
worker).

On the receiving end, in `queue.py`:

```python
async def worker(worker_id: int):
    while True:
        priority_value, counter, request_data = await request_queue.get()
        req = request_data["req"]
        team = request_data["team"]
        future = request_data["future"]
        trace_context = request_data["trace_context"]

        with tracer.start_as_current_span("worker_processing", context=trace_context):
            ...
```

Passing `context=trace_context` tells OpenTelemetry "treat this new span as a child of
whatever was captured there," even though it's executing in a different task entirely.
This is the one line that stitches the two halves of the request back into a single trace.

### The full span tree

Once every layer is wired up, one `/generate` request produces this tree (each level
strictly nested inside the one above it):

```
generate_request              (main.py — the whole HTTP request)
├── auth_check                (auth.py — a sibling, not nested; runs as a FastAPI
│                               dependency *before* generate_request's span opens)
└── worker_processing         (queue.py — everything the background worker does)
    ├── circuit_breaker.check (queue.py — is the provider's circuit open?)
    └── provider.call         (queue.py — the actual attempt to call a provider)
        └── retry.attempt     (retry.py — one single try inside call_with_retry;
                                 repeats 1-3 times per provider.call)
```

Two things about this tree are worth calling out because they came from testing, not
from the initial design:

**`auth_check` has `parent_id: null`, not a child of `generate_request`.** This looks
like a bug at first glance but isn't — `get_team`/`check_rate_limit` run as a FastAPI
dependency, which executes *before* the endpoint body (and therefore before
`generate_request`'s span opens). There's genuinely no active span yet at that point
for it to nest under. This is a real, valid design question (should auth become a child
by reordering the code?) rather than a wiring mistake — it's flagged rather than fixed,
since fixing it means changing where the span opens relative to FastAPI's dependency
injection, a real tradeoff, not a bug fix.

**There's no code path that produces two sibling `provider.call` spans in one trace.**
The plan going in was: primary provider fails → same request falls back to the second
provider → you'd see two `provider.call` spans side-by-side under one `worker_processing`.
Reading the actual code in `queue.py` shows this isn't how it's built: on a failure, each
branch does `circuit_breakers[...].record_failure(); ...; raise` — it re-raises
immediately rather than catching the failure and trying the other provider inline. The
`else` branch (the fallback path) only runs on a **different, later request**, once
`can_attempt()` already returns `False` because a previous request's failures tripped
the breaker. This was a real, useful finding surfaced by testing, not an assumption:
fallback in this codebase is *time-shifted* across requests, not *inline* within one.

### `retry.attempt` — nesting inside a black box

The retry loop lives in `call_with_retry` (`retry.py`), which `queue.py` calls as an
opaque function — `queue.py` has no visibility into how many attempts happen inside.
That has a direct consequence for where the span goes: it can't be added from the
outside; it has to go *inside* `call_with_retry`, wrapping each individual attempt:

```python
async def call_with_retry(call_fn, max_attempts=3):
    for attempt in range(max_attempts):
        with tracer.start_as_current_span("retry.attempt") as attempt_span:
            attempt_span.set_attribute("attempt_number", attempt + 1)
            try:
                result = await call_fn()
                attempt_span.set_attribute("outcome", "success")
                return result
            except httpx.HTTPStatusError as e:
                status_code = e.response.status_code
                retryable = is_retryable(status_code)
                attempt_span.set_attribute("outcome", "failure")
                attempt_span.set_attribute("error_type", "retryable" if retryable else "non_retryable")
                attempt_span.record_exception(e)

                if not retryable:
                    raise
                if attempt == max_attempts - 1:
                    raise

                delay = calculate_backoff(attempt)
                attempt_span.set_attribute("backoff_seconds", delay)
                await asyncio.sleep(delay)
            except Exception as e:
                attempt_span.set_attribute("outcome", "failure")
                attempt_span.set_attribute("error_type", "non_retryable")
                attempt_span.record_exception(e)
                raise
```

Because this code runs *while* the caller's `provider.call` span is still open (it's
called from inside that `with` block in `queue.py`), each `retry.attempt` span
automatically nests under `provider.call` — no context needs to be threaded through
manually here, unlike the queue-boundary case above. The nesting "just works" whenever
you're still inside the same async call stack.

One subtlety worth internalizing: `backoff_seconds` is only set on attempts that are
*not* the last one. On the final exhausted attempt, the code raises immediately without
sleeping, so there's nothing to record — this was confirmed directly in testing (see below).

The `except Exception as e:` branch at the bottom catches anything that isn't an
`httpx.HTTPStatusError` — a connection error, a DNS failure, a timeout. It records the
failure with `error_type="non_retryable"` and re-raises immediately, matching the
original (pre-tracing) behavior: this codebase's retry logic only ever retries on
*classified* retryable HTTP status codes, never on network-level errors.

### `call_span.record_exception(e)` vs. a text attribute

Every failure path calls `record_exception(e)` rather than, say,
`set_attribute("error", str(e))`. This is the OpenTelemetry-idiomatic way to attach an
error to a span — trace viewers (Jaeger, etc.) render exceptions specially, with full
stack traces, rather than as an opaque string. We saw this directly in the console
export: the exception event includes `exception.type`, `exception.message`, and a full
`exception.stacktrace`, not just a one-line message.

### What testing the trace tree actually revealed

Forcing real failures (not just reading the code and assuming it works) surfaced two
things worth remembering as a pattern, not just as facts about this codebase:

1. **A test double doesn't always fail the way you expect it to.** Pointing
   `ANTHROPIC_URL` at `https://httpbin.org/status/503` was meant to force a clean,
   retryable `HTTPStatusError`. Under load it sometimes actually timed out instead
   (`error_type: "ReadTimeout"`) — a different exception class entirely. This turned out
   to validate the code rather than break it: `error_type: "retryable"` /
   `type(e).__name__` faithfully reported *whatever actually happened*, without the
   tracing/metrics code needing to know every possible failure mode in advance. If it
   had been hardcoded to assume `HTTPStatusError`, this would have silently produced
   wrong labels instead.
2. **An external test target (like `httpbin.org`) can be its own source of flakiness**
   under rapid repeated hits — some requests simply never reached our code at all
   (`000` / connection reset). The fix was switching to a fully local, deterministic
   failure: pointing the URL at `http://127.0.0.1:1/...` inside the container (a port
   nothing listens on) gives an instant, reliable "connection refused" every single
   time, with no dependency on a third party's behavior or rate limits.

---

## 2. Metrics

### Why metrics are a different tool from tracing, not a replacement

A trace tells you the full story of *one* request. It cannot answer "what's our P95
latency across the last hour" or "what's our current error rate" — you'd have to
inspect thousands of individual trees by hand. Metrics exist for exactly that:
pre-aggregated numeric time series, cheap to store and query, purpose-built for
dashboards and alerting. The two are complementary: a metric tells you *something is
wrong* (error rate spiked); a trace, found via the same time window, tells you *why*
(which span in the tree took the extra 400ms).

### The four instrument types used here, and why each one fits its job

```python
requests_total = meter.create_counter(...)             # only ever goes up
errors_total = meter.create_counter(...)                # only ever goes up
request_duration = meter.create_histogram(..., unit="s")        # distribution of values
provider_call_duration = meter.create_histogram(..., unit="s")  # distribution of values
fallback_triggered_total = meter.create_counter(...)    # only ever goes up
circuit_breaker_state = meter.create_observable_gauge(...)  # current state, not a delta
```

- **Counter**: a number that only increases. Good for "how many times did X happen"
  (requests, errors, fallback events). You never decrement a counter — if something can
  go down, it's not a counter.
- **Histogram**: records individual observed values (e.g. "this request took 4.8
  seconds") and buckets them, so you can later ask "what's the 95th percentile" without
  having stored every raw value forever. This is what backs the P50/P95/P99 latency
  panels.
- **Observable gauge**: reports the *current value* of something on demand (via a
  callback), rather than accumulating deltas over time. This is the right tool
  specifically because the circuit breaker has **three** states
  (`CLOSED`/`HALF_OPEN`/`OPEN`), not two — an `UpDownCounter` (`+1`/`-1`) works cleanly
  for a two-state on/off thing, but breaks down with three states: does
  `HALF_OPEN → OPEN` (a failed trial request) double-count as another `+1` on top of the
  original open event? An observable gauge sidesteps this entirely by not being additive
  at all — it just reports "the true value right now" whenever something reads it,
  so there's nothing to double-count.

### Two histograms, not one — a design decision, not an accident

It would have been simpler to record only one duration metric. That was deliberately
rejected, because the two numbers answer genuinely different questions:

- **`gateway_request_duration_seconds`** — recorded once per request, wrapping the
  *entire* `worker_processing` span (circuit check + provider call + all retries +
  fallback, if any). This is "how long did the client actually wait" — the number a
  user or an on-call engineer cares about first, and the one that belongs on a P50/P95/P99
  headline panel.
- **`gateway_provider_call_duration_seconds`** — recorded once per `provider.call`
  attempt, labeled by `provider`. This is diagnostic: "was OpenAI slow, or was Anthropic
  slow, specifically?"

If you only recorded the total, you'd know P99 is bad but not *why* — one slow
provider, or retries piling up? If you only recorded per-call, a request with 3 retries
would show up as three separate, individually-fast-ish samples instead of one genuinely
slow end-to-end request — which would make the client-facing latency look better than
it actually was. That's a real metric bug, the kind that's embarrassing to explain
after the fact, and it's why both exist side by side.

### Where the timing code lives — `queue.py`

```python
with tracer.start_as_current_span("worker_processing", context=trace_context):
    worker_start = time.monotonic()
    outcome = "failure"
    try:
        ...
        with tracer.start_as_current_span("provider.call") as call_span:
            call_span.set_attribute("provider", "anthropic")
            call_start = time.monotonic()
            try:
                raw = await call_with_retry(...)
                ...
                provider_call_duration.record(
                    time.monotonic() - call_start, {"provider": "anthropic", "outcome": "success"}
                )
            except Exception as e:
                ...
                provider_call_duration.record(
                    time.monotonic() - call_start, {"provider": "anthropic", "outcome": "failure"}
                )
                errors_total.add(1, {"provider": "anthropic", "error_type": type(e).__name__})
                raise
    except Exception as e:
        print(f"WORKER {worker_id} ERROR: {e}")
        future.set_exception(e)
        requests_total.add(1, {"team": team["team_id"], "outcome": "failure"})
    else:
        future.set_result(result)
        outcome = "success"
        requests_total.add(1, {"team": team["team_id"], "model": req.model, "outcome": "success"})
    finally:
        request_duration.record(time.monotonic() - worker_start, {"outcome": outcome})
```

Two mechanics worth understanding, not just copying:

- **`time.monotonic()`, not `time.time()`.** Wall-clock time can jump backwards (NTP
  sync, DST) or be adjusted by the system; a monotonic clock only ever moves forward,
  which is what you want for measuring elapsed duration. This distinction matters more
  than it looks — a wall-clock adjustment mid-request could otherwise produce a negative
  or wildly wrong duration.
- **`finally` is attached to the outer `try/except/else`, not per-call.** This is what
  guarantees `request_duration` records exactly once per request, no matter how many
  `provider.call` attempts happened inside — one success, one primary failure, or a
  primary failure followed by a fallback attempt, all still produce exactly one
  `request_duration` sample. The `outcome` variable starts as `"failure"` and only
  flips to `"success"` inside the `else` block (which Python only runs if the `try`
  completed with no exception), so a request that raises records `outcome="failure"`
  correctly without needing to duplicate that logic in the `except` block.

### `type(e).__name__` — the label that reports truth, not assumption

```python
errors_total.add(1, {"provider": "anthropic", "error_type": type(e).__name__})
```

This could have been a hardcoded string per exception branch, but `type(e).__name__`
was chosen specifically because it self-documents whatever actually failed —
`HTTPStatusError`, `ReadTimeout`, `ConnectError` — without the metrics code needing an
exhaustive, hand-maintained list of every possible failure mode. This paid off directly
during testing: a forced-timeout scenario produced `error_type: "ReadTimeout"`, which
wasn't the failure mode being aimed for (a clean 503) but was still correctly and
usefully recorded, because the code never assumed which exception it would get.

### `fallback_triggered_total` — firing at the point of commitment, not before

```python
if fallback_model is None or fallback_model not in team["allowed_models"]:
    raise CircuitOpenError("Anthropic circuit is open and no authorized fallback is available")
if not circuit_breakers["openai"].can_attempt():
    raise CircuitOpenError("Both providers are unavailable")
fallback_triggered_total.add(1, {"from_provider": "anthropic", "to_provider": "openai"})
fallback_req = req.model_copy(update={"model": fallback_model})
```

The `.add(1, ...)` call sits *after* both guard clauses (is a fallback model allowed
for this team? is the fallback provider's own circuit closed?) and *before* the actual
call attempt. That ordering means the metric only fires once we know the fallback is
actually going to be attempted — not speculatively, and not double-counted against a
fallback that was rejected outright by a guard clause above it.

### The observable gauge for circuit breaker state

```python
_CIRCUIT_STATE_VALUES = {
    CircuitState.CLOSED: 0,
    CircuitState.HALF_OPEN: 1,
    CircuitState.OPEN: 2,
}

def _observe_circuit_breaker_state(options):
    return [
        Observation(_CIRCUIT_STATE_VALUES[breaker.state], {"provider": name})
        for name, breaker in circuit_breakers.items()
    ]

circuit_breaker_state = meter.create_observable_gauge(
    "gateway_circuit_breaker_state",
    callbacks=[_observe_circuit_breaker_state],
    description="Current circuit breaker state: 0=closed, 1=half_open, 2=open",
)
```

This reads directly from `circuit_breakers` (the same module-level dict
`{"openai": CircuitBreaker(), "anthropic": CircuitBreaker()}` that `queue.py` already
calls `.can_attempt()`/`.record_success()`/`.record_failure()` on). Crucially,
**`record_failure`/`record_success`/`can_attempt` in `circuit_breaker.py` were not
touched at all.** They already mutate `.state`; the gauge's callback just reads that
state fresh every time something scrapes it. No manual `.add()` calls were sprinkled
into the state machine — the gauge is a passive observer of state that already exists,
which is exactly what "observable" means in OpenTelemetry's vocabulary (as opposed to
instruments you actively call `.add()`/`.record()` on).

### The bucket-boundary bug, and why it mattered

The default OpenTelemetry histogram bucket boundaries are
`[0, 5, 10, 25, 50, 75, 100, 250, 500, 750, 1000, 2500, 5000, 7500, 10000, +Inf]`
(implicitly in whatever unit the histogram uses — here, seconds). Real latencies in
this gateway mostly land between 1 and 10 seconds. Against those default buckets,
almost every observed request lands inside the very first non-zero bucket
(`le="5.0"`), which makes `histogram_quantile()` — the PromQL function used to compute
P50/P95/P99 — return values with almost no resolution: everything looks the same,
because the buckets are too coarse to distinguish "fast" from "slow" within the range
that actually matters.

The fix is an explicit `View` that overrides the default bucket boundaries for
matching instruments:

```python
_LATENCY_BUCKETS = (0.1, 0.25, 0.5, 1, 2, 3, 5, 7.5, 10, 15, 20, 30)

_duration_view = View(
    instrument_name="gateway_*_duration_seconds",
    aggregation=ExplicitBucketHistogramAggregation(_LATENCY_BUCKETS),
)

metric_reader = PrometheusMetricReader()
metrics.set_meter_provider(MeterProvider(metric_readers=[metric_reader], views=[_duration_view]))
```

The `instrument_name` wildcard (`gateway_*_duration_seconds`) matches both
`gateway_request_duration_seconds` and `gateway_provider_call_duration_seconds` in one
`View`, since both need the same latency-appropriate bucket layout. This was confirmed
directly: before the fix, `histogram_quantile(0.95, ...)` on real traffic snapped to a
coarse bucket edge; after the fix, it returned `7.25` — a real, specific number sitting
naturally inside the finer-grained buckets. **The general lesson**: histogram bucket
boundaries are not a cosmetic detail — they determine whether your quantile queries can
distinguish anything at all. Always size them to the actual expected range of the data,
not the library default.

---

## 3. From console to Prometheus

### Why the exporter had to change, not just the sink

`ConsoleMetricExporter` was paired with `PeriodicExportingMetricReader` — a *push*
model, where the app itself decides on a timer ("every 5 seconds") to compute and ship
its metrics somewhere. Prometheus works the opposite way: it's *pull*-based — it scrapes
a `/metrics` HTTP endpoint on its own schedule and expects the target to just report
current values on demand. This isn't a drop-in swap of one argument; it's a different
reader entirely:

```python
metric_reader = PrometheusMetricReader()
metrics.set_meter_provider(MeterProvider(metric_readers=[metric_reader], views=[_duration_view]))
```

`PrometheusMetricReader` has no export interval — it doesn't push anything anywhere. It
just maintains an internal registry that reports current instrument values whenever
something asks. That "something" is `prometheus_client`'s WSGI app, mounted directly
into the FastAPI app in `main.py`:

```python
from prometheus_client import make_wsgi_app
from starlette.middleware.wsgi import WSGIMiddleware

app.mount("/metrics", WSGIMiddleware(make_wsgi_app()))
```

FastAPI/Starlette are ASGI (async), but `prometheus_client`'s built-in HTTP handler is
WSGI (sync) — `WSGIMiddleware` bridges the two so a synchronous WSGI app can be mounted
inside an async Starlette app as a sub-application.

**A real gotcha hit here**: `app.mount("/metrics", ...)` without a trailing slash means
requests to `/metrics` come back as a `307 Temporary Redirect` to `/metrics/` — this is
standard Starlette sub-app mounting behavior, not a bug, but it's easy to miss if your
HTTP client doesn't follow redirects by default (plain `curl` doesn't). The scrape
config in `prometheus.yml` has to target `/metrics/` (trailing slash) directly to avoid
relying on redirect-following at all:

```yaml
scrape_configs:
  - job_name: llm-gateway
    metrics_path: /metrics/
    static_configs:
      - targets: ["llm-gateway:8000"]
```

Note the target is `llm-gateway:8000`, the Docker Compose *service name*, not
`localhost` — Prometheus runs in its own container, on the same Compose network, and
has to reach the gateway by its service DNS name.

---

## 4. Grafana and the dashboard

### Provisioning as files, not clicking through a UI

Both the Prometheus datasource and the dashboard itself are defined as files under
`grafana/provisioning/`, mounted into the Grafana container, rather than configured by
hand through the web UI:

```
grafana/provisioning/
├── datasources/prometheus.yml   # tells Grafana where Prometheus lives
└── dashboards/
    ├── dashboards.yml           # tells Grafana to load *.json files from this folder
    └── gateway.json             # the actual dashboard: 6 panels, PromQL per panel
```

This matters for a reason beyond convenience: it means the entire observability stack
— tracing, metrics, and now the dashboard that visualizes them — is defined in version
control and reproducible from a clean `docker compose up`, rather than being tribal
knowledge sitting in someone's browser session.

### The six panels, and the PromQL behind each

| Panel | Query | What it answers |
|---|---|---|
| Requests/sec | `sum(rate(gateway_requests_total[1m])) by (outcome)` | Is traffic flowing, and what fraction is failing? |
| Error rate | `sum(rate(gateway_errors_total[1m])) by (provider, error_type)` | Which provider, which failure mode? |
| Request latency P50/95/99 | `histogram_quantile(0.95, sum(rate(gateway_request_duration_seconds_bucket[5m])) by (le))` | What does the client actually experience? |
| Provider call latency P50/95/99 | same, but `by (le, provider)` on `gateway_provider_call_duration_seconds_bucket` | Which provider is slow, independent of retries/queueing? |
| Fallback events | `sum(rate(gateway_fallback_triggered_total[5m])) by (from_provider, to_provider)` | How often are we degrading to the backup provider? |
| Circuit breaker state | `gateway_circuit_breaker_state` (raw gauge, rendered as a state timeline) | Is a provider currently considered down? |

`rate(...)` turns a monotonically-increasing counter into "how fast is this increasing
right now" — a counter's raw value (e.g. "14,392 total requests") is nearly useless on
its own; its *rate of change* is what's actually interesting for a live dashboard.
`histogram_quantile()` takes the bucketed histogram data and interpolates an estimated
percentile from it — this is exactly why the bucket-boundary fix earlier mattered: this
function is only as precise as the buckets it's given.

### A provisioning bug that's worth understanding, not just fixing

After initially provisioning the Prometheus datasource with an auto-generated `uid`,
the dashboard JSON's panels referenced the *literal string* `"Prometheus"` as the
datasource `uid` — which happened to resolve correctly via a name-matching fallback,
but was fragile (it would silently break if that fallback behavior ever changed, or on
a fresh volume where matching went differently). The fix was pinning an explicit `uid`
in the datasource config:

```yaml
datasources:
  - name: Prometheus
    uid: prometheus
    type: prometheus
    access: proxy
    url: http://prometheus:9090
    isDefault: true
```

**This broke Grafana on restart.** The running container's persisted internal SQLite
database still referenced the *old*, auto-generated `uid` from its first boot; the
newly-declared `uid: prometheus` conflicted with that persisted state, and the entire
provisioning module failed to start — taking the whole Grafana server down with it,
not just the datasource. The actual error:

```
Datasource provisioning error: data source not found
```

The fix was `docker compose rm -f -s -v grafana` (remove the container *and* its
volume) followed by `docker compose up -d grafana`, forcing a clean re-provision from
the files rather than trying to reconcile against stale persisted state. **The general
lesson**: provisioning-as-code and a stateful, persisted database underneath it can
disagree with each other once the container has booted once — a fresh container isn't
always equivalent to a freshly-provisioned one if a volume survives in between.

---

## 5. Verifying it end to end — the discipline, not just the result

Every piece in this chapter was tested by deliberately forcing failure, not just by
reading code and assuming it worked. A few examples of what that actually looked like:

- Forcing a 503 loop against a fake Anthropic URL to confirm `retry.attempt` spans
  showed exactly 3 attempts, growing backoff, and no `backoff_seconds` on the final
  (immediately-raised) attempt.
- Firing 5 requests against an unroutable local address to genuinely trip
  `circuit_breakers["anthropic"]` open (5 is the hardcoded threshold in
  `circuit_breaker.py`), then confirming the 6th request actually took the fallback
  branch — and discovering along the way that team-a's model allowlist didn't include
  the fallback model, so the very first attempt at this test failed for an unrelated,
  real reason (an authorization gap, not a tracing bug).
- Cross-checking two *independently computed* signals against each other in the same
  metrics export window — `gateway_fallback_triggered_total{from=anthropic,to=openai}`
  incrementing at the same moment `gateway_circuit_breaker_state{provider=anthropic}`
  read `2` (OPEN). Two different instruments agreeing with each other, and with what
  actually happened, is meaningfully stronger evidence than either one not crashing.

Every temporary change made to force a failure (a swapped `ANTHROPIC_URL`, a
temporarily widened team allowlist) was reverted immediately afterward, confirmed with
`git diff` back to a clean state, before moving to the next piece.

---

## Summary: the mental model to keep

- **Tracing** is per-request, causal, and nested — it exists to answer "what happened,
  in what order, for this one request." The hard part is propagating context across
  any boundary where execution moves to a different task/thread/process (here: the
  `asyncio.Queue` between the endpoint and the worker).
- **Metrics** are aggregate and numeric — they exist to answer "what's true across
  everything, right now or over time." Choosing the right instrument type
  (counter vs. histogram vs. gauge) is a real design decision tied to the actual shape
  of the underlying data (monotonic count vs. distribution vs. current state).
- **Dashboards** are just PromQL queries against those metrics, rendered as panels —
  their usefulness is entirely downstream of whether the metrics underneath them were
  designed with the right labels, the right bucket boundaries, and the right instrument
  types in the first place.
- Across all three layers, the recurring theme was: **the real codebase never quite
  matches an idealized sketch** (no `fallback_chain` loop, no `ProviderError` type, no
  `call_provider()` helper — just duplicated if/else branches per provider), and the
  actual value came from reading the real code before wiring anything in, and then
  proving each piece with a forced failure rather than trusting that it compiled.
