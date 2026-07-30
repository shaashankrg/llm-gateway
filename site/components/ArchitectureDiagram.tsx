/**
 * The README's ASCII architecture diagram, redrawn as SVG.
 * Scales with its container via viewBox; readable down to ~340px wide.
 */

const BOX = "fill-ink-850 stroke-ink-600";
const TEXT = "fill-slate-200 font-[500]";
const MUTED = "fill-slate-500";
const ACCENT = "fill-accent";

function Arrow({ x, y1, y2 }: { x: number; y1: number; y2: number }) {
  return <line x1={x} y1={y1} x2={x} y2={y2} className="stroke-ink-600" strokeWidth={1.5} markerEnd="url(#arrow)" />;
}

export default function ArchitectureDiagram({ className = "" }: { className?: string }) {
  return (
    // Below ~640px the labels would shrink past legibility, so the diagram
    // scrolls in its own container instead of scaling down further.
    <figure className={`-mx-1 overflow-x-auto px-1 ${className}`}>
      <svg
        viewBox="0 0 900 620"
        className="h-auto w-full min-w-[600px]"
        role="img"
        aria-label="Architecture: clients call nginx, which round-robins across three FastAPI gateway replicas. Each replica authenticates, rate limits, checks budget, enqueues by priority, then a worker pool calls providers through a circuit breaker with retry and failover. Redis holds shared rate-limit and budget state across replicas. Prometheus and Grafana collect metrics."
      >
        <defs>
          <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" className="fill-ink-600" />
          </marker>
          <marker id="arrow-accent" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" className="fill-accent/70" />
          </marker>
        </defs>

        {/* ── clients ─────────────────────────────────────────────── */}
        <g>
          <rect x={355} y={10} width={190} height={44} rx={6} className={BOX} strokeWidth={1} />
          <text x={450} y={31} textAnchor="middle" className={`${TEXT} text-[14px]`}>clients</text>
          <text x={450} y={46} textAnchor="middle" className={`${MUTED} font-mono text-[10px]`}>X-API-Key · X-Priority</text>
        </g>
        <Arrow x={450} y1={54} y2={80} />

        {/* ── nginx ───────────────────────────────────────────────── */}
        <g>
          <rect x={355} y={80} width={190} height={44} rx={6} className={BOX} strokeWidth={1} />
          <text x={450} y={101} textAnchor="middle" className={`${TEXT} text-[14px]`}>nginx</text>
          <text x={450} y={116} textAnchor="middle" className={`${MUTED} font-mono text-[10px]`}>round-robin across replicas</text>
        </g>
        <Arrow x={450} y1={124} y2={150} />

        {/* ── gateway ─────────────────────────────────────────────── */}
        <g>
          <rect x={90} y={150} width={720} height={190} rx={8} className="fill-ink-900 stroke-accent/25" strokeWidth={1.25} />
          <text x={112} y={176} className={`${TEXT} text-[14px]`}>FastAPI gateway</text>
          <text x={252} y={176} className={`${ACCENT} font-mono text-[11px]`}>×3 replicas</text>

          {/* pipeline row: auth → rate limit → budget */}
          {[
            { x: 112, label: "authenticate", n: "1" },
            { x: 302, label: "rate limit", n: "2" },
            { x: 492, label: "budget", n: "3" },
          ].map((s, i) => (
            <g key={s.n}>
              <rect x={s.x} y={192} width={160} height={38} rx={5} className="fill-ink-800 stroke-ink-600" strokeWidth={1} />
              <circle cx={s.x + 18} cy={211} r={9} className="fill-accent/15 stroke-accent/50" strokeWidth={1} />
              <text x={s.x + 18} y={215} textAnchor="middle" className={`${ACCENT} font-mono text-[10px]`}>{s.n}</text>
              <text x={s.x + 34} y={216} className={`${TEXT} text-[12.5px]`}>{s.label}</text>
              {i < 2 && (
                <line
                  x1={s.x + 162} y1={211} x2={s.x + 188} y2={211}
                  className="stroke-ink-600" strokeWidth={1.5} markerEnd="url(#arrow)"
                />
              )}
            </g>
          ))}

          {/* priority queue */}
          <line x1={450} y1={230} x2={450} y2={248} className="stroke-ink-600" strokeWidth={1.5} markerEnd="url(#arrow)" />
          <rect x={252} y={248} width={396} height={34} rx={5} className="fill-ink-800 stroke-ink-600" strokeWidth={1} />
          <circle cx={272} cy={265} r={9} className="fill-accent/15 stroke-accent/50" strokeWidth={1} />
          <text x={272} y={269} textAnchor="middle" className={`${ACCENT} font-mono text-[10px]`}>4</text>
          <text x={288} y={270} className={`${TEXT} text-[12.5px]`}>priority queue</text>
          <text x={392} y={270} className={`${MUTED} font-mono text-[10.5px]`}>realtime &gt; batch</text>

          {/* worker pool + its three behaviors */}
          <line x1={450} y1={282} x2={450} y2={296} className="stroke-ink-600" strokeWidth={1.5} markerEnd="url(#arrow)" />
          <rect x={112} y={296} width={186} height={32} rx={5} className="fill-ink-800 stroke-ink-600" strokeWidth={1} />
          <circle cx={132} cy={312} r={9} className="fill-accent/15 stroke-accent/50" strokeWidth={1} />
          <text x={132} y={316} textAnchor="middle" className={`${ACCENT} font-mono text-[10px]`}>5</text>
          <text x={148} y={317} className={`${TEXT} text-[12.5px]`}>worker pool</text>

          {[
            { x: 318, label: "circuit breaker" },
            { x: 480, label: "retry + backoff" },
            { x: 642, label: "failover routing" },
          ].map((s) => (
            <g key={s.label}>
              <rect x={s.x} y={296} width={156} height={32} rx={5} className="fill-ink-800 stroke-ink-600" strokeWidth={1} />
              <text x={s.x + 78} y={317} textAnchor="middle" className={`${TEXT} text-[12px]`}>{s.label}</text>
            </g>
          ))}
          <line x1={298} y1={312} x2={316} y2={312} className="stroke-ink-600" strokeWidth={1.5} markerEnd="url(#arrow)" />
        </g>

        {/* ── branch down to Redis and providers ──────────────────── */}
        <path d="M 260 340 L 260 392" className="stroke-ink-600" strokeWidth={1.5} markerEnd="url(#arrow)" fill="none" />
        <path d="M 640 340 L 640 392" className="stroke-accent/50" strokeWidth={1.5} markerEnd="url(#arrow-accent)" fill="none" />
        {/* Plain digits, not circled-number glyphs — those render as tofu on some systems. */}
        <text x={272} y={368} className={`${MUTED} font-mono text-[10px]`}>shared state (2,3)</text>
        <text x={652} y={368} className="fill-accent/70 font-mono text-[10px]">outbound calls (5)</text>

        {/* Redis */}
        <g>
          <rect x={90} y={392} width={330} height={160} rx={8} className={BOX} strokeWidth={1} />
          <text x={112} y={418} className={`${TEXT} text-[14px]`}>Redis</text>
          {["token buckets", "daily spend", "budget reservations"].map((t, i) => (
            <text key={t} x={112} y={444 + i * 20} className={`${MUTED} font-mono text-[11px]`}>{t}</text>
          ))}
          <line x1={112} y1={512} x2={398} y2={512} className="stroke-ink-700" strokeWidth={1} />
          <text x={112} y={532} className="fill-accent/70 font-mono text-[10.5px]">one instance, shared by all replicas —</text>
          <text x={112} y={546} className="fill-accent/70 font-mono text-[10.5px]">so quotas stay global</text>
        </g>

        {/* Providers */}
        <g>
          <rect x={470} y={392} width={340} height={92} rx={8} className={BOX} strokeWidth={1} />
          <text x={492} y={418} className={`${TEXT} text-[14px]`}>OpenAI</text>
          <text x={562} y={418} className={`${ACCENT} font-mono text-[13px]`}>⇄</text>
          <text x={584} y={418} className={`${TEXT} text-[14px]`}>Anthropic</text>
          <text x={492} y={442} className={`${MUTED} font-mono text-[10.5px]`}>primary exhausts retries — the same</text>
          <text x={492} y={457} className={`${MUTED} font-mono text-[10.5px]`}>request completes on the other</text>
          <text x={492} y={472} className={`${MUTED} font-mono text-[10.5px]`}>provider</text>
        </g>

        {/* Observability, off to the side */}
        <g>
          <rect x={470} y={500} width={340} height={52} rx={8} className="fill-ink-850 stroke-ink-700" strokeDasharray="4 3" strokeWidth={1} />
          <text x={492} y={524} className={`${TEXT} text-[13px]`}>Prometheus</text>
          <text x={584} y={524} className={`${MUTED} text-[13px]`}>→</text>
          <text x={604} y={524} className={`${TEXT} text-[13px]`}>Grafana</text>
          <text x={492} y={541} className={`${MUTED} font-mono text-[10.5px]`}>traces · metrics · breaker state</text>
        </g>

        {/* dotted scrape line from the gateway to Prometheus */}
        <path
          d="M 810 250 Q 862 250 862 526 L 814 526"
          className="stroke-ink-700"
          strokeWidth={1.25}
          strokeDasharray="3 4"
          fill="none"
          markerEnd="url(#arrow)"
        />
        <text x={866} y={390} className={`${MUTED} font-mono text-[9.5px]`} transform="rotate(90 866 390)">/metrics</text>
      </svg>
    </figure>
  );
}
