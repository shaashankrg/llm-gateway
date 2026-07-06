import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from app.budget import TEAM_BUDGETS, calculate_cost, record_spend_and_check_budget
from app.models import StandardRequest, StandardResponse
from app.queue import start_workers
from app.rate_limit import check_rate_limit
from app.providers.anthropic_provider import (
    call_anthropic,
    from_anthropic_response,
    stream_anthropic,
    to_anthropic_request,
)
from app.providers.openai_provider import call_openai, from_openai_response, stream_openai, to_openai_request

background_tasks = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(start_workers(4))
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    yield


app = FastAPI(title="LLM Gateway", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.post("/generate", response_model=StandardResponse)
async def generate(req: StandardRequest, team: dict = Depends(check_rate_limit)):
    if req.model not in team["allowed_models"]:
        raise HTTPException(status_code=403, detail=f"Model {req.model} not allowed for this team")
    
    if "claude" in req.model:
        raw = await call_anthropic(to_anthropic_request(req))
        result = from_anthropic_response(raw)
    else:
        raw = await call_openai(to_openai_request(req))
        result = from_openai_response(raw)

    cost = calculate_cost(result.model, result.input_tokens, result.output_tokens)
    daily_budget = TEAM_BUDGETS.get(team["team_id"], 1.00)
    record_spend_and_check_budget(team["team_id"], cost, daily_budget)

    return result


def _finalize_stream_cost(usage_holder: dict, team_id: str, model: str) -> None:
    usage = usage_holder.get("usage")
    if not usage:
        print(f"WARNING: no usage data captured for team {team_id} model {model}; stream cost not recorded")
        return

    cost = calculate_cost(model, usage["input_tokens"], usage["output_tokens"])
    daily_budget = TEAM_BUDGETS.get(team_id, 1.00)
    try:
        record_spend_and_check_budget(team_id, cost, daily_budget)
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


from app.auth import get_priority

@app.get("/test-priority")
def test_priority(priority: str = Depends(get_priority)):
    return {"priority": priority}