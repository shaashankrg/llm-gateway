import asyncio
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from opentelemetry import context as otel_context
from prometheus_client import make_wsgi_app
from starlette.middleware.wsgi import WSGIMiddleware

from app.auth import get_priority
from app.budget import TEAM_BUDGETS, calculate_cost, reconcile_budget, record_spend_and_check_budget, reserve_budget
from app.circuit_breaker import CircuitOpenError, circuit_breakers
from app.health import health_check_loop
from app.models import StandardRequest, StandardResponse
from app.queue import enqueue_request, start_workers
from app.rate_limit import check_rate_limit
from app.tracing import tracer
from app.providers.anthropic_provider import stream_anthropic, to_anthropic_request
from app.providers.openai_provider import stream_openai, to_openai_request

background_tasks = set()


NUM_WORKERS = int(os.environ.get("NUM_WORKERS", "4"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    for coro in (start_workers(NUM_WORKERS), health_check_loop()):
        task = asyncio.create_task(coro)
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
    yield


app = FastAPI(title="LLM Gateway", lifespan=lifespan)
app.mount("/metrics", WSGIMiddleware(make_wsgi_app()))


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/generate", response_model=StandardResponse)
async def generate(
    req: StandardRequest,
    team: dict = Depends(check_rate_limit),
    priority: str = Depends(get_priority),
):
    with tracer.start_as_current_span("generate_request") as span:
        span.set_attribute("team_id", team["team_id"])
        span.set_attribute("model", req.model)
        span.set_attribute("priority", priority)

        if req.model not in team["allowed_models"]:
            raise HTTPException(status_code=403, detail=f"Model {req.model} not allowed for this team")

        trace_context = otel_context.get_current()
        future = asyncio.get_running_loop().create_future()
        await enqueue_request(
            priority, {"req": req, "team": team, "future": future, "trace_context": trace_context}
        )
        try:
            result = await future
        except httpx.HTTPStatusError as e:
            raise HTTPException(
                status_code=502,
                detail=f"Upstream provider error: {e.response.status_code} {e.response.reason_phrase}",
            )
        except httpx.HTTPError as e:
            raise HTTPException(status_code=503, detail=f"Upstream provider unavailable: {e}")
        except CircuitOpenError as e:
            raise HTTPException(status_code=503, detail=str(e))

        cost = calculate_cost(result.model, result.input_tokens, result.output_tokens)
        daily_budget = TEAM_BUDGETS.get(team["team_id"], 1.00)
        await record_spend_and_check_budget(team["team_id"], cost, daily_budget)

        return result


# Rough chars-per-token heuristic — there's no tokenizer in this codebase,
# and the reservation only needs to be a reasonable upper bound, not exact.
_ESTIMATED_CHARS_PER_TOKEN = 4


def _estimate_stream_cost(req: StandardRequest) -> float:
    estimated_input_tokens = max(1, len(req.prompt) // _ESTIMATED_CHARS_PER_TOKEN)
    return calculate_cost(req.model, estimated_input_tokens, req.max_tokens)


async def _reconcile_stream_budget(usage_holder: dict, team_id: str, model: str, reserved_cost: float) -> None:
    usage = usage_holder.get("usage")
    if not usage:
        # Stream broke (or was cancelled) before usage arrived — nothing was
        # actually spent, so release the full reservation.
        await reconcile_budget(team_id, reserved_cost, 0.0)
        return

    cost = calculate_cost(model, usage["input_tokens"], usage["output_tokens"])
    daily_budget = TEAM_BUDGETS.get(team_id, 1.00)
    new_total = await reconcile_budget(team_id, reserved_cost, cost)
    if new_total >= daily_budget:
        # The response has already been fully streamed to the client by the time
        # this runs — there's no HTTP status left to change. The best we can do
        # is record the overage so the *next* request gets rejected.
        print(f"team {team_id} went over budget on a stream: ${new_total:.4f} / ${daily_budget:.2f}")


async def _tracked_stream(generator, provider: str, usage_holder: dict, team_id: str, model: str, reserved_cost: float):
    """Wraps a provider's SSE generator so budget reconciliation and circuit
    breaker signaling both happen off the generator's own lifecycle (normal
    close, exception, or GeneratorExit from an early client disconnect),
    instead of Starlette's `background=`, which only fires after the
    generator returns *normally* — it never runs if the generator raises
    partway through a stream, which is exactly the failure case that needs
    the reservation released and the breaker notified.
    """
    try:
        async for chunk in generator:
            yield chunk
    except Exception:
        circuit_breakers[provider].record_failure()
        raise
    else:
        circuit_breakers[provider].record_success()
    finally:
        await _reconcile_stream_budget(usage_holder, team_id, model, reserved_cost)


@app.post("/generate/stream")
async def generate_stream(req: StandardRequest, team: dict = Depends(check_rate_limit)):
    if req.model not in team["allowed_models"]:
        raise HTTPException(status_code=403, detail=f"Model {req.model} not allowed for this team")

    team_id = team["team_id"]
    daily_budget = TEAM_BUDGETS.get(team_id, 1.00)
    estimated_cost = _estimate_stream_cost(req)
    if not await reserve_budget(team_id, estimated_cost, daily_budget):
        raise HTTPException(
            status_code=402,
            detail=f"Budget exceeded: estimated cost ${estimated_cost:.4f} would exceed ${daily_budget:.2f} daily budget",
        )

    provider = "anthropic" if "claude" in req.model else "openai"
    if not circuit_breakers[provider].can_attempt():
        await reconcile_budget(team_id, estimated_cost, 0.0)
        raise HTTPException(status_code=503, detail=f"{provider} is currently unavailable")

    usage_holder: dict = {}
    if provider == "anthropic":
        payload = to_anthropic_request(req)
        payload["stream"] = True
        generator = stream_anthropic(payload, usage_holder)
    else:
        payload = to_openai_request(req)
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        generator = stream_openai(payload, usage_holder)

    tracked = _tracked_stream(generator, provider, usage_holder, team_id, req.model, estimated_cost)
    return StreamingResponse(tracked, media_type="text/plain")


@app.get("/test-priority")
def test_priority(priority: str = Depends(get_priority)):
    return {"priority": priority}