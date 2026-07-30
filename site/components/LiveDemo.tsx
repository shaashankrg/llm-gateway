"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  BreakerState,
  BudgetRow,
  FeedEvent,
  GatewaySnapshot,
  coerceEvents,
  feedStreamUrl,
  fetchFeed,
  fetchSnapshot,
  hasGatewayUrl,
  triggerChaos,
} from "@/lib/gateway";

const MAX_ROWS = 15;
const CHAOS_SECONDS = 30;

const BREAKER_STYLE: Record<BreakerState, { dot: string; text: string; ring: string; label: string }> = {
  closed: { dot: "bg-status-ok", text: "text-status-ok", ring: "border-status-ok/30 bg-status-ok/5", label: "closed" },
  half_open: { dot: "bg-status-warn", text: "text-status-warn", ring: "border-status-warn/30 bg-status-warn/5", label: "half-open" },
  open: { dot: "bg-status-bad", text: "text-status-bad", ring: "border-status-bad/30 bg-status-bad/5", label: "open" },
  unknown: { dot: "bg-slate-600", text: "text-slate-500", ring: "border-ink-700 bg-ink-850", label: "unknown" },
};

function ProviderPill({ name, state }: { name: string; state: BreakerState }) {
  const s = BREAKER_STYLE[state];
  return (
    <div className={`flex items-center gap-2.5 rounded-md border px-3 py-2 transition-colors ${s.ring}`}>
      <span className="relative flex h-2 w-2 shrink-0">
        {state === "open" && (
          <span className={`absolute inline-flex h-full w-full rounded-full ${s.dot} opacity-60 motion-safe:animate-ping`} />
        )}
        <span className={`relative inline-flex h-2 w-2 rounded-full ${s.dot}`} />
      </span>
      <span className="text-sm font-medium text-slate-200">{name}</span>
      <span className={`ml-auto font-mono text-[0.7rem] uppercase tracking-wider ${s.text}`}>{s.label}</span>
    </div>
  );
}

function statusTone(status: number | string) {
  const n = Number(status);
  if (Number.isNaN(n)) return "text-slate-400";
  if (n < 300) return "text-status-ok";
  if (n === 429 || n === 402) return "text-status-warn";
  return "text-status-bad";
}

function statusGlyph(status: number | string) {
  const n = Number(status);
  if (!Number.isNaN(n) && n < 300) return "✓";
  if (n === 429 || n === 402) return "◍";
  return "✕";
}

function clockOf(ts: string) {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return String(ts).slice(11, 23) || "--:--:--";
  return d.toLocaleTimeString("en-GB", { hour12: false }) + "." + String(d.getMilliseconds()).padStart(3, "0");
}

function FeedRow({ e, fresh }: { e: FeedEvent; fresh: boolean }) {
  return (
    <li
      className={`grid grid-cols-[auto_1fr_auto] items-center gap-x-3 gap-y-1 border-b border-ink-800/70 px-3 py-2 font-mono text-[0.72rem] sm:grid-cols-[7.5rem_5rem_1fr_auto_3.5rem_1.25rem] sm:gap-x-4 ${
        fresh ? "motion-safe:animate-row-in" : ""
      } ${e.failover ? "bg-status-warn/[0.055]" : ""}`}
    >
      <span className="tabular text-slate-600">{clockOf(e.timestamp)}</span>
      <span className="text-slate-400">{e.team}</span>

      <span className="col-span-3 flex items-center gap-2 sm:col-span-1">
        <span className="text-slate-300">{e.provider}</span>
        {e.failover && (
          <span className="rounded border border-status-warn/40 bg-status-warn/10 px-1.5 py-px text-[0.62rem] font-semibold uppercase tracking-wide text-status-warn">
            failover
          </span>
        )}
      </span>

      <span className="tabular hidden text-right text-slate-500 sm:inline">{Math.round(e.latency_ms)}ms</span>
      <span className={`tabular text-right ${statusTone(e.status)}`}>{e.status}</span>
      <span className={`text-right ${statusTone(e.status)}`}>{statusGlyph(e.status)}</span>
    </li>
  );
}

