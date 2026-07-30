"""
Endpoints that back the public showcase site's live panel.

Kept in its own module because none of this is part of the gateway proper —
it's a read-only window onto state the gateway already keeps, plus one
control that drives the same failure injection the chaos test uses.

Exposed under /demo:
    GET  /demo/status   breaker state + per-team spend in one call
    GET  /demo/feed     recent request events (JSON, or SSE with ?stream=1)
    POST /demo/chaos    force a provider to fail for a bounded window
"""

import asyncio
import json
import os
import time
from collections import deque
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.budget import TEAM_BUDGETS, r
from app.circuit_breaker import circuit_breakers

router = APIRouter(prefix="/demo", tags=["demo"])

# Ring buffer of recent requests. Deliberately in-process and bounded: this is
# a display buffer for the demo panel, not an audit log. With replicas, each
# serves its own recent slice, which is fine for a feed.
_EVENTS: deque = deque(maxlen=100)

# Optional shared secret. Only gates the mutating endpoint (/demo/chaos);
# the read-only ones stay open so the panel works without shipping a token.
_DEMO_TOKEN = os.environ.get("DEMO_TOKEN", "")

# Longest outage anyone can request, so a public button can't wedge the
# gateway into a permanently failing state.
_MAX_CHAOS_SECONDS = 120


def record_event(
    team: str,
    provider: str,
    failover: bool,
    latency_ms: float,
    status: int,
) -> None:
    """
    Append one served request to the feed buffer.

    Called from the request path, so the panel shows real traffic rather than
    anything synthesised here. Must never raise into the caller — a display
    buffer is not worth failing a request over.
    """
    try:
        _EVENTS.appendleft(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "team": team,
                "provider": provider,
                "failover": bool(failover),
                "latency_ms": round(float(latency_ms), 1),
                "status": int(status),
            }
        )
    except Exception:
        pass


@router.get("/status")
async def demo_status():
    """Breaker state and today's spend, shaped for the panel."""
    providers = [
        {"provider": name, "state": breaker.state.value}
        for name, breaker in circuit_breakers.items()
    ]

    budgets = []
    for team, cap in TEAM_BUDGETS.items():
        spend = 0.0
        try:
            raw = await r.get(f"spend:{team}:daily")
            spend = float(raw) if raw else 0.0
        except Exception:
            # Redis unreachable — report zero rather than failing the panel.
            spend = 0.0
        budgets.append({"team": team, "spend": round(spend, 6), "cap": cap})

    return {"providers": providers, "budgets": budgets}


@router.get("/feed")
async def demo_feed(limit: int = 15, stream: int = 0):
    """
    Recent request events.

    Default is a plain JSON array. With ?stream=1 the same events arrive as
    Server-Sent Events, pushing only what's new since the last frame.
    """
    limit = max(1, min(limit, 100))

    if not stream:
        return list(_EVENTS)[:limit]

    async def event_source():
        # Prime the client with recent history, newest last so it renders in order.
        backlog = list(_EVENTS)[:limit]
        if backlog:
            yield f"data: {json.dumps(backlog)}\n\n"

        last_seen = _EVENTS[0]["timestamp"] if _EVENTS else None
        while True:
            await asyncio.sleep(1.0)

            fresh = []
            for event in _EVENTS:
                if event["timestamp"] == last_seen:
                    break
                fresh.append(event)

            if fresh:
                last_seen = fresh[0]["timestamp"]
                yield f"data: {json.dumps(fresh)}\n\n"
            else:
                # Comment frame keeps proxies from timing out an idle stream.
                yield ": keepalive\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class ChaosRequest(BaseModel):
    provider: str = Field(default="openai")
    duration_seconds: int = Field(default=30, ge=1, le=_MAX_CHAOS_SECONDS)


@router.post("/chaos")
async def demo_chaos(body: ChaosRequest, request: Request):
    """
    Force a provider to fail for a bounded window.

    Uses the same `mock:fail_count:<provider>` key the chaos test drives, so
    the panel exercises the real breaker/retry/failover path rather than a
    special demo-only code path.
    """
    if _DEMO_TOKEN and request.headers.get("X-Demo-Token") != _DEMO_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid demo token")

    if body.provider not in circuit_breakers:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {body.provider}")

    key = f"mock:fail_count:{body.provider}"
    # A large counter keeps failures coming for the whole window; the expiry is
    # what ends the outage. Bug #4 in the README was an outage that never ended
    # because a leftover count outlived its window — the TTL is the fix, so the
    # key cannot survive its own duration even if the clear task dies.
    await r.set(key, 10_000_000, ex=body.duration_seconds)

    return {
        "ok": True,
        "provider": body.provider,
        "duration_seconds": body.duration_seconds,
        "ends_at": time.time() + body.duration_seconds,
    }
