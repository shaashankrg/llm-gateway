/**
 * All prose for the page, in one place. Every number here is quoted from the
 * repo's README "Measured results" section, which in turn quotes test output.
 * Nothing on this page is estimated or rounded up.
 */

export const GITHUB_URL = "https://github.com/shaashankrg/llm-gateway";
export const LINKEDIN_URL = "https://www.linkedin.com/in/YOUR-HANDLE"; // TODO: replace
export const CONTACT_EMAIL = "shashankgundreddy82@gmail.com";

export const HERO = {
  name: "LLM Gateway",
  pitch:
    "A self-hosted API gateway that sits between an org's apps and its LLM providers — the single chokepoint every model call flows through.",
  subline:
    "Without one, every service reimplements provider failover badly, nobody can answer what the company is spending until the invoice arrives, and one team's batch job starves everyone else's user-facing traffic. This builds those once, correctly, and enforces them for everyone.",
  stack: ["FastAPI", "Redis", "Docker", "OpenTelemetry", "Prometheus", "Grafana"],
};

export type Stat = {
  label: string;
  value: string;
  unit?: string;
  detail: string;
};

export const STATS: Stat[] = [
  {
    label: "Success rate during outage",
    value: "89.4–89.8",
    unit: "%",
    detail:
      "Sustained mixed traffic across 55 teams through a 30s forced OpenAI outage, excluding rate-limit 429s. Range across 3 runs — no single best run quoted.",
  },
  {
    label: "Recovery time, live traffic",
    value: "4.47–5.05",
    unit: "s",
    detail:
      "Time from outage end to steady-state success, measured from real request outcomes.",
  },
  {
    label: "Recovery time, breaker gauge",
    value: "4.72–5.20",
    unit: "s",
    detail:
      "The same recovery measured independently from the Prometheus breaker gauge, sampled every 0.25s. The two methods agree within 0.3s.",
  },
  {
    label: "Quota under scale-out",
    value: "10 / 40",
    detail:
      "40 requests for one team through the load balancer across 3 replicas: exactly 10 succeed, 30 return 429 — identical to a single instance. Verified at 500, 1000, and 1500 concurrency.",
  },
];

export const NEGATIVE_RESULT = {
  title: "A negative result, reported as one",
  headline: "Three instances were not faster than one.",
  table: [
    { topology: "Single instance", throughput: "~172 – 195 req/s" },
    { topology: "3 instances + nginx", throughput: "~157 – 200 req/s" },
  ],
  diagnosis: [
    "Every container sat below 1% CPU while latency ran multi-second — requests were queued, not working. Nothing was resource-bound.",
    "The ConsoleSpanExporter was emitting ~90,000 log lines per 500 requests; those synchronous stdout writes serialize the event loop. Gated behind DISABLE_CONSOLE_SPANS=1.",
    "The load generator is the bottleneck. Two client processes against a single instance produced ~185 req/s aggregate — more than one client process could generate alone (~158 req/s).",
  ],
  conclusion:
    "The scale-out infrastructure is correct — round-robin verified, shared-state quota verified — but throughput is client-bound, so this setup cannot demonstrate linear scaling. Proving that needs a distributed load generator, which isn't built here. There is no \"3× throughput\" claim because the method doesn't support one.",
};

/**
 * Grafana dashboard panels, from grafana/provisioning/dashboards/gateway.json.
 * `src` points at a screenshot in site/public — leave it null to render a
 * labelled placeholder slot instead of a broken image.
 */
export type DashboardShot = {
  title: string;
  caption: string;
  src: string | null;
  wide?: boolean;
};

