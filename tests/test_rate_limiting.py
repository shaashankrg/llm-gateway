import asyncio

import pytest

# Must match CAPACITY in app/rate_limit.py and the key configured for team-a-key
# in app/auth.py's TEAM_CONFIG.
BUCKET_CAPACITY = 10
API_KEY = "team-a-key"


@pytest.mark.asyncio
async def test_rate_limit_allows_exactly_bucket_capacity_under_concurrency(test_client, redis_client):
    """
    Fires bucket_capacity * 5 concurrent requests against a freshly-flushed bucket
    and confirms exactly `capacity` succeed. Concurrency is the point: sequential
    requests can never overlap inside the Lua script, so they could never expose
    a race condition even if the script weren't atomic.
    """
    concurrent_requests = BUCKET_CAPACITY * 5

    async def make_request():
        response = await test_client.post(
            "/generate",
            json={"model": "gpt-4", "prompt": "hi", "max_tokens": 5},
            headers={"x-api-key": API_KEY},
        )
        return response.status_code

    results = await asyncio.gather(*[make_request() for _ in range(concurrent_requests)])

    successes = sum(1 for status in results if status == 200)
    rejections = sum(1 for status in results if status == 429)

    assert successes == BUCKET_CAPACITY, (
        f"expected exactly {BUCKET_CAPACITY} requests to pass the rate limiter, got {successes} — "
        f"a mismatch here means the Lua script isn't atomic under real concurrency"
    )
    assert rejections == concurrent_requests - BUCKET_CAPACITY
