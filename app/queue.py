import asyncio
import time

from app.circuit_breaker import circuit_breakers, CircuitOpenError
from app.providers.anthropic_provider import call_anthropic, from_anthropic_response, to_anthropic_request
from app.providers.openai_provider import call_openai, from_openai_response, to_openai_request
from app.retry import call_with_retry
from app.tracing import errors_total, fallback_triggered_total, provider_call_duration, request_duration, requests_total, tracer

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

_PROVIDER_FUNCS = {
    "anthropic": (call_anthropic, to_anthropic_request, from_anthropic_response),
    "openai": (call_openai, to_openai_request, from_openai_response),
}


class CircuitOpenSkip(Exception):
    """Raised by attempt_provider when the provider's circuit is open — nothing was attempted."""


async def attempt_provider(provider: str, req):
    """Try one provider for one request: circuit check, call+retry, metrics, span.

    Raises CircuitOpenSkip if the circuit is open (caller should try the next provider).
    Raises whatever call_with_retry raised if the circuit was closed but the call failed
    (caller should record the error and try the next provider).
    Returns the StandardResponse on success.
    """
    with tracer.start_as_current_span("circuit_breaker.check") as cb_span:
        cb_span.set_attribute("provider", provider)
        can_attempt = circuit_breakers[provider].can_attempt()
        cb_span.set_attribute("circuit_open", not can_attempt)

    if not can_attempt:
        raise CircuitOpenSkip(provider)

    call_fn, to_request, from_response = _PROVIDER_FUNCS[provider]

    with tracer.start_as_current_span("provider.call") as call_span:
        call_span.set_attribute("provider", provider)
        call_span.set_attribute("model", req.model)
        call_start = time.monotonic()
        try:
            raw = await call_with_retry(lambda: call_fn(to_request(req)))
            result = from_response(raw)
            circuit_breakers[provider].record_success()
            call_span.set_attribute("success", True)
            provider_call_duration.record(
                time.monotonic() - call_start, {"provider": provider, "outcome": "success"}
            )
            return result
        except Exception as e:
            circuit_breakers[provider].record_failure()
            call_span.set_attribute("success", False)
            call_span.record_exception(e)
            provider_call_duration.record(
                time.monotonic() - call_start, {"provider": provider, "outcome": "failure"}
            )
            errors_total.add(1, {"provider": provider, "error_type": type(e).__name__})
            raise


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
        trace_context = request_data["trace_context"]

        primary = "anthropic" if "claude" in req.model else "openai"
        fallback = "openai" if primary == "anthropic" else "anthropic"

        with tracer.start_as_current_span("worker_processing", context=trace_context):
            worker_start = time.monotonic()
            outcome = "failure"
            try:
                try:
                    result = await attempt_provider(primary, req)
                except Exception:
                    # Primary is unavailable — either its circuit is open (CircuitOpenSkip,
                    # no attempt was made) or attempt_provider tried it and exhausted retries.
                    # Either way, this single request now falls back to the other provider,
                    # instead of failing outright and waiting for a later request to reroute.
                    fallback_model = FALLBACK_MODEL.get(req.model)
                    if fallback_model is None or fallback_model not in team["allowed_models"]:
                        raise CircuitOpenError(f"{primary} is unavailable and no authorized fallback is available")
                    fallback_triggered_total.add(1, {"from_provider": primary, "to_provider": fallback})
                    fallback_req = req.model_copy(update={"model": fallback_model})
                    try:
                        result = await attempt_provider(fallback, fallback_req)
                    except CircuitOpenSkip:
                        raise CircuitOpenError("Both providers are unavailable")
            except Exception as e:
                print(f"WORKER {worker_id} ERROR: {e}")
                future.set_exception(e)
                requests_total.add(1, {"team": team["team_id"], "outcome": "failure"})
            else:
                future.set_result(result)
                outcome = "success"
                requests_total.add(1, {"team": team["team_id"], "model": req.model, "outcome": "success"})
            finally:
                request_duration.record(time.monotonic() - worker_start, {"outcome": outcome})

async def start_workers(num_workers: int):
    await asyncio.gather(*[worker(i) for i in range(num_workers)])
