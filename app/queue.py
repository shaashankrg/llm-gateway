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

        with tracer.start_as_current_span("worker_processing", context=trace_context):
            worker_start = time.monotonic()
            outcome = "failure"
            try:
                if "claude" in req.model:
                    with tracer.start_as_current_span("circuit_breaker.check") as cb_span:
                        cb_span.set_attribute("provider", "anthropic")
                        can_attempt = circuit_breakers["anthropic"].can_attempt()
                        cb_span.set_attribute("circuit_open", not can_attempt)

                    if can_attempt:
                        with tracer.start_as_current_span("provider.call") as call_span:
                            call_span.set_attribute("provider", "anthropic")
                            call_span.set_attribute("model", req.model)
                            call_start = time.monotonic()
                            try:
                                raw = await call_with_retry(lambda: call_anthropic(to_anthropic_request(req)))
                                result = from_anthropic_response(raw)
                                circuit_breakers["anthropic"].record_success()
                                call_span.set_attribute("success", True)
                                provider_call_duration.record(
                                    time.monotonic() - call_start, {"provider": "anthropic", "outcome": "success"}
                                )
                            except Exception as e:
                                circuit_breakers["anthropic"].record_failure()
                                call_span.set_attribute("success", False)
                                call_span.record_exception(e)
                                provider_call_duration.record(
                                    time.monotonic() - call_start, {"provider": "anthropic", "outcome": "failure"}
                                )
                                errors_total.add(1, {"provider": "anthropic", "error_type": type(e).__name__})
                                raise
                    else:
                        # Anthropic circuit is open — try OpenAI instead, if this team is allowed to use it
                        fallback_model = FALLBACK_MODEL.get(req.model)
                        if fallback_model is None or fallback_model not in team["allowed_models"]:
                            raise CircuitOpenError("Anthropic circuit is open and no authorized fallback is available")
                        if not circuit_breakers["openai"].can_attempt():
                            raise CircuitOpenError("Both providers are unavailable")
                        fallback_triggered_total.add(1, {"from_provider": "anthropic", "to_provider": "openai"})
                        fallback_req = req.model_copy(update={"model": fallback_model})
                        with tracer.start_as_current_span("provider.call") as call_span:
                            call_span.set_attribute("provider", "openai")
                            call_span.set_attribute("model", fallback_model)
                            call_start = time.monotonic()
                            try:
                                raw = await call_with_retry(lambda: call_openai(to_openai_request(fallback_req)))
                                result = from_openai_response(raw)
                                circuit_breakers["openai"].record_success()
                                call_span.set_attribute("success", True)
                                provider_call_duration.record(
                                    time.monotonic() - call_start, {"provider": "openai", "outcome": "success"}
                                )
                            except Exception as e:
                                circuit_breakers["openai"].record_failure()
                                call_span.set_attribute("success", False)
                                call_span.record_exception(e)
                                provider_call_duration.record(
                                    time.monotonic() - call_start, {"provider": "openai", "outcome": "failure"}
                                )
                                errors_total.add(1, {"provider": "openai", "error_type": type(e).__name__})
                                raise
                else:
                    with tracer.start_as_current_span("circuit_breaker.check") as cb_span:
                        cb_span.set_attribute("provider", "openai")
                        can_attempt = circuit_breakers["openai"].can_attempt()
                        cb_span.set_attribute("circuit_open", not can_attempt)

                    if can_attempt:
                        with tracer.start_as_current_span("provider.call") as call_span:
                            call_span.set_attribute("provider", "openai")
                            call_span.set_attribute("model", req.model)
                            call_start = time.monotonic()
                            try:
                                raw = await call_with_retry(lambda: call_openai(to_openai_request(req)))
                                result = from_openai_response(raw)
                                circuit_breakers["openai"].record_success()
                                call_span.set_attribute("success", True)
                                provider_call_duration.record(
                                    time.monotonic() - call_start, {"provider": "openai", "outcome": "success"}
                                )
                            except Exception as e:
                                circuit_breakers["openai"].record_failure()
                                call_span.set_attribute("success", False)
                                call_span.record_exception(e)
                                provider_call_duration.record(
                                    time.monotonic() - call_start, {"provider": "openai", "outcome": "failure"}
                                )
                                errors_total.add(1, {"provider": "openai", "error_type": type(e).__name__})
                                raise
                    else:
                        # OpenAI circuit is open — try Anthropic instead, if this team is allowed to use it
                        fallback_model = FALLBACK_MODEL.get(req.model)
                        if fallback_model is None or fallback_model not in team["allowed_models"]:
                            raise CircuitOpenError("OpenAI circuit is open and no authorized fallback is available")
                        if not circuit_breakers["anthropic"].can_attempt():
                            raise CircuitOpenError("Both providers are unavailable")
                        fallback_triggered_total.add(1, {"from_provider": "openai", "to_provider": "anthropic"})
                        fallback_req = req.model_copy(update={"model": fallback_model})
                        with tracer.start_as_current_span("provider.call") as call_span:
                            call_span.set_attribute("provider", "anthropic")
                            call_span.set_attribute("model", fallback_model)
                            call_start = time.monotonic()
                            try:
                                raw = await call_with_retry(lambda: call_anthropic(to_anthropic_request(fallback_req)))
                                result = from_anthropic_response(raw)
                                circuit_breakers["anthropic"].record_success()
                                call_span.set_attribute("success", True)
                                provider_call_duration.record(
                                    time.monotonic() - call_start, {"provider": "anthropic", "outcome": "success"}
                                )
                            except Exception as e:
                                circuit_breakers["anthropic"].record_failure()
                                call_span.set_attribute("success", False)
                                call_span.record_exception(e)
                                provider_call_duration.record(
                                    time.monotonic() - call_start, {"provider": "anthropic", "outcome": "failure"}
                                )
                                errors_total.add(1, {"provider": "anthropic", "error_type": type(e).__name__})
                                raise
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
