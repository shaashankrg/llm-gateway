# LLM Gateway — project showcase site

Single-page portfolio site for the [LLM Gateway](../README.md) project. Next.js 15 (App Router) + TypeScript + Tailwind, deployable to Vercel as-is.

All prose and every number live in [`lib/content.ts`](lib/content.ts), quoted from the gateway README's "Measured results" section. Edit that one file to change copy.

## Run it

```bash
cd site
npm install
cp .env.example .env.local   # optional; without it the demo panel shows its offline state
npm run dev
```

## Deploy

```bash
cd site
vercel deploy
```

If the repo root is the Vercel project root, set **Root Directory** to `site` in the project settings.

## Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `NEXT_PUBLIC_GATEWAY_URL` | for the live panel | Base URL of the deployed FastAPI gateway, no trailing slash (e.g. `https://llm-gateway.fly.dev`). If unset, the live panel renders its offline state and the rest of the page is unaffected. |
| `NEXT_PUBLIC_DEMO_TOKEN` | optional | Sent as `X-Demo-Token` on every `/demo/*` request. `NEXT_PUBLIC_*` values are embedded in the client bundle, so treat this as a light abuse throttle, not a secret. |

Set both in Vercel under **Settings → Environment Variables**, then redeploy — `NEXT_PUBLIC_*` values are inlined at build time, so changing them requires a rebuild, not just a restart.

## Backend endpoints you still need to add

The page works today against the gateway's existing `/metrics`, degrading gracefully for anything missing. Two endpoints unlock the full live panel:

### 1. `POST /demo/chaos` — trigger a simulated outage

Request body:

```json
{ "provider": "openai", "duration_seconds": 30 }
```

Should force the named provider to fail for the window, then clear itself — the same mechanism `chaos_test.py` drives via `mock:fail_count:<provider>`. Note bug #4 from the gateway README: delete the key at outage end so the outage actually ends.

Any 2xx response is treated as success. A `404` shows "not deployed on this gateway yet" instead of a generic error.

### 2. `GET /demo/feed` — recent request events

Two shapes are supported; the panel tries SSE first and falls back to polling automatically.

- **Polling:** `GET /demo/feed?limit=15` → a JSON array, or `{"events": [...]}`.
- **SSE:** `GET /demo/feed?stream=1` → `text/event-stream`, each `data:` frame carrying one event or an array of them.

Event shape (unknown fields are ignored, missing ones defaulted):

```json
{
  "timestamp": "2026-07-30T14:02:11.482Z",
  "team": "team-a",
  "provider": "anthropic",
  "failover": true,
  "latency_ms": 812,
  "status": 200
}
```

### 3. `GET /demo/status` — optional, but saves a round trip

If present, it supplies breaker state and budgets together and is preferred over scraping `/metrics`:

```json
{
  "providers": [
    { "provider": "openai", "state": "open" },
    { "provider": "anthropic", "state": "closed" }
  ],
  "budgets": [
    { "team": "team-a", "spend": 0.412, "cap": 1.0 },
    { "team": "team-b", "spend": 0.087, "cap": 1.0 }
  ]
}
```

Without it, breaker pills fall back to parsing `gateway_circuit_breaker_state` from `/metrics` (this already works), and the budget bars render at zero.

### CORS

All of these are called from the browser on a different origin, so the gateway needs Vercel's domain allowed:

```python
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://your-site.vercel.app", "http://localhost:3000"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Demo-Token"],
)
```

Without CORS the panel shows its offline state rather than breaking.

## Offline behavior

The panel never crashes the page. It degrades in this order:

1. `/demo/status` → full state in one call.
2. `/metrics` → breaker pills live, budgets at zero.
3. Nothing reachable, no URL set, or demo endpoints 404 → **"Demo backend is asleep right now — here's what it looks like when running."**

Both probes run **concurrently** against a 3.5s abort timeout, so the offline state appears in ~4s rather than the ~12s it took when they ran in series. That distinction matters: a scale-to-zero host accepts the TCP connection and then goes quiet, so there is no fast failure to detect — it has to be timed out.

### The wake-up button

When the reason is `unreachable`, the offline state offers **"Wake it up (~30s)"**. It polls `/healthz` with a generous per-attempt timeout for up to 90s, showing a spinner, then loads the live panel automatically once the container answers. Background polling pauses while a wake is in flight so it can't stamp the panel back to offline mid-boot.

This is deliberately honest rather than hidden — a free-tier container really is asleep, and saying so reads better than a demo that looks broken.

## Drop in a demo recording

The offline state reserves a 16:9 slot. Record ~20s of the running panel: idle, click the outage button, the OpenAI pill goes red, failover rows appear, then recovery.

Save it to `public/`, then set `DEMO_RECORDING` near the top of [`components/LiveDemo.tsx`](components/LiveDemo.tsx):

```ts
const DEMO_RECORDING = { src: "/demo-recording.mp4", type: "video" as const };
```

The placeholder disappears on its own. **Prefer `.mp4` over `.gif`** — roughly an order of magnitude smaller for the same length, and it renders muted/autoplay/loop so it behaves like a GIF anyway. To generate traffic while recording:

```bash
python demo_traffic.py --seconds 60 --chaos
```

## Before you ship

- [ ] Replace `LINKEDIN_URL` in [`lib/content.ts`](lib/content.ts) — it's a placeholder.
- [ ] Set `NEXT_PUBLIC_GATEWAY_URL` in Vercel.
- [ ] Add CORS on the gateway for the Vercel domain.
- [ ] Add `/demo/chaos` and `/demo/feed`.
- [ ] Drop in `public/demo-recording.gif` for when the backend is asleep.

## Layout

```
app/
  layout.tsx     fonts, metadata
  page.tsx       hero → results → demo → decisions → capabilities → limits → footer
  globals.css    design tokens, base styles
components/
  ArchitectureDiagram.tsx   README's ASCII diagram redrawn as SVG
  LiveDemo.tsx              interactive panel (client component)
  Sections.tsx              results, decisions, capabilities, limits
lib/
  content.ts     all copy and numbers
  gateway.ts     gateway client, Prometheus parsing, offline handling
```
