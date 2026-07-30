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
async function timedFetch(path: string, init: RequestInit = {}, ms = 6000): Promise<Response> {
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
export async function fetchSnapshot(): Promise<GatewaySnapshot> {
  if (!GATEWAY_URL) {
    return { online: false, reason: "no-url", providers: UNKNOWN_PROVIDERS, budgets: [] };
  }

  // Preferred path: a single demo endpoint with everything the panel needs.
  try {
    const res = await timedFetch("/demo/status");
    if (res.ok) {
      const data = (await res.json()) as {
        providers?: ProviderStatus[];
        budgets?: BudgetRow[];
      };
      if (data.providers?.length) {
        return {
          online: true,
          reason: null,
          providers: data.providers,
          budgets: data.budgets ?? [],
        };
      }
    }
    // A 404 here just means the optional endpoint isn't built — keep going.
  } catch {
    // Network-level failure; /metrics may still answer, so don't give up yet.
  }

  // Fallback: scrape the Prometheus endpoint, which already exists today.
  try {
    const res = await timedFetch("/metrics");
    if (res.ok) {
      const providers = parseBreakerMetrics(await res.text());
      return { online: true, reason: null, providers, budgets: [] };
    }
    return { online: false, reason: "not-implemented", providers: UNKNOWN_PROVIDERS, budgets: [] };
  } catch {
    return { online: false, reason: "unreachable", providers: UNKNOWN_PROVIDERS, budgets: [] };
  }
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
