/**
 * Thin client for the deployed FastAPI gateway. Every function here is written
 * to fail soft: the showcase page must render fine when the backend is asleep,
 * missing the /demo/* endpoints entirely, or blocked by CORS.
 */

export const GATEWAY_URL = (process.env.NEXT_PUBLIC_GATEWAY_URL ?? "").replace(/\/+$/, "");
const DEMO_TOKEN = process.env.NEXT_PUBLIC_DEMO_TOKEN ?? "";

export type BreakerState = "closed" | "half_open" | "open" | "unknown";

export type ProviderStatus = {
  provider: "openai" | "anthropic";
  state: BreakerState;
};

export type FeedEvent = {
  timestamp: string;
  team: string;
  provider: string;
  failover: boolean;
  latency_ms: number;
  status: number | string;
};

export type BudgetRow = {
  team: string;
  spend: number;
  cap: number;
};

/** Why the live panel isn't showing data, if it isn't. */
export type OfflineReason = "no-url" | "unreachable" | "not-implemented" | null;

export type GatewaySnapshot = {
  online: boolean;
  reason: OfflineReason;
  providers: ProviderStatus[];
  budgets: BudgetRow[];
};

function headers(): HeadersInit {
  const h: Record<string, string> = { Accept: "application/json" };
  if (DEMO_TOKEN) h["X-Demo-Token"] = DEMO_TOKEN;
  return h;
}

/**
 * fetch with a hard timeout. A gateway that is asleep on a scale-to-zero host
 * will hang rather than refuse, and the panel must not hang with it.
 */
async function timedFetch(path: string, init: RequestInit = {}, ms = 3500): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), ms);
  try {
    return await fetch(`${GATEWAY_URL}${path}`, {
      ...init,
      headers: { ...headers(), ...(init.headers ?? {}) },
      signal: controller.signal,
      cache: "no-store",
      mode: "cors",
    });
  } finally {
    clearTimeout(timer);
  }
}

const BREAKER_BY_CODE: Record<string, BreakerState> = {
  "0": "closed",
  "1": "half_open",
  "2": "open",
};

/**
 * Parse `gateway_circuit_breaker_state{provider="openai"} 0` out of the
 * Prometheus text exposition format. Deliberately tolerant: label order,
 * quoting style, and float-formatted values ("0.0", "2") all vary by client.
 */
export function parseBreakerMetrics(text: string): ProviderStatus[] {
  const found = new Map<string, BreakerState>();
  const line = /^gateway_circuit_breaker_state\{([^}]*)\}\s+([0-9.eE+-]+)/;

  for (const raw of text.split("\n")) {
    const trimmed = raw.trim();
    if (trimmed.startsWith("#") || !trimmed) continue;

    const m = trimmed.match(line);
    if (!m) continue;

    const provider = m[1].match(/provider\s*=\s*"([^"]*)"/)?.[1];
    if (!provider) continue;

    // Values arrive as "0", "0.0", or "2" depending on the exporter.
    const code = String(Math.round(Number(m[2])));
    found.set(provider.toLowerCase(), BREAKER_BY_CODE[code] ?? "unknown");
  }

  return (["openai", "anthropic"] as const).map((provider) => ({
    provider,
    state: found.get(provider) ?? "unknown",
  }));
}

const UNKNOWN_PROVIDERS: ProviderStatus[] = [
  { provider: "openai", state: "unknown" },
  { provider: "anthropic", state: "unknown" },
];

/**
 * One poll of gateway state. Prefers /demo/status (breaker + budgets in one
 * shot); falls back to scraping /metrics if that endpoint isn't deployed yet.
 */
export async function fetchSnapshot(timeoutMs = 3500): Promise<GatewaySnapshot> {
  if (!GATEWAY_URL) {
    return { online: false, reason: "no-url", providers: UNKNOWN_PROVIDERS, budgets: [] };
  }

  // Both probes run concurrently rather than in series. Against a sleeping
  // host each one hangs for the full timeout, and running them sequentially
  // doubled the time before the panel could admit it was offline.
  const statusProbe = timedFetch("/demo/status", {}, timeoutMs)
    .then(async (res) => {
      if (!res.ok) return null;
      const data = (await res.json()) as { providers?: ProviderStatus[]; budgets?: BudgetRow[] };
      if (!data.providers?.length) return null;
      return {
        online: true as const,
        reason: null,
        providers: data.providers,
        budgets: data.budgets ?? [],
      };
    })
    .catch(() => null);

  const metricsProbe = timedFetch("/metrics", {}, timeoutMs)
    .then(async (res) => {
      if (!res.ok) return null;
      return {
        online: true as const,
        reason: null,
        providers: parseBreakerMetrics(await res.text()),
        budgets: [] as BudgetRow[],
      };
    })
    .catch(() => null);

  // Prefer /demo/status when it answers — it carries budgets too — but don't
  // wait on it if /metrics already proved the gateway is awake.
  const [status, metrics] = await Promise.all([statusProbe, metricsProbe]);
  const best = status ?? metrics;
  if (best) return best;

  return { online: false, reason: "unreachable", providers: UNKNOWN_PROVIDERS, budgets: [] };
}

/**
 * Poke a sleeping host and resolve once it answers.
 *
 * Free hosting tiers spin the container down after idle; the first request
 * then takes ~30-60s while it boots. Rather than hide that, the panel offers
 * it as a button, so the wait is something the visitor chose and can watch.
 */