function BudgetBar({ row }: { row: BudgetRow }) {
  const pct = row.cap > 0 ? Math.min(100, (row.spend / row.cap) * 100) : 0;
  const tone = pct >= 100 ? "bg-status-bad" : pct >= 80 ? "bg-status-warn" : "bg-accent";

  return (
    <div>
      <div className="mb-1.5 flex items-baseline justify-between font-mono text-[0.7rem]">
        <span className="text-slate-400">{row.team}</span>
        <span className="tabular text-slate-500">
          <span className={pct >= 80 ? "text-status-warn" : "text-slate-300"}>${row.spend.toFixed(3)}</span>
          <span className="text-slate-600"> / ${row.cap.toFixed(2)}</span>
        </span>
      </div>
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-ink-800">
        <div className={`h-full rounded-full transition-[width] duration-700 ease-out ${tone}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

/** Shown when the backend is asleep, missing /demo/*, or has no URL configured. */
function OfflineState({ reason }: { reason: GatewaySnapshot["reason"] }) {
  const note =
    reason === "no-url"
      ? "NEXT_PUBLIC_GATEWAY_URL isn't set for this deployment."
      : reason === "not-implemented"
        ? "The gateway answered, but the /demo endpoints aren't deployed on it yet."
        : "Couldn't reach the gateway.";

  return (
    <div className="flex flex-col items-center justify-center rounded-lg border border-dashed border-ink-700 bg-ink-950/60 px-6 py-14 text-center">
      {/* Sleeping-terminal mark, drawn rather than imported. */}
      <svg viewBox="0 0 48 48" className="mb-5 h-10 w-10 text-slate-700" fill="none" aria-hidden="true">
        <rect x="4" y="9" width="40" height="30" rx="3" stroke="currentColor" strokeWidth="2" />
        <path d="M13 20l5 4-5 4" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
        <path d="M25 28h9" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
      </svg>

      <p className="max-w-md text-[0.95rem] text-slate-400">
        Demo backend is asleep right now — here&apos;s what it looks like when running.
      </p>
      <p className="mt-2 font-mono text-[0.7rem] text-slate-600">{note}</p>

      {/*
        REPLACE ME ─────────────────────────────────────────────────────────
        Drop a recording of the running panel at site/public/demo-recording.gif
        (or .mp4/.webm), then swap this placeholder block for:

          <img
            src="/demo-recording.gif"
            alt="The live panel handling a simulated OpenAI outage"
            className="mt-6 w-full max-w-2xl rounded-md border border-ink-700"
          />

        A ~20s capture works well: idle state, click the outage button, watch
        the OpenAI pill go red and failover tags appear, then recovery.
        ────────────────────────────────────────────────────────────────────
      */}
      <div className="mt-6 flex aspect-[16/9] w-full max-w-2xl items-center justify-center rounded-md border border-ink-700 bg-ink-900/80">
        <div className="text-center">
          <p className="font-mono text-[0.7rem] uppercase tracking-[0.2em] text-slate-700">screenshot / gif slot</p>
          <p className="mt-2 font-mono text-[0.68rem] text-slate-700">public/demo-recording.gif</p>
        </div>
      </div>
    </div>
  );
}

export default function LiveDemo() {
  const [snapshot, setSnapshot] = useState<GatewaySnapshot>({
    online: false,
    reason: hasGatewayUrl() ? null : "no-url",
    providers: [
      { provider: "openai", state: "unknown" },
      { provider: "anthropic", state: "unknown" },
    ],
    budgets: [],
  });
  const [events, setEvents] = useState<FeedEvent[]>([]);
  const [chaosLeft, setChaosLeft] = useState(0);
  const [chaosUntil, setChaosUntil] = useState<number | null>(null);
  const [chaosError, setChaosError] = useState<string | null>(null);
  // Until the first poll resolves we show a neutral loading frame, not "offline".
  const [checked, setChecked] = useState(false);

  const freshestRef = useRef<string | null>(null);

  // ── status + budget polling ────────────────────────────────────────────
  useEffect(() => {
    let alive = true;

    const poll = async () => {
      const snap = await fetchSnapshot();
      if (!alive) return;
      setSnapshot(snap);
      setChecked(true);
    };

    poll();
    const id = setInterval(poll, 2500);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  // ── request feed: SSE when available, polling otherwise ────────────────
  const pushEvents = useCallback((incoming: FeedEvent[]) => {
    if (!incoming.length) return;
    setEvents((prev) => {
      const seen = new Set(prev.map((e) => `${e.timestamp}|${e.team}|${e.status}`));
      const added = incoming.filter((e) => !seen.has(`${e.timestamp}|${e.team}|${e.status}`));
      if (!added.length) return prev;

      // Newest first; the backend may hand us either order.
      const merged = [...added, ...prev].sort(
        (a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime()
      );
      freshestRef.current = merged[0]?.timestamp ?? null;
      return merged.slice(0, MAX_ROWS);
    });
  }, []);

  useEffect(() => {
    if (!snapshot.online) return;

    let source: EventSource | null = null;
    let pollId: ReturnType<typeof setInterval> | null = null;
    let cancelled = false;

    const startPolling = () => {
      if (pollId || cancelled) return;
      const tick = async () => {
        const feed = await fetchFeed(MAX_ROWS);
        if (feed && !cancelled) pushEvents(feed);
      };
      tick();
      pollId = setInterval(tick, 1500);
    };

    // Try SSE first; fall back to polling the moment it errors out.
    try {
      source = new EventSource(feedStreamUrl());
      source.onmessage = (msg) => {
        try {
          pushEvents(coerceEvents(JSON.parse(msg.data)));
        } catch {
          // A malformed frame shouldn't kill the stream.
        }
      };
      source.onerror = () => {
        source?.close();
        source = null;
        startPolling();
      };
    } catch {
      startPolling();
    }

    return () => {
      cancelled = true;
      source?.close();
      if (pollId) clearInterval(pollId);
    };
  }, [snapshot.online, pushEvents]);

  // ── chaos countdown ────────────────────────────────────────────────────
  // Driven off a wall-clock deadline rather than by decrementing state, so a
  // re-render mid-tick can't drop a second and the countdown stays truthful
  // even if the tab is backgrounded and timers are throttled.
  useEffect(() => {
    if (chaosUntil === null) return;

    const tick = () => {
      const left = Math.max(0, Math.ceil((chaosUntil - Date.now()) / 1000));
      setChaosLeft(left);
      if (left === 0) setChaosUntil(null);
    };

    tick();
    const id = setInterval(tick, 250);
    return () => clearInterval(id);
  }, [chaosUntil]);

  const onChaos = async () => {
    setChaosError(null);
    // Optimistic so the button reacts immediately; rolled back if the call fails.
    setChaosUntil(Date.now() + CHAOS_SECONDS * 1000);

    const res = await triggerChaos(CHAOS_SECONDS, "openai");
    if (!res.ok) {
      setChaosUntil(null);
      setChaosLeft(0);
      setChaosError(res.error ?? "Couldn't start the outage.");
    }
  };

  const offline = checked && !snapshot.online;
  const chaosActive = chaosUntil !== null;

  return (
    <div className="card overflow-hidden">
      {/* title bar */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-ink-700 bg-ink-850/60 px-4 py-3 sm:px-5">
        <div className="flex gap-1.5" aria-hidden="true">
          <span className="h-2.5 w-2.5 rounded-full bg-ink-600" />
          <span className="h-2.5 w-2.5 rounded-full bg-ink-600" />
          <span className="h-2.5 w-2.5 rounded-full bg-ink-600" />
        </div>
        <span className="font-mono text-[0.72rem] text-slate-500">gateway · live</span>
        <span
          className={`ml-auto flex items-center gap-1.5 font-mono text-[0.68rem] uppercase tracking-wider ${
            snapshot.online ? "text-status-ok" : "text-slate-600"
          }`}
        >
          <span className={`h-1.5 w-1.5 rounded-full ${snapshot.online ? "bg-status-ok" : "bg-slate-700"}`} />
          {!checked ? "connecting" : snapshot.online ? "connected" : "offline"}
        </span>
      </div>

      <div className="p-4 sm:p-6">
        {offline ? (
          <OfflineState reason={snapshot.reason} />
        ) : (
          <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_20rem] lg:gap-8">
            {/* ── feed (first on desktop, second on mobile) ── */}
            <div className="order-2 lg:order-1">
              <div className="mb-2.5 flex items-baseline justify-between">
                <h3 className="font-mono text-[0.7rem] uppercase tracking-[0.18em] text-slate-500">request feed</h3>
                <span className="font-mono text-[0.65rem] text-slate-600">last {MAX_ROWS}</span>
              </div>

              <ul className="min-h-[18rem] overflow-hidden rounded-md border border-ink-700 bg-ink-950/70">
                {events.length === 0 ? (
                  <li className="flex min-h-[18rem] items-center justify-center px-4 text-center font-mono text-[0.72rem] text-slate-600">
                    {checked ? "waiting for traffic…" : "connecting…"}
                  </li>
                ) : (
                  events.map((e, i) => (
                    <FeedRow
                      key={`${e.timestamp}-${e.team}-${i}`}
                      e={e}
                      fresh={i === 0 && e.timestamp === freshestRef.current}
                    />
                  ))
                )}
              </ul>
            </div>

            {/* ── controls ── */}
            <div className="order-1 flex flex-col gap-6 lg:order-2">
              <div>
                <h3 className="mb-2.5 font-mono text-[0.7rem] uppercase tracking-[0.18em] text-slate-500">
                  circuit breakers
                </h3>
                <div className="flex flex-col gap-2">
                  {snapshot.providers.map((p) => (
                    <ProviderPill
                      key={p.provider}
                      name={p.provider === "openai" ? "OpenAI" : "Anthropic"}
                      state={p.state}
                    />
                  ))}
                </div>
              </div>

              <div>
                <button
                  onClick={onChaos}
                  disabled={chaosActive}
                  className="w-full rounded-md border border-status-bad/40 bg-status-bad/10 px-4 py-2.5 text-sm font-medium text-status-bad transition-colors hover:bg-status-bad/20 focus:outline-none focus-visible:ring-2 focus-visible:ring-status-bad/50 disabled:cursor-not-allowed disabled:border-ink-700 disabled:bg-ink-850 disabled:text-slate-500"
                >
                  {chaosActive ? (
                    <span className="tabular font-mono text-[0.8rem]">outage active · {chaosLeft}s remaining</span>
                  ) : (
                    "Simulate OpenAI outage (30s)"
                  )}
                </button>

                {chaosActive && (
                  <div className="mt-2 h-0.5 w-full overflow-hidden rounded-full bg-ink-800">
                    <div
                      className="h-full bg-status-bad transition-[width] duration-1000 ease-linear"
                      style={{ width: `${(chaosLeft / CHAOS_SECONDS) * 100}%` }}
                    />
                  </div>
                )}
                {chaosError && <p className="mt-2 font-mono text-[0.68rem] text-status-warn">{chaosError}</p>}
                {!chaosActive && !chaosError && (
                  <p className="mt-2 text-[0.72rem] leading-relaxed text-slate-600">
                    Forces OpenAI to fail. Watch the breaker trip, requests re-route to Anthropic mid-flight, then
                    recover.
                  </p>
                )}
              </div>

              <div>
                <h3 className="mb-3 font-mono text-[0.7rem] uppercase tracking-[0.18em] text-slate-500">
                  daily budgets
                </h3>
                <div className="flex flex-col gap-3.5">
                  {(snapshot.budgets.length
                    ? snapshot.budgets
                    : [
                        { team: "team-a", spend: 0, cap: 1 },
                        { team: "team-b", spend: 0, cap: 1 },
                      ]
                  ).map((b) => (
                    <BudgetBar key={b.team} row={b} />
                  ))}
                </div>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
