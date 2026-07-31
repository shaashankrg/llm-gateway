import ArchitectureDiagram from "@/components/ArchitectureDiagram";
import LiveDemo from "@/components/LiveDemo";
import { Capabilities, Dashboard, Limits, Results, Section } from "@/components/Sections";
import { CONTACT_EMAIL, GITHUB_URL, HERO, LINKEDIN_URL } from "@/lib/content";

function GitHubIcon() {
  return (
    <svg viewBox="0 0 16 16" className="h-4 w-4" fill="currentColor" aria-hidden="true">
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z" />
    </svg>
  );
}

export default function Page() {
  return (
    <main>
      {/* ─────────────────────────── hero ─────────────────────────── */}
      <header className="relative overflow-hidden">
        {/* faint grid, terminal-adjacent rather than decorative gradient */}
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-0 opacity-[0.35]"
          style={{
            backgroundImage:
              "linear-gradient(to right, rgba(148,163,184,0.055) 1px, transparent 1px), linear-gradient(to bottom, rgba(148,163,184,0.055) 1px, transparent 1px)",
            backgroundSize: "56px 56px",
            maskImage: "radial-gradient(ellipse 80% 55% at 50% 0%, #000 40%, transparent 100%)",
            WebkitMaskImage: "radial-gradient(ellipse 80% 55% at 50% 0%, #000 40%, transparent 100%)",
          }}
        />

        <div className="shell relative pb-14 pt-16 sm:pt-24">
          <div className="flex items-center gap-2.5 font-mono text-[0.7rem] uppercase tracking-[0.2em] text-slate-500">
            <span className="h-1.5 w-1.5 rounded-full bg-accent" />
            backend infrastructure
          </div>

          <h1 className="mt-5 text-4xl font-semibold tracking-tight text-slate-50 sm:text-6xl">{HERO.name}</h1>

          {/* Plain language first, then the precise technical framing. */}
          <p className="mt-5 max-w-3xl text-lg leading-relaxed text-slate-200 sm:text-xl">{HERO.plain}</p>

          <p className="mt-4 max-w-3xl text-[0.95rem] leading-relaxed text-slate-400">{HERO.pitch}</p>

          <p className="mt-3 max-w-3xl text-[0.95rem] leading-relaxed text-slate-500">{HERO.subline}</p>

          <div className="mt-8 flex flex-wrap items-center gap-3">
            <a
              href={GITHUB_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-2 rounded-md border border-ink-600 bg-ink-850 px-4 py-2.5 text-sm font-medium text-slate-200 transition-colors hover:border-slate-500 hover:bg-ink-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50"
            >
              <GitHubIcon />
              View source
            </a>
            <a
              href="#demo"
              className="inline-flex items-center gap-2 rounded-md border border-accent/40 bg-accent/10 px-4 py-2.5 text-sm font-medium text-accent transition-colors hover:bg-accent/20 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/50"
            >
              <span className="h-1.5 w-1.5 rounded-full bg-accent" />
              Try the live demo
            </a>
            <span className="text-[0.8rem] text-slate-500">
              — break it yourself, in the browser
            </span>
          </div>

          {/*
            Headline evidence sits above the architecture diagram on purpose.
            The diagram is a wall of boxes to a non-engineer, and it was the
            first thing after the hero — putting three legible numbers here
            means a skimmer gets the point before deciding whether to scroll.
          */}
          <dl className="mt-10 grid gap-x-10 gap-y-6 border-t border-ink-800 pt-7 sm:grid-cols-3">
            {HERO.highlights.map((h) => (
              <div key={h.figure}>
                <dt className="flex items-baseline gap-2">
                  <span className="tabular font-mono text-3xl font-semibold text-slate-100">{h.figure}</span>
                  <span className="text-[0.85rem] text-slate-300">{h.label}</span>
                </dt>
                <dd className="mt-1.5 text-[0.8rem] leading-relaxed text-slate-500">{h.detail}</dd>
              </div>
            ))}
          </dl>

          <div className="mt-8 flex flex-wrap gap-x-3 gap-y-2 font-mono text-[0.7rem] text-slate-600">
            {HERO.stack.map((s) => (
              <span key={s}>{s}</span>
            ))}
          </div>

          <div className="mt-14 rounded-lg border border-ink-800 bg-ink-900/50 p-4 sm:p-7">
            <p className="eyebrow">request path</p>
            <p className="mb-6 mt-2 max-w-2xl text-[0.85rem] leading-relaxed text-slate-500">
              What happens to a single request: it&apos;s authenticated, checked against the team&apos;s rate limit and
              budget, queued by priority, then sent to a provider — retrying and switching providers if one fails.
            </p>
            <ArchitectureDiagram />
          </div>
        </div>
      </header>

      <Results />

      {/* ────────────────────────── live demo ─────────────────────── */}
      <Section
        id="demo"
        eyebrow="live demo"
        title="Watch it handle a real outage"
        intro="This panel talks to the deployed gateway. Trip OpenAI and watch the circuit breaker open, in-flight requests re-route to Anthropic, and the system recover — the same behavior the chaos test measures above."
      >
        <LiveDemo />
      </Section>

      <Dashboard />
      <Capabilities />
      <Limits />

      {/* ─────────────────────────── footer ──────────────────────── */}
      <footer className="border-t border-ink-800 py-12">
        <div className="shell flex flex-col gap-6 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <p className="font-mono text-sm text-slate-300">LLM Gateway</p>
            <p className="mt-1.5 font-mono text-[0.72rem] text-slate-600">
              Built with FastAPI, Redis, Docker, OpenTelemetry, Prometheus, Grafana.
            </p>
          </div>

          <nav className="flex flex-wrap gap-x-6 gap-y-2 font-mono text-[0.78rem]">
            <a
              href={GITHUB_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="text-slate-400 transition-colors hover:text-accent"
            >
              GitHub
            </a>
            <a
              href={LINKEDIN_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="text-slate-400 transition-colors hover:text-accent"
            >
              LinkedIn
            </a>
            <a href={`mailto:${CONTACT_EMAIL}`} className="text-slate-400 transition-colors hover:text-accent">
              Email
            </a>
          </nav>
        </div>
      </footer>
    </main>
  );
}
