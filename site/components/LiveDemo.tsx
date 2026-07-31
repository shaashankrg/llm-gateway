"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  BreakerState,
  BudgetRow,
  burnBudget,
  FeedEvent,
  GatewaySnapshot,
  coerceEvents,
  feedStreamUrl,
  fetchFeed,
  fetchSnapshot,
  hasGatewayUrl,
  sendDemoRequest,
  triggerChaos,
  wakeGateway,
} from "@/lib/gateway";

const MAX_ROWS = 15;
const CHAOS_SECONDS = 30;

/**
 * One request every ~3s. Fast enough that the feed always looks alive, slow
 * enough that rows are readable rather than blurring past — and it keeps the
 * demo's footprint on the host trivial.
 */
const TRICKLE_MS = 3000;

/**
 * Running tally of what the visitor's own traffic did.
 *
 * The panel used to show only state — pills and scrolling rows — so someone
 * could watch an entire outage and have no idea whether it went well. These
 * are the numbers that turn it into an experiment with a result. The
 * direct/failover split is the whole point: without it, successful failover
 * is indistinguishable from nothing having gone wrong.
 */
type Tally = {
  attempted: number;
  direct: number;
  viaFailover: number;
  failed: number;
};

const EMPTY_TALLY: Tally = { attempted: 0, direct: 0, viaFailover: 0, failed: 0 };

/**
 * Outcome of the recovery clock.
 *
 * "insufficient-load" is its own case rather than a small number: if demo
 * traffic never tripped the breaker, there was no recovery to time, and
 * reporting a near-zero figure would be noise presented as a measurement.
 */
type Recovery =
  | { kind: "idle" }
  | { kind: "measured"; ms: number }
  | { kind: "insufficient-load" };

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

function Scoreboard({ tally, recovery }: { tally: Tally; recovery: Recovery }) {
  const settled = tally.direct + tally.viaFailover + tally.failed;
  const rate = settled > 0 ? ((tally.direct + tally.viaFailover) / settled) * 100 : null;

  return (
    <dl className="grid grid-cols-2 gap-x-4 gap-y-3 rounded-md border border-ink-700 bg-ink-950/60 p-3.5 sm:grid-cols-4">
      <div>
        <dt className="font-mono text-[0.62rem] uppercase tracking-wider text-slate-600">Requests</dt>
        <dd className="tabular mt-1 font-mono text-lg text-slate-200">{tally.attempted}</dd>
      </div>

      <div>
        <dt className="font-mono text-[0.62rem] uppercase tracking-wider text-slate-600">Succeeded</dt>
        <dd className="tabular mt-1 font-mono text-lg text-slate-200">
          {tally.direct + tally.viaFailover}
        </dd>
        {/* The split is the proof: failover is invisible without it. */}
        <dd className="mt-0.5 font-mono text-[0.6rem] leading-relaxed text-slate-600">
          {tally.direct} direct ·{" "}
          <span className={tally.viaFailover > 0 ? "text-status-warn" : ""}>
            {tally.viaFailover} via failover
          </span>
        </dd>
      </div>

      <div>
        <dt className="font-mono text-[0.62rem] uppercase tracking-wider text-slate-600">Success rate</dt>
        <dd className="tabular mt-1 font-mono text-lg text-slate-200">
          {rate === null ? "—" : `${rate.toFixed(1)}%`}
        </dd>
      </div>

      <div>
        <dt className="font-mono text-[0.62rem] uppercase tracking-wider text-slate-600">Recovery</dt>
        <dd className="tabular mt-1 font-mono text-lg text-slate-200">
          {recovery.kind === "measured" ? `${(recovery.ms / 1000).toFixed(2)}s` : "—"}
        </dd>
        <dd className="mt-0.5 font-mono text-[0.6rem] leading-relaxed text-slate-600">
          {recovery.kind === "measured"
            ? "at demo load"
            : recovery.kind === "insufficient-load"
              ? "breaker never tripped"
              : "outage end → breaker closed"}
        </dd>
      </div>
    </dl>
  );
}

/**
 * Explains why the panel's recovery figure sits below the documented one.
 *
 * At demo traffic (~0.3 req/s) the breaker trips a handful of times at most,
 * so the cooldown elapses almost immediately after the outage ends. The
 * README's 4.47-5.05s comes from sustained chaos-test traffic, where the
 * breaker re-trips on each failed probe. Both are correct measurements of
 * different loads — but a visitor reading 0.67s next to a resume claiming
 * ~5s sees a contradiction unless the difference is stated on the page.
 */
