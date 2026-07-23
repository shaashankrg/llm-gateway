import asyncio
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from opentelemetry import context as otel_context
from prometheus_client import make_wsgi_app
from starlette.background import BackgroundTask
from starlette.middleware.wsgi import WSGIMiddleware

from app.auth import get_priority
from app.budget import TEAM_BUDGETS, calculate_cost, record_spend_and_check_budget
from app.circuit_breaker import CircuitOpenError
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


async def _finalize_stream_cost(usage_holder: dict, team_id: str, model: str) -> None:
    usage = usage_holder.get("usage")
    if not usage:
        print(f"WARNING: no usage data captured for team {team_id} model {model}; stream cost not recorded")
        return

    cost = calculate_cost(model, usage["input_tokens"], usage["output_tokens"])
    daily_budget = TEAM_BUDGETS.get(team_id, 1.00)
    try:
        await record_spend_and_check_budget(team_id, cost, daily_budget)
    except HTTPException as e:
        # The response has already been fully streamed to the client by the time
        # this runs — there's no HTTP status left to change. The best we can do
        # is record the overage so the *next* request gets rejected.
        print(f"team {team_id} went over budget on a stream: {e.detail}")


@app.post("/generate/stream")
async def generate_stream(req: StandardRequest, team: dict = Depends(check_rate_limit)):
    if req.model not in team["allowed_models"]:
        raise HTTPException(status_code=403, detail=f"Model {req.model} not allowed for this team")

    usage_holder: dict = {}
    if "claude" in req.model:
        payload = to_anthropic_request(req)
        payload["stream"] = True
        generator = stream_anthropic(payload, usage_holder)
    else:
        payload = to_openai_request(req)
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        generator = stream_openai(payload, usage_holder)

    background = BackgroundTask(_finalize_stream_cost, usage_holder, team["team_id"], req.model)
    return StreamingResponse(generator, media_type="text/plain", background=background)


@app.get("/test-priority")
def test_priority(priority: str = Depends(get_priority)):
    return {"priority": priority}