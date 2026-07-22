import asyncio
import random

import httpx

from app.tracing import tracer

RETRYABLE_STATUS_CODES = {429, 500, 502, 503}


def is_retryable(status_code: int) -> bool:
    return status_code in RETRYABLE_STATUS_CODES


def calculate_backoff(attempt: int) -> float:
    base_delay = 2 ** attempt
    jitter = random.uniform(0, 1)
    return base_delay + jitter


async def call_with_retry(call_fn, max_attempts=3):
    for attempt in range(max_attempts):
        with tracer.start_as_current_span("retry.attempt") as attempt_span:
            attempt_span.set_attribute("attempt_number", attempt + 1)
            try:
                result = await call_fn()
                attempt_span.set_attribute("outcome", "success")
                return result
            except httpx.HTTPStatusError as e:
                status_code = e.response.status_code
                retryable = is_retryable(status_code)
                attempt_span.set_attribute("outcome", "failure")
                attempt_span.set_attribute("error_type", "retryable" if retryable else "non_retryable")
                attempt_span.record_exception(e)

                if not retryable:
                    raise  # permanent failure, no point retrying

                if attempt == max_attempts - 1:
                    raise  # out of retries, give up for real

                delay = calculate_backoff(attempt)
                attempt_span.set_attribute("backoff_seconds", delay)
                print(f"Retryable error {status_code}, attempt {attempt + 1}/{max_attempts}, waiting {delay:.2f}s")
                await asyncio.sleep(delay)
            except Exception as e:
                attempt_span.set_attribute("outcome", "failure")
                attempt_span.set_attribute("error_type", "non_retryable")
                attempt_span.record_exception(e)
                raise
