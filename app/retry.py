import asyncio
import random

import httpx

RETRYABLE_STATUS_CODES = {429, 500, 502, 503}


def is_retryable(status_code: int) -> bool:
    return status_code in RETRYABLE_STATUS_CODES


def calculate_backoff(attempt: int) -> float:
    base_delay = 2 ** attempt
    jitter = random.uniform(0, 1)
    return base_delay + jitter


async def call_with_retry(call_fn, max_attempts=3):
    for attempt in range(max_attempts):
        try:
            return await call_fn()
        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code

            if not is_retryable(status_code):
                raise  # permanent failure, no point retrying

            if attempt == max_attempts - 1:
                raise  # out of retries, give up for real

            delay = calculate_backoff(attempt)
            print(f"Retryable error {status_code}, attempt {attempt + 1}/{max_attempts}, waiting {delay:.2f}s")
            await asyncio.sleep(delay)