export async function wakeGateway(totalMs = 90000): Promise<boolean> {
  if (!GATEWAY_URL) return false;

  const deadline = Date.now() + totalMs;
  while (Date.now() < deadline) {
    try {
      // A generous per-attempt timeout: the point is to hold the connection
      // open while the container boots, not to fail fast.
      const res = await timedFetch("/healthz", {}, 20000);
      if (res.ok) return true;
    } catch {
      // Still asleep — wait a beat and try again.
    }
    await new Promise((r) => setTimeout(r, 2000));
  }
  return false;
}

/** Toggle the simulated provider outage. Returns whether the call landed. */
export async function triggerChaos(
  durationSeconds = 30,
  provider = "openai"
): Promise<{ ok: boolean; error?: string }> {
  if (!GATEWAY_URL) return { ok: false, error: "No gateway URL configured." };

  try {
    const res = await timedFetch("/demo/chaos", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider, duration_seconds: durationSeconds }),
    });

    if (res.status === 404) {
      return { ok: false, error: "/demo/chaos is not deployed on this gateway yet." };
    }
    if (!res.ok) {
      return { ok: false, error: `Gateway returned ${res.status}.` };
    }
    return { ok: true };
  } catch {
    return { ok: false, error: "Gateway unreachable." };
  }
}

/**
 * Push a demo team's spend to just under its cap, so the next few requests
 * are rejected with a real 402 and the budget bar visibly fills.
 */
export async function burnBudget(team = "team-b"): Promise<{ ok: boolean; error?: string }> {
  if (!GATEWAY_URL) return { ok: false, error: "No gateway URL configured." };
  try {
    const res = await timedFetch("/demo/burn", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // Exactly at cap: the next request is refused, so the 402 is immediate.
      body: JSON.stringify({ team, fraction: 1.0 }),
    });
    if (res.status === 404) return { ok: false, error: "/demo/burn isn't deployed yet." };
    if (!res.ok) return { ok: false, error: `Gateway returned ${res.status}.` };
    return { ok: true };
  } catch {
    return { ok: false, error: "Gateway unreachable." };
  }
}

function coerceEvent(raw: unknown): FeedEvent | null {
  if (!raw || typeof raw !== "object") return null;
  const e = raw as Record<string, unknown>;
  if (e.timestamp === undefined && e.team === undefined) return null;

  return {
    timestamp: String(e.timestamp ?? new Date().toISOString()),
    team: String(e.team ?? "unknown"),
    provider: String(e.provider ?? "—"),
    failover: Boolean(e.failover),
    latency_ms: Number(e.latency_ms ?? 0),
    status: (e.status as number | string) ?? 200,
  };
}

/** Normalize whatever shape /demo/feed returns into a flat event list. */
export function coerceEvents(payload: unknown): FeedEvent[] {
  const list = Array.isArray(payload)
    ? payload
    : Array.isArray((payload as { events?: unknown[] })?.events)
      ? (payload as { events: unknown[] }).events
      : [];

  return list.map(coerceEvent).filter((e): e is FeedEvent => e !== null);
}

/** Polling read of the request feed, used when SSE isn't available. */
export async function fetchFeed(limit = 15): Promise<FeedEvent[] | null> {
  if (!GATEWAY_URL) return null;
  try {
    const res = await timedFetch(`/demo/feed?limit=${limit}`);
    if (!res.ok) return null;
    return coerceEvents(await res.json());
  } catch {
    return null;
  }
}

export const hasGatewayUrl = () => GATEWAY_URL.length > 0;
export const feedStreamUrl = () => `${GATEWAY_URL}/demo/feed?stream=1`;

/*
 * Demo traffic.
 *
 * The feed only shows real requests, so without this the panel greets every
 * visitor with an empty box. Sending a trickle from the browser means the
 * demo is self-demonstrating: rows are already moving by the time someone
 * has read the heading.
 *
 * These team keys are public on purpose. They reach a mock-mode gateway that
 * makes no upstream provider calls, and they're still subject to the same
 * rate limit and daily budget as any other team — so the worst a visitor can
 * do is exhaust a demo team's own quota.
 */
const DEMO_TEAMS = [
  { key: "team-a-key", models: ["gpt-4", "claude-sonnet-5"] },
  { key: "team-b-key", models: ["gpt-4", "claude-sonnet-5"] },
];

const DEMO_PROMPTS = [
  "Summarize circuit breakers in one line.",
  "What is a token bucket?",
  "Explain exponential backoff.",
  "Why use a priority queue here?",
];

const pick = <T,>(xs: T[]): T => xs[Math.floor(Math.random() * xs.length)];

/**
 * Fire one request through the gateway to give the feed something to show.
 * Resolves regardless of outcome — a 429 or 503 is a perfectly good row.
 */
export async function sendDemoRequest(): Promise<void> {
  if (!GATEWAY_URL) return;

  const team = pick(DEMO_TEAMS);
  try {
    await timedFetch(
      "/generate",
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-API-Key": team.key,
          "X-Priority": Math.random() < 0.75 ? "realtime" : "batch",
        },
        body: JSON.stringify({
          prompt: pick(DEMO_PROMPTS),
          model: pick(team.models),
          max_tokens: 30,
        }),
      },
      20000
    );
  } catch {
    // Nothing to do — the request's own outcome is what the feed reports.
  }
}
