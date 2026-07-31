import {
  CAPABILITIES,
  DASHBOARD_SHOTS,
  LIMITS,
  NEGATIVE_RESULT,
  STATS,
  type DashboardShot,
  type Stat,
} from "@/lib/content";

export function Section({
  id,
  eyebrow,
  title,
  intro,
  children,
}: {
  id: string;
  eyebrow: string;
  title: string;
  intro?: string;
  children: React.ReactNode;
}) {
  return (
    <section id={id} className="scroll-mt-8 border-t border-ink-800 py-16 sm:py-20">
      <div className="shell">
        <p className="eyebrow">{eyebrow}</p>
        <h2 className="section-title mt-3">{title}</h2>
        {intro && <p className="mt-3 max-w-3xl text-[0.95rem] leading-relaxed text-slate-400">{intro}</p>}
        <div className="mt-9">{children}</div>
      </div>
    </section>
  );
}

function StatCard({ stat }: { stat: Stat }) {
  // Figures are near-white rather than color-coded: every stat here is a good
  // result, so a hue would carry no information and only add noise. Size and
  // weight do the emphasis instead.
  return (
    <div className="card flex flex-col p-5 transition-colors hover:border-ink-600">
      <p className="font-mono text-[0.68rem] uppercase tracking-[0.16em] text-slate-500">{stat.label}</p>
      <p className="mt-3.5 flex items-baseline gap-1">
        <span className="tabular font-mono text-stat font-semibold text-slate-100">{stat.value}</span>
        {stat.unit && <span className="font-mono text-xl text-slate-500">{stat.unit}</span>}
      </p>
      <p className="mt-3.5 text-[0.82rem] leading-relaxed text-slate-500">{stat.detail}</p>
    </div>
  );
}