function RecoveryNote({ recovery }: { recovery: Recovery }) {
  if (recovery.kind === "idle") return null;

  if (recovery.kind === "insufficient-load") {
    return (
      <p className="mt-2.5 text-[0.7rem] leading-relaxed text-slate-500">
        The breaker needs 5 consecutive failures to open, and this demo&apos;s trickle of traffic didn&apos;t
        produce them before the outage window closed — so there was no recovery to time. The chaos test drives
        sustained traffic and measures{" "}
        <span className="tabular font-mono text-slate-400">4.47–5.05s</span>.
      </p>
    );
  }

  return (
    <p className="mt-2.5 text-[0.7rem] leading-relaxed text-slate-500">
      <span className="tabular font-mono text-slate-400">{(recovery.ms / 1000).toFixed(2)}s</span> here vs.{" "}
      <span className="tabular font-mono text-slate-400">4.47–5.05s</span> in the chaos test above. Recovery is
      measured from outage end to the breaker closing, and the breaker re-trips on every failed probe — so what
      matters is when the <em>last</em>{" "}trip lands relative to the end of the window. A single visitor&apos;s
      traffic trips it at different moments than the test&apos;s sustained load, so this figure moves run to run.
      Neither number is the &ldquo;real&rdquo; one; the repeated-run range is the one worth quoting.
    </p>
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

/**
 * Set this once a recording exists at site/public/. Use an .mp4 if you have
 * one — it's an order of magnitude smaller than an equivalent GIF and loops
 * just as well. Leave null to show a labelled placeholder instead.
 */
const DEMO_RECORDING: { src: string; type: "video" | "image" } | null = null;
// e.g. { src: "/demo-recording.mp4", type: "video" }

/** Shown when the backend is asleep, missing /demo/*, or has no URL configured. */
function OfflineState({
  reason,
  onWake,
  waking,
  wakeFailed,
}: {
  reason: GatewaySnapshot["reason"];
  onWake: () => void;
  waking: boolean;
  wakeFailed: boolean;
}) {
  const note =
    reason === "no-url"
      ? "NEXT_PUBLIC_GATEWAY_URL isn't set for this deployment."
      : reason === "not-implemented"
        ? "The gateway answered, but the /demo endpoints aren't deployed on it yet."
        : "The gateway is hosted on a free tier that spins down when idle.";

  // Nothing to wake if there's no URL or the host is up but missing endpoints.
  const canWake = reason === "unreachable";

  return (
    <div className="flex flex-col items-center justify-center rounded-lg border border-dashed border-ink-700 bg-ink-950/60 px-6 py-12 text-center">
      <p className="max-w-lg text-[0.95rem] text-slate-300">
        Demo backend is asleep right now — here&apos;s what it looks like when running.
      </p>
      <p className="mt-2 max-w-md font-mono text-[0.7rem] leading-relaxed text-slate-600">{note}</p>

      {canWake && (
        <div className="mt-5">
          <button
            onClick={onWake}
            disabled={waking}
            className="inline-flex items-center gap-2.5 rounded-md border border-accent/40 bg-accent/10 px-4 py-2.5 text-sm font-medium text-accent transition-colors hover:bg-accent/20 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 disabled:cursor-progress disabled:border-ink-700 disabled:bg-ink-850 disabled:text-slate-500"
          >
            {waking && (
              <span
                className="h-3 w-3 animate-spin rounded-full border border-current border-t-transparent"
                aria-hidden="true"
              />
            )}
            {waking ? "Waking it up… (~30s)" : wakeFailed ? "Try again" : "Wake it up (~30s)"}
          </button>
          <p className="mt-2.5 font-mono text-[0.68rem] text-slate-600">
            {waking
              ? "Booting the container — the panel loads by itself when it answers."
              : wakeFailed
                ? "Didn't come up in time. It may be mid-deploy — try again in a minute."
                : "Starts the container and connects the panel automatically."}
          </p>
        </div>
      )}

      {/*
        Optional recording. Left unset on purpose: the wake button produces the
        real panel in well under a minute, and a video sitting next to it would
        mostly persuade people to watch a recording instead of running the
        thing. Set DEMO_RECORDING above if that trade ever looks wrong.
      */}
      {DEMO_RECORDING && (
        <div className="mt-7 w-full max-w-2xl">
          {DEMO_RECORDING.type === "video" ? (
            <video
              src={DEMO_RECORDING.src}
              autoPlay
              loop
              muted
              playsInline
              className="w-full rounded-md border border-ink-700"
            />
          ) : (
            // eslint-disable-next-line @next/next/no-img-element -- static asset
            <img
              src={DEMO_RECORDING.src}
              alt="The live panel handling a simulated OpenAI outage"
              className="w-full rounded-md border border-ink-700"
            />
          )}
        </div>
      )}

      {/* Say what waking actually buys, so the button is worth pressing. */}
      {!DEMO_RECORDING && canWake && (
        <dl className="mt-8 grid w-full max-w-lg gap-x-8 gap-y-2.5 text-left sm:grid-cols-2">
          {[
            ["Circuit breakers", "live state per provider"],
            ["Request feed", "real traffic, failovers tagged"],
            ["Outage button", "trips OpenAI for 30s"],
            ["Budget bars", "per-team spend against caps"],
          ].map(([term, desc]) => (
            <div key={term} className="flex gap-2.5 border-t border-ink-800 pt-2.5">
              <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-slate-700" />
              <div>
                <dt className="font-mono text-[0.72rem] text-slate-400">{term}</dt>
                <dd className="text-[0.72rem] leading-relaxed text-slate-600">{desc}</dd>
              </div>
            </div>
          ))}
        </dl>
      )}
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
  const [onScreen, setOnScreen] = useState(false);
  const [waking, setWaking] = useState(false);
  const [wakeFailed, setWakeFailed] = useState(false);
  const [burnError, setBurnError] = useState<string | null>(null);
  const [tally, setTally] = useState<Tally>(EMPTY_TALLY);
  const [recovery, setRecovery] = useState<Recovery>({ kind: "idle" });
  // Set the moment the outage window ends; cleared once the breaker closes.
  const recoveryStartRef = useRef<number | null>(null);
  // Whether the breaker was actually observed open during this run.
  const breakerTrippedRef = useRef(false);
  // Guards the auto-wake so it fires at most once per page load.
  const autoWokeRef = useRef(false);

  const freshestRef = useRef<string | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);

  // Declared before the effects below, which re-attach the visibility observer
  // when the panel swaps between its offline and live layouts.
  const offline = checked && !snapshot.online;

  // ── status + budget polling ────────────────────────────────────────────
  useEffect(() => {
    // While a wake is in flight its own probe owns the connection: a 3.5s
    // poll would keep timing out against the booting container and stamp the
    // panel back to offline underneath the spinner.
    if (waking) return;

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
  }, [waking]);

  // ── recovery clock: stops the moment the breaker is seen closed ────────
  useEffect(() => {
    const openai = snapshot.providers.find((p) => p.provider === "openai");

    // Track whether the breaker actually opened during this run. If demo
    // traffic was too thin to trip it, there is no recovery to report and a
    // near-zero number would be noise dressed up as a measurement.
    if (openai?.state === "open" || openai?.state === "half_open") {
      breakerTrippedRef.current = true;
    }

    if (recoveryStartRef.current === null) return;

    if (openai?.state === "closed") {
      setRecovery(
        breakerTrippedRef.current
          ? { kind: "measured", ms: Date.now() - recoveryStartRef.current }
          : { kind: "insufficient-load" }
      );
      recoveryStartRef.current = null;
    }
  }, [snapshot]);

  // ── request feed: SSE when available, polling otherwise ────────────────
  const pushEvents = useCallback((incoming: FeedEvent[]) => {
    if (!incoming.length) return;
    setEvents((prev) => {
      const seen = new Set(prev.map((e) => `${e.timestamp}|${e.team}|${e.status}`));
      const added = incoming.filter((e) => !seen.has(`${e.timestamp}|${e.team}|${e.status}`));
      if (!added.length) return prev;

      // Count each event exactly once, here, where de-duplication already
      // happened — tallying at the fetch site would double-count anything
      // that arrives on both the SSE stream and a polling pass.
      setTally((t) => {
        const next = { ...t };
        for (const e of added) {
          next.attempted += 1;
          const ok = Number(e.status) < 300;
          if (!ok) next.failed += 1;
          else if (e.failover) next.viaFailover += 1;
          else next.direct += 1;
        }
        return next;
      });

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

  // ── self-demonstrating traffic ─────────────────────────────────────────
  // The feed reports real requests only, so the panel drives a trickle of its
  // own while it's on screen. Gated on visibility (both IntersectionObserver
  // and tab focus) so it never runs for someone who scrolled past or left the
  // tab open in the background.
  // Watch visibility separately, and keep it in state — the observer has to be
  // attached after the ref is populated, which a ref alone can't trigger.
  useEffect(() => {
    const node = panelRef.current;
    if (!node) return;

    const observer = new IntersectionObserver(
      ([entry]) => setOnScreen(entry.isIntersecting),
      { threshold: 0.15 }
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [offline]);

  useEffect(() => {
    if (!snapshot.online || !onScreen) return;

    let timer: ReturnType<typeof setTimeout> | null = null;
    let stopped = false;

    const loop = async () => {
      if (stopped) return;
      if (document.visibilityState === "visible") {
        await sendDemoRequest();
      }
      // Jittered so rows land unevenly, the way real traffic does.
      if (!stopped) timer = setTimeout(loop, TRICKLE_MS + Math.random() * 1000);
    };

    loop();

    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
  }, [snapshot.online, onScreen]);

  // ── chaos countdown ────────────────────────────────────────────────────
  // Driven off a wall-clock deadline rather than by decrementing state, so a
  // re-render mid-tick can't drop a second and the countdown stays truthful
  // even if the tab is backgrounded and timers are throttled.
  useEffect(() => {
    if (chaosUntil === null) return;

    const tick = () => {
      const left = Math.max(0, Math.ceil((chaosUntil - Date.now()) / 1000));
      setChaosLeft(left);
      if (left === 0) {
        setChaosUntil(null);
        // Start the recovery clock at outage end. It stops when the breaker
        // is next observed closed — the same definition the chaos test uses.
        recoveryStartRef.current = Date.now();
      }
    };

    tick();
    const id = setInterval(tick, 250);
    return () => clearInterval(id);
  }, [chaosUntil]);

  const onBurn = async () => {
    setBurnError(null);
    const res = await burnBudget("team-b");
    if (!res.ok) {
      setBurnError(res.error ?? "Couldn't reach the gateway.");
      return;
    }
    // Reflect it immediately rather than waiting for the next poll.
    setSnapshot(await fetchSnapshot());
  };

  const onWake = async () => {
    setWaking(true);
    setWakeFailed(false);

    const awake = await wakeGateway();
    if (awake) {
      // Give the app a moment to finish starting before the first real poll,
      // so the panel doesn't flash back to offline right after waking.
      await new Promise((r) => setTimeout(r, 1200));
      setSnapshot(await fetchSnapshot(8000));
    } else {
      setWakeFailed(true);
    }
    setWaking(false);
  };

  // Most visitors should never have to press the wake button: if the panel
  // scrolls into view while the host is asleep, start booting it right away.
  // The button stays for anyone whose automatic attempt timed out.
  // Declared here so onWake is already in scope (const isn't hoisted).
  useEffect(() => {
    if (!onScreen || !offline || waking || wakeFailed) return;
    if (snapshot.reason !== "unreachable") return;
    if (autoWokeRef.current) return;

    autoWokeRef.current = true;
    onWake();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onScreen, offline, waking, wakeFailed, snapshot.reason]);

  const onChaos = async () => {
    setChaosError(null);
    // Each outage is its own experiment — reset the scoreboard so the numbers
    // describe this run rather than everything since the page loaded.
    setTally(EMPTY_TALLY);
    setRecovery({ kind: "idle" });
    recoveryStartRef.current = null;
    breakerTrippedRef.current = false;
    // Optimistic so the button reacts immediately; rolled back if the call fails.
    setChaosUntil(Date.now() + CHAOS_SECONDS * 1000);

    const res = await triggerChaos(CHAOS_SECONDS, "openai");
    if (!res.ok) {
      setChaosUntil(null);
      setChaosLeft(0);
      setChaosError(res.error ?? "Couldn't start the outage.");
    }
  };

  const chaosActive = chaosUntil !== null;

  return (
    <div ref={panelRef} className="card overflow-hidden">
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
          {waking ? "waking" : !checked ? "connecting" : snapshot.online ? "connected" : "offline"}
        </span>
      </div>

      <div className="p-4 sm:p-6">
        {offline ? (
          <OfflineState
            reason={snapshot.reason}
            onWake={onWake}
            waking={waking}
            wakeFailed={wakeFailed}
          />
        ) : (
          <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_20rem] lg:gap-8">
            {/* ── feed (first on desktop, second on mobile) ── */}
            <div className="order-2 lg:order-1">
              <div className="mb-2.5 flex items-baseline justify-between">
                <h3 className="font-mono text-[0.7rem] uppercase tracking-[0.18em] text-slate-500">
                  this session
                </h3>
                <span className="font-mono text-[0.65rem] text-slate-600">
                  {chaosActive ? "during outage" : "since page load"}
                </span>
              </div>
              <div className="mb-5">
                <Scoreboard tally={tally} recovery={recovery} />
                <RecoveryNote recovery={recovery} />
              </div>

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
                <div className="mb-3 flex items-baseline justify-between gap-2">
                  <h3 className="font-mono text-[0.7rem] uppercase tracking-[0.18em] text-slate-500">
                    daily budgets
                  </h3>
                  <button
                    onClick={onBurn}
                    className="font-mono text-[0.65rem] text-accent/80 underline decoration-dotted underline-offset-2 transition-colors hover:text-accent"
                  >
                    spend team-b&apos;s budget
                  </button>
                </div>
                {burnError && <p className="mb-2 font-mono text-[0.65rem] text-status-warn">{burnError}</p>}
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