export const DASHBOARD_SHOTS: DashboardShot[] = [
  {
    title: "Circuit breaker state",
    caption:
      "OpenAI trips to open and recovers while Anthropic stays closed. This is the gauge the chaos test reads as ground truth for when the breaker actually opened — rather than inferring it from responses.",
    src: "/grafana-breaker.png",
    wide: true,
  },
  {
    title: "Fallback events",
    caption:
      "Cross-provider failovers as they fire, labelled by direction: openai → anthropic. Each step is a request that would otherwise have failed.",
    src: "/grafana-fallback.png",
  },
  {
    title: "Request latency (P50/P95/P99)",
    caption:
      "P50 stays flat on the floor while P95/P99 jump to ~4-5s — the retry-then-failover cost lands only on the requests that hit the failing provider, not on everyone.",
    src: "/grafana-request-latency.png",
  },
  {
    title: "Provider call latency",
    caption:
      "Split per provider, so a slow upstream is distinguishable from a slow gateway. OpenAI's percentiles climb during the outage; Anthropic's stay flat.",
    src: "/grafana-provider-latency.png",
  },
  {
    title: "Error rate",
    caption:
      "Upstream errors by provider and type, rising as the outage begins and returning to zero on recovery.",
    src: "/grafana-error-rate.png",
  },
];

export type Capability = { name: string; detail: string };

export const CAPABILITIES: Capability[] = [
  {
    name: "Provider abstraction",
    detail:
      "One request/response schema translated per provider, so callers never touch OpenAI vs. Anthropic payload differences.",
  },
  {
    name: "Authentication & authorization",
    detail: "API-key → team lookup, with a per-team allow-list of models (403 on an unauthorized model).",
  },
  {
    name: "Rate limiting",
    detail:
      "Token bucket (capacity 10, refill 10/min) computed inside a Redis Lua script so read-refill-decrement is atomic.",
  },
  {
    name: "Cost budgeting",
    detail:
      "Per-team daily spend tracked in Redis from real token usage, with an 80% warning and a hard 402 at 100%.",
  },
  {
    name: "Streaming budgets",
    detail:
      "Optimistic reserve → stream → reconcile, so a team can't start a stream it can't afford and a broken stream refunds its reservation.",
  },
  {
    name: "Priority queue",
    detail:
      "asyncio.PriorityQueue with realtime ahead of batch, drained by a configurable worker pool; the handler awaits a Future the worker resolves.",
  },
  {
    name: "Retries",
    detail: "Exponential backoff with jitter, only on retryable statuses — a 400 fails immediately.",
  },
  {
    name: "Circuit breaker",
    detail: "CLOSED → OPEN after 5 failures → HALF_OPEN probe after a 30s cooldown, per provider.",
  },
  {
    name: "Cross-provider failover",
    detail:
      "When the primary exhausts retries or is circuit-open, the same request re-routes to the other provider.",
  },
  {
    name: "Streaming (SSE)",
    detail:
      "Both providers' event formats normalized to plain text chunks, with usage extracted from differently-shaped final events.",
  },
  {
    name: "Observability",
    detail:
      "OpenTelemetry traces spanning the queue boundary, Prometheus metrics, and a provisioned Grafana dashboard.",
  },
  {
    name: "Horizontal scale-out",
    detail: "3 replicas behind nginx sharing one Redis, with quota correctness verified across instances.",
  },
];

export const LIMITS = [
  {
    title: "Circuit breaker state is per-process",
    body: "With multiple replicas, each keeps its own breaker, so an outage is detected independently N times instead of once globally. Moving the state to Redis would fix this.",
  },
  {
    title: "Team config is hardcoded",
    body: "Teams live in app/auth.py as a stand-in for a real datastore.",
  },
  {
    title: "Throughput measurement is client-bound",
    body: "The numbers reflect a single-threaded Python load generator saturating before the server does, so the gateway's true ceiling is unmeasured.",
  },
  {
    title: "The stream cost estimator is a heuristic",
    body: "A ~4 chars/token approximation, not a real tokenizer. It only needs to be a sane upper bound for the reservation, but it will over-reserve.",
  },
  {
    title: "Daily budget windows are approximate",
    body: "A 24h Redis TTL from first write, not a calendar-aligned reset.",
  },
];