export function Results() {
  return (
    <Section
      id="results"
      eyebrow="measured results"
      title="Every claim here is backed by a test in the repo"
      intro="Ranges are across repeated runs — no single best run is quoted as if it were typical. Recovery is measured two independent ways because a number that can't be cross-validated isn't one worth quoting."
    >
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {STATS.map((s) => (
          <StatCard key={s.label} stat={s} />
        ))}
      </div>

      {/* The negative result gets its own full-width card and equal visual weight. */}
      <div className="mt-4 overflow-hidden rounded-lg border border-ink-600 bg-ink-900/70">
        <div className="flex items-center gap-2.5 border-b border-ink-700 px-5 py-3">
          <span className="h-1.5 w-1.5 rounded-full bg-status-warn/70" />
          <h3 className="font-mono text-[0.72rem] uppercase tracking-[0.18em] text-slate-300">
            {NEGATIVE_RESULT.title}
          </h3>
        </div>

        <div className="grid gap-7 p-5 sm:p-7 lg:grid-cols-[minmax(0,22rem)_minmax(0,1fr)] lg:gap-10">
          <div>
            <p className="text-lg font-semibold leading-snug text-slate-100">{NEGATIVE_RESULT.headline}</p>
            <table className="mt-5 w-full border-collapse text-left">
              <thead>
                <tr className="border-b border-ink-700">
                  <th className="pb-2 font-mono text-[0.65rem] font-normal uppercase tracking-wider text-slate-600">
                    Topology
                  </th>
                  <th className="pb-2 text-right font-mono text-[0.65rem] font-normal uppercase tracking-wider text-slate-600">
                    @ 500 concurrent
                  </th>
                </tr>
              </thead>
              <tbody>
                {NEGATIVE_RESULT.table.map((row) => (
                  <tr key={row.topology} className="border-b border-ink-800/70">
                    <td className="py-2.5 text-[0.85rem] text-slate-400">{row.topology}</td>
                    <td className="tabular py-2.5 text-right font-mono text-[0.85rem] text-slate-200">
                      {row.throughput}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="mt-3 font-mono text-[0.68rem] text-slate-600">warmed, tracing noise disabled</p>
          </div>

          <div>
            <p className="font-mono text-[0.68rem] uppercase tracking-[0.16em] text-slate-500">
              Rather than dropping the experiment, it was diagnosed
            </p>
            <ol className="mt-4 space-y-3.5">
              {NEGATIVE_RESULT.diagnosis.map((d, i) => (
                <li key={i} className="flex gap-3.5">
                  <span className="tabular mt-px shrink-0 font-mono text-[0.7rem] text-slate-600">
                    {String(i + 1).padStart(2, "0")}
                  </span>
                  <span className="text-[0.86rem] leading-relaxed text-slate-400">{d}</span>
                </li>
              ))}
            </ol>
            <p className="mt-5 border-l-2 border-ink-600 pl-4 text-[0.86rem] leading-relaxed text-slate-300">
              {NEGATIVE_RESULT.conclusion}
            </p>
          </div>
        </div>
      </div>
    </Section>
  );
}

function Shot({ shot }: { shot: DashboardShot }) {
  return (
    <figure className={`card overflow-hidden ${shot.wide ? "md:col-span-2" : ""}`}>
      {shot.src ? (
        // eslint-disable-next-line @next/next/no-img-element -- static asset, no optimization needed
        <img src={shot.src} alt={shot.title} className="w-full border-b border-ink-800" />
      ) : (
        /*
          REPLACE ME ────────────────────────────────────────────────────────
          Screenshot this Grafana panel, save it to site/public/, then set
          `src` for this entry in lib/content.ts (e.g. "/grafana-breaker.png").
          This placeholder disappears automatically once src is set.
          ───────────────────────────────────────────────────────────────────
        */
        <div
          className={`flex items-center justify-center border-b border-ink-800 bg-ink-950/70 ${
            shot.wide ? "aspect-[21/6]" : "aspect-[16/9]"
          }`}
        >
          <p className="font-mono text-[0.65rem] uppercase tracking-[0.2em] text-slate-700">
            grafana screenshot slot
          </p>
        </div>
      )}
      <figcaption className="p-4">
        <p className="font-mono text-[0.8rem] text-slate-200">{shot.title}</p>
        <p className="mt-1.5 text-[0.8rem] leading-relaxed text-slate-500">{shot.caption}</p>
      </figcaption>
    </figure>
  );
}

export function Dashboard() {
  return (
    <Section
      id="observability"
      eyebrow="observability"
      title="What it looks like from the inside"
      intro="OpenTelemetry traces span the queue boundary — the trace context is captured at enqueue and re-attached inside the worker, so one request produces one connected trace instead of two orphans. These panels ship provisioned with the repo."
    >
      <div className="grid gap-4 md:grid-cols-2">
        {DASHBOARD_SHOTS.map((s) => (
          <Shot key={s.title} shot={s} />
        ))}
      </div>
    </Section>
  );
}

export function Capabilities() {
  return (
    <Section id="capabilities" eyebrow="what it does" title="Capabilities">
      <div className="card divide-y divide-ink-800 overflow-hidden">
        {CAPABILITIES.map((c) => (
          <div
            key={c.name}
            className="grid gap-1 px-5 py-3.5 transition-colors hover:bg-ink-850/50 sm:grid-cols-[13rem_minmax(0,1fr)] sm:gap-6"
          >
            <span className="font-mono text-[0.8rem] text-slate-200">{c.name}</span>
            <span className="text-[0.84rem] leading-relaxed text-slate-500">{c.detail}</span>
          </div>
        ))}
      </div>
    </Section>
  );
}

export function Limits() {
  return (
    <Section
      id="limits"
      eyebrow="known limits"
      title="Where this stops being trustworthy"
      intro="Stated plainly, because knowing where a system stops being trustworthy is part of building it."
    >
      <ul className="grid gap-3 md:grid-cols-2">
        {LIMITS.map((l) => (
          <li key={l.title} className="flex gap-3.5 rounded-lg border border-ink-800 bg-ink-900/40 p-4">
            <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-slate-600" />
            <div>
              <p className="font-mono text-[0.82rem] text-slate-200">{l.title}</p>
              <p className="mt-1.5 text-[0.82rem] leading-relaxed text-slate-500">{l.body}</p>
            </div>
          </li>
        ))}
      </ul>
    </Section>
  );
}
