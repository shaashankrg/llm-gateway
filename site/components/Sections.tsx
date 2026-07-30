import { CAPABILITIES, DECISIONS, LIMITS, NEGATIVE_RESULT, STATS, type Stat } from "@/lib/content";

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

const TONE: Record<NonNullable<Stat["tone"]>, string> = {
  ok: "text-status-ok",
  info: "text-status-info",
  warn: "text-status-warn",
};

function StatCard({ stat }: { stat: Stat }) {
  return (
    <div className="card flex flex-col p-5 transition-colors hover:border-ink-600">
      <p className="font-mono text-[0.68rem] uppercase tracking-[0.16em] text-slate-500">{stat.label}</p>
      <p className="mt-3.5 flex items-baseline gap-1">
        <span className={`tabular font-mono text-stat font-semibold ${TONE[stat.tone ?? "ok"]}`}>{stat.value}</span>
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
      <div className="mt-4 overflow-hidden rounded-lg border border-status-warn/25 bg-status-warn/[0.035]">
        <div className="flex items-center gap-2.5 border-b border-status-warn/20 px-5 py-3">
          <span className="h-1.5 w-1.5 rounded-full bg-status-warn" />
          <h3 className="font-mono text-[0.72rem] uppercase tracking-[0.18em] text-status-warn">
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
                  <span className="tabular mt-px shrink-0 font-mono text-[0.7rem] text-status-warn/70">
                    {String(i + 1).padStart(2, "0")}
                  </span>
                  <span className="text-[0.86rem] leading-relaxed text-slate-400">{d}</span>
                </li>
              ))}
            </ol>
            <p className="mt-5 border-l-2 border-status-warn/40 pl-4 text-[0.86rem] leading-relaxed text-slate-300">
              {NEGATIVE_RESULT.conclusion}
            </p>
          </div>
        </div>
      </div>
    </Section>
  );
}

export function Decisions() {
  return (
    <Section
      id="decisions"
      eyebrow="engineering decisions"
      title="Decisions worth explaining"
      intro="The parts where the obvious implementation was wrong, and why."
    >
      <div className="grid gap-4 md:grid-cols-2">
        {DECISIONS.map((d) => (
          <article key={d.title} className="card flex flex-col p-5 transition-colors hover:border-ink-600">
            <span className="w-fit rounded border border-ink-700 bg-ink-850 px-2 py-0.5 font-mono text-[0.62rem] uppercase tracking-wider text-accent/70">
              {d.tag}
            </span>
            <h3 className="mt-3.5 font-mono text-[0.95rem] font-medium leading-snug text-slate-100">{d.title}</h3>
            <p className="mt-2.5 text-[0.85rem] leading-relaxed text-slate-400">{d.body}</p>
          </article>
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
