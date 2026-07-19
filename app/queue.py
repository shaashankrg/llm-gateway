import asyncio

from app.circuit_breaker import circuit_breakers, CircuitOpenError
from app.providers.anthropic_provider import call_anthropic, from_anthropic_response, to_anthropic_request
from app.providers.openai_provider import call_openai, from_openai_response, to_openai_request
from app.retry import call_with_retry

request_queue = asyncio.PriorityQueue()
_counter = 0

PRIORITY_VALUES = {
    "realtime": 1,
    "batch": 5,
}

FALLBACK_MODEL = {
    "gpt-4": "claude-sonnet-5",
    "gpt-4o-mini": "claude-sonnet-5",
    "claude-sonnet-5": "gpt-4o-mini",
}

async def enqueue_request(priority: str, request_data: dict):
    global _counter
    priority_value = PRIORITY_VALUES[priority]
    _counter += 1
    await request_queue.put((priority_value, _counter, request_data))

async def worker(worker_id: int):
    while True:
        priority_value, counter, request_data = await request_queue.get()
        req = request_data["req"]
        team = request_data["team"]
        future = request_data["future"]

        try:
            if "claude" in req.model:
                if circuit_breakers["anthropic"].can_attempt():
                    try:
                        raw = await call_with_retry(lambda: call_anthropic(to_anthropic_request(req)))
                        result = from_anthropic_response(raw)
                        circuit_breakers["anthropic"].record_success()
                    except Exception:
                        circuit_breakers["anthropic"].record_failure()
                        raise
                else:
                    # Anthropic circuit is open — try OpenAI instead, if this team is allowed to use it
                    fallback_model = FALLBACK_MODEL.get(req.model)
                    if fallback_model is None or fallback_model not in team["allowed_models"]:
                        raise CircuitOpenError("Anthropic circuit is open and no authorized fallback is available")
                    if not circuit_breakers["openai"].can_attempt():
                        raise CircuitOpenError("Both providers are unavailable")
                    fallback_req = req.model_copy(update={"model": fallback_model})
                    try:
                        raw = await call_with_retry(lambda: call_openai(to_openai_request(fallback_req)))
                        result = from_openai_response(raw)
                        circuit_breakers["openai"].record_success()
                    except Exception:
                        circuit_breakers["openai"].record_failure()
                        raise
            else:
                if circuit_breakers["openai"].can_attempt():
                    try:
                        raw = await call_with_retry(lambda: call_openai(to_openai_request(req)))
                        result = from_openai_response(raw)
                        circuit_breakers["openai"].record_success()
                    except Exception:
                        circuit_breakers["openai"].record_failure()
                        raise
                else:
                    # OpenAI circuit is open — try Anthropic instead, if this team is allowed to use it
                    fallback_model = FALLBACK_MODEL.get(req.model)
                    if fallback_model is None or fallback_model not in team["allowed_models"]:
                        raise CircuitOpenError("OpenAI circuit is open and no authorized fallback is available")
                    if not circuit_breakers["anthropic"].can_attempt():
                        raise CircuitOpenError("Both providers are unavailable")
                    fallback_req = req.model_copy(update={"model": fallback_model})
                    try:
                        raw = await call_with_retry(lambda: call_anthropic(to_anthropic_request(fallback_req)))
                        result = from_anthropic_response(raw)
                        circuit_breakers["anthropic"].record_success()
                    except Exception:
                        circuit_breakers["anthropic"].record_failure()
                        raise
        except Exception as e:
            print(f"WORKER {worker_id} ERROR: {e}")
            future.set_exception(e)
        else:
            future.set_result(result)

async def start_workers(num_workers: int):
    await asyncio.gather(*[worker(i) for i in range(num_workers)])
